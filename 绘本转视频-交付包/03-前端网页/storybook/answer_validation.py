"""Strict contracts for draft answers and a separate evidence review call."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StoryContext(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str = Field(max_length=1500)
    evidence_pages: list[int] = Field(max_length=100)
    uncertain: bool = False


class AnswerDraft(BaseModel):
    model_config = ConfigDict(strict=True)
    answer: str = Field(min_length=1, max_length=5000)
    evidence_scope: Literal["book", "current_frame"]
    evidence_pages: list[int] = Field(max_length=100)
    # Older producers omitted this flag. References and review remain mandatory.
    uncertain: bool = False
    seek_page: int | None = None
    target_label: str = Field(default="", max_length=120)
    target_kind: Literal["object", "word", "none"] = "none"
    story_context: StoryContext | None = None


class EvidenceVerdict(BaseModel):
    model_config = ConfigDict(strict=True)
    supported: bool
    evidence_scope: Literal["book", "current_frame"]
    uncertain: bool
    evidence_pages: list[int] = Field(max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


VERIFIER_VERSION = "ANSWER_VERIFICATION_V1"
