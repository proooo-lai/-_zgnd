"""Cancel in-flight session turns and prevent writes while history is cleared."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import HTTPException


class SessionWork:
    def __init__(self):
        self.active = {}
        self.clearing = set()
        self.versions = {}

    def version(self, book_id, session_id):
        return self.versions.get((book_id, session_id), 0)

    @asynccontextmanager
    async def operation(self, book_id, session_id, expected_version=None):
        key = (book_id, session_id)
        if key in self.clearing or (expected_version is not None and expected_version != self.version(*key)):
            raise HTTPException(409, "本会话正在清理，请稍后再试")
        task = asyncio.current_task()
        self.active.setdefault(key, set()).add(task)
        try:
            yield
        finally:
            tasks = self.active.get(key)
            if tasks:
                tasks.discard(task)
                if not tasks:
                    self.active.pop(key, None)

    @asynccontextmanager
    async def clear(self, book_id, session_id):
        key = (book_id, session_id)
        if key in self.clearing:
            raise HTTPException(409, "本会话已在清理，请稍后再试")
        self.clearing.add(key)
        self.versions[key] = self.version(*key) + 1
        try:
            tasks = [task for task in self.active.get(key, set()) if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            yield
        finally:
            self.clearing.discard(key)
