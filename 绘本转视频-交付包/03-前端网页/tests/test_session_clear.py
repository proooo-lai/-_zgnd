"""A session clear is a barrier around active and incoming main turns."""
import asyncio

import httpx
import pytest
from fastapi import HTTPException

from storybook.server import Question, create_app
from test_interaction import make_book
from test_server import settings_for, OfflineProvider, wav_bytes


class WaitingAnswer:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def available(self, name):
        return name == "chat"

    def mode(self, name):
        return "mock" if self.available(name) else "unconfigured"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            return {"evidence_scope": "book", "supported": True, "uncertain": False,
                    "evidence_pages": [1], "reason": "Synthetic source evidence."}
        self.entered.set()
        await self.release.wait()
        return {"answer": "Synthetic book answer.", "evidence_scope": "book",
                "evidence_pages": [1], "uncertain": False}

    async def close(self):
        pass


def test_clear_cancels_active_main_turn_before_deleting_session_history(tmp_path):
    provider = WaitingAnswer()
    app = create_app(settings_for(tmp_path), provider)
    make_book(app.state.store)

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ask = asyncio.create_task(client.post("/api/books/synthetic/ask", json={
                "question": "What is red?", "session_id": "main", "time": 10}))
            await asyncio.wait_for(provider.entered.wait(), 1)
            try:
                cleared = await asyncio.wait_for(client.delete("/api/books/synthetic/history?session_id=main"), 3)
                assert cleared.status_code == 200
                assert ask.done(), "clearing must stop and join an answer already in progress"
                assert app.state.store.history("synthetic", "main") == []
            finally:
                provider.release.set()
                await asyncio.wait_for(asyncio.gather(ask, return_exceptions=True), 3)
            assert app.state.store.history("synthetic", "main") == []

    asyncio.run(exercise())


def test_stream_created_before_clear_cannot_start_after_clear_finishes(tmp_path):
    app = create_app(settings_for(tmp_path), WaitingAnswer())
    make_book(app.state.store)
    work = app.state.session_work

    async def exercise():
        expected = work.version("synthetic", "main")
        async with work.clear("synthetic", "main"):
            pass
        called = False

        async def late_write():
            nonlocal called
            called = True

        with pytest.raises(HTTPException) as error:
            await session_call(work, expected, late_write)
        assert error.value.status_code == 409
        assert not called

    asyncio.run(exercise())


def test_stream_route_snapshots_session_before_returning_delayed_sse(tmp_path):
    app = create_app(settings_for(tmp_path), WaitingAnswer())
    make_book(app.state.store)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/books/{book_id}/ask/stream")

    async def exercise():
        response = await route.endpoint(book_id="synthetic", body=Question(question="What is red?", session_id="main", time=10))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.delete("/api/books/synthetic/history?session_id=main")).status_code == 200
        events = [part async for part in response.body_iterator]
        assert any("本会话正在清理" in part for part in events)
        assert app.state.store.history("synthetic", "main") == []

    asyncio.run(exercise())


def test_voice_stream_snapshots_session_before_waiting_for_audio_body(tmp_path):
    app = create_app(settings_for(tmp_path), OfflineProvider())
    make_book(app.state.store)
    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/books/{book_id}/voice/stream")

    class DelayedUpload:
        filename = "question.wav"

        def __init__(self):
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def read(self, _limit):
            self.entered.set()
            await self.release.wait()
            return wav_bytes()

    async def exercise():
        audio = DelayedUpload()
        pending = asyncio.create_task(route.endpoint(book_id="synthetic", audio=audio, session_id="main", time=10,
                                                     frame=None, voice=None))
        await audio.entered.wait()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.delete("/api/books/synthetic/history?session_id=main")).status_code == 200
        audio.release.set()
        response = await pending
        events = [part async for part in response.body_iterator]
        assert any("本会话正在清理" in part for part in events)
        assert app.state.store.history("synthetic", "main") == []

    asyncio.run(exercise())


async def session_call(work, expected, operation):
    async with work.operation("synthetic", "main", expected):
        await operation()
