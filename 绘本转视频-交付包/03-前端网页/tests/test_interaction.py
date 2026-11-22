"""Behavioral interaction tests using synthetic books and no paid API calls."""
import asyncio
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from storybook.interaction import Interactor, scene_at, response_language
from storybook.schema import contain_transform, page_to_video_bbox
from storybook.storage import Store


def encoded_frame(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (160, 90), color).save(stream, "PNG")
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()


class ScriptedProvider:
    def __init__(self, replies=(), enabled=("chat",)):
        self.replies = list(replies)
        self.enabled = set(enabled)
        self.calls = []

    def available(self, name):
        return name in self.enabled

    def mode(self, name):
        return "mock" if self.available(name) else "unconfigured"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            data = json.loads(prompt.rsplit("\n", 1)[1])
            return {"evidence_scope": "book", "supported": True, "uncertain": False,
                    "evidence_pages": [p["page_number"] for p in data["cited_evidence"]], "reason": "Synthetic fixture evidence."}
        self.calls.append({"prompt": prompt, "images": []})
        return self.replies.pop(0)

    async def vision(self, prompt, images):
        self.calls.append({"prompt": prompt, "images": list(images),
                           "first_pixel": Image.open(images[0]).convert("RGB").getpixel((0, 0))})
        return self.replies.pop(0)

    async def close(self):
        pass


def make_book(store, generated=False):
    transform = contain_transform(200, 400, 1280, 720)
    if generated:
        transform.update(kind="generated_frame", requires_frame_grounding=True)
    book = {"id": "synthetic", "title": "A Bird and an Apple", "language": "en", "duration": 120,
            "video_url": "/media/synthetic/book.mp4", "mode": "cloud" if generated else "local",
            "review": {"status": "pending"},
            "pages": [
                {"number": 1, "text": "A red apple is under the tree.", "description": "A bird sees the apple.",
                 "image_url": "/media/synthetic/pages/0001.png", "words": [{"text": "apple", "bbox": [.1, .8, .2, .1]}],
                 "objects": [{"id": "scene", "label": "tree scene", "bbox": [.1, .1, .8, .8], "confidence": 1},
                             {"id": "apple", "label": "apple", "label_zh": "苹果", "label_en": "apple",
                              "description": "It is red.", "bbox": [.25, .5, .2, .2], "confidence": 1}]},
                {"number": 2, "text": "The bird shares the apple with a rabbit.", "description": "They eat together.",
                 "image_url": "/media/synthetic/pages/0002.png", "words": [], "objects": []}],
            "scenes": [{"page_number": 1, "start": 0, "end": 60, "transform": transform},
                       {"page_number": 2, "start": 60, "end": 120, "transform": transform}]}
    store.save(book)
    pages = store.directory(book["id"]) / "pages"
    pages.mkdir()
    for number in (1, 2):
        Image.new("RGB", (200, 400), "blue").save(pages / f"{number:04}.png")
    return book


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "data")
    settings = SimpleNamespace(ffmpeg="ffmpeg")
    return store, settings


def test_smallest_containing_object_wins_including_exact_boundary(setup):
    store, settings = setup
    book = make_book(store)
    bbox = page_to_video_bbox([.25, .5, .2, .2], book["scenes"][0]["transform"])
    provider = ScriptedProvider(enabled=())
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", bbox[0], bbox[1], 5, "s1", encoded_frame()))
    assert result["object"]["id"] == "apple"
    assert result["boxes"][0]["bbox"] == pytest.approx(bbox)
    assert result["uncertain"] is False
    history = store.history("synthetic", "s1")
    assert history[0]["frame_url"].endswith("frame.jpg")
    assert history[0]["input_kind"] == "touch"


def test_known_letterbox_never_invokes_semantic_guessing(setup):
    store, settings = setup
    make_book(store)
    provider = ScriptedProvider(enabled=("vision",))
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", .02, .5, 5, "s1", encoded_frame()))
    assert result["uncertain"] is True
    assert result["boxes"] == []
    assert provider.calls == []


def test_generated_video_click_uses_actual_uploaded_frame_not_old_pdf_boxes(setup):
    store, settings = setup
    make_book(store, generated=True)
    provider = ScriptedProvider([{"found": True, "label": "moving bird", "description": "It is blue.",
                                 "bbox_1000": [380, 450, 580, 700], "confidence": .95}], enabled=("vision",))
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", .46, .55, 12, "s1", encoded_frame("red")))
    assert result["object"]["label"] == "moving bird"
    assert result["boxes"][0]["source"] == "clicked_video_frame"
    assert provider.calls[0]["first_pixel"][0] > 240  # Uploaded red frame, not blue PDF image.
    assert provider.calls[0]["first_pixel"][2] < 20


def test_video_touch_context_keeps_names_but_excludes_source_geometry(setup):
    store, settings = setup
    book = make_book(store, generated=True)
    book["pages"][0]["objects"][1]["geometry"] = {"polygon": [[.25, .5], [.45, .7]]}
    store.save(book)
    provider = ScriptedProvider([{"found": True, "label": "apple", "description": "It is red.",
                                 "bbox_1000": [380, 450, 580, 700], "confidence": .95}], enabled=("vision",))
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", .46, .55, 12, "s1", encoded_frame()))
    context = json.loads(provider.calls[0]["prompt"].rsplit("\n", 1)[1].split("：", 1)[1])
    assert any(item.get("label") == "apple" and item.get("label_zh") == "苹果" for item in context["objects"])
    assert all(set(item) <= {"label", "label_zh", "label_en"} for item in context["objects"])
    assert result["boxes"][0]["bbox"] == [.38, .45, .2, .25]


def test_low_confidence_visual_hit_is_never_marked_certain(setup):
    store, settings = setup
    make_book(store, generated=True)
    provider = ScriptedProvider([{"found": True, "label": "maybe bird", "description": "Unclear.",
                                 "bbox_1000": [380, 450, 580, 700], "confidence": .1}], enabled=("vision",))
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", .46, .55, 12, "s1", encoded_frame()))
    assert result["uncertain"] is True


def test_visual_box_not_containing_click_is_rejected(setup):
    store, settings = setup
    make_book(store, generated=True)
    provider = ScriptedProvider([{"found": True, "label": "bird elsewhere", "bbox_1000": [800, 800, 900, 900],
                                 "confidence": .99}], enabled=("vision",))
    result = asyncio.run(Interactor(store, provider, settings).touch("synthetic", .46, .55, 12, "s1", encoded_frame()))
    assert result["boxes"] == []
    assert result["uncertain"] is True


def test_followup_question_contains_previous_answer_and_whole_book(setup):
    store, settings = setup
    make_book(store)
    provider = ScriptedProvider([{"evidence_scope": "book", "answer": "The bird shares it with a rabbit.", "evidence_pages": [2]},
                                 {"evidence_scope": "book", "answer": "它们一起吃苹果。", "evidence_pages": [2]}])
    reader = Interactor(store, provider, settings)
    async def exercise():
        await reader.ask("synthetic", "Who shares the apple?", "s1", 10)
        return await reader.ask("synthetic", "它们随后做什么？", "s1", 10)
    result = asyncio.run(exercise())
    assert "The bird shares it with a rabbit." in provider.calls[1]["prompt"]
    assert "A red apple is under the tree." in provider.calls[1]["prompt"]
    assert "The bird shares the apple with a rabbit." in provider.calls[1]["prompt"]
    assert len(store.history("synthetic", "s1")) == 2
    assert result["evidence"][0]["page_number"] == 2
    assert result["timings"]["measurement"] == "server_processing_not_audible_latency"
    assert "latency_ms" not in result["timings"]


def test_model_cannot_cite_or_seek_nonexistent_page_with_certainty(setup):
    store, settings = setup
    make_book(store)
    provider = ScriptedProvider([{"evidence_scope": "book", "answer": "It is on page 999.", "evidence_pages": [999],
                                 "seek_page": 999, "target_label": "apple", "uncertain": False}])
    result = asyncio.run(Interactor(store, provider, settings).ask("synthetic", "Find the apple", "s1"))
    assert result["evidence"] == []
    assert "seek_time" not in result
    assert result["uncertain"] is True


def test_generated_target_search_never_returns_source_page_geometry(setup, monkeypatch):
    store, settings = setup
    book = make_book(store, generated=True)
    provider = ScriptedProvider([{"found": True, "boxes": [{"label": "apple", "bbox": [.7, .1, .1, .2]}]}], enabled=("vision",))
    reader = Interactor(store, provider, settings)
    async def actual_frame(book_arg, timestamp, output, uploaded=None):
        Image.new("RGB", (160, 90), "red").save(output)
        return output
    monkeypatch.setattr(reader, "frame", actual_frame)
    folder = store.directory("synthetic")
    result = asyncio.run(reader.ground_target(book, book["scenes"][0], "apple", "object", folder, 10))
    assert result[0]["source"] == "current_video_frame"
    assert result[0]["bbox"] == [.7, .1, .1, .2]


def test_scene_switch_occurs_on_actual_timeline_boundary(setup):
    store, _ = setup
    book = make_book(store)
    assert scene_at(book, 59.999)["page_number"] == 1
    assert scene_at(book, 60)["page_number"] == 2


def test_fact_question_does_not_jump_when_model_suggests_unsolicited_target(setup):
    store, settings = setup
    make_book(store)
    provider = ScriptedProvider([{"evidence_scope": "book", "answer": "The apple is red.", "evidence_pages": [1],
                                 "seek_page": 1, "target_label": "apple", "target_kind": "object", "uncertain": False}])
    result = asyncio.run(Interactor(store, provider, settings).ask("synthetic", "What shade is the apple?", "s1"))
    assert result["answer"] == "The apple is red."
    assert "seek_time" not in result
    assert result["boxes"] == []
    assert result["evidence"][0]["page_number"] == 1


def test_cross_language_question_and_explicit_preference():
    assert response_language("What color is the apple?") == "en"
    assert response_language("这是什么？") == "zh"
    assert response_language("这个是什么？用英文回答") == "en"
    assert response_language("What happened? Reply in Chinese.") == "zh"
