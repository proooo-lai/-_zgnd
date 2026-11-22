"""HTTP integration checks with isolated data and deterministic providers."""
import asyncio
import io
import json
import struct
import time
import wave
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from storybook.providers import ProviderError
from storybook.schema import contain_transform
from storybook.server import create_app
from storybook.settings import Settings


class OfflineProvider:
    def __init__(self, asr_error=None):
        self.asr_error = asr_error
        self.seen_audio = None

    def available(self, name):
        return name in ("asr", "chat")

    def mode(self, name):
        return "mock" if self.available(name) else "unconfigured"

    async def asr(self, audio):
        self.seen_audio = Path(audio)
        if self.asr_error:
            raise ProviderError(self.asr_error)
        with wave.open(str(audio)) as stream:
            assert stream.getframerate() == 16000
            assert stream.getnchannels() == 1
            assert stream.getsampwidth() == 2
        return "Where is the bird?"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            return {"evidence_scope": "book", "supported": True, "uncertain": False, "evidence_pages": [1], "reason": "Exact original fixture text."}
        return {"evidence_scope": "book", "answer": "The bird is by the tree.", "evidence_pages": [1], "uncertain": False}

    async def close(self):
        pass


def settings_for(tmp_path):
    return Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")


def seed_book(app):
    store = app.state.store
    book = {"id": "book1", "title": "A Bird", "language": "en", "duration": 120,
            "video_url": "/media/book1/book.mp4", "mode": "local", "review": {"status": "pending"},
            "pages": [{"number": 1, "text": "The bird is by the tree.", "description": "A blue bird.",
                       "image_url": "/media/book1/pages/0001.png", "objects": [], "words": []}],
            "scenes": [{"page_number": 1, "start": 0, "end": 120,
                        "transform": contain_transform(1280, 720, 1280, 720)}]}
    store.save(book)
    return book


def wav_bytes():
    data = io.BytesIO()
    with wave.open(data, "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(22050)
        stream.writeframes(struct.pack("<hh", 0, 0) * 2205)
    return data.getvalue()


def pdf_bytes():
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((50, 50), "A new, unseen book.")
    data = document.tobytes()
    document.close()
    return data


def test_media_cannot_traverse_to_sibling_private_files(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    seed_book(app)
    private = tmp_path / "data" / "private.wav"
    private.write_bytes(b"must-not-be-exposed")
    with TestClient(app) as client:
        for url in ("/media/book1/..%2Fprivate.wav", "/media/book1/%2e%2e%5cprivate.wav",
                    "/media/book1/..%2f..%2foutside.wav"):
            response = client.get(url)
            assert response.status_code in (400, 404)
            assert b"must-not-be-exposed" not in response.content
        assert client.get("/api/books/bad%5cid").status_code == 400
        assert client.get("/api/books/book1/history?session_id=..%2Fbad").status_code == 400


def test_voice_failure_has_clear_error_and_does_not_leave_unowned_audio(tmp_path):
    provider = OfflineProvider(asr_error="语音识别服务不可用")
    app = create_app(settings_for(tmp_path), provider)
    seed_book(app)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/voice", data={"session_id": "s1", "time": "12"},
                               files={"audio": ("question.wav", wav_bytes(), "audio/wav")})
        assert response.status_code == 502
        assert "语音识别" in response.json()["detail"]
        assert client.get("/api/books/book1/history?session_id=s1").json()["turns"] == []
    assert not list((tmp_path / "data" / "book1" / "turns").rglob("*.wav"))
    assert not list((tmp_path / "data" / "book1" / "turns").rglob("recording.input"))


def test_invalid_audio_is_client_error_not_internal_server_failure(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    seed_book(app)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/voice", files={"audio": ("bad.wav", b"not audio", "audio/wav")})
        assert response.status_code == 400


def test_voice_answer_failure_cleans_audio_without_claiming_a_completed_turn(tmp_path):
    class FailingAnswerProvider(OfflineProvider):
        async def asr(self, audio):
            await super().asr(audio)
            return 'Tell me about the bird'
        async def chat(self, prompt):
            raise ProviderError("问答服务暂不可用")
    app = create_app(settings_for(tmp_path), FailingAnswerProvider())
    seed_book(app)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/voice", data={"session_id": "s1"},
                               files={"audio": ("question.wav", wav_bytes(), "audio/wav")})
        assert response.status_code == 502
        assert client.get("/api/books/book1/history?session_id=s1").json()["turns"] == []
    assert not list((tmp_path / "data" / "book1" / "turns").rglob("*.wav"))


def test_voice_history_keeps_transcript_audio_and_complete_server_timing(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    seed_book(app)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/voice", data={"session_id": "s1", "time": "12"},
                               files={"audio": ("../../escape.wav", wav_bytes(), "audio/wav")})
        assert response.status_code == 200
        answer = response.json()
        history = client.get("/api/books/book1/history?session_id=s1").json()["turns"]
        assert answer["transcript"] == "Where is the bird?"
        assert history[0]["transcript"] == answer["transcript"]
        assert history[0]["timings"]["asr_ms"] == answer["timings"]["asr_ms"]
        assert history[0]["timings"]["server_total_ms"] == answer["timings"]["server_total_ms"]
        assert history[0]["input_audio_url"].endswith("question.wav")
        assert answer["timings"]["measurement"] == "server_processing_not_audible_latency"
        clear = client.delete("/api/books/book1/history?session_id=s1")
        assert clear.status_code == 200
        assert client.get("/api/books/book1/history?session_id=s1").json()["turns"] == []
    assert not (tmp_path / "escape.wav").exists()
    assert not list((tmp_path / "data" / "book1" / "turns").rglob("*.wav"))


def test_browser_metric_is_not_claimed_as_audible_two_second_compliance(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    with TestClient(app) as client:
        payload = {"turn_id": "t1", "session_id": "s1", "book_id": "book1", "input_kind": "mic",
                   "latency_ms": 750, "playback_event": "playing", "warmup_complete": True,
                   "same_clock": True, "measurement_source": "browser_playback_event"}
        assert client.post("/api/metrics", json=payload).status_code == 200
        payload.update(turn_id="t2", input_kind="text", latency_ms=50)
        assert client.post("/api/metrics", json=payload).status_code == 200
        metrics = client.get("/api/metrics").json()
        assert metrics["summary"]["browser_voice_samples"] == 1
        assert metrics["summary"]["browser_proxy_under_2s_ratio"] == 1
        assert metrics["summary"]["audible_compliance"] == "not_established_by_browser_events"
        payload["latency_ms"] = -1
        assert client.post("/api/metrics", json=payload).status_code == 422


def test_new_pdf_upload_exposes_real_pipeline_progress_and_single_job_lock(tmp_path, monkeypatch):
    import storybook.server as server
    seen = {}
    async def delayed_build(pdf_path, book_dir, provider, settings, **kwargs):
        seen["bytes"] = pdf_path.read_bytes()
        seen["language"] = kwargs["language"]
        await kwargs["progress"](.2, "识别新绘本", {"agent": "OCR"})
        await asyncio.sleep(.1)
        await kwargs["progress"](.8, "生成视频", {"agent": "Video"})
        await asyncio.sleep(.1)
        return {"id": book_dir.name, "title": "New book", "language": kwargs["language"], "pages": [],
                "scenes": [], "review": {"status": "pending"}, "mode": kwargs["mode"], "duration": 120}
    monkeypatch.setattr(server, "build_book", delayed_build)
    app = create_app(settings_for(tmp_path), OfflineProvider())
    with TestClient(app) as client:
        response = client.post("/api/books", files={"file": ("../../new.pdf", pdf_bytes(), "application/pdf")},
                               data={"language": "en", "mode": "local"})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        concurrent = client.post("/api/books", files={"file": ("next.pdf", pdf_bytes(), "application/pdf")})
        assert concurrent.status_code == 409
        samples = []
        for _ in range(30):
            job = client.get(f"/api/jobs/{job_id}").json()
            samples.append(job["progress"])
            if job["status"] in ("completed", "failed"):
                break
            time.sleep(.02)
        assert job["status"] == "completed"
        assert any(0 < x < 1 for x in samples)
        assert samples == sorted(samples)
        assert [e["agent"] for e in job["events"]] == ["OCR", "Video"]
        assert client.get(f"/api/books/{job_id}").json()["title"] == "New book"
        assert seen["language"] == "en"
        assert seen["bytes"].startswith(b"%PDF-")
        assert not (tmp_path / "new.pdf").exists()


def test_cloud_upload_does_not_silently_use_local_mode(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    with TestClient(app) as client:
        response = client.post("/api/books", files={"file": ("new.pdf", pdf_bytes(), "application/pdf")}, data={"mode": "cloud"})
        assert response.status_code == 409
        assert "缺少配置" in response.json()["detail"]
        assert client.get("/api/books").json()["books"] == []


def test_invalid_click_coordinates_rejected_before_model_call(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    seed_book(app)
    with TestClient(app) as client:
        assert client.post("/api/books/book1/touch", json={"x": 1.2, "y": .4, "time": 0}).status_code == 422
        assert client.post("/api/books/book1/touch", json={"x": .5, "y": -.2, "time": 0}).status_code == 422


def test_cancel_stops_generation_and_releases_lock(tmp_path, monkeypatch):
    import storybook.server as server

    async def hanging_build(pdf_path, book_dir, provider, settings, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(server, "build_book", hanging_build)
    app = create_app(settings_for(tmp_path), OfflineProvider())
    with TestClient(app) as client:
        response = client.post("/api/books", files={"file": ("new.pdf", pdf_bytes(), "application/pdf")})
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        job = {}
        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "running":
                break
            time.sleep(.02)
        assert job["status"] == "running"

        cancel = client.post(f"/api/jobs/{job_id}/cancel")

        assert cancel.status_code == 200
        assert cancel.json() == {"cancelled": True, "status": "interrupted"}
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "interrupted"
        assert app.state.store.books() == [], "取消的任务不能留下绘本"
        assert not app.state.book_lock.locked(), "取消后必须释放生成锁"


def test_cancel_unknown_job_is_409(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    with TestClient(app) as client:
        assert client.post("/api/jobs/missing/cancel").status_code == 409
