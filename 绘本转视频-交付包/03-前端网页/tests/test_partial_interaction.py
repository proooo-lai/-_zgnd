"""Speech already delivered must remain auditable when the turn later fails."""
import asyncio
import base64
import io
import json
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storybook.interaction import Interactor
from storybook.providers import ANSWER_CALLBACK, ProviderError
from storybook.schema import contain_transform
from storybook.server import create_app
from storybook.settings import Settings
from storybook.storage import Store


def seed(store):
    store.save({"id": "book1", "title": "Bird", "language": "en", "duration": 120,
                "mode": "local", "review": {}, "video_url": "/media/book1/book.mp4",
                "pages": [{"number": 1, "text": "A blue bird. It sits in a tree.", "description": "A bird.",
                           "image_url": "/media/book1/pages/0001.png", "objects": [], "words": []}],
                "scenes": [{"page_number": 1, "start": 0, "end": 120,
                            "transform": contain_transform(1280, 720, 1280, 720)}]})


class PartialProvider:
    def __init__(self, mode):
        self.failure = mode
        self.sent = asyncio.Event()
        self.calls = 0

    def available(self, name):
        return name in ("chat", "tts", "asr")

    def mode(self, name):
        return "dashscope"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            return {"evidence_scope": "book", "supported": True, "uncertain": False, "evidence_pages": [1], "reason": "Synthetic bird facts."}
        if self.failure == "before_audio":
            raise ProviderError("model failed before speech")
        assert ANSWER_CALLBACK.get() is None, "Unverified model output must not start TTS"
        if self.failure == "model_tail":
            raise ProviderError("model tail is invalid")
        return {"evidence_scope": "book", "answer": "The bird is blue. It sits in a tree.", "evidence_pages": [1],
                "seek_page": 1 if self.failure == "grounding" else None,
                "target_label": "bird" if self.failure == "grounding" else ""}

    async def tts_stream(self, text, language, out, on_pcm, voice=None):
        self.calls += 1
        pcm = b"\x00\x20" * 30
        if self.failure == "rest_tts" and self.calls == 2:
            raise ProviderError("remaining speech unavailable")
        await on_pcm({"data": base64.b64encode(pcm).decode(), "sample_rate": 24000, "index": 0})
        self.sent.set()
        if self.failure == "pcm_incomplete":
            raise ProviderError("audio stream interrupted")
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(pcm)
        return out

    async def asr(self, audio):
        return "Tell me about the bird"

    async def close(self):
        pass


def configured(tmp_path):
    return Settings(data_dir=tmp_path / "data", config_file=tmp_path / "none.json")


@pytest.mark.parametrize("failure", ["grounding"])
def test_already_emitted_audio_survives_later_failure(tmp_path, monkeypatch, failure):
    async def run():
        settings = configured(tmp_path)
        store = Store(settings.data_dir)
        seed(store)
        reader = Interactor(store, PartialProvider(failure), settings)
        events = []
        async def emit(kind, payload):
            events.append((kind, payload))
        async def fail_ground(*args):
            await asyncio.wait_for(reader.provider.sent.wait(), 1)
            raise ProviderError("grounding failed")
        monkeypatch.setattr(reader, "ground_target", fail_ground)
        with pytest.raises(ProviderError):
            await reader.ask("book1", "Find the bird", "s1", emit=emit)
        assert any(kind == "audio_pcm" for kind, _ in events)
        turns = store.history("book1", "s1")
        assert len(turns) == 1
        turn = turns[0]
        assert turn["status"] == "partial" and turn["success"] is False
        assert turn["answer"] == "" and turn["answer_partial"] == "The bird is blue."
        assert turn["audio_complete"] is False
        assert turn["evidence_validation"]["decision"] == "supported"
        assert turn["evidence"][0]["page_number"] == 1
        path = store.directory("book1") / "turns" / turn["turn_id"] / Path(turn["audio_url"]).name
        with wave.open(str(path), "rb") as w:
            assert w.readframes(w.getnframes()) == b"\x00\x20" * 30
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["before_audio", "model_tail"])
def test_failure_without_emitted_audio_creates_no_turn(tmp_path, failure):
    async def run():
        settings = configured(tmp_path)
        store = Store(settings.data_dir)
        seed(store)
        reader = Interactor(store, PartialProvider(failure), settings)
        async def emit(kind, payload):
            pytest.fail("no audio expected")
        with pytest.raises(ProviderError):
            await reader.ask("book1", "Tell me about the bird", "s1", emit=emit)
        assert store.history("book1", "s1") == []
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["rest_tts", "pcm_incomplete"])
def test_incomplete_speech_preserves_text_and_emitted_audio_with_failure_status(tmp_path, failure):
    async def run():
        settings = configured(tmp_path)
        store = Store(settings.data_dir)
        seed(store)
        reader = Interactor(store, PartialProvider(failure), settings)
        async def emit(kind, payload):
            pass
        result = await reader.ask("book1", "Tell me about the bird", "s1", emit=emit)
        assert result["status"] == "partial_audio" and result["success"] is False
        assert result["audio_error"] and result["audio_complete"] is False
        assert result["answer"] == "The bird is blue. It sits in a tree."
        assert result["audio_chunks"] and result["audio_url"]
        turn = store.history("book1", "s1")[0]
        assert turn["audio_chunks"] == result["audio_chunks"]
        assert turn["status"] == "partial_audio"
    asyncio.run(run())


def test_voice_stream_keeps_input_and_failed_turn_after_pcm_was_sent(tmp_path):
    app = create_app(configured(tmp_path), PartialProvider("pcm_incomplete"))
    seed(app.state.store)
    audio = io.BytesIO()
    with wave.open(audio, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x10" * 1600)
    with TestClient(app) as client:
        response = client.post("/api/books/book1/voice/stream", data={"session_id": "s1"},
                               files={"audio": ("question.wav", audio.getvalue(), "audio/wav")})
        assert "event: audio_pcm" in response.text and "event: result" in response.text
        assert "partial_audio" in response.text
        turns = client.get("/api/books/book1/history?session_id=s1").json()["turns"]
        assert len(turns) == 1 and turns[0]["status"] == "partial_audio"
        turn = turns[0]
        assert turn["transcript"] == "Tell me about the bird"
        assert client.get(turn["input_audio_url"]).status_code == 200
        assert client.get(turn["audio_url"]).status_code == 200
