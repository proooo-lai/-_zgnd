from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from .schema import write_json

IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "history.sqlite3"
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,book_id TEXT,session_id TEXT,created REAL,payload TEXT)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_session ON turns(book_id,session_id,created)")
            connection.execute("CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT,created REAL,payload TEXT)")

    def connect(self):
        return sqlite3.connect(self.db, timeout=10)

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise ValueError("无效的标识符")
        return value

    def directory(self, book_id):
        return self.root / self.identifier(book_id)

    def book(self, book_id):
        path = self.directory(book_id) / "book.json"
        if not path.is_file():
            raise FileNotFoundError("未找到绘本")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, book):
        write_json(self.directory(book["id"]) / "book.json", book)

    def books(self):
        result = []
        for path in self.root.glob("*/book.json"):
            try:
                book = json.loads(path.read_text(encoding="utf-8"))
                result.append({key: book.get(key) for key in ("id", "title", "language", "mode", "duration", "review", "video_url")})
            except (OSError, ValueError):
                continue
        return result

    def delete(self, book_id):
        book_id = self.identifier(book_id)
        directory = self.directory(book_id)
        if not (directory / "book.json").is_file():
            raise FileNotFoundError("未找到绘本")
        with self.connect() as connection:
            connection.execute("DELETE FROM turns WHERE book_id=?", (book_id,))
        shutil.rmtree(directory)

    def append(self, book_id, session_id, turn):
        self.identifier(book_id)
        self.identifier(session_id)
        with self.connect() as connection:
            connection.execute("INSERT INTO turns VALUES (?,?,?,?,?)", (turn["turn_id"], book_id, session_id, time.time(), json.dumps(turn, ensure_ascii=False)))

    def history(self, book_id, session_id, limit=1000):
        self.identifier(book_id)
        self.identifier(session_id)
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM turns WHERE book_id=? AND session_id=? ORDER BY created DESC LIMIT ?", (book_id, session_id, limit)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def update_turn(self, book_id, session_id, turn_id, updates):
        with self.connect() as connection:
            row = connection.execute("SELECT payload FROM turns WHERE id=? AND book_id=? AND session_id=?", (turn_id, book_id, session_id)).fetchone()
            if row:
                turn = json.loads(row[0])
                turn.update(updates)
                connection.execute("UPDATE turns SET payload=? WHERE id=?", (json.dumps(turn, ensure_ascii=False), turn_id))

    def clear_history(self, book_id, session_id):
        self.identifier(book_id)
        self.identifier(session_id)
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM turns WHERE book_id=? AND session_id=?", (book_id, session_id)).fetchall()
            connection.execute("DELETE FROM turns WHERE book_id=? AND session_id=?", (book_id, session_id))
        # Only delete files referenced by this session, within its own turn folder.
        directory = self.directory(book_id) / "turns"
        for row in rows:
            turn_id = json.loads(row[0]).get("turn_id", "")
            if IDENTIFIER.fullmatch(turn_id):
                folder = directory / turn_id
                if folder.is_dir():
                    for file in folder.iterdir():
                        if file.is_file():
                            file.unlink()
                    folder.rmdir()

    def metric(self, event):
        with self.connect() as connection:
            connection.execute("INSERT INTO metrics(created,payload) VALUES (?,?)", (time.time(), json.dumps(event, ensure_ascii=False, allow_nan=False)))

    def metrics(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT payload FROM metrics ORDER BY id DESC LIMIT 10000").fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]
