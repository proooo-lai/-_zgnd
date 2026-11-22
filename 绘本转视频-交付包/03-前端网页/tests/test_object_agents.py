"""Object assistants own their target, frozen frame and dialogue history."""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from storybook.server import create_app
from storybook.interaction import Interactor
from storybook.object_agents import ObjectAgents
from test_interaction import make_book, encoded_frame, ScriptedProvider
from test_server import settings_for, OfflineProvider, wav_bytes


def create(client, parent="main", **changes):
    body = dict(x=.47, y=.55, time=10, frame=encoded_frame(), session_id=parent,
                agent_id="object-" + uuid.uuid4().hex, request_id=str(uuid.uuid4()))
    body.update(changes)
    response = client.post("/api/books/synthetic/object-agents", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def ask_body(question="hello", parent="main"):
    return {"question": question, "session_id": parent, "request_id": str(uuid.uuid4())}


def test_click_and_object_answers_never_enter_main_dialogue(tmp_path):
    provider = ScriptedProvider([{"answer": "The apple is red.", "evidence_scope": "book", "evidence_pages": [1], "uncertain": False}])
    app = create_app(settings_for(tmp_path), provider)
    make_book(app.state.store)
    with TestClient(app) as client:
        child = create(client)
        assert child["agent"]["label"] == "apple"
        agent = child["agent"]["id"]
        assert agent != "main"
        reply = client.post(f"/api/books/synthetic/object-agents/{agent}/ask", json=ask_body("What is its story?"))
        assert reply.status_code == 200
        assert reply.json()["evidence"]
        assert app.state.store.history("synthetic", "main") == []
        assert len(app.state.store.history("synthetic", agent)) == 2
        assert client.get(child["agent"]["avatar_url"]).status_code == 200
        second = create(client, x=.52, y=.4)
        assert second["agent"]["id"] != agent
        assert len(app.state.store.history("synthetic", second["agent"]["id"])) == 1
        wrong = client.post(f"/api/books/synthetic/object-agents/{agent}/ask", json=ask_body(parent="another-parent"))
        assert wrong.status_code == 404
        assert client.get(f"/api/books/other/object-agents/{agent}/history?session_id=main").status_code == 404


def test_empty_click_does_not_open_an_askable_agent(tmp_path):
    app = create_app(settings_for(tmp_path), ScriptedProvider(enabled=()))
    make_book(app.state.store)
    with TestClient(app) as client:
        result = create(client, x=.01, y=.5)
        assert result["agent"] is None
        assert result["result"]["uncertain"]
        assert app.state.store.history("synthetic", "main") == []


def test_target_survives_long_conversation_and_frame_cannot_be_spoofed(tmp_path):
    provider = ScriptedProvider([{"answer": "The apple is red.", "evidence_scope": "book", "evidence_pages": [1], "uncertain": False}])
    app = create_app(settings_for(tmp_path), provider)
    make_book(app.state.store)
    with TestClient(app) as client:
        agent = create(client)["agent"]["id"]
        for number in range(15):
            app.state.store.append("synthetic", agent, {"turn_id": f"prior-{number}", "question": "Earlier?", "answer": "Reference only."})
        reply = client.post(f"/api/books/synthetic/object-agents/{agent}/ask", json=ask_body("What is its story?"))
        assert reply.status_code == 200
        prompt = provider.calls[0]["prompt"]
        data = json.loads(next(line for line in prompt.splitlines() if line.startswith('{"book_title"')))
        assert data["selected_object"]["label"] == "apple"
        assert data["current_time"] == 10
        assert data["prior_turns"][-1]["question"] == "Earlier?"
        assert provider.calls[0]["images"] == []
        spoof = client.post(f"/api/books/synthetic/object-agents/{agent}/ask", json={**ask_body(), "time": 70, "frame": encoded_frame("green")})
        assert spoof.status_code == 422


@pytest.mark.parametrize("stream", [False, True])
def test_voice_uses_child_session_and_its_frozen_context(tmp_path, stream):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    make_book(app.state.store)
    with TestClient(app) as client:
        agent = create(client)["agent"]["id"]
        path = f"/api/books/synthetic/object-agents/{agent}/voice" + ("/stream" if stream else "")
        response = client.post(path, data={"session_id": "main", "request_id": str(uuid.uuid4())}, files={"audio": ("question.wav", wav_bytes(), "audio/wav")})
        assert response.status_code == 200, response.text
        if stream:
            assert 'event: transcript' in response.text
            assert 'event: result' in response.text
        else:
            assert response.json()["transcript"] == "Where is the bird?"
        assert app.state.store.history("synthetic", "main") == []
        history = app.state.store.history("synthetic", agent)
        assert len(history) == 2
        assert history[-1]["input_kind"] == "voice"
        assert history[-1]["frame_url"].endswith("frame.jpg")


def test_clearing_parent_also_clears_only_its_object_agents(tmp_path):
    app = create_app(settings_for(tmp_path), ScriptedProvider(enabled=()))
    make_book(app.state.store)
    with TestClient(app) as client:
        first = create(client)["agent"]["id"]
        other = create(client, parent="other")["agent"]["id"]
        assert client.delete("/api/books/synthetic/history?session_id=main").status_code == 200
        assert app.state.store.history("synthetic", first) == []
        assert len(app.state.store.history("synthetic", other)) == 1
        assert client.get(f"/api/books/synthetic/object-agents/{first}/history?session_id=main").status_code == 404
        assert client.get(f"/api/books/synthetic/object-agents/{other}/history?session_id=other").status_code == 200


def test_delayed_cancel_cannot_cancel_a_newer_object_question(tmp_path):
    app = create_app(settings_for(tmp_path), ScriptedProvider(enabled=()))
    make_book(app.state.store)
    agents = ObjectAgents(app.state.store, Interactor(app.state.store, app.state.provider, app.state.settings))
    with TestClient(app) as client:
        agent = create(client)["agent"]["id"]

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def current_question():
            async with agents.operation("synthetic", agent, "main", str(uuid.uuid4())):
                entered.set()
                await release.wait()

        task = asyncio.create_task(current_question())
        await entered.wait()
        try:
            await agents.cancel("synthetic", agent, "main", str(uuid.uuid4()))
            assert not task.done()
        finally:
            release.set()
            await task

    asyncio.run(exercise())


def test_cancel_during_target_recognition_removes_unseen_child_session(tmp_path):
    app = create_app(settings_for(tmp_path), ScriptedProvider(enabled=()))
    make_book(app.state.store)
    agents = ObjectAgents(app.state.store, Interactor(app.state.store, app.state.provider, app.state.settings))

    class WaitingInteractor:
        def __init__(self):
            self.entered = asyncio.Event()

        async def touch(self, *args):
            self.entered.set()
            await asyncio.Future()

    reader = WaitingInteractor()
    agents.interactor = reader
    agent, request = "object-" + uuid.uuid4().hex, str(uuid.uuid4())

    async def exercise():
        task = asyncio.create_task(agents.create("synthetic", .47, .55, 10, "main", encoded_frame(), None,
                                                 agent_id=agent, request_id=request))
        done, _ = await asyncio.wait({task}, timeout=.05)
        if done:
            await task
        await reader.entered.wait()
        await agents.cancel("synthetic", agent, "main", request)
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)

    asyncio.run(exercise())
    assert not agents.directory("synthetic", agent).exists()
    assert app.state.store.history("synthetic", agent) == []


def test_cancel_arriving_before_create_is_remembered_for_that_request(tmp_path):
    app = create_app(settings_for(tmp_path), ScriptedProvider(enabled=()))
    make_book(app.state.store)
    agents = ObjectAgents(app.state.store, Interactor(app.state.store, app.state.provider, app.state.settings))
    agent, request = "object-" + uuid.uuid4().hex, str(uuid.uuid4())

    async def exercise():
        await agents.cancel("synthetic", agent, "main", request)
        with pytest.raises(Exception) as error:
            await agents.create("synthetic", .47, .55, 10, "main", encoded_frame(), None,
                                agent_id=agent, request_id=request)
        assert getattr(error.value, "status_code", None) == 409

    asyncio.run(exercise())
    assert not agents.directory("synthetic", agent).exists()
