"""Keep current-frame observation separate from source-book storytelling."""
import asyncio
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .answer_validation import StoryContext
from .grounding import pointed_frame
from .safety import UNTRUSTED_CONTENT_RULE


class FrameAction(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str = Field(min_length=1, max_length=1500)
    uncertain: bool


async def joined(*calls):
    """Do not leave sibling model requests running after failure/cancellation."""
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def draft_followup(provider, question: str, selected: dict, pages: list,
                         current_page: int, frame: Path, language: str):
    output_language = "English" if language == "en" else "中文"
    images, point_rule = pointed_frame(frame, selected)
    frame_prompt = (UNTRUSTED_CONTENT_RULE +
        "\n只观察实际帧中的选中对象，直接描述它此刻可见的动作或姿态。"
        "不推断意图或画外行为，不引入故事情节。选中对象名只用于定位，不是事实证据。"
        '只返回JSON {"answer":"一句直接回答","uncertain":false}。'
        "看不清或无法确认对象时uncertain=true。" + point_rule + "回答语言：" + output_language + "\n" +
        json.dumps({"question": question, "selected_object": selected}, ensure_ascii=False))
    story_prompt = (UNTRUSTED_CONTENT_RULE +
        "\n只从本次原书证据检索与选中对象有关的一条情节或角色关系，作为动作回答的故事补充。"
        "不描述当前画面动作，不把其他页情节说成此刻正在发生，不重复或补写历史回答。"
        "使用第三人称叙述，保留原文意图、态度的限定程度，不把犹豫推断成已作出的决定。"
        "每一事实和专名都需要所引页支持；若姓名来自其他页，必须同时引用实际写出姓名的那一页。"
        "优先使用不增加身份事实的代称，如书里的孩子、主人公或该角色，避免非必要的人名。对象名仅供消歧。"
        '只返回JSON {"answer":"简短故事背景，不加故事里前缀","evidence_pages":[实际支持的页码],"uncertain":false}。'
        "证据不足则answer为空且evidence_pages=[]、uncertain=true。回答语言：" + output_language + "\n" +
        json.dumps({"question": question, "selected_object": selected, "book_evidence": pages}, ensure_ascii=False))
    frame_raw, story_raw = await joined(provider.vision(frame_prompt, images), provider.chat(story_prompt))
    try:
        action = FrameAction.model_validate(frame_raw)
        story = StoryContext.model_validate(story_raw)
    except ValidationError:
        return None
    return {"answer": action.answer, "uncertain": action.uncertain, "evidence_scope": "current_frame",
            "evidence_pages": [current_page], "story_context": story.model_dump()}
