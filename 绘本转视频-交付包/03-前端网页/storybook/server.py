from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .interaction import Interactor
from .object_agents import ObjectAgents
from .metrics import acoustic_record_error
from .pipeline import build_book
from .providers import Provider, ProviderError
from .schema import write_json
from .session_work import SessionWork
from .settings import ROOT, Settings
from .storage import Store
from .video import run_process


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    voice: str | None = Field(default=None, max_length=80)
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    time: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    frame: str | None = Field(default=None, max_length=8_000_000)


class Touch(BaseModel):
    voice: str | None = Field(default=None, max_length=80)
    x: float = Field(ge=0, le=1, allow_inf_nan=False)
    y: float = Field(ge=0, le=1, allow_inf_nan=False)
    time: float = Field(default=0, ge=0, le=100000, allow_inf_nan=False)
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,80}$")
    frame: str | None = Field(default=None, max_length=8_000_000)


class Review(BaseModel):
    reviewer: str = Field(min_length=1, max_length=80)
    approved: bool
    note: str = Field(default="", max_length=2000)


class ObjectQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=4000)
    session_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")
    voice: str | None = Field(default=None, max_length=80)


class ObjectTouch(Touch):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(pattern=r"^object-[a-f0-9]{32}$")
    request_id: str = Field(pattern=r"^[0-9a-fA-F-]{36}$")


class Metric(BaseModel):
    turn_id: str = Field(max_length=80)
    session_id: str = Field(max_length=80)
    book_id: str = Field(max_length=80)
    input_kind: str = Field(max_length=40)
    latency_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    playback_event: str = Field(max_length=80)
    measurement_source: Literal["browser_playback_event", "audio_loopback", "actual_audible"] = "browser_playback_event"
    measurement_kind: Literal["browser_playback_event", "audio_loopback", "actual_audible"] = "browser_playback_event"
    clock_domain: str = Field(default="", max_length=120)
    warmup_complete: bool = Field(default=False, strict=True)
    same_clock: bool = Field(default=False, strict=True)
    speech_end_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    first_audible_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_measurement(self):
        if self.measurement_kind != self.measurement_source:
            raise ValueError("测量类型与来源不一致；浏览器播放事件不能标记为声学测量")
        if self.measurement_kind != "browser_playback_event":
            error = acoustic_record_error(self.model_dump())
            if error:
                raise ValueError("声学测量字段不一致：" + error)
        return self


def create_app(settings=None, provider=None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    provider = provider or Provider(settings)
    interactor = Interactor(store, provider, settings)
    object_agents = ObjectAgents(store, interactor)
    session_work = SessionWork()
    jobs = {}
    active_tasks = set()
    job_tasks = {}
    book_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        await provider.close()

    app = FastAPI(title="纸书智伴", version="1.0.0", lifespan=lifespan)
    app.state.settings, app.state.store, app.state.provider = settings, store, provider
    app.state.object_agents, app.state.session_work = object_agents, session_work
    # 供测试断言运行期状态（任务表 / 生成锁），不改变运行时行为
    app.state.jobs, app.state.book_lock = jobs, book_lock

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": "未找到绘本或所请求的文件"})

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/status")
    async def status():
        return {"version": "1.0.0", "configured": {name: provider.available(name) for name in ("vision", "chat", "asr", "tts", "video")},
                "provider_modes": {name: provider.mode(name) for name in ("vision", "chat", "asr", "tts", "video")},
                "interaction_vision": {"configured": interactor.provider.available("vision"),
                                       "mode": interactor.provider.mode("vision"),
                                       "source": getattr(interactor.provider, "vision_role", "vision")},
                "ffmpeg": bool(settings.ffmpeg), "ffprobe": bool(settings.ffprobe),
                "limits": {"min_duration": 100, "max_duration": 180, "min_fps": 24, "min_resolution": [1280, 720]},
                "generation_busy": book_lock.locked(), "metrics_note": "浏览器播放事件仅作首音代理；实际两秒验收需声学回环测量"}

    def require_local(request):
        host = request.client.host if request.client else ""
        if host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(403, "模型配置只能在运行服务的本机修改")

    @app.get("/api/config")
    async def get_config(request: Request):
        require_local(request)
        return settings.public()

    @app.post("/api/config")
    async def save_config(request: Request):
        require_local(request)
        body = await request.json()
        if not isinstance(body, dict) or len(json.dumps(body)) > 40000:
            raise ValueError("配置格式无效")
        if book_lock.locked():
            raise HTTPException(409, "绘本正在生成，请等待当前任务结束后修改模型配置")
        settings.save_providers(body.get("providers", body))
        return {"saved": True, "config": settings.public()}

    @app.get("/api/books")
    async def list_books():
        return {"books": store.books()}

    @app.get("/api/books/{book_id}")
    async def get_book(book_id: str):
        return store.book(book_id)

    async def run_generation(job_id, pdf_path, book_dir, language, mode, reviewer, age_band):
        job = jobs[job_id]
        job_path = settings.data_dir / "_jobs" / f"{job_id}.json"

        async def progress(value, message, event=None):
            job.update(progress=value, message=message, updated_at=time.time())
            if event:
                job["events"].append(event)
            write_json(job_path, job)

        async with book_lock:
            try:
                job["status"] = "running"
                await progress(0, "开始处理绘本")
                book = await build_book(pdf_path, book_dir, provider, settings, language=language, mode=mode,
                                        reviewer=reviewer, age_band=age_band, progress=progress)
                store.save(book)
                job.update(status="completed", book_id=book["id"], progress=1, message="绘本已生成，请进行内容复核")
            except asyncio.CancelledError:
                job.update(status="interrupted", error="服务已停止；原始输入与已有产物已保留", message="任务中断")
                raise
            except Exception as error:
                # Do not serialize third-party response bodies or full request URLs.
                public_error = str(error) if isinstance(error, (ProviderError, ValueError, RuntimeError)) else type(error).__name__
                job.update(status="failed", error=public_error[:3000], message="生成未完成，详情见错误信息")
            finally:
                job["finished_at"] = time.time()
                write_json(job_path, job)

    @app.post("/api/books")
    async def upload_book(file: UploadFile = File(...), language: str = Form("auto"), mode: str = Form("local"), reviewer: str = Form(""), age_band: str = Form("6-8")):
        if language not in ("auto", "zh", "en") or mode not in ("local", "cloud"):
            raise ValueError("无效的语言或生成模式")
        if age_band not in ("3-5", "6-8", "9-12", "13-17"):
            raise ValueError("请选择年龄分组")
        if any(j.get("status") in ("queued", "running") for j in jobs.values()):
            raise HTTPException(409, "已有绘本正在生成，请等待当前任务结束")
        if mode == "cloud":
            missing = [n for n in ("vision", "chat", "video", "tts") if not provider.available(n)]
            if missing:
                raise HTTPException(409, "云端生成缺少配置：" + "、".join(missing))
        if not settings.ffmpeg:
            raise HTTPException(409, "缺少 FFmpeg，请运行安装脚本")
        job_id = uuid.uuid4().hex
        book_dir = store.directory(job_id)
        book_dir.mkdir(parents=True)
        source = book_dir / "source.pdf"
        size = 0
        try:
            with source.open("wb") as stream:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_mb * 1024 * 1024:
                        raise ValueError(f"PDF 超过 {settings.max_upload_mb}MB")
                    stream.write(chunk)
            with source.open("rb") as stream:
                if b"%PDF-" not in stream.read(1024):
                    raise ValueError("请选择有效 PDF 文件")
        except Exception:
            source.unlink(missing_ok=True)
            raise
        jobs[job_id] = {"id": job_id, "job_id": job_id, "book_id": job_id, "status": "queued", "progress": 0,
                        "message": "任务已创建", "events": [], "created_at": time.time()}
        task = asyncio.create_task(run_generation(job_id, source, book_dir, language, mode, reviewer[:80], age_band))
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)
        job_tasks[job_id] = task
        task.add_done_callback(lambda _t, key=job_id: job_tasks.pop(key, None))
        return {"job_id": job_id}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        store.identifier(job_id)
        task = job_tasks.get(job_id)
        if task is None or task.done():
            raise HTTPException(409, "任务不在运行中")
        task.cancel()
        # 等任务把状态写成 interrupted 并释放生成锁；取消异常由 gather 收下，不再冒泡
        await asyncio.gather(task, return_exceptions=True)
        job = jobs.get(job_id, {})
        return {"cancelled": True, "status": job.get("status", "interrupted")}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        store.identifier(job_id)
        if job_id in jobs:
            return jobs[job_id]
        path = settings.data_dir / "_jobs" / f"{job_id}.json"
        if not path.is_file():
            raise HTTPException(404, "任务不存在")
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") in ("queued", "running"):
            job.update(status="interrupted", error="上次运行在任务完成前停止；请重新导入")
        return job

    async def session_operation(book_id, session_id, operation, expected_version=None):
        async with session_work.operation(book_id, session_id, expected_version):
            return await operation()

    @app.post("/api/books/{book_id}/ask")
    async def ask(book_id: str, body: Question):
        return await session_operation(book_id, body.session_id,
            lambda: interactor.ask(book_id, body.question.strip(), body.session_id, body.time, body.frame, voice=body.voice))

    def stream_response(operation):
        async def events():
            queue = asyncio.Queue()

            async def emit(kind, payload):
                await queue.put((kind, payload))

            async def run():
                try:
                    result = await operation(emit)
                    await emit("result", result)
                except Exception as error:
                    detail = str(error) if isinstance(error, (ValueError, ProviderError)) else (error.detail if isinstance(error, HTTPException) else "处理失败，请重试")
                    await emit("error", {"detail": detail})
                finally:
                    await queue.put(None)

            task = asyncio.create_task(run())
            try:
                yield 'event: stage\ndata: {"message":"正在理解提问"}\n\n'
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    kind, payload = item
                    yield f"event: {kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/books/{book_id}/ask/stream")
    async def ask_stream(book_id: str, body: Question):
        store.book(book_id)
        version = session_work.version(book_id, body.session_id)
        return stream_response(lambda emit: session_operation(book_id, body.session_id,
            lambda: interactor.ask(book_id, body.question.strip(), body.session_id, body.time, body.frame, emit=emit, voice=body.voice), version))

    @app.post("/api/books/{book_id}/touch")
    async def touch(book_id: str, body: Touch):
        return await session_operation(book_id, body.session_id,
            lambda: interactor.touch(book_id, body.x, body.y, body.time, body.session_id, body.frame, body.voice))

    @app.post("/api/books/{book_id}/object-agents")
    async def create_object_agent(book_id: str, body: ObjectTouch):
        return await object_agents.create(book_id, body.x, body.y, body.time, body.session_id, body.frame,
                                          body.voice, agent_id=body.agent_id, request_id=body.request_id)

    @app.get("/api/books/{book_id}/object-agents/{agent_id}/history")
    async def object_history(book_id: str, agent_id: str, session_id: str):
        context = object_agents.get(book_id, agent_id, session_id)
        return {"agent": object_agents.public(context), "turns": store.history(book_id, agent_id)}

    @app.post("/api/books/{book_id}/object-agents/{agent_id}/cancel")
    async def cancel_object(book_id: str, agent_id: str, session_id: str, request_id: str):
        await object_agents.cancel(book_id, agent_id, session_id, request_id)
        return {"cancelled": True}

    @app.post("/api/books/{book_id}/object-agents/{agent_id}/ask")
    async def ask_object(book_id: str, agent_id: str, body: ObjectQuestion):
        return await object_agents.ask(book_id, agent_id, body.session_id, body.request_id, body.question.strip(), voice=body.voice)

    @app.post("/api/books/{book_id}/object-agents/{agent_id}/ask/stream")
    async def ask_object_stream(book_id: str, agent_id: str, body: ObjectQuestion):
        object_agents.get(book_id, agent_id, body.session_id)
        return stream_response(lambda emit: object_agents.ask(book_id, agent_id, body.session_id, body.request_id,
            body.question.strip(), voice=body.voice, emit=emit))

    async def process_voice_inner(book_id, audio, session_id, time, emit=None, frame=None, voice=None, focus_turn=None):
        store.book(book_id)
        store.identifier(session_id)
        if not math.isfinite(time) or time < 0 or time > 100000:
            raise ValueError("无效的视频时间")
        if not provider.available("asr"):
            raise HTTPException(409, "请先在本机设置中配置语音识别服务")
        started = __import__("time").perf_counter()
        turn_id = uuid.uuid4().hex
        folder = store.directory(book_id) / "turns" / turn_id
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / "recording.input"
        raw_data = await audio.read(20 * 1024 * 1024 + 1)
        if len(raw_data) > 20 * 1024 * 1024:
            raise ValueError("音频不得超过 20MB")
        raw.write_bytes(raw_data)
        wav = folder / "question.wav"
        try:
            await run_process([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], timeout=60)
        except RuntimeError:
            wav.unlink(missing_ok=True)
            raise HTTPException(400, "无法解码音频，请上传有效的 WAV、MP3 或 WebM 音频") from None
        finally:
            raw.unlink(missing_ok=True)
        try:
            transcript = await provider.asr(wav)
            if emit:
                await emit("transcript", {"text": transcript})
        except Exception:
            wav.unlink(missing_ok=True)
            raise
        asr_ms = round((__import__("time").perf_counter() - started) * 1000)
        try:
            result = await interactor.ask(book_id, transcript, session_id, time, frame=frame,
                                          turn_id=turn_id, input_kind="voice", emit=emit, voice=voice, focus_turn=focus_turn)
        except BaseException:
            # A failed turn may already have emitted speech. Its partial history
            # owns the input and emitted prefix, even though it is not successful.
            recorded = any(turn.get("turn_id") == turn_id for turn in store.history(book_id, session_id))
            if recorded:
                store.update_turn(book_id, session_id, turn_id, {"transcript": transcript, "timings": {"asr_ms": asr_ms}})
            else:
                for file in folder.iterdir():
                    if file.is_file():
                        file.unlink(missing_ok=True)
            raise
        result["transcript"] = transcript
        result["timings"]["asr_ms"] = asr_ms
        result["timings"]["server_total_ms"] = round((__import__("time").perf_counter() - started) * 1000)
        store.update_turn(book_id, session_id, turn_id, {"transcript": transcript, "timings": result["timings"]})
        return result

    async def process_voice(book_id, audio, session_id, time, emit=None, frame=None, voice=None, focus_turn=None):
        return await session_operation(book_id, session_id,
            lambda: process_voice_inner(book_id, audio, session_id, time, emit, frame, voice, focus_turn))

    @app.post("/api/books/{book_id}/voice")
    async def voice(book_id: str, audio: UploadFile = File(...), session_id: str = Form("default"), time: float = Form(0),
                    frame: str | None = Form(None, max_length=8_000_000), voice: str | None = Form(None, max_length=80)):
        return await process_voice(book_id, audio, session_id, time, frame=frame, voice=voice)

    @app.post("/api/books/{book_id}/voice/stream")
    async def voice_stream(book_id: str, audio: UploadFile = File(...), session_id: str = Form("default"), time: float = Form(0),
                           frame: str | None = Form(None, max_length=8_000_000), voice: str | None = Form(None, max_length=80)):
        store.book(book_id)
        version = session_work.version(book_id, session_id)
        if not provider.available("asr"):
            raise HTTPException(409, "请先在本机设置中配置语音识别服务")
        # Read UploadFile before the response starts so its request-scoped file
        # cannot close while the streaming generator is still consuming it.
        import io
        contents = await audio.read(20 * 1024 * 1024 + 1)
        if len(contents) > 20 * 1024 * 1024:
            raise ValueError("音频不得超过 20MB")
        copied = UploadFile(file=io.BytesIO(contents), filename=audio.filename)
        return stream_response(lambda emit: session_operation(book_id, session_id,
            lambda: process_voice_inner(book_id, copied, session_id, time, emit, frame, voice), version))

    async def process_object_voice(book_id, agent_id, parent, request_id, audio, voice, emit=None):
        async with object_agents.operation(book_id, agent_id, parent, request_id):
            params = object_agents.parameters(book_id, agent_id, parent)
            timestamp = params.pop("timestamp")
            return await process_voice(book_id, audio, time=timestamp, emit=emit, voice=voice, **params)

    @app.post("/api/books/{book_id}/object-agents/{agent_id}/voice")
    async def object_voice(book_id: str, agent_id: str, audio: UploadFile = File(...), session_id: str = Form(...),
                           request_id: str = Form(...), voice: str | None = Form(None, max_length=80)):
        return await process_object_voice(book_id, agent_id, session_id, request_id, audio, voice)

    @app.post("/api/books/{book_id}/object-agents/{agent_id}/voice/stream")
    async def object_voice_stream(book_id: str, agent_id: str, audio: UploadFile = File(...), session_id: str = Form(...),
                                  request_id: str = Form(...), voice: str | None = Form(None, max_length=80)):
        object_agents.get(book_id, agent_id, session_id)
        import io
        contents = await audio.read(20 * 1024 * 1024 + 1)
        if len(contents) > 20 * 1024 * 1024:
            raise ValueError("音频不得超过 20MB")
        copied = UploadFile(file=io.BytesIO(contents), filename=audio.filename)
        return stream_response(lambda emit: process_object_voice(book_id, agent_id, session_id, request_id, copied, voice, emit))

    @app.delete("/api/books/{book_id}")
    async def delete_book(book_id: str):
        if book_lock.locked():
            raise HTTPException(409, "绘本正在生成，请等当前任务结束后再删除")
        for job in list(jobs.values()):
            if job.get("book_id") == book_id and job.get("status") in ("queued", "running"):
                raise HTTPException(409, "这本绘本正在生成，请等任务结束后再删除")
        await object_agents.clear(book_id)
        store.delete(book_id)
        for job_id, job in list(jobs.items()):
            if job.get("book_id") == book_id:
                jobs.pop(job_id, None)
                job_path = settings.data_dir / "_jobs" / f"{job_id}.json"
                try:
                    job_path.unlink()
                except OSError:
                    pass
        return {"deleted": True, "book_id": book_id}

    @app.get("/api/books/{book_id}/history")
    async def history(book_id: str, session_id: str = "default"):
        return {"turns": store.history(book_id, session_id)}

    @app.delete("/api/books/{book_id}/history")
    async def clear_history(book_id: str, session_id: str = "default"):
        store.identifier(session_id)
        async with session_work.clear(book_id, session_id):
            await object_agents.clear(book_id, session_id)
            store.clear_history(book_id, session_id)
        return {"cleared": True}

    @app.post("/api/books/{book_id}/review")
    async def review(book_id: str, body: Review):
        if not body.reviewer.strip():
            raise ValueError("请填写复核责任人")
        book = store.book(book_id)
        book["review"].update(status="approved" if body.approved else "rejected", reviewer=body.reviewer.strip(), note=body.note, reviewed_at=time.time())
        store.save(book)
        return book["review"]

    @app.post("/api/metrics")
    async def metric(body: Metric):
        event = body.model_dump()
        store.metric(event)
        return {"recorded": True}

    @app.get("/api/metrics")
    async def metrics():
        events = store.metrics()
        proxies = [e["latency_ms"] for e in events if e.get("input_kind") in ("voice", "mic") and e.get("measurement_source") == "browser_playback_event"]
        return {"events": events, "summary": {"browser_voice_samples": len(proxies),
            "browser_proxy_under_2s_ratio": sum(v <= 2000 for v in proxies) / len(proxies) if proxies else None,
            "audible_compliance": "not_established_by_browser_events"}}

    @app.post("/api/warmup")
    async def warmup():
        results = {}
        if provider.available("chat"):
            try:
                await provider.chat('返回 JSON {"ready":true}')
                results["chat"] = "ready"
            except ProviderError as error:
                results["chat"] = str(error)
        if provider.available("tts"):
            try:
                await provider.tts("你好", "zh", settings.data_dir / "_warmup" / "ready.wav")
                results["tts"] = "ready"
            except ProviderError as error:
                results["tts"] = str(error)
        return {"results": results, "note": "预热不等于两秒首音验收通过"}

    @app.get("/media/{book_id}/{relative:path}")
    async def media(book_id: str, relative: str):
        directory = store.directory(book_id).resolve()
        candidate = (directory / relative).resolve()
        if not candidate.is_relative_to(directory) or candidate.suffix.lower() not in (".png", ".jpg", ".jpeg", ".wav", ".mp3", ".mp4", ".webm", ".pdf"):
            raise HTTPException(404, "资源不存在")
        if not candidate.is_file():
            raise HTTPException(404, "资源不存在")
        return FileResponse(candidate)

    @app.middleware("http")
    async def revalidate_web_assets(request, call_next):
        """HTML 不缓存、静态资源走 ETag 校验。

        否则浏览器会按启发式规则长期缓存 index.html，改完前端用户看到的还是旧界面。
        """
        response = await call_next(request)
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif request.url.path.endswith((".css", ".js")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    web = ROOT / "web"
    if web.is_dir():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app()
