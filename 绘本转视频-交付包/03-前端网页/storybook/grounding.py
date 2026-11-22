"""Visible click reference and explicit model-to-video coordinate conversion."""
from pathlib import Path
import math
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, StrictInt


class ClickObservation(BaseModel):
    model_config = ConfigDict(strict=True)
    found: bool
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=1000)
    bbox_1000: list[StrictInt] = Field(default_factory=list, max_length=4)
    coordinate_space: Literal['full_frame', 'crop']
    confidence: float = Field(default=0, ge=0, le=1)

    def video_box(self, transform: dict | None = None):
        if not self.found or not self.label.strip() or len(self.bbox_1000) != 4:
            return None
        left, top, right, bottom = self.bbox_1000
        if not (0 <= left < right <= 1000 and 0 <= top < bottom <= 1000):
            return None
        box = [left / 1000, top / 1000, (right - left) / 1000, (bottom - top) / 1000]
        if self.coordinate_space == 'crop':
            if not transform:
                return None
            fw, fh = transform['frame_size']
            x, y, width, height = transform['crop_rect']
            return [(x + box[0]*width)/fw, (y + box[1]*height)/fh,
                    box[2]*width/fw, box[3]*height/fh]
        return box


def make_click_context(source: Path, folder: Path, x: float, y: float) -> tuple[list[Path], dict]:
    """A bounded square crop, with an explicit pixel-edge transform to the frame.

    Shifting the window at edges avoids inventing padded image content. Image
    upscaling changes only display resolution, never the normalized coordinates.
    """
    if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in (x,y)):
        raise ValueError('点击坐标必须在画面范围内。')
    folder.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        fw, fh = original.size
        side = min(min(fw, fh), max(1, round(min(fw, fh)*.65)))
        left = max(0, min(fw-side, round(x*fw-side/2)))
        top = max(0, min(fh-side, round(y*fh-side/2)))
        local = [(x*fw-left)/side, (y*fh-top)/side]
        crop = original.convert('RGB').crop((left,top,left+side,top+side))
        display_side = min(1024, max(640, side))
        crop = crop.resize((display_side, display_side), Image.Resampling.LANCZOS)
        crop_path = folder/'click-crop.png'
        crop.save(crop_path)
    full_marker = mark_click(source, folder/'click-reference.png', x, y)
    crop_marker = mark_click(crop_path, folder/'click-crop-reference.png', *local)
    return [source,full_marker,crop_path,crop_marker], {
        'version':'click-crop-v1', 'frame_size':[fw,fh], 'crop_rect':[left,top,side,side],
        'display_size':[display_side,display_side], 'crop_point':local,
        'coordinate_convention':'pixel_edges_xywh; normalized_bbox_1000_corners'}


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
    images, _ = make_click_context(frame,frame.parent,*point)
    return images, ("\n第一张是实际视频帧，第二张是同一帧的点击参考图，只有红色空心圆与十字不同。"
                             "第三张是点击附近的未标记局部放大，第四张是对应局部标记图；优先核对局部中心，再结合整帧。"
                             "只回答十字中心指向的选中对象，不能改为旁边同名对象。"
                             "标记本身和点击坐标仅用于定位，不是动作证据；核对动作时以未标记原帧为准。")
