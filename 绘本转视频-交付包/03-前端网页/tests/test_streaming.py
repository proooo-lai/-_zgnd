"""Streaming behavior: escaped JSON, request isolation, early speech and cancellation."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from storybook.interaction import Interactor
from storybook.providers import ANSWER_CALLBACK, Provider, ProviderError, partial_answer
from storybook.schema import contain_transform
from storybook.server import create_app
from storybook.settings import Settings
from storybook.storage import Store


@pytest.mark.parametrize("raw, expected", [
    ('{"other":"waiting"', ""),
    ('{"answer":"小鸟在树下。', "小鸟在树下。"),
    (r'{"answer":"He said \"hello\". More', 'He said "hello". More'),
    (r'{"answer":"C:\\books\\page.\nNext', 'C:\\books\\page.\nNext'),
    (r'{"answer":"\u5c0f\u9e1f\u3002', "小鸟。"),
    (r'{"answer":"\u5c0f\u9e', ""),
    (r'{"answer":"\ud83d', ""),
    (r'{"answer":"\ud83d\ude42', "🙂"),
    ('{"answer":"Done.","evidence_pages":[1]}', "Done."),
])
def test_partial_json_answer_never_emits_escape_fragments(raw, expected):
    assert partial_answer(raw) == expected


class SSEBytes(httpx.AsyncByteStream):
    def __init__(self, text, usage=False):
        self.text, self.usage, self.closed = text, usage, False

    async def __aiter__(self):
        # Split inside Chinese UTF-8 and JSON escapes across transport packets.
        for offset in range(0, len(self.text), 5):
            chunk = {"choices": [{"delta": {"content": self.text[offset:offset + 5]}}]}
            packet = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
            for i in range(0, len(packet), 7):
                await asyncio.sleep(0)
                yield packet[i:i + 7]
        if self.usage:
            yield b'data: {"choices":[],"usage":{"total_tokens":25}}\n\n'
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def configured_settings(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")
    settings.providers = {"chat": {"base_url": "https://api.example.test/v1", "model": "test-model", "api_key": "test-only-key"}}
    return settings


def test_provider_sse_first_sentence_once_and_concurrent_context_isolation(tmp_path):
    async def run():
        provider = Provider(configured_settings(tmp_path))
        await provider.client.aclose()
        streams, requests, spoken = [], [], {"甲": [], "乙": []}
        async def transport(request):
            payload = json.loads(request.content)
            requests.append(payload)
            label = payload["messages"][-1]["content"]
            text = json.dumps({"answer": f"这是{label}的第一句。这是第二句。", "evidence_pages": [1]}, ensure_ascii=True)
            stream = SSEBytes(text)
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        async def request_one(label):
            async def callback(sentence):
                spoken[label].append(sentence)
            token = ANSWER_CALLBACK.set(callback)
            try:
                return await provider.chat(label)
            finally:
                ANSWER_CALLBACK.reset(token)
        try:
            results = await asyncio.gather(request_one("甲"), request_one("乙"))
            assert results[0]["answer"].startswith("这是甲")
            assert results[1]["answer"].startswith("这是乙")
            assert spoken == {"甲": ["这是甲的第一句。"], "乙": ["这是乙的第一句。"]}
            assert all(p["stream"] is True for p in requests)
            assert all(stream.closed for stream in streams)
            assert ANSWER_CALLBACK.get() is None
        finally:
            await provider.close()
    asyncio.run(run())


def test_provider_sse_accepts_terminal_usage_event(tmp_path):
    async def run():
        provider = Provider(configured_settings(tmp_path))
        await provider.client.aclose()
        stream = SSEBytes('{"answer":"A complete answer.","evidence_pages":[1]}', usage=True)
        provider.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)))
        spoken = []
        async def callback(text):
            spoken.append(text)
        token = ANSWER_CALLBACK.set(callback)
        try:
            result = await provider.chat("question")
            assert result["answer"] == "A complete answer."
            assert spoken == ["A complete answer."]
        finally:
            ANSWER_CALLBACK.reset(token)
            await provider.close()
    asyncio.run(run())


def seed_book(store):
    book = {"id": "book1", "title": "A Bird", "language": "en", "duration": 120,
            "video_url": "/media/book1/book.mp4", "mode": "local", "review": {"status": "pending"},
            "pages": [{"number": 1, "text": "A bird sits by the tree.", "description": "A blue bird.",
                       "image_url": "/media/book1/pages/0001.png", "objects": [], "words": []}],
            "scenes": [{"page_number": 1, "start": 0, "end": 120,
                        "transform": contain_transform(1280, 720, 1280, 720)}]}
    store.save(book)
    return book


class SpeakingProvider:
    def __init__(self, target=False):
        self.spoken = []
        self.target = target

    def available(self, name):
        return name in ("chat", "tts")

    def mode(self, name):
        return "mock"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            return {"evidence_scope": "book", "supported": True, "uncertain": False, "evidence_pages": [1], "reason": "The synthetic page says blue bird by tree."}
        callback = ANSWER_CALLBACK.get()
        if callback:
            await callback("The bird is blue.")
        await asyncio.sleep(.005)
        return {"evidence_scope": "book", "answer": "The bird is blue. It sits by the tree.", "evidence_pages": [1],
                "seek_page": 1 if self.target else None, "target_label": "bird" if self.target else "",
                "target_kind": "object", "uncertain": False}

    async def tts(self, text, language, out, voice=None):
        self.spoken.append(text)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mock-audio")
        return out

    async def close(self):
        pass


def parse_events(text):
    result = []
    for block in text.strip().split("\n\n"):
        values = dict(line.split(":", 1) for line in block.splitlines() if ":" in line)
        if "data" in values:
            result.append((values.get("event", "message").strip(), json.loads(values["data"])))
    return result


def test_http_stream_audio_segments_emit_once_and_result_references_same_queue(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")
    provider = SpeakingProvider()
    app = create_app(settings, provider)
    seed_book(app.state.store)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/ask/stream", json={"question": "Tell me about the bird", "session_id": "s1"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = parse_events(response.text)
        assert events[0][0] == "stage"
        assert events[-1][0] == "result"
        chunks = [payload for kind, payload in events if kind == "audio"]
        final = events[-1][1]
        assert [chunk["index"] for chunk in chunks] == [0, 1]
        assert len({chunk["url"] for chunk in chunks}) == 2
        assert final["audio_chunks"] == chunks
        assert provider.spoken == ["The bird is blue.", "It sits by the tree."]
        history = client.get("/api/books/book1/history?session_id=s1").json()["turns"]
        assert len(history) == 1
        assert history[0]["audio_chunks"] == chunks


def test_first_audio_is_available_before_slow_target_grounding(tmp_path, monkeypatch):
    async def run():
        settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")
        store = Store(settings.data_dir)
        seed_book(store)
        provider = SpeakingProvider(target=True)
        reader = Interactor(store, provider, settings)
        audio_ready = asyncio.Event()
        order = []
        async def emit(kind, payload):
            if kind == "audio":
                order.append("audio")
                audio_ready.set()
        async def slow_ground(*args):
            await asyncio.wait_for(audio_ready.wait(), .5)
            order.append("grounded")
            return [{"bbox": [.1, .2, .2, .2], "label": "bird"}]
        monkeypatch.setattr(reader, "ground_target", slow_ground)
        result = await reader.ask("book1", "Find the bird", "s1", emit=emit)
        assert order.index("audio") < order.index("grounded")
        assert result["boxes"]
        assert ANSWER_CALLBACK.get() is None
    asyncio.run(run())


def test_cancel_during_grounding_cancels_and_joins_early_tts_task(tmp_path, monkeypatch):
    async def run():
        started, cancelled, grounding = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class SlowSpeech(SpeakingProvider):
            async def tts(self, text, language, out, voice=None):
                self.child_task = asyncio.current_task()
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    cancelled.set()
        settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")
        store = Store(settings.data_dir)
        seed_book(store)
        provider = SlowSpeech(target=True)
        reader = Interactor(store, provider, settings)
        async def slow_ground(*args):
            grounding.set()
            await asyncio.sleep(60)
        async def emit(*args):
            pass
        monkeypatch.setattr(reader, "ground_target", slow_ground)
        task = asyncio.create_task(reader.ask("book1", "Find the bird", "s1", emit=emit))
        try:
            await asyncio.wait_for(started.wait(), .5)
            await asyncio.wait_for(grounding.wait(), .5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert cancelled.is_set(), "Detached first-sentence TTS must be cancelled before ask exits"
            assert provider.child_task.done()
            assert store.history("book1", "s1") == []
            assert ANSWER_CALLBACK.get() is None
        finally:
            task.cancel()
            if getattr(provider, "child_task", None):
                provider.child_task.cancel()
                await asyncio.gather(provider.child_task, return_exceptions=True)
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_stream_disconnect_stops_model_operation_and_does_not_append_history(tmp_path):
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()
        class BlockingProvider(SpeakingProvider):
            async def chat(self, prompt):
                started.set()
                try:
                    await asyncio.sleep(60)
                finally:
                    stopped.set()
        settings = Settings(data_dir=tmp_path / "data", config_file=tmp_path / "absent.json")
        app = create_app(settings, BlockingProvider())
        seed_book(app.state.store)
        sent, received = [], False
        body = json.dumps({"question": "Describe the story", "session_id": "s1"}).encode()
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "scheme": "http", "method": "POST",
                 "path": "/api/books/book1/ask/stream", "raw_path": b"/api/books/book1/ask/stream",
                 "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json")],
                 "client": ("testclient", 1234), "server": ("localhost", 80)}
        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await started.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            sent.append(message)
        await asyncio.wait_for(app(scope, receive, send), 2)
        assert stopped.is_set()
        assert app.state.store.history("book1", "s1") == []
        assert not any(b"event: result" in message.get("body", b"") for message in sent)
    asyncio.run(run())
