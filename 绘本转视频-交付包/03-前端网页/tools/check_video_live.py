"""One explicit short cloud-video integration check using self-created art."""
from __future__ import annotations

import asyncio
import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storybook.providers import Provider, ProviderError
from storybook.settings import Settings
from storybook.video import media_duration, probe_media


async def main(name="video-probe", image=None):
    image = Path(image) if image else ROOT / "artifacts" / "live_validation" / "probe.png"
    if not image.is_file():
        raise SystemExit(
            "缺少参考图：" + str(image) + "\n"
            "用 --image 指定一张 16:9 的静帧，或用 python tools/make_demo_book.py 生成 demo 绘本后传它的 pages/0001.png。"
        )
    # 与其它联调脚本一致：产物落在工程内的 artifacts/
    output = ROOT / "artifacts" / "live_validation"
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / ("video.json" if name == "video-probe" else name + ".json")
    video_path = output / (name + ".mp4")
    trace_path = video_path.with_suffix(".task.json")
    # A second invocation must never silently create a second paid task.
    if trace_path.exists():
        raise SystemExit("A video probe trace already exists; inspect the prior task instead of resubmitting.")
    settings = Settings()
    provider = Provider(settings)
    config = provider.config("video")
    secrets = [v.get("api_key", "") for v in settings.providers.values() if isinstance(v, dict)]
    diagnostics = []

    def safe(value):
        value = str(value)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        value = re.sub(r"https?://\S+", "[url]", value)
        value = re.sub(r"\b\d{8,}\b", "[account-or-request-id]", value)
        value = re.sub(r"\b(?:sk|ak|ep)-[A-Za-z0-9_-]+", "[identifier]", value)
        value = re.sub(r"(?i)(request id:\s*)\S+", r"\1[redacted]", value)
        return value[:1000]

    async def response_hook(response):
        if response.is_error:
            await response.aread()
            diagnostic = {"http_status": response.status_code}
            try:
                body = response.json()
                error = body.get("error", {}) if isinstance(body, dict) else {}
                if isinstance(error, dict):
                    for key in ("code", "message"):
                        if key in error:
                            diagnostic[key] = safe(error[key])
            except ValueError:
                diagnostic["code"] = "non_json_error"
            diagnostics.append(diagnostic)

    provider.client.event_hooks["response"] = [response_hook]
    report = {"model": config.get("model"), "status": "starting", "requested_seconds": 5,
              "requested_resolution": "720p", "source": "self_created_art", "started_at": time.time()}
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    started = time.perf_counter()
    try:
        await provider.generate_clip(
            image,
            "Keep the reference illustration style and exact original composition. "
            "Animate the little blue bird with a very gentle natural blink and a small bob. "
            "Keep the red apple and tree fixed. Do not add objects or characters. "
            "Preserve all original Chinese text exactly. No camera move, no subtitles.",
            video_path, 5,
        )
        probe = await probe_media(video_path, settings.ffprobe)
        stream = next(s for s in probe["streams"] if s.get("codec_type") == "video")
        report.update(status="succeeded", ok=True, duration=media_duration(probe),
                      width=stream.get("width"), height=stream.get("height"),
                      frame_rate=stream.get("avg_frame_rate"), codec=stream.get("codec_name"),
                      output=str(video_path))
    except Exception as error:
        report.update(status="failed", ok=False,
                      error=safe(error) if isinstance(error, ProviderError) else type(error).__name__)
    finally:
        report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        report["diagnostics"] = diagnostics
        if trace_path.exists():
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            report["task_id"] = trace.get("task_id")
            report["task_status"] = trace.get("status")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False), flush=True)
        await provider.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", choices=("video-probe", "video-probe-enabled"), default="video-probe")
    parser.add_argument("--image", default=None, help="图生视频的参考静帧；默认 artifacts/live_validation/probe.png")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.name, arguments.image))
