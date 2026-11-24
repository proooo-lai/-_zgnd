"""Unmapped transitions still use actual pixels and honest source evidence."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from storybook.server import create_app
from storybook.interaction import scene_at
from tests.test_interaction import ScriptedProvider, make_book
from tests.test_object_agents import create, ask_body
from tests.test_server import settings_for


HIT = {"found": True, "label": "bird", "description": "It is blue.",
       "coordinate_space": "full_frame", "bbox_1000": [300, 300, 700, 800], "confidence": .95}


def unmapped_book(store):
    book = make_book(store, generated=True)
    book["mode"] = "imported_video"
    book["scenes"][0]["end"] = 58
    book["scenes"][1]["end"] = 118
    store.save(book)
    return book


@pytest.mark.parametrize("timestamp", [59, 119])
def test_transition_and_outro_can_open_frozen_object_without_inventing_page(tmp_path, timestamp):
    provider = ScriptedProvider([HIT], enabled=("vision",))
    app = create_app(settings_for(tmp_path), provider)
    book = unmapped_book(app.state.store)
    assert scene_at(book, timestamp) is None
    with TestClient(app) as client:
        child = create(client, time=timestamp)
        assert child["agent"]["label"] == "bird"
        assert child["agent"]["page_number"] is None
        assert child["result"]["evidence"] == []
        assert child["result"]["interaction_time"] == timestamp
        assert child["result"]["boxes"][0]["source"] == "clicked_video_frame"
        assert app.state.store.history("synthetic", "main") == []


def test_blank_transition_does_not_inherit_nearby_page_object(tmp_path):
    provider = ScriptedProvider([{"found": False, "coordinate_space": "full_frame"}], enabled=("vision",))
    app = create_app(settings_for(tmp_path), provider)
    unmapped_book(app.state.store)
    with TestClient(app) as client:
        child = create(client, time=59)
        assert child["agent"] is None
        assert child["result"]["status"] == "object_not_confirmed"
        assert child["result"]["evidence"] == []


def test_unmapped_object_can_answer_from_frozen_frame_with_timestamp_evidence(tmp_path):
    provider = ScriptedProvider([HIT,
        {"answer": "It is blue.", "evidence_scope": "current_frame", "evidence_pages": [], "uncertain": False},
        {"supported": True, "evidence_scope": "current_frame", "evidence_pages": [], "uncertain": False, "reason": "The frozen frame shows blue feathers."}], enabled=("vision",))
    app = create_app(settings_for(tmp_path), provider)
    unmapped_book(app.state.store)
    with TestClient(app) as client:
        child = create(client, time=59)
        agent = child["agent"]["id"]
        result = client.post(f"/api/books/synthetic/object-agents/{agent}/ask", json=ask_body("What color is it in this frame?")).json()
        assert result["answer"] == "It is blue."
        assert not result["uncertain"]
        assert result["evidence"] == []
        assert result["frame_evidence"]["time"] == 59
        assert result["frame_evidence"]["source"] == "actual_video_frame"
        assert client.get(result["frame_evidence"]["image_url"]).status_code == 200
        assert provider.calls[1]["images"][0].name == "frame.jpg"
        assert app.state.store.history("synthetic", "main") == []


def test_unmapped_background_still_requires_source_book_evidence(tmp_path):
    provider = ScriptedProvider([HIT,
        {"answer": "The bird shares the apple with a rabbit.", "evidence_scope": "book", "evidence_pages": [2], "uncertain": False},
        {"supported": True, "evidence_scope": "book", "evidence_pages": [2], "uncertain": False, "reason": "Page two contains the event."}], enabled=("vision",))
    app = create_app(settings_for(tmp_path), provider)
    unmapped_book(app.state.store)
    with TestClient(app) as client:
        child = create(client, time=59)
        result = client.post(f"/api/books/synthetic/object-agents/{child['agent']['id']}/background", json={"session_id": "main", "request_id": str(uuid.uuid4())}).json()
        assert result["background_state"] == "supported"
        assert result["evidence"][0]["page_number"] == 2
        assert "frame_evidence" not in result
        context = json.loads(next(line for line in provider.calls[1]["prompt"].splitlines() if line.startswith('{"book_title"')))
        assert context["current_page"] is None
        assert "current page" not in context["question"]
        assert "no confirmed source page" in context["question"]
        assert any(path.parent.name == "pages" for path in provider.calls[1]["images"])


def test_book_claim_with_empty_pages_is_rejected_even_with_frame(tmp_path):
    provider = ScriptedProvider([HIT,
        {"answer": "The bird lives in a castle.", "evidence_scope": "book", "evidence_pages": [], "uncertain": False}], enabled=("vision",))
    app = create_app(settings_for(tmp_path), provider)
    unmapped_book(app.state.store)
    with TestClient(app) as client:
        child = create(client, time=59)
        result = client.post(f"/api/books/synthetic/object-agents/{child['agent']['id']}/ask", json=ask_body("Where does it live in the book?")).json()
        assert result["uncertain"] and not result["success"]
        assert "castle" not in result["answer"]
        assert "frame_evidence" not in result


def test_unmapped_followup_separately_verifies_action_and_source_story(tmp_path):
    class FollowupProvider(ScriptedProvider):
        async def vision(self, prompt, images):
            if "ANSWER_VERIFICATION_V1" in prompt:
                return {"supported": True, "evidence_scope": "current_frame", "evidence_pages": [],
                        "uncertain": False, "reason": "The actual frame supports the posture."}
            if "只观察实际帧中的选中对象" in prompt:
                return {"answer": "It is standing.", "uncertain": False}
            return await super().vision(prompt, images)
    provider = FollowupProvider([HIT, {"answer": "The bird shares the apple with a rabbit.", "evidence_pages": [2], "uncertain": False}], enabled=("vision", "chat"))
    app = create_app(settings_for(tmp_path), provider)
    unmapped_book(app.state.store)
    with TestClient(app) as client:
        child = create(client, time=59)
        result = client.post(f"/api/books/synthetic/object-agents/{child['agent']['id']}/ask", json=ask_body("What is it doing?")).json()
        assert not result["uncertain"] and result["success"]
        assert result["answer"].startswith("It is standing. In the story,")
        assert result["frame_evidence"]["time"] == 59
        assert [e["page_number"] for e in result["evidence"]] == [2]
        assert result["evidence_validation"]["story_review"]["evidence_scope"] == "book"
