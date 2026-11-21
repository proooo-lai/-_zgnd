"""Create a silent local preview from an explicitly supplied authorized PDF.

    python tools/make_demo_book.py --pdf /path/to/authorized.pdf

No directories are searched. No model calls or invented story facts are used.
This limited preview is not a generative-video or generalization acceptance run.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storybook.pipeline import assert_allowed_input, extract_pdf, detect_language
from storybook.schema import contain_transform, write_json
from storybook.settings import Settings


def run(ffmpeg, args):
    result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", *args], capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace")[-800:])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True, help="显式指定获授权的 PDF；不会自动搜索数据目录")
    parser.add_argument("--book-id", default="demo-local")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--max-pages", type=int, default=3)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args.book_id):
        parser.error("book-id 只能包含字母、数字、下划线和连字符")
    if not 1 <= args.max_pages <= 100:
        parser.error("max-pages 应在 1 到 100 之间")
    # Reject protected lexical paths before resolution, enumeration or reads.
    source = assert_allowed_input(Path(args.pdf))
    data = assert_allowed_input(args.data_dir)
    folder = data / args.book_id
    if folder.exists():
        parser.error("目标目录已存在，请使用新的 book-id，避免覆盖原有作品")
    settings = Settings(data_dir=data, config_file=data / "no-demo-config.json")
    if not settings.ffmpeg:
        parser.error("未找到 FFmpeg，请先安装媒体工具")
    pages, source_info = extract_pdf(source, folder)
    source_count = len(pages)
    pages = pages[:args.max_pages]
    scenes, concat = [], []
    segments = folder / "segments"
    segments.mkdir()
    for page in pages:
        number = page["number"]
        segment = segments / f"{number:04}.mp4"
        run(settings.ffmpeg, ["-loop", "1", "-i", str(folder / "pages" / f"{number:04}.png"),
                             "-t", "8", "-r", "24", "-vf",
                             "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p",
                             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-y", str(segment)])
        concat.append("file '" + segment.name + "'")
        page.update(title=f"第 {number} 页", questions=[], description="")
        scenes.append({"page_number": number, "start": (number - 1) * 8.0, "end": number * 8.0,
                       "narration": page["native_text"], "has_narration": False,
                       "video_prompt": "原页静态预览，未调用生成模型，未合成语音",
                       "transform": contain_transform(page["image_width"], page["image_height"], 1280, 720)})
    listing = segments / "concat.txt"
    listing.write_text("\n".join(concat), encoding="utf-8")
    run(settings.ffmpeg, ["-f", "concat", "-safe", "1", "-i", str(listing), "-c", "copy", "-y", str(folder / "book.mp4")])
    book = {"id": args.book_id, "title": (source_info["pdf_title"] or source.stem) + "（本地预览）",
            "language": detect_language(pages), "age_band": "待复核", "mode": "local", "duration": len(pages) * 8,
            "video_url": f"/media/{args.book_id}/book.mp4", "pages": pages, "scenes": scenes,
            "review": {"status": "pending", "reviewer": "", "age_band": "待复核"},
            "warnings": [f"本地静态无声预览，仅展示前 {len(pages)}/{source_count} 页；未调用模型，不代表完整生成验收。"],
            "provenance": {**source_info, "generative_video_used": False, "vision_used": False,
                           "story_model_used": False, "execution": "local_static_preview", "page_coverage": [p["number"] for p in pages]}}
    write_json(folder / "book.json", book)
    print(json.dumps({"book_id": args.book_id, "pages": len(pages), "mode": "local"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
