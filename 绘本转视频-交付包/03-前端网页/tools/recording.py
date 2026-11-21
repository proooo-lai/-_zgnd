"""Convert one continuous browser WebM recording to MP4 without cuts.

The source recording is preserved. This tool only converts container/codecs;
it does not manufacture interactions or certify that a recording is genuine.
FAQ prohibits secondary editing. Conversion without cuts is technically
distinct from editing, but this tool does not claim an organizer exemption.

    python tools/recording.py recording.webm recording.mp4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


class RecordingError(RuntimeError):
    pass


def checked_path(value: str | Path, must_exist: bool = True) -> Path:
    path = Path(value).expanduser()
    if "测试集" in str(path).replace("\\", "/").split("/"):
        raise RecordingError("拒绝访问测试集目录。")
    path = path.resolve()
    if "测试集" in path.parts:
        raise RecordingError("文件指向受保护的测试集目录。")
    if must_exist and not path.is_file():
        raise RecordingError(f"输入文件不存在: {path}")
    return path


def find_binary(name: str, override: str | None = None) -> str:
    if override:
        return str(checked_path(override))
    bundled = ROOT / "tools" / "bin" / (name + (".exe" if os.name == "nt" else ""))
    if bundled.is_file():
        return str(bundled)
    system = shutil.which(name)
    if system:
        return str(checked_path(system))
    if name == "ffmpeg":
        try:
            import imageio_ffmpeg
            return str(checked_path(imageio_ffmpeg.get_ffmpeg_exe()))
        except (ImportError, RuntimeError):
            pass
    raise RecordingError(f"找不到 {name}，请放入 tools/bin 或通过 --{name} 指定。")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def run_command(command: list[str], timeout: float = 900) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecordingError(f"录屏转换工具运行失败: {type(exc).__name__}") from None
    if result.returncode:
        raise RecordingError(f"录屏转换工具返回 {result.returncode}: {result.stderr[-2000:]}")
    return result


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    result = run_command([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-show_format",
                          "-of", "json", str(path)])
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RecordingError("ffprobe 未返回有效视频信息。") from None
    videos = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "video"]
    audios = [stream for stream in info.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise RecordingError("连续浏览器录屏应恰好包含一个视频轨；拒绝静默删除其他视频轨。")
    try:
        duration = float(info.get("format", {}).get("duration", "nan"))
    except (TypeError, ValueError):
        duration = float("nan")
    # MediaRecorder WebM often omits duration; decoded video frame counts alone
    # do not recover variable timestamps. Query the final packet timestamp.
    if not math.isfinite(duration):
        packets = run_command([ffprobe, "-v", "error", "-show_entries", "packet=pts_time,duration_time",
                               "-of", "csv=p=0", str(path)])
        maximum = 0.0
        for line in packets.stdout.splitlines():
            values = line.split(",")
            try:
                point = float(values[0])
                length = float(values[1]) if len(values) > 1 and values[1] != "N/A" else 0.0
                if math.isfinite(point) and math.isfinite(length):
                    maximum = max(maximum, point + length)
            except ValueError:
                continue
        duration = maximum
    if not math.isfinite(duration) or duration <= 0:
        raise RecordingError("无法确定录屏有效时长。")
    try:
        frames = int(videos[0]["nb_read_frames"])
    except (KeyError, TypeError, ValueError):
        raise RecordingError("无法完整解码并统计录屏帧数，拒绝生成未经验证的成片。") from None
    if frames <= 0:
        raise RecordingError("录屏没有可解码视频帧。")
    return {
        "duration_seconds": duration,
        "video_frames": frames,
        "width": videos[0].get("width"),
        "height": videos[0].get("height"),
        "video_codec": videos[0].get("codec_name"),
        "audio_tracks": len(audios),
        "audio_codecs": [stream.get("codec_name") for stream in audios],
        "raw": info,
    }


def conversion_command(ffmpeg: str, source: Path, destination: Path, info: dict[str, Any]) -> list[str]:
    # No trim, concat, seek, time-stretch, fixed frame rate, shortest or overlay.
    command = [ffmpeg, "-hide_banner", "-v", "warning", "-nostdin", "-n", "-i", str(source),
               "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-fps_mode:v", "passthrough"]
    width, height = int(info["width"]), int(info["height"])
    # Preserve odd dimensions rather than silently resizing/cropping a screen.
    command += ["-pix_fmt", "yuv420p" if width % 2 == 0 and height % 2 == 0 else "yuv444p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination)]
    return command


def convert_recording(source: str | Path, destination: str | Path, *, ffmpeg: str | None = None,
                      ffprobe: str | None = None) -> dict[str, Any]:
    source_path = checked_path(source)
    output_path = checked_path(destination, must_exist=False)
    if source_path.suffix.lower() != ".webm":
        raise RecordingError("输入必须是原始连续 WebM 录屏。")
    if output_path.suffix.lower() != ".mp4":
        raise RecordingError("输出文件名必须以 .mp4 结尾。")
    manifest_path = output_path.with_suffix(".recording.json")
    if output_path.exists() or manifest_path.exists():
        raise RecordingError("输出或记录文件已存在；请使用新文件名，工具不会覆盖原件。")
    encoder = find_binary("ffmpeg", ffmpeg)
    inspector = find_binary("ffprobe", ffprobe)
    before = probe(source_path, inspector)
    if before["duration_seconds"] > 300:
        raise RecordingError(f"录屏时长 {before['duration_seconds']:.3f} 秒超过 300 秒；请重新连续录制，工具不会裁剪。")
    if before["audio_tracks"] == 0:
        raise RecordingError("录屏没有音轨，无法完整展示语音交互；请开启录屏音频后重新录制。")
    source_hash = sha256(source_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(output_path.stem + ".partial-" + uuid.uuid4().hex + ".mp4")
    command = conversion_command(encoder, source_path, temporary, before)
    started = time.time()
    try:
        run_command(command)
        after = probe(temporary, inspector)
        if after["duration_seconds"] > 300:
            raise RecordingError("编码后完整录屏超过 300 秒，请重新录制稍短的连续演示；未做裁剪。")
        if after["video_frames"] != before["video_frames"]:
            raise RecordingError("编码前后视频帧数不一致，输出未通过完整性验证。")
        if (after["width"], after["height"]) != (before["width"], before["height"]):
            raise RecordingError("编码前后分辨率不一致。")
        if after["audio_tracks"] != before["audio_tracks"]:
            raise RecordingError("编码前后音轨数量不一致。")
        if abs(after["duration_seconds"] - before["duration_seconds"]) > 0.25:
            raise RecordingError("编码前后总时长差超过 250 毫秒，需检查录屏时间戳。")
        if sha256(source_path) != source_hash:
            raise RecordingError("转换期间原始录屏发生变化，拒绝保存不一致的结果。")
        manifest = {
            "operation": "container_and_codec_conversion_only",
            "source": str(source_path), "source_sha256": source_hash,
            "output": str(output_path), "output_sha256": sha256(temporary),
            "source_preserved": True,
            "command_argv": command,
            "finalization": "atomic rename of validated temporary MP4; no media edits",
            "started_at_unix": started, "completed_at_unix": time.time(),
            "input_probe": before, "output_probe": after,
            "verification": {"identical_video_frame_count": True, "identical_dimensions": True,
                             "identical_audio_track_count": True, "duration_under_300s": True},
            "editing": {"cuts": False, "concatenation": False, "speed_change": False,
                        "interaction_fabrication": False, "frame_rate_conversion": False},
            "scope": "This conversion preserves the complete timeline and original recording. It does not verify interaction authenticity or claim an organizer exemption from the no-editing rule.",
        }
        # Exclusive creation prevents a second run from replacing an existing artifact.
        with manifest_path.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
        try:
            # Windows rename fails if target exists; prevent overwrite on all platforms.
            if output_path.exists():
                raise RecordingError("输出文件在转换期间被创建，拒绝覆盖。")
            temporary.rename(output_path)
        except Exception:
            manifest_path.unlink(missing_ok=True)
            raise
        return manifest
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="原始连续 WebM 录屏")
    parser.add_argument("destination", help="新的 MP4 文件名")
    parser.add_argument("--ffmpeg")
    parser.add_argument("--ffprobe")
    args = parser.parse_args(argv)
    try:
        manifest = convert_recording(args.source, args.destination, ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
        print(json.dumps({"output": manifest["output"], "output_sha256": manifest["output_sha256"],
                          "duration_seconds": manifest["output_probe"]["duration_seconds"],
                          "video_frames": manifest["output_probe"]["video_frames"],
                          "original_recording_preserved": True}, ensure_ascii=False, indent=2))
        return 0
    except (RecordingError, OSError, ValueError) as exc:
        print(f"录屏格式转换失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
