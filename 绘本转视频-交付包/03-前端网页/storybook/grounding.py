"""Visible click reference and explicit model-to-video coordinate conversion."""
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ClickObservation(BaseModel):
    model_config = ConfigDict(strict=True)
    found: bool
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)
    bbox_1000: list[StrictInt] = Field(default_factory=list, max_length=4)
    confidence: float = Field(default=0, ge=0, le=1)

    def video_box(self):
        if not self.found or not self.label.strip() or len(self.bbox_1000) != 4:
            return None
        left, top, right, bottom = self.bbox_1000
        if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
            return None
        return [left / 1000, top / 1000, (right - left) / 1000, (bottom - top) / 1000]


def mark_click(source: Path, output: Path, x: float, y: float) -> Path:
    """Add a point on a copy, preserving the full frame and its coordinates."""
    with Image.open(source) as original:
        marked = original.convert("RGB")
        draw = ImageDraw.Draw(marked)
        px, py = round(x * (marked.width - 1)), round(y * (marked.height - 1))
        radius = max(6, round(min(marked.size) * .015))
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), outline="white", width=6)
        draw.ellipse((px - radius, py - radius, px + radius, py + radius), outline="red", width=3)
        draw.line((px - 4, py, px + 4, py), fill="red", width=2)
        draw.line((px, py - 4, px, py + 4), fill="red", width=2)
        marked.save(output, format="PNG")
    return output


def pointed_frame(frame: Path, selected: dict | None):
    """Share the exact target reference between action generation and review."""
    point = (selected or {}).get("click_point")
    if not point:
        return [frame], "\n附图为实际视频帧。无法明确定位对象时应表示不确定。"
    marked = mark_click(frame, frame.with_name("followup-click-reference.png"), *point)
    return [frame, marked], ("\n第一张是实际视频帧，第二张是同一帧的点击参考图，只有红色空心圆与十字不同。"
                             "只回答十字中心指向的选中对象，不能改为旁边同名对象。"
                             "标记本身和点击坐标仅用于定位，不是动作证据；核对动作时以未标记原帧为准。")
