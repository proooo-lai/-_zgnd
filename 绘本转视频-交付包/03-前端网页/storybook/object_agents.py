"""Independent, book-grounded object assistants; no extra model service needed."""
from __future__ import annotations

import asyncio
import base64
import json
import re
import shutil
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import HTTPException
from PIL import Image

from .interaction import selected_object
from .schema import write_json

BACKGROUND_VERSION = "object-background-context-v2"


class ObjectAgents:
    def __init__(self, store, interactor):
        self.store, self.interactor = store, interactor
        self.active = {}
        self.clearing = set()
        self.cancelled = {}

    @staticmethod
    def request_token(value):
        try:
            return str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("无效的对象助手请求编号") from None

    @staticmethod
    def agent_identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"object-[0-9a-f]{32}", value):
            raise ValueError("无效的对象助手标识")
        return value

    def directory(self, book_id, agent_id):
        return self.store.directory(book_id) / "object_agents" / self.agent_identifier(agent_id)

    def get(self, book_id, agent_id, parent):
        self.store.book(book_id)
        self.store.identifier(parent)
        path = self.directory(book_id, agent_id) / "context.json"
        if not path.is_file():
            raise FileNotFoundError()
        context = json.loads(path.read_text(encoding="utf-8"))
        if context["parent_session_id"] != parent or context["status"] != "ready":
            raise FileNotFoundError()
        return context

    @asynccontextmanager
    async def operation(self, book_id, agent_id, parent, request_id):
        key = (book_id, agent_id)
        request_id = self.request_token(request_id)
        token = (*key, request_id)
        now = time.monotonic()
        self.cancelled = {saved: expires for saved, expires in self.cancelled.items() if expires > now}
        if token in self.cancelled:
            raise HTTPException(409, "本轮请求已取消")
        if book_id in self.clearing or (book_id, parent) in self.clearing or key in self.active:
            raise HTTPException(409, "对象助手正在处理，请稍后再试")
        self.active[key] = (parent, request_id, asyncio.current_task())
        try:
            yield
        finally:
            self.active.pop(key, None)

    async def create(self, book_id, x, y, timestamp, parent, frame, voice, *, agent_id, request_id):
        self.store.book(book_id)
        self.store.identifier(parent)
        self.agent_identifier(agent_id)
        request_id = self.request_token(request_id)
        folder = self.directory(book_id, agent_id)
        context = {"id": agent_id, "book_id": book_id, "parent_session_id": parent,
                   "session_id": agent_id, "time": timestamp, "status": "recognizing"}
        async with self.operation(book_id, agent_id, parent, request_id):
            write_json(folder / "context.json", context)
            try:
                result = await self.interactor.touch(book_id, x, y, timestamp, agent_id, frame, voice)
                focus = selected_object([result], result.get("interaction_page"), timestamp)
                if not focus:
                    context["status"] = "unrecognized"
                    write_json(folder / "context.json", context)
                    return {"agent": None, "result": result}
                # Freeze the exact frame used for recognition, never a later player frame.
                source = self.store.directory(book_id) / "turns" / result["turn_id"] / "frame.jpg"
                if not source.is_file():
                    await self.interactor.frame(self.store.book(book_id), timestamp, source)
                shutil.copyfile(source, folder / "frame.jpg")
                with Image.open(source) as image:
                    bbox = (result.get("boxes") or [{}])[0].get("bbox")
                    if bbox:
                        bx, by, width, height = bbox
                        image = image.crop((max(0, int(bx * image.width)), max(0, int(by * image.height)),
                                            min(image.width, max(1, int((bx + width) * image.width))),
                                            min(image.height, max(1, int((by + height) * image.height)))))
                    image.thumbnail((180, 180))
                    image.convert("RGB").save(folder / "avatar.jpg", "JPEG")
                context.update(status="ready", label=focus["label"], page_number=focus["page_number"],
                               background_available=any(self.interactor.provider.available(name) for name in ("chat", "vision")),
                               focus_turn=result, avatar_url=f"/media/{book_id}/object_agents/{agent_id}/avatar.jpg")
                write_json(folder / "context.json", context)
                return {"agent": self.public(context), "result": result}
            except asyncio.CancelledError:
                self.store.clear_history(book_id, agent_id)
                shutil.rmtree(folder, ignore_errors=True)
                raise
            except BaseException:
                context["status"] = "failed"
                write_json(folder / "context.json", context)
                raise

    @staticmethod
    def public(context):
        return {**{key: context[key] for key in ("id", "session_id", "label", "time", "page_number", "avatar_url")},
                "background_available": context.get("background_available", False)}

    def parameters(self, book_id, agent_id, parent):
        context = self.get(book_id, agent_id, parent)
        frame = self.directory(book_id, agent_id) / "frame.jpg"
        return {"session_id": agent_id, "timestamp": context["time"],
                "frame": "data:image/jpeg;base64," + base64.b64encode(frame.read_bytes()).decode("ascii"),
                "focus_turn": context["focus_turn"]}

    async def ask(self, book_id, agent_id, parent, request_id, question, **options):
        async with self.operation(book_id, agent_id, parent, request_id):
            return await self.interactor.ask(book_id, question, **self.parameters(book_id, agent_id, parent), **options)

    async def background(self, book_id, agent_id, parent, request_id, voice=None):
        async with self.operation(book_id, agent_id, parent, request_id):
            context = self.get(book_id, agent_id, parent)
            if context.get("background_result", {}).get("background_version") == BACKGROUND_VERSION:
                return {**context["background_result"], "background_execution": "reused_same_object"}
            question = ("请仅根据原书可定位的证据，介绍它在故事里的相关背景、情节和角色关系，并给出支持介绍的原书页码。"
                        "不以外貌、百科常识或之前的介绍代替故事证据；对没有证据的经历、动机和身世明确说明无法确认，不要编造。")
            result = await self.interactor.ask(book_id, question, **self.parameters(book_id, agent_id, parent),
                                               input_kind="object_background", voice=voice)
            result.update(background_state="supported" if not result.get("uncertain") and result.get("evidence") else "unconfirmed",
                          background_execution="live", background_version=BACKGROUND_VERSION)
            self.store.update_turn(book_id, agent_id, result["turn_id"],
                                   {key: result[key] for key in ("background_state", "background_execution", "background_version")})
            validation = result.get("evidence_validation", {})
            reusable = validation.get("decision") == "supported" or validation.get("reason") in (
                "uncertain_or_invalid_references", "evidence_does_not_support_answer")
            if reusable:
                context["background_result"] = result
                write_json(self.directory(book_id, agent_id) / "context.json", context)
            return result

    async def cancel(self, book_id, agent_id, parent, request_id):
        self.store.book(book_id)
        self.store.identifier(parent)
        self.agent_identifier(agent_id)
        request_id = self.request_token(request_id)
        item = self.active.get((book_id, agent_id))
        context_path = self.directory(book_id, agent_id) / "context.json"
        if item and item[0] != parent:
            raise FileNotFoundError()
        if not item and context_path.is_file():
            context = json.loads(context_path.read_text(encoding="utf-8"))
            if context.get("parent_session_id") != parent:
                raise FileNotFoundError()
        elif not item:
            self.agent_identifier(agent_id)
        now = time.monotonic()
        self.cancelled = {saved: expires for saved, expires in self.cancelled.items() if expires > now}
        token = (book_id, agent_id, request_id)
        self.cancelled[token] = time.monotonic() + 300
        if len(self.cancelled) > 8192:
            oldest = sorted(self.cancelled, key=self.cancelled.get)[:len(self.cancelled) - 8192]
            for saved in oldest:
                self.cancelled.pop(saved, None)
        if item and item[1] == request_id:
            task = item[2]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def clear(self, book_id, parent=None):
        """Cancel and join child work before deleting records; no late resurrection."""
        guard = book_id if parent is None else (book_id, parent)
        self.clearing.add(guard)
        try:
            tasks = [item[2] for (book, _), item in list(self.active.items())
                     if book == book_id and (parent is None or parent == item[0])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            root = self.store.directory(book_id) / "object_agents"
            for path in root.glob("*/context.json"):
                context = json.loads(path.read_text(encoding="utf-8"))
                if parent is None or context["parent_session_id"] == parent:
                    self.store.clear_history(book_id, context["session_id"])
                    shutil.rmtree(path.parent)
        finally:
            self.clearing.discard(guard)
