"""删除绘本：清理数据目录与问答记录，未知 id 返回 404，生成中拒绝删除。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from storybook.schema import contain_transform
from storybook.server import create_app
from storybook.settings import Settings


class QuietProvider:
    def available(self, name):
        return name in ("asr", "chat")

    def mode(self, name):
        return "mock"

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
    pages = store.directory("book1") / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "0001.png").write_bytes(b"png-placeholder")
    store.append("book1", "s1", {"turn_id": "t1", "answer": "The bird is by the tree.", "status": "complete"})
    return book


def test_delete_book_removes_directory_and_history(tmp_path):
    app = create_app(settings_for(tmp_path), QuietProvider())
    seed_book(app)
    store = app.state.store
    with TestClient(app) as client:
        assert [book["id"] for book in store.books()] == ["book1"]
        assert store.history("book1", "s1"), "precondition: a recorded turn exists"

        response = client.delete("/api/books/book1")

        assert response.status_code == 200
        assert response.json() == {"deleted": True, "book_id": "book1"}
        assert store.books() == []
        assert not store.directory("book1").exists(), "book directory must be gone"
        assert store.history("book1", "s1") == [], "turn rows must be purged with the book"


def test_delete_missing_book_is_404(tmp_path):
    app = create_app(settings_for(tmp_path), QuietProvider())
    with TestClient(app) as client:
        response = client.delete("/api/books/ghost")
        assert response.status_code == 404
        assert "未找到" in response.json()["detail"]


@pytest.mark.parametrize("bad_id", ["..%2F..%2Fwindows", "bad id", "x" * 81])
def test_delete_rejects_invalid_identifier(tmp_path, bad_id):
    app = create_app(settings_for(tmp_path), QuietProvider())
    with TestClient(app) as client:
        response = client.delete(f"/api/books/{bad_id}")
        assert response.status_code in (400, 404, 405), "invalid ids must never reach the filesystem"


def test_delete_blocked_while_job_running(tmp_path):
    app = create_app(settings_for(tmp_path), QuietProvider())
    seed_book(app)
    app.state.jobs["job1"] = {"book_id": "book1", "status": "running", "progress": 0.3}
    with TestClient(app) as client:
        response = client.delete("/api/books/book1")
        assert response.status_code == 409
        assert "生成" in response.json()["detail"]
        assert app.state.store.books(), "生成中的书不能被删掉"


def test_delete_blocked_while_lock_held(tmp_path):
    app = create_app(settings_for(tmp_path), QuietProvider())
    seed_book(app)
    asyncio.run(app.state.book_lock.acquire())
    try:
        with TestClient(app) as client:
            response = client.delete("/api/books/book1")
            assert response.status_code == 409
            assert app.state.store.books()
    finally:
        app.state.book_lock.release()
