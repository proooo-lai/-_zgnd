"""Regression cases use original synthetic facts, never contest questions."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from storybook.interaction import Interactor
from storybook.pipeline import _vision_cached
from storybook.providers import ANSWER_CALLBACK, ProviderError
from storybook.server import create_app
from storybook.settings import Settings
from storybook.storage import Store
from tools.evaluate import summarize_audible_metrics


def seed(store):
    store.save({"id": "original", "title": "Blue bird", "language": "en", "duration": 8,
                "pages": [{"number": 1, "text": "A blue bird rests.", "description": "A bird.",
                           "image_url": "/media/original/pages/0001.png", "objects": []}],
                "scenes": [{"page_number": 1, "start": 0, "end": 8}]})


class AnswerProvider:
    def __init__(self, draft, verdict=None):
        self.draft = draft
        self.verdict = verdict or {"evidence_scope": "book", "supported": True, "uncertain": False, "evidence_pages": [1], "reason": "Explicit source fact."}
        self.spoken = []
        self.verified = False

    def available(self, name):
        return name in ("chat", "tts")

    def mode(self, name):
        return "mock"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            self.verified = True
            assert "A blue bird rests." in prompt
            return self.verdict
        callback = ANSWER_CALLBACK.get()
        if callback:
            await callback("There are seven purple elephants.")
        return self.draft

    async def tts(self, text, language, out, voice=None):
        self.spoken.append(text)
        out.write_bytes(b"test-audio")
        return out

    async def close(self):
        pass


@pytest.mark.parametrize("fields", [
    {"evidence_pages": []}, {"evidence_pages": [999]}, {"evidence_pages": [True]},
    {"evidence_pages": [1, 999]}, {"evidence_pages": "1"}, {"uncertain": "false"},
    {"uncertain": True}, {"evidence_scope": "book", "answer": {"bad": "type"}},
])
def test_untrusted_drafts_never_reach_answer_or_speech(tmp_path, fields):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "There are seven purple elephants.", "evidence_pages": [1], "uncertain": False, **fields})
    async def emit(*args):
        pass
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask("original", "What is here?", "s1", emit=emit))
    assert "elephants" not in result["answer"]
    assert all("elephants" not in text for text in provider.spoken)
    assert result["uncertain"] is True and result["status"] == "insufficient_evidence"
    assert result["evidence_validation"]["decision"] == "rejected"


def test_real_page_number_alone_does_not_prove_the_answer(tmp_path):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "There are seven purple elephants.", "evidence_pages": [1]},
                              {"evidence_scope": "book", "supported": False, "uncertain": False, "evidence_pages": [1], "reason": "No elephants in the evidence."})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask("original", "What is here?", "s1"))
    assert provider.verified
    assert "elephants" not in result["answer"]
    assert result["uncertain"] is True


def test_translation_is_verified_before_any_streamed_speech(tmp_path):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "一只蓝色的小鸟在休息。", "evidence_pages": [1], "uncertain": False})
    events = []
    async def emit(kind, payload):
        assert provider.verified, "No model draft may be spoken before evidence verification"
        events.append((kind, payload))
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask("original", "它是什么颜色？", "s1", emit=emit))
    assert result["answer"] == "一只蓝色的小鸟在休息。"
    assert result["evidence_validation"]["decision"] == "supported"
    assert provider.spoken == [result["answer"]]
    assert events


class VisionProvider:
    def __init__(self, model="a", error=None):
        self.settings = SimpleNamespace(providers={"vision": {"kind": "openai", "base_url": "https://example.test/v1", "model": model, "api_key": "private-test-value"}})
        self.calls, self.error = 0, error

    async def vision(self, prompt, images):
        self.calls += 1
        if self.error:
            raise self.error
        return {"text": "Original bird.", "description": self.settings.providers["vision"]["model"], "objects": []}


def test_cache_isolates_models_and_records_call_origin(tmp_path):
    image = tmp_path / "image.bin"
    image.write_bytes(b"synthetic-image")
    settings = SimpleNamespace(data_dir=tmp_path)
    first, second = VisionProvider("a"), VisionProvider("b")
    async def run():
        a = await _vision_cached(first, "prompt", image, settings)
        hit = await _vision_cached(first, "prompt", image, settings)
        b = await _vision_cached(second, "prompt", image, settings)
        assert a["description"] == hit["description"] == "a"
        assert b["description"] == "b" and second.calls == 1
        assert first.calls == 1
        assert a["_provenance"]["execution"] == "live"
        assert hit["_provenance"]["execution"] == "cache_hit"
        assert a["_provenance"]["created_at"] == hit["_provenance"]["created_at"]
        assert a["_provenance"]["validation"]["status"] == "schema_validated"
    asyncio.run(run())
    for path in (tmp_path / ".vision-cache").glob("*.json"):
        assert "private-test-value" not in path.read_text()


@pytest.mark.parametrize("status,code", [(401, "InvalidApiKey"), (429, "InsufficientQuota"), (400, "DataInspectionFailed")])
def test_cache_does_not_retry_auth_quota_or_policy_errors(tmp_path, monkeypatch, status, code):
    image = tmp_path / "image.bin"
    image.write_bytes(b"original")
    provider = VisionProvider(error=ProviderError("rejected", status_code=status, code=code))
    async def no_wait(*args):
        pass
    monkeypatch.setattr("storybook.pipeline.asyncio.sleep", no_wait)
    with pytest.raises(ProviderError):
        asyncio.run(_vision_cached(provider, "prompt", image, SimpleNamespace(data_dir=tmp_path)))
    assert provider.calls == 1


def metric(**updates):
    return {"turn_id": "t1", "session_id": "s1", "book_id": "original", "input_kind": "mic", "latency_ms": 750,
            "playback_event": "loopback_onset", "measurement_source": "audio_loopback", "measurement_kind": "audio_loopback",
            "clock_domain": "external-recorder-monotonic", "same_clock": True, "warmup_complete": True,
            "speech_end_ms": 1000, "first_audible_ms": 1750, **updates}


def test_acoustic_metric_roundtrip_preserves_evaluator_contract(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, config_file=tmp_path / "absent.json"), AnswerProvider({}))
    with TestClient(app) as client:
        assert client.post("/api/metrics", json=metric()).status_code == 200
        events = client.get("/api/metrics").json()["events"]
        assert events[0]["measurement_kind"] == "audio_loopback"
        assert events[0]["clock_domain"] == "external-recorder-monotonic"
        assert summarize_audible_metrics(events)["status"] == "measured"


@pytest.mark.parametrize("updates", [
    {"measurement_source": "browser_playback_event"}, {"playback_event": "audio_playing"},
    {"first_audible_ms": 500}, {"clock_domain": ""}, {"same_clock": False}, {"latency_ms": 100},
])
def test_invalid_acoustic_claim_is_rejected(tmp_path, updates):
    app = create_app(Settings(data_dir=tmp_path, config_file=tmp_path / "absent.json"), AnswerProvider({}))
    with TestClient(app) as client:
        assert client.post("/api/metrics", json=metric(**updates)).status_code == 422


@pytest.mark.parametrize("verdict", [
    {"evidence_scope": "book", "supported": "true", "uncertain": False, "evidence_pages": [1], "reason": "invalid boolean"},
    {"evidence_scope": "book", "supported": True, "uncertain": False, "evidence_pages": [999], "reason": "invalid reference"},
    {"evidence_scope": "book", "supported": True, "uncertain": True, "evidence_pages": [1], "reason": "conflicting evidence"},
])
def test_invalid_or_uncertain_verifier_results_fail_closed(tmp_path, verdict):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "A blue bird rests.", "evidence_pages": [1]}, verdict)
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask("original", "What rests?", "s1"))
    assert result["status"] == "insufficient_evidence" and result["evidence"] == []
    assert "A blue bird rests." not in result["answer"]


def test_cache_invalidates_schema_endpoint_and_secret_rotation_is_not_identity(tmp_path, monkeypatch):
    import storybook.pipeline as pipeline
    image = tmp_path / "image.bin"
    image.write_bytes(b"original")
    provider = VisionProvider()
    settings = SimpleNamespace(data_dir=tmp_path)
    async def run():
        await _vision_cached(provider, "prompt", image, settings)
        provider.settings.providers["vision"]["api_key"] = "rotated-private-test-value"
        await _vision_cached(provider, "prompt", image, settings)
        assert provider.calls == 1
        provider.settings.providers["vision"]["base_url"] = "https://other.example.test/v1"
        await _vision_cached(provider, "prompt", image, settings)
        assert provider.calls == 2
        monkeypatch.setattr(pipeline, "SCHEMA_VERSION", "next-test-schema")
        await _vision_cached(provider, "prompt", image, settings)
        assert provider.calls == 3
        for path in (tmp_path / ".vision-cache").glob("*.json"):
            path.write_text('{"identity": {}, "result": {"text": 5}}')
        await _vision_cached(provider, "prompt", image, settings)
        assert provider.calls == 4
    asyncio.run(run())


def test_explicit_transient_rejection_retries_only_with_a_bound(tmp_path, monkeypatch):
    image = tmp_path / "image.bin"
    image.write_bytes(b"original")
    provider = VisionProvider(error=ProviderError("busy", status_code=429, code="Throttling", submission_state="rejected"))
    sleeps = []
    async def no_wait(delay):
        sleeps.append(delay)
    monkeypatch.setattr("storybook.pipeline.asyncio.sleep", no_wait)
    with pytest.raises(ProviderError):
        asyncio.run(_vision_cached(provider, "prompt", image, SimpleNamespace(data_dir=tmp_path)))
    assert provider.calls == 3 and sleeps == [2, 4]


def test_malformed_cached_kind_is_revalidated_and_refetched(tmp_path):
    import json
    image = tmp_path / "image.bin"
    image.write_bytes(b"original")
    provider = VisionProvider()
    settings = SimpleNamespace(data_dir=tmp_path)
    async def run():
        await _vision_cached(provider, "prompt", image, settings)
        path = next((tmp_path / ".vision-cache").glob("*.json"))
        entry = json.loads(path.read_text())
        entry["result"]["kind"] = []
        path.write_text(json.dumps(entry))
        result = await _vision_cached(provider, "prompt", image, settings)
        assert provider.calls == 2
        assert result["_provenance"]["execution"] == "live"
    asyncio.run(run())


@pytest.mark.parametrize("question", ["What color is the background in this current frame?",
    "What color is the bird in the current video?", "视频里的小鸟是什么颜色？", "屏幕上小鸟是什么颜色？"])
def test_explicit_current_frame_question_cannot_be_verified_by_page_text_only(tmp_path, question):
    import base64
    import io
    from PIL import Image
    store = Store(tmp_path)
    seed(store)
    image = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(image, "PNG")
    provider = AnswerProvider({"evidence_scope": "book", "answer": "The background is blue.", "evidence_pages": [1]})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask(
        "original", question, "s1",
        frame="data:image/png;base64," + base64.b64encode(image.getvalue()).decode()))
    assert result["status"] == "insufficient_evidence"
    assert "The background is blue." not in result["answer"]
    assert all("The background is blue." not in text for text in provider.spoken)


def test_text_evidence_still_answers_ordinary_book_color_questions(tmp_path):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "The bird is blue.", "evidence_pages": [1]})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask(
        "original", "What color is the bird in the book?", "s1"))
    assert result["answer"] == "The bird is blue." and result["status"] == "completed"


def test_verifier_can_require_actual_frame_even_when_draft_claims_book_scope(tmp_path):
    store = Store(tmp_path)
    seed(store)
    provider = AnswerProvider({"evidence_scope": "book", "answer": "The bird is blue.", "evidence_pages": [1]},
        {"evidence_scope": "current_frame", "supported": True, "uncertain": False, "evidence_pages": [1], "reason": "Question requires visual evidence."})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask("original", "Can you describe that bird?", "s1"))
    assert result["status"] == "insufficient_evidence"
    assert "The bird is blue." not in result["answer"]


@pytest.mark.parametrize("draft_scope", ["book", "current_frame"])
def test_actual_frame_answer_can_pass_with_visual_evidence(tmp_path, draft_scope):
    import base64
    import io
    from PIL import Image
    store = Store(tmp_path)
    seed(store)
    pages = store.directory("original") / "pages"
    pages.mkdir()
    Image.new("RGB", (40, 40), "blue").save(pages / "0001.png")
    image = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(image, "PNG")
    class FrameProvider(AnswerProvider):
        def available(self, name):
            return name in ("vision", "chat", "tts")
        async def vision(self, prompt, images):
            with Image.open(images[0]) as first:
                assert first.convert("RGB").getpixel((0, 0))[0] > 240
            if "ANSWER_VERIFICATION_V1" in prompt:
                self.verified = True
                assert '"actual_frame_available": true' in prompt
                return {"evidence_scope": "current_frame", "supported": True, "uncertain": False,
                        "evidence_pages": [1], "reason": "The real captured frame is red."}
            return {"evidence_scope": draft_scope, "answer": "The background is red.", "evidence_pages": [1]}
    provider = FrameProvider({})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask(
        "original", "What color is the background in this frame?", "s1",
        frame="data:image/png;base64," + base64.b64encode(image.getvalue()).decode()))
    assert result["answer"] == "The background is red." and result["status"] == "completed"
    assert result["evidence_scope"] == "current_frame" and provider.verified
