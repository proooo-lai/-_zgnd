"""FFmpeg video assembly with explicit timing and reversible page geometry."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
from pathlib import Path

from .schema import contain_transform, sha256_file, write_json


async def run_process(arguments: list[str], timeout: float = 600) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *[str(x) for x in arguments], stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as error:
        raise RuntimeError(f"找不到可执行程序：{Path(arguments[0]).name}") from error
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        message = stderr.decode("utf-8", errors="replace")[-2500:]
        raise RuntimeError(f"{Path(arguments[0]).name} 执行失败：{message}")
    return stdout


def _ffmpeg_binary() -> str:
    """定位可用的 ffmpeg：优先仓库自带，其次 PATH，最后 imageio 附带的那份。"""
    local = Path(__file__).resolve().parents[1] / "tools" / "bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if local.is_file():
        return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO = re.compile(r"Stream #\d+:\d+.*?: Video: (\w+).*?(\d{2,5})x(\d{2,5}).*?(\d+(?:\.\d+)?) fps")
_AUDIO = re.compile(r"Stream #\d+:\d+.*?: Audio: (\w+)")


async def _probe_with_ffmpeg(path: Path) -> dict:
    """没有 ffprobe 时的退路：解析 ffmpeg -i 的 stderr，输出同样结构的探针结果。"""
    process = await asyncio.create_subprocess_exec(
        _ffmpeg_binary(), "-hide_banner", "-i", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, raw_err = await process.communicate()
    text = raw_err.decode("utf-8", "replace")
    duration = 0.0
    match = _DURATION.search(text)
    if match:
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    streams: list[dict] = []
    video = _VIDEO.search(text)
    if video:
        streams.append({"codec_type": "video", "codec_name": video.group(1),
                        "width": int(video.group(2)), "height": int(video.group(3)),
                        "avg_frame_rate": f"{video.group(4)}/1", "duration": str(duration)})
    audio = _AUDIO.search(text)
    if audio:
        streams.append({"codec_type": "audio", "codec_name": audio.group(1), "duration": str(duration)})
    if not streams and "No such file" in text:
        raise ValueError("无法读取媒体文件")
    return {"format": {"duration": str(duration)}, "streams": streams}


async def probe_media(path: Path, ffprobe: str) -> dict:
    if ffprobe and Path(ffprobe).is_file():
        output = await run_process([ffprobe, "-v", "error", "-show_format", "-show_streams",
                                    "-of", "json", str(path)], timeout=60)
        return json.loads(output)
    return await _probe_with_ffmpeg(path)


def media_duration(probe: dict) -> float:
    durations = [probe.get("format", {}).get("duration", 0)]
    durations += [stream.get("duration", 0) for stream in probe.get("streams", [])]
    valid = []
    for value in durations:
        try:
            duration = float(value)
            if math.isfinite(duration) and duration > 0:
                valid.append(duration)
        except (TypeError, ValueError):
            pass
    return max(valid, default=0)


def allocate_durations(weights: list[float], audio_durations: list[float],
                       target: float = 120, fps: int = 24,
                       minimum: float = 100, maximum: float = 180) -> list[float]:
    if not weights or len(weights) != len(audio_durations):
        raise ValueError("每个镜头必须有权重及配音时长。")
    if fps < 1 or not 0 < minimum <= target <= maximum:
        raise ValueError("视频时间范围无效。")
    minima = [max(1.0, value + 0.35 if value > 0 else 1.0) for value in audio_durations]
    required = sum(minima)
    total = max(target, required)
    if total > maximum - len(weights) / fps:
        raise ValueError("配音和逐页展示所需时长超过 180 秒；请缩短旁白后重新生成，不能截断配音。")
    weight_sum = sum(max(0.1, w) for w in weights)
    spare = max(0, total - required)
    frames = [math.ceil((floor + spare * max(0.1, w) / weight_sum) * fps)
              for floor, w in zip(minima, weights)]
    if sum(frames) / fps > maximum:
        raise ValueError("帧率取整后时长超过上限。")
    return [f / fps for f in frames]


def check_video_metadata(probe: dict, minimum: float = 100, maximum: float = 180) -> dict:
    video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("生成结果没有视频轨。")
    try:
        numerator, denominator = video.get("avg_frame_rate", "0/1").split("/")
        fps = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        fps = 0
    duration = media_duration(probe)
    if duration < minimum - 0.05 or duration > maximum + 0.05:
        raise ValueError(f"成片时长 {duration:.3f} 秒不在 {minimum}–{maximum} 秒范围内。")
    if video.get("width", 0) < 1280 or video.get("height", 0) < 720 or fps < 23.99:
        raise ValueError("成片必须至少 1280×720 且帧率不低于 24fps。")
    return {"duration": duration, "width": video["width"], "height": video["height"],
            "fps": fps, "video_codec": video.get("codec_name"),
            "audio_tracks": sum(s.get("codec_type") == "audio" for s in probe.get("streams", []))}


async def render_segment(image: Path, output: Path, duration: float, settings,
                         image_width: int, image_height: int,
                         audio: Path | None = None, clip: Path | None = None) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    width, height, fps = settings.width, settings.height, settings.fps
    transform = contain_transform(image_width, image_height, width, height)
    if clip:
        clip_probe = await probe_media(clip, settings.ffprobe)
        stream = next(s for s in clip_probe["streams"] if s.get("codec_type") == "video")
        transform = contain_transform(stream["width"], stream["height"], width, height)
        # Generated pixels have their own geometry; PDF boxes must never be used directly.
        transform["kind"] = "generated_frame"
        transform["requires_frame_grounding"] = True
    left, top, cw, ch = transform["content_rect"]
    arguments = [settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if clip:
        arguments += ["-i", str(clip)]
    else:
        # 本地生成：单帧输入交给 zoompan 扩成整段动画帧。
        arguments += ["-i", str(image)]
    if audio:
        arguments += ["-i", str(audio)]
    else:
        arguments += ["-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono"]
    if clip:
        filters = (f"scale={cw}:{ch}:flags=lanczos,pad={width}:{height}:{left}:{top}:color=0xf4f1e9,"
                   f"setsar=1,fps={fps},format=yuv420p")
        filters += f",tpad=stop_mode=clone:stop_duration={duration:.6f}"
    else:
        # 本地生成：静态页做缓慢推拉（Ken Burns），让成片是"视频"而不是幻灯片。
        frames = max(2, int(round(duration * fps)))
        filters = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0xf4f1e9,"
                   f"zoompan=z='min(1+0.06*on/{frames},1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                   f":d={frames}:s={width}x{height}:fps={fps},"
                   f"setsar=1,format=yuv420p")
    arguments += ["-map", "0:v:0", "-map", "1:a:0", "-vf", filters,
                  "-af", "apad", "-t", f"{duration:.6f}", "-c:v", "libx264",
                  "-preset", "veryfast", "-crf", "20", "-threads", "2",
                  "-c:a", "aac", "-ar", "24000", "-ac", "1", "-b:a", "96k",
                  "-movflags", "+faststart", str(output)]
    await run_process(arguments, timeout=max(180, duration * 12))
    return transform


async def render_source_motion(image: Path, output: Path, duration: float, settings) -> dict:
    """Explicit local fallback: move the intact page inside a safe canvas.

    This never claims to animate characters or to use a generative video model.
    Unlike a zoom into the page, the bounded motion cannot crop source content.
    Actual-frame grounding remains mandatory because its placement varies.
    """
    from PIL import Image
    if not math.isfinite(duration) or duration <= 0 or output.exists() or image.resolve()==output.resolve():
        raise ValueError('保真动效必须使用新输出路径和有效时长。')
    with Image.open(image) as source:
        iw,ih=source.size
    width,height,fps=settings.width,settings.height,settings.fps
    border=16
    geometry=contain_transform(iw,ih,width-2*border,height-2*border)
    _,_,cw,ch=geometry['content_rect']
    left,top=(width-cw)//2,(height-ch)//2
    amplitude=min(6,left-border/2,top-border/2)
    if amplitude<0:
        raise ValueError('保真动效画布没有安全边距。')
    frames=max(1,round(duration*fps));actual_duration=frames/fps
    filters=(f'[0:v]scale={cw}:{ch}:flags=lanczos,setsar=1[page];'
        f'color=c=0xf4f1e9:s={width}x{height}:r={fps}:d={actual_duration:.9f}[canvas];'
        f"[canvas][page]overlay=x='{left}+{amplitude}*sin(2*PI*t/{actual_duration:.9f})':"
        f"y='{top}+{amplitude}*sin(2*PI*t/{actual_duration:.9f})':shortest=1,format=yuv420p[v]")
    output.parent.mkdir(parents=True,exist_ok=True)
    audit={'version':'source-preserved-motion-v1','status':'rendering','source_sha256':sha256_file(image),
        'source_size':[iw,ih],'canvas_size':[width,height],'source_rect':[left,top,cw,ch],
        'translation_amplitude_px':amplitude,'duration':actual_duration,'fps':fps,
        'requires_frame_grounding':True,'generative_video_used':False,
        'rendering':'source_preserved_motion','reason':'Explicit fallback preserves all source objects and text.'}
    sidecar=output.with_suffix('.motion.json');write_json(sidecar,audit)
    try:
        await run_process([settings.ffmpeg,'-y','-hide_banner','-loglevel','error','-loop','1','-framerate',str(fps),
            '-i',str(image),'-filter_complex_threads','1','-filter_complex',filters,'-map','[v]',
            '-an','-frames:v',str(frames),'-c:v','libx264','-preset','veryfast','-crf','18',
            '-threads','2','-movflags','+faststart',str(output)],timeout=max(180,duration*12))
        if sha256_file(image)!=audit['source_sha256']:
            raise ValueError('渲染期间原图已变化。')
        audit.update(status='completed',output_sha256=sha256_file(output))
    except BaseException:
        audit['status']='failed';output.unlink(missing_ok=True);write_json(sidecar,audit);raise
    write_json(sidecar,audit)
    return audit


async def concatenate_segments(segments: list[Path], output: Path, settings) -> dict:
    if not segments:
        raise ValueError("没有可拼接的镜头。")
    manifest = output.parent / "concat.txt"
    def escaped(path: Path) -> str:
        return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
    manifest.write_text("\n".join(f"file '{escaped(path)}'" for path in segments) + "\n", encoding="utf-8")
    # Preserve video PTS=0 while re-encoding AAC once. Stream-copying AAC would
    # shift the first visible frame by encoder priming (~43 ms at 24 kHz),
    # breaking exact page/scene lookup near every boundary.
    await run_process([settings.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-copyts",
                       "-f", "concat", "-safe", "0", "-i", str(manifest),
                       "-c:v", "copy", "-c:a", "aac", "-ar", "24000", "-ac", "1", "-b:a", "96k",
                       "-af", "aresample=async=1:first_pts=0", "-avoid_negative_ts", "disabled",
                       "-movflags", "+faststart", str(output)])
    return await probe_media(output, settings.ffprobe)
