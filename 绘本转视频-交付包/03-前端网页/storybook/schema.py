"""Validated geometry and stable serialization used by every agent.

All boxes are normalized ``[left, top, width, height]`` in the original
page image unless the field explicitly says ``video_bbox``.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"


def normalize_bbox(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in value)
    except (ValueError, TypeError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
        return None
    right, bottom = min(1.0, x + w), min(1.0, y + h)
    x, y = max(0.0, x), max(0.0, y)
    if right <= x or bottom <= y:
        return None
    return [round(x, 6), round(y, 6), round(right - x, 6), round(bottom - y, 6)]


def rect_bbox(rect: Any, page_width: float, page_height: float) -> list[float] | None:
    return normalize_bbox([rect[0] / page_width, rect[1] / page_height,
                           (rect[2] - rect[0]) / page_width,
                           (rect[3] - rect[1]) / page_height])


def contain_transform(image_width: int, image_height: int,
                      video_width: int, video_height: int) -> dict:
    """Match ffmpeg's even-dimension contain/pad filter exactly."""
    scale = min(video_width / image_width, video_height / image_height)
    width = max(2, int(image_width * scale) // 2 * 2)
    height = max(2, int(image_height * scale) // 2 * 2)
    # yuv420p pad positions are aligned to even pixels by FFmpeg.
    left = ((video_width - width) // 2) // 2 * 2
    top = ((video_height - height) // 2) // 2 * 2
    return {"kind": "contain", "image_width": image_width, "image_height": image_height,
            "video_width": video_width, "video_height": video_height,
            "content_rect": [left, top, width, height],
            "normalized_rect": [left / video_width, top / video_height,
                                width / video_width, height / video_height]}


def page_to_video_bbox(box: list[float], transform: dict) -> list[float]:
    left, top, width, height = transform["normalized_rect"]
    return [left + box[0] * width, top + box[1] * height,
            box[2] * width, box[3] * height]


def video_to_page_point(x: float, y: float, transform: dict) -> tuple[float, float] | None:
    left, top, width, height = transform["normalized_rect"]
    if not (left <= x <= left + width and top <= y <= top + height):
        return None
    return ((x - left) / width, (y - top) / height)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def sanitize_objects(raw: Any, page_number: int, source: str) -> list[dict]:
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw[:160]:
        if not isinstance(item, dict):
            continue
        box = normalize_bbox(item.get("bbox"))
        label = str(item.get("label", "")).strip()[:120]
        if not box or not label:
            continue
        result.append({"id": f"p{page_number}-o{len(result) + 1}", "label": label,
                       "label_en": str(item.get("label_en", ""))[:120],
                       "label_zh": str(item.get("label_zh", ""))[:120],
                       "bbox": box, "description": str(item.get("description", ""))[:1000],
                       "source": source,
                       "confidence": min(1.0, max(0.0, float(item.get("confidence", 0.5))))
                       if isinstance(item.get("confidence", 0.5), (int, float))
                       and math.isfinite(item.get("confidence", 0.5)) else 0.5})
    return result


def sanitize_words(raw: Any) -> list[dict]:
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw[:2000]:
        if isinstance(item, dict):
            box = normalize_bbox(item.get("bbox"))
            text = str(item.get("text", "")).strip()[:200]
            if box and text:
                result.append({"text": text, "bbox": box})
    return result
