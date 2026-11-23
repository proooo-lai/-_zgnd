"""Reusable PDF → OCR/VLM → story → speech/video → review agent pipeline.

The only input for book understanding is the uploaded PDF. Query scripts,
expected answers and test folders are never opened by this module.
"""
from __future__ import annotations

import asyncio
import inspect
import hashlib
import json
import math
import re
import shutil
import time
import uuid
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pymupdf
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .providers import ProviderError
from .fidelity import protect_video_details, PROTECTION_VERSION, ProtectionPlanningError

from .safety import UNTRUSTED_CONTENT_RULE, review_document
from .schema import (SCHEMA_VERSION, rect_bbox, sanitize_objects, sanitize_words,
                     sha256_file, write_json)
from .video import (allocate_durations, check_video_metadata, concatenate_segments,
                    media_duration, probe_media, render_segment, run_process)


def prepare_video_reference(source: Path, output: Path, width: int, height: int) -> dict:
    """Fit the whole source page on the exact video canvas before cloud upload.

    Seedance center-crops references when their aspect ratio differs from the
    requested output. Padding first protects border text and corner objects;
    this transform describes the reference only, never generated-frame boxes.
    """
    if width < 1 or height < 1:
        raise ValueError("视频参考图尺寸必须为正整数。")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as original:
        image = ImageOps.exif_transpose(original).convert("RGBA")
        source_size = image.size
        contained = ImageOps.contain(image, (width, height), method=Image.Resampling.LANCZOS)
        left, top = (width - contained.width) // 2, (height - contained.height) // 2
        canvas = Image.new("RGB", (width, height), (244, 241, 233))
        canvas.paste(contained, (left, top), contained.getchannel("A"))
        canvas.save(output, format="PNG")
    return {"kind": "reference_contain_pad", "source_size": list(source_size),
            "canvas_size": [width, height],
            "content_rect": [left, top, contained.width, contained.height],
            "background": "#f4f1e9", "not_valid_for_generated_frame_grounding": True}


async def synthesize_narration(provider, text: str, language: str, output: Path) -> Path:
    """Prefer DashScope's streamed WAV assembly without retrying a paid call."""
    stream = getattr(provider, "tts_stream", None)
    mode = getattr(provider, "mode", None)
    if callable(stream) and callable(mode) and mode("tts") == "dashscope":
        return Path(await stream(text, language, output, on_pcm=None))
    return Path(await provider.tts(text, language, output))


def validate_video_quality(result: dict) -> dict:
    """Do not coerce a missing flag or a string 'false' into a passing result."""
    if (not isinstance(result, dict) or type(result.get("consistent")) is not bool
            or type(result.get("uncertain")) is not bool or not isinstance(result.get("issues"), list)):
        raise ValueError("视频质量 Agent 返回格式无效，不能确认画面是否忠于原书。")
    issues = []
    for issue in result["issues"]:
        if (not isinstance(issue, dict) or issue.get("severity") not in ("major", "minor")
                or not isinstance(issue.get("description"), str) or not issue["description"].strip()):
            raise ValueError("视频质量 Agent 的问题列表格式无效，不能确认画面是否忠于原书。")
        issues.append({"severity": issue["severity"], "description": issue["description"].strip()})
    rejected = not result["consistent"] or any(issue["severity"] == "major" for issue in issues)
    decision = "rejected" if rejected else ("needs_review" if result["uncertain"] or issues else "sampled_consistent")
    return {"consistent": result["consistent"], "uncertain": result["uncertain"], "issues": issues,
            "decision": decision, "scope": "three_sampled_frames_requires_human_full_video_review"}


async def inspect_video_quality(source: Path, clip: Path, duration: float, provider, settings,
                                book_dir: Path, scene_index: int, *, protection: dict | None = None) -> dict:
    """Compare original art with real generated frames, without regeneration."""
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("原始生成片段时长无效，无法进行质量核对。")
    agent_dir = book_dir / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    protected = bool(protection and protection.get('status') == 'composited')
    stem = f"video-quality-{scene_index + 1:04}" + ('-protected' if protected else '')
    sample_dir = agent_dir / stem
    sample_dir.mkdir(exist_ok=True)
    tail = max(0.0, duration - max(0.08, 1 / max(1, settings.fps)))
    seconds = [min(0.5, tail), min(duration / 2, tail), tail]
    images = []
    for index, timestamp in enumerate(seconds):
        sample = sample_dir / f"frame-{index + 1}.png"
        await run_process([settings.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.6f}",
                           "-i", str(clip), "-frames:v", "1", str(sample)], timeout=45)
        if not sample.is_file() or sample.stat().st_size == 0:
            raise ValueError("质量核对未能提取实际视频帧，已停止处理。")
        images.append(sample)
    prompt = (UNTRUSTED_CONTENT_RULE + "\n你是绘本视频质量核对 Agent。第一张图是原始绘本页，后面三张是实际生成视频的采样帧，"
              f"依次位于{seconds[0]:.3f}秒、{seconds[1]:.3f}秒、{seconds[2]:.3f}秒。"
              "请逐帧与第一张原图比较，而非只比较三张视频帧是否彼此相似。"
              "只核对这些变化：凭空新增或缺失独立语义对象；角色或物品数量改变；角色外形、颜色或身份重大改变；原画面文字被篡改。"
              "特别核对原本空白的背景、树冠、桌面等区域是否长出了原图没有的物品，不能因为符合常识就忽略。"
              "眨眼、呼吸、轻微肢体动作、合理透视与短暂遮挡不是对象增删，不要误判。排版补边留白和整体等比例缩放不算问题。"
              "若文字模糊无法核对、对象被遮挡无法确定数量，应uncertain=true并说明，不能编造差异。"
              "重大新增/缺失、数量变化、身份重大变化或确证的文字篡改为major；轻微不影响语义的偏差为minor。"
              "任何major应consistent=false；没有确证差异但无法判断时consistent=true且uncertain=true。"
              '只返回JSON {"consistent":true,"issues":[{"severity":"major或minor","description":"具体原图与哪一采样帧的差异"}],"uncertain":false}。'
              "无问题时issues=[]。不要把采样通过表述为所有帧已验证。")
    if protected:
        prompt += ('\n本视频叠加了有限原图文字/小物件保护区域；这不证明视频已经正确。'
            '额外逐帧核对保护区与运动画面是否对齐，是否出现错位、重影、重复对象、裁断或不合理遮挡。'
            '这些问题影响语义或明显破坏画面时也是major，必须consistent=false。'
            '另返回protection_aligned布尔字段；只有能确认三帧保护区域及边界均对齐才为true，'
            '无法确认也必须false，不可因原文来自原图便默认通过。'
            '\n保护区域参考坐标与变换：'+json.dumps({k:protection[k] for k in
                ('regions','reference_transform','video_size','mapping') if k in protection},ensure_ascii=False))
    (agent_dir / f"{stem}.prompt.txt").write_text(prompt, encoding="utf-8")
    try:
        raw = await provider.vision(prompt, [source, *images])
    except Exception as error:
        # Quality is a release gate. If the verifier is unavailable, the clip
        # cannot be marked consistent and no paid video retry is attempted.
        failure = {"consistent": False, "uncertain": True,
                   "issues": [{"severity": "major", "description": "质量核对模型调用失败，无法确认生成画面与原图一致。"}],
                   "decision": "rejected", "sample_seconds": seconds,
                   "sample_paths": [str(image.relative_to(book_dir)).replace("\\", "/") for image in images],
                   "source_sha256": sha256_file(source), "clip_sha256": sha256_file(clip),
                   "error_type": type(error).__name__}
        write_json(agent_dir / f"{stem}.json", failure)
        raise ValueError("视频质量核对模型调用失败，已拒绝该片段；原片和核对帧已保留，未自动重试视频生成。") from None
    write_json(agent_dir / f"{stem}.json", {"model_result": raw, "sample_seconds": seconds,
               "source_sha256": sha256_file(source), "clip_sha256": sha256_file(clip)})
    quality = validate_video_quality(raw)
    if protected:
        quality['protection_aligned'] = raw.get('protection_aligned') is True
        quality['postprocessing_version'] = protection.get('version')
        if not quality['protection_aligned']:
            quality.update(consistent=False,decision='rejected')
            quality['issues'].append({'severity':'major','description':'无法确认原图保护区域与生成画面对齐，拒绝合成片段。'})
    quality.update(sample_seconds=seconds,
                   sample_paths=[str(image.relative_to(book_dir)).replace("\\", "/") for image in images],
                   source_sha256=sha256_file(source), clip_sha256=sha256_file(clip))
    write_json(agent_dir / f"{stem}.json", quality)
    return quality


def require_video_quality(quality: dict, page_number: int):
    if quality["decision"] == "rejected":
        raise ValueError(f"第 {page_number} 页视频与原绘本不一致，质量检查已拒绝该片段；原片和核对结果已保留，未自动重试视频生成。")


async def review_clip_with_protection(source: Path, reference: Path, clip: Path, page: dict,
                                     transform: dict, provider, settings, book_dir: Path, index: int,
                                     *, duration: float, encode_lock=None):
    """Inspect raw output first; make at most one local, separately gated repair.

    Never changes passing footage, never resubmits a paid generation, and never
    promotes a refused/failed repair to success. Each inspection has its own files.
    """
    raw_quality = await inspect_video_quality(source,clip,duration,provider,settings,book_dir,index)
    audit = {'raw_quality':raw_quality,'quality':raw_quality,
             'detail_protection':{'version':PROTECTION_VERSION,'status':'not_required'}}
    if raw_quality['decision'] == 'rejected':
        output = book_dir/'raw_clips'/f'protected-{index+1:04}.mp4'
        try:
            async with (encode_lock or asyncio.Semaphore(1)):
                plan = await protect_video_details(reference,clip,output,page,transform,settings)
        except ProtectionPlanningError as error:
            plan = {'version':PROTECTION_VERSION,'status':'refused','reason':str(error)}
        audit['detail_protection'] = plan
        if plan['status'] == 'composited':
            clip = output
            plan['output_path'] = output.relative_to(book_dir).as_posix()
            audit['quality'] = await inspect_video_quality(source,clip,duration,provider,settings,
                                                           book_dir,index,protection=plan)
    (book_dir/'agents').mkdir(parents=True,exist_ok=True)
    write_json(book_dir/'agents'/f'detail-review-{index+1:04}.json',audit)
    return clip,audit


def assert_allowed_input(path: Path) -> Path:
    # Check the lexical path first: no stat/read/resolve of a forbidden directory.
    if any(part.casefold() in {"测试集", "test_set", "hidden_test", "hidden_tests"}
           for part in str(path).replace("\\", "/").split("/")):
        raise ValueError("该目录被标记为隔离测试集，禁止访问。")
    resolved = path.resolve()
    if any(part.casefold() in {"测试集", "test_set", "hidden_test", "hidden_tests"}
           for part in resolved.parts):
        raise ValueError("该路径指向隔离测试集，禁止访问。")
    return resolved


def extract_pdf(pdf_path: Path, book_dir: Path, max_pages: int = 100) -> tuple[list[dict], dict]:
    pdf_path = assert_allowed_input(pdf_path)
    images_dir = book_dir / "pages"
    images_dir.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(pdf_path) as document:
        if document.is_encrypted:
            raise ValueError("请提供未加密的绘本 PDF。")
        if not 1 <= len(document) <= max_pages:
            raise ValueError(f"绘本页数应在 1 到 {max_pages} 页之间。")
        pages = []
        for index, page in enumerate(document):
            number = index + 1
            # Rotation is baked into the image, so normalize coordinates against
            # the rotated page rectangle after applying rotation_matrix.
            width, height = page.rect.width, page.rect.height
            if width <= 0 or height <= 0:
                raise ValueError(f"第 {number} 页尺寸无效。")
            scale = min(2.0, 1800 / max(width, height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False,
                                    colorspace=pymupdf.csRGB, annots=False)
            image_path = images_dir / f"{number:04}.png"
            pixmap.save(image_path)
            words = []
            for word in page.get_text("words", sort=True):
                rect = pymupdf.Rect(word[:4]) * page.rotation_matrix
                bbox = rect_bbox(rect, width, height)
                if bbox and word[4].strip():
                    words.append({"text": word[4], "bbox": bbox, "source": "pdf_text_layer"})
            annotations = []
            for annotation in page.annots() or []:
                label = (annotation.info.get("content") or annotation.info.get("subject") or "").strip()
                if not label:
                    continue
                bbox = rect_bbox(annotation.rect * page.rotation_matrix, width, height)
                if bbox:
                    annotations.append({"label": label[:120], "bbox": bbox,
                                        "description": label, "confidence": 1.0})
            native_text = page.get_text("text", sort=True).strip()
            pages.append({"number": number, "image_url": f"/media/{book_dir.name}/pages/{number:04}.png",
                          "image_width": pixmap.width, "image_height": pixmap.height,
                          "native_text": native_text, "text": native_text,
                          "description": "", "kind": "story", "words": words,
                          "objects": sanitize_objects(annotations, number, "pdf_annotation"),
                          "understanding": "text_layer_only", "uncertainties": []})
        metadata = {"pdf_title": str(document.metadata.get("title") or ""),
                    "page_count": len(document), "source_sha256": sha256_file(pdf_path)}
    return pages, metadata


def detect_language(pages: list[dict]) -> str:
    text = " ".join(str(page.get("text", "")) for page in pages)
    return "zh" if len(re.findall(r"[\u4e00-\u9fff]", text)) > 3 else "en"


VISION_CACHE_VERSION = "vision-cache-v2"


class VisualPageResult(BaseModel):
    model_config = ConfigDict(strict=True)
    text: str
    description: str
    kind: Literal["cover", "copyright", "story", "transition"] = "story"
    title: str = ""
    objects: list[dict] = Field(default_factory=list)
    words: list[dict] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)

    @field_validator("title", mode="before")
    @classmethod
    def optional_title(cls, value):
        # A missing optional book title carries no story fact. Keep all other
        # fields and non-null title values under the existing strict schema.
        return "" if value is None else value


def provider_identity(provider, settings, name):
    """Allowlisted identity; never retain keys, headers or account credentials."""
    config = (provider.config(name) if callable(getattr(provider, "config", None)) else
              getattr(getattr(provider, "settings", settings), "providers", {}).get(name, {}))
    return {"implementation": type(provider).__module__ + "." + type(provider).__qualname__,
            **{key: config.get(key, "") for key in ("kind", "base_url", "model", "deployment", "version")},
            "completion_contract": {"version": "json-completion-v1", "temperature": 0.15,
                                    "max_tokens": 7000, "parse_retry_max_tokens": 8192}}


async def _vision_cached(provider, prompt: str, image: Path, settings, *, cache_only=False) -> dict | None:
    identity = {"cache_version": VISION_CACHE_VERSION, "schema_version": SCHEMA_VERSION,
                "provider": provider_identity(provider, settings, "vision"),
                "image_sha256": sha256_file(image),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    cache = Path(settings.data_dir) / ".vision-cache" / f"{key}.json"
    if cache.is_file():
        try:
            entry = json.loads(cache.read_text(encoding="utf-8"))
            if (entry["identity"] == identity and isinstance(entry.get("created_at"), str)
                    and entry.get("validation") == {"status": "schema_validated", "schema_version": SCHEMA_VERSION}):
                result = VisualPageResult.model_validate(entry["result"]).model_dump()
                return {**result, "_provenance": {**identity, "cache_key": key, "execution": "cache_hit",
                        "created_at": entry["created_at"], "validation": entry["validation"]}}
        except (ValueError, OSError, KeyError, TypeError):
            pass
    if cache_only:
        return None
    for attempt in range(3):
        try:
            result = VisualPageResult.model_validate(await provider.vision(prompt, [image])).model_dump()
            break
        except ProviderError as error:
            transient = (error.code in {"Throttling", "Throttling.RateQuota", "RateLimitExceeded", "rate_limit_exceeded"}
                         or (error.status_code in {408, 429, 502, 503, 504} and not error.code))
            # Connection/ambiguous POST failures may already be billed. Do not
            # resubmit them without an explicit transient rejection response.
            if not transient or attempt == 2 or error.submission_state == "submission_unconfirmed":
                raise
            await asyncio.sleep(min(8.0, 2.0 ** (attempt + 1)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    audit = {"created_at": datetime.now(timezone.utc).isoformat(),
             "validation": {"status": "schema_validated", "schema_version": SCHEMA_VERSION}}
    temporary = cache.with_suffix("." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps({"identity": identity, "result": result, **audit}, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(cache)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "_provenance": {**identity, "cache_key": key, "execution": "live", **audit}}


def _vision_prompt(page: dict) -> str:
    shape = {"text": "本页故事原文，保持原语言，排除水印和版权信息",
             "description": "画面事实，包含动作、关系、颜色、数量和环境",
             "kind": "cover|copyright|story|transition", "title": "仅封面填写书名",
             "objects": [{"label": "对象原语言名称", "label_zh": "中文名称", "label_en": "English name",
                          "bbox": [0.1, 0.2, 0.3, 0.4], "description": "可见特征",
                          "confidence": 0.9}],
             "words": [{"text": "原图内的单词或中文词语，不是另加字幕", "bbox": [0.1, 0.2, 0.1, 0.03]}],
             "uncertainties": ["看不清的文字或无法确认的内容"]}
    return (UNTRUSTED_CONTENT_RULE + "\n你是绘本 OCR 与视觉结构分析 Agent。只返回 JSON。"
            "逐字识别原图正文；文本层只供参考，它可能只有水印，不能代替看图。"
            "提取所有有意义的人物、动物、物品，分开标注重复对象。"
            "每个 bbox 均为原图归一化 [x左,y上,width宽,height高]，每个值在0到1之间，不能写成右下坐标。"
            "objects 只包含实际画面中可见对象，不能因为文字提及就虚构对象。"
            "words 只标记原画面中的实际文字，以单词或中文词语为粒度；不要给水印框。"
            "若整页纯版权说明则 kind=copyright；无字但推进情节的页面仍为story。"
            f"\nPDF页码={page['number']}\n目标结构={json.dumps(shape, ensure_ascii=False)}"
            f"\n<untrusted_pdf_text>{page['native_text'][:18000]}</untrusted_pdf_text>")


def merge_visual_page(page: dict, result: dict) -> dict:
    if not isinstance(result, dict) or not isinstance(result.get("text"), str):
        raise ValueError(f"第 {page['number']} 页的视觉输出缺少 text 字段。")
    if not isinstance(result.get("description"), str):
        raise ValueError(f"第 {page['number']} 页的视觉输出缺少 description 字段。")
    merged = dict(page)
    merged.update(text=result["text"].strip(), description=result["description"].strip(),
                  understanding="vision", uncertainties=result.get("uncertainties", []))
    merged["vision_provenance"] = result.get("_provenance", {})
    merged["title"] = str(result.get("title", ""))[:200]
    kind = result.get("kind", "story")
    merged["kind"] = kind if kind in {"cover", "copyright", "story", "transition"} else "story"
    objects = sanitize_objects(result.get("objects", []), page["number"], "vision")
    # Preserve official PDF annotations without interpreting their contents as commands.
    for annotation in page["objects"]:
        if not any(annotation["label"] == obj["label"] and annotation["bbox"] == obj["bbox"] for obj in objects):
            item = dict(annotation)
            item["id"] = f"p{page['number']}-o{len(objects) + 1}"
            objects.append(item)
    merged["objects"] = objects
    visual_words = sanitize_words(result.get("words", []))
    merged['visual_words'] = [{**word, 'source':'vision'} for word in visual_words]
    # Use exact PDF geometry only for words confirmed to belong to story text.
    def comparable(text):
        # Match formatting variants only; never overwrite original OCR text.
        text = unicodedata.normalize('NFKC',text).translate(str.maketrans({'’':"'",'‘':"'",'“':'"','”':'"'}))
        return re.sub(r'\s+','',text).casefold()
    story_text = comparable(merged['text'])
    confirmed = [word for word in page["words"] if comparable(word['text']) and comparable(word['text']) in story_text]
    # PDF geometry is authoritative for its own words, not for handwritten or
    # raster-only story text. Preserve both sources without replacing raw OCR.
    combined = [dict(word) for word in confirmed]
    for word in visual_words:
        if confirmed and comparable(word['text']) not in story_text:
            continue
        x,y,w,h = word['bbox']
        already_covered = any(comparable(word['text']) in comparable(native['text'])
            and native['bbox'][0] <= x+w/2 <= native['bbox'][0]+native['bbox'][2]
            and native['bbox'][1] <= y+h/2 <= native['bbox'][1]+native['bbox'][3] for native in confirmed)
        if not already_covered:
            combined.append({**word,'source':'vision'})
    merged["words"] = combined
    return merged


def _story_prompt(pages: list[dict], language: str, target: float, age_band: str) -> str:
    evidence = [{"page_number": p["number"], "kind": p["kind"], "text": p["text"],
                 "description": p["description"], "objects": [{"label": o["label"],
                    "description": o["description"]} for o in p["objects"]]} for p in pages]
    # Leave time for pauses, visual reading and different speech providers.
    budget = max(60, int(target * (2.4 if language == "zh" else 1.65)))
    return (UNTRUSTED_CONTENT_RULE + "\n你是整书叙事 Agent。只输出JSON。"
            "使用整本绘本统一角色称呼和故事顺序，只改写已有事件，不添加角色、因果或结局。"
            "每个给定页必须出现一次且顺序不变，每页一个scene；包括无字但推进情节的页面。"
            f"旁白语言={language}，年龄段={age_band}，全片目标={target}秒；"
            f"所有旁白合计严格不超过{budget}{'个汉字' if language == 'zh' else '个英文单词'}。"
            "不必朗读版权文字。保持原画笔触、配色、人物服装、外形与场景。"
            "video_prompt 应描述本页小幅自然运动，严禁改变原有文字或添加不在原图中的对象。"
            "scene 结构是 {page_number:整数,narration:旁白,video_prompt:动画指令}。"
            "顶层结构 {title:书名,summary:整书摘要,character_bible:角色外形和名称一致性规则,scenes:[scene]}。"
            f"\n<untrusted_book>{json.dumps(evidence, ensure_ascii=False)}</untrusted_book>")


def validate_story(result: dict, pages: list[dict]) -> list[dict]:
    scenes = result.get("scenes") if isinstance(result, dict) else None
    expected = [page["number"] for page in pages]
    if not isinstance(scenes, list) or len(scenes) != len(expected):
        raise ValueError("故事 Agent 没有逐页覆盖正文，请重试；系统不会省略缺失页面。")
    cleaned = []
    for page_number, scene in zip(expected, scenes):
        if not isinstance(scene, dict) or scene.get("page_number") != page_number:
            raise ValueError("故事 Agent 的页码覆盖或顺序错误。")
        narration = scene.get("narration")
        if not isinstance(narration, str) or not narration.strip():
            raise ValueError(f"第 {page_number} 页缺少有效旁白。")
        cleaned.append({"page_number": page_number, "narration": narration.strip(),
                        "video_prompt": str(scene.get("video_prompt", "保持原图风格和角色，仅添加自然微小动作。"))})
    return cleaned


async def build_book(pdf_path: Path, book_dir: Path, provider, settings,
                     language: str = "auto", mode: str = "local", reviewer: str = "",
                     age_band: str = "6-8", progress=None) -> dict:
    if mode not in {"local", "cloud"}:
        raise ValueError("生成模式应为 local 或 cloud。")
    if language not in {"auto", "zh", "en"}:
        raise ValueError("语言应为 auto、zh 或 en。")
    if settings.width < 1280 or settings.height < 720 or settings.fps < 24:
        raise ValueError("配置必须至少 1280×720、24fps。")
    if not 100 <= settings.target_duration <= 180:
        raise ValueError("目标视频时长必须在 100–180 秒内。")
    pdf_path = assert_allowed_input(Path(pdf_path))
    book_dir = Path(book_dir)
    book_dir.mkdir(parents=True, exist_ok=True)
    if (book_dir / "book.json").exists():
        raise ValueError("该生成目录已有完成作品；新生成必须使用新的运行目录。")
    started = time.perf_counter()
    run_id = uuid.uuid4().hex
    events, warnings = [], []
    async def report(value: float, message: str, agent: str, **details):
        event = {"agent": agent, "message": message, "elapsed_s": round(time.perf_counter() - started, 3), **details}
        events.append(event)
        write_json(book_dir / "agents" / "events.json", events)
        if progress:
            callback = progress(value, message, event)
            if inspect.isawaitable(callback):
                await callback
    if mode == "cloud":
        missing = [name for name in ("vision", "chat", "tts", "video") if not provider.available(name)]
        if missing:
            raise ValueError("正式生成缺少服务配置：" + ", ".join(missing))
    else:
        warnings.append("本地模式使用原页图排版，不是生成式动画，不能作为已满足正式视频生成要求的证明。")
    source = book_dir / "input.pdf"
    if pdf_path != source.resolve():
        await asyncio.to_thread(shutil.copy2, pdf_path, source)
    await report(0.03, "读取 PDF 并逐页提取原图、文字和批注", "OCR")
    pages, source_info = await asyncio.to_thread(extract_pdf, source, book_dir, settings.max_pages)
    write_json(book_dir / "agents" / "ocr.json", pages)
    semaphore = asyncio.Semaphore(max(1, min(20, settings.concurrency)))
    complete = 0
    async def understand(page: dict) -> dict:
        nonlocal complete
        async with semaphore:
            prompt = _vision_prompt(page)
            (book_dir / "agents" / f"vision-{page['number']:04}.prompt.txt").write_text(prompt, encoding="utf-8")
            image = book_dir / "pages" / f"{page['number']:04}.png"
            result = await _vision_cached(provider, prompt, image, settings)
            write_json(book_dir / "agents" / f"vision-{page['number']:04}.json", result)
            enriched = merge_visual_page(page, result)
            complete += 1
            await report(0.08 + 0.27 * complete / len(pages), f"图文理解 {complete}/{len(pages)} 页", "VLM", page=page["number"],
                         execution=result["_provenance"]["execution"], cache_key=result["_provenance"]["cache_key"])
            return enriched
    if provider.available("vision"):
        understanding_tasks = [asyncio.create_task(understand(page)) for page in pages]
        try:
            pages = await asyncio.gather(*understanding_tasks)
        except BaseException:
            # Join siblings before the caller records a terminal run status.
            # Already completed cache entries remain available for a later run.
            for task in understanding_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*understanding_tasks, return_exceptions=True)
            raise
    else:
        warnings.append("未配置视觉模型：仅保留 PDF 文本层和批注；扫描页、水印页、物体理解与触控识别尚不可用。")
        for page in pages:
            page["uncertainties"] = ["未进行图像理解；文本层可能不完整或仅含水印。"]
        await report(0.35, "未配置视觉模型，已保留原页图与文本层供本地演示", "VLM", status="unavailable")
    selected = [page for page in pages if page["kind"] != "copyright"]
    if not selected:
        raise ValueError("绘本没有可生成的正文、封面或过渡页。")
    actual_language = detect_language(pages) if language == "auto" else language
    title = next((page.get("title") for page in pages if page.get("title")), "") or source_info["pdf_title"] or pdf_path.stem
    await report(0.38, "建立整书角色与叙事结构", "Story")
    if provider.available("chat"):
        prompt = _story_prompt(selected, actual_language, settings.target_duration, age_band)
        (book_dir / "agents" / "story.prompt.txt").write_text(prompt, encoding="utf-8")
        story = await provider.chat(prompt)
        write_json(book_dir / "agents" / "story.json", story)
        scenes = validate_story(story, selected)
        title = str(story.get("title") or title)[:200]
    else:
        story = {"summary": "", "character_bible": "保留原页图中的角色及画面。"}
        scenes = [{"page_number": page["number"], "narration": page["text"] or page["description"],
                   "video_prompt": ""} for page in selected]
        warnings.append("未配置叙事模型：旁白直接使用已提取原文，未执行整书生成式改写。")
    review = review_document([p["text"] + "\n" + p["description"] for p in pages]
                             + [s["narration"] for s in scenes], reviewer, age_band)
    write_json(book_dir / "agents" / "safety.json", review)
    if review["status"] == "blocked":
        raise ValueError("内容初筛发现高风险项，已停止生成；请由内容安全负责人检查来源。")
    audio_dir = book_dir / "audio"
    audio_dir.mkdir(exist_ok=True)
    audio_paths: list[Path | None] = [None] * len(scenes)
    audio_lengths = [0.0] * len(scenes)
    speech_complete = 0
    async def speak(index: int, scene: dict):
        nonlocal speech_complete
        async with semaphore:
            if not scene["narration"].strip():
                speech_complete += 1
                await report(0.40 + 0.12 * speech_complete / len(scenes),
                             f"旁白处理 {speech_complete}/{len(scenes)} 页（本页无可确认旁白）", "Speech", page=scene["page_number"])
                return
            output = await synthesize_narration(provider, scene["narration"], actual_language, audio_dir / f"{index + 1:04}.wav")
            output = Path(output)
            if not output.is_file() or output.stat().st_size == 0:
                raise ValueError(f"第 {scene['page_number']} 页 TTS 没有返回音频。")
            audio_paths[index] = output
            audio_lengths[index] = media_duration(await probe_media(output, settings.ffprobe))
            if audio_lengths[index] <= 0:
                raise ValueError("无法读取 TTS 时长，已停止生成以避免截断音频。")
            speech_complete += 1
            await report(0.40 + 0.12 * speech_complete / len(scenes),
                         f"旁白合成 {speech_complete}/{len(scenes)} 页", "Speech",
                         page=scene["page_number"], audio_seconds=round(audio_lengths[index], 3))
    for narration_attempt in range(3):
        speech_complete = 0
        if provider.available("tts"):
            await report(0.40, "开始逐页合成旁白", "Speech", attempt=narration_attempt + 1)
            speech_tasks = [asyncio.create_task(speak(i, s)) for i, s in enumerate(scenes)]
            try:
                await asyncio.gather(*speech_tasks)
            except BaseException:
                # Do not submit queued speech or write progress after the run
                # becomes terminal. Completed WAVs remain available for review.
                for task in speech_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*speech_tasks, return_exceptions=True)
                raise
            if any(path is None for path in audio_paths):
                warnings.append("部分页面没有可确认的原文或旁白，因此没有配音。")
        else:
            warnings.append("未配置可用 TTS，视频没有旁白；静音音轨仅用于兼容播放器。")
        try:
            durations = allocate_durations([max(1, len(s["narration"])) for s in scenes], audio_lengths,
                                           settings.target_duration, settings.fps)
            break
        except ValueError:
            if not provider.available("chat") or narration_attempt == 2:
                raise
            # Measured speech, rather than assumed character rate, determines a
            # shorter narration retry. Original attempts remain auditable.
            shortened_budget_seconds = min(settings.target_duration * .7,
                settings.target_duration * 130 / max(1, sum(audio_lengths))) / (narration_attempt + 1)
            prompt = _story_prompt(selected, actual_language, shortened_budget_seconds, age_band)
            prompt += (f"\n上次旁白实际合成总长 {sum(audio_lengths):.2f} 秒，超过片长预算。"
                       "请明显压缩重复修饰，仍保留每页核心事件和全部页码，不能省略正文页面。")
            attempt_name = f"story-shortened-{narration_attempt + 1}"
            (book_dir / "agents" / f"{attempt_name}.prompt.txt").write_text(prompt, encoding="utf-8")
            story = await provider.chat(prompt)
            write_json(book_dir / "agents" / f"{attempt_name}.json", story)
            scenes = validate_story(story, selected)
            review = review_document([p["text"] + "\n" + p["description"] for p in pages]
                                     + [s["narration"] for s in scenes], reviewer, age_band)
            if review["status"] == "blocked":
                raise ValueError("缩短后的旁白未通过内容初筛，已停止生成。")
            write_json(book_dir / "agents" / "safety.json", review)
            audio_dir = book_dir / "audio" / f"attempt-{narration_attempt + 2}"
            audio_dir.mkdir(parents=True, exist_ok=True)
            audio_paths = [None] * len(scenes)
            audio_lengths = [0.0] * len(scenes)
            await report(0.47, "根据实测语音长度压缩旁白，并保留所有正文页面", "Story",
                         attempt=narration_attempt + 2)
    await report(0.53, "旁白和逐页镜头时间已对齐，开始生成视频", "Video")
    segments_dir = book_dir / "segments"
    segments_dir.mkdir(exist_ok=True)
    raw_dir = book_dir / "raw_clips"
    raw_dir.mkdir(exist_ok=True)
    timeline, elapsed, complete = [], 0.0, 0
    pages_by_number = {page["number"]: page for page in pages}
    for index, (scene, duration) in enumerate(zip(scenes, durations)):
        timeline.append({**scene, "index": index, "start": round(elapsed, 6),
                         "end": round(elapsed + duration, 6), "duration": duration,
                         "audio_duration": audio_lengths[index], "has_narration": audio_paths[index] is not None})
        elapsed += duration
    # Bound encoding separately: a large API concurrency must not launch 20 encoders.
    encoders = asyncio.Semaphore(2)
    async def make_scene(index: int, scene: dict):
        nonlocal complete
        page = pages_by_number[scene["page_number"]]
        image = book_dir / "pages" / f"{page['number']:04}.png"
        clip = None
        if mode == "cloud":
            reference = raw_dir / f"reference-{index + 1:04}.png"
            scene["source_reference_transform"] = await asyncio.to_thread(
                prepare_video_reference, image, reference, settings.width, settings.height)
            scene["source_reference_url"] = f"/media/{book_dir.name}/raw_clips/{reference.name}"
            scene["source_reference_sha256"] = sha256_file(reference)
            prompt = (UNTRUSTED_CONTENT_RULE + "\n保持参考原图的画风、配色、文字、角色形象和构图。"
                      "不要增添未在原图出现的角色。使用细微自然动作；画面中的文字保持清晰，不添加字幕。"
                      "参考图的四周留白是整页排版的一部分，请保留完整画幅，不裁剪、不放大填满、不扩展边缘故事内容。"
                      f"\n整书一致性：{story.get('character_bible', '')}"
                      f"\n本页：{scene['video_prompt']}")
            (book_dir / "agents" / f"video-{index + 1:04}.prompt.txt").write_text(prompt, encoding="utf-8")
            async with semaphore:
                clip = Path(await provider.generate_clip(reference, prompt, raw_dir / f"{index + 1:04}.mp4",
                                                         int(math.ceil(scene["duration"]))))
            scene["raw_clip_sha256"] = sha256_file(clip)
            raw_duration = media_duration(await probe_media(clip, settings.ffprobe))
            scene["raw_clip_duration"] = raw_duration
            # Local source-patch restoration remains diagnostic-only: saved
            # clips showed camera drift and seams. Do not silently enable an
            # unvalidated repair on newly generated books.
            async with semaphore:
                scene['quality'] = await inspect_video_quality(
                    image,clip,raw_duration,provider,settings,book_dir,index)
            await report(0.53 + 0.34 * complete / len(timeline),
                         f"第 {page['number']} 页视频与原图一致性核对", "Quality",
                         page=page["number"], decision=scene["quality"]["decision"])
            require_video_quality(scene["quality"], page["number"])
            if scene["quality"]["decision"] == "needs_review":
                warnings.append(f"第 {page['number']} 页视频质量采样存在不确定性或轻微差异，必须人工复核，不能视为已合格。")
            if raw_duration + 0.05 < scene["duration"]:
                warnings.append(f"第 {page['number']} 页模型原片短于镜头时长，尾帧延长 {scene['duration'] - raw_duration:.2f} 秒以完整播放配音。")
        async with encoders:
            transform = await render_segment(image, segments_dir / f"{index + 1:04}.mp4", scene["duration"],
                                              settings, page["image_width"], page["image_height"],
                                              audio_paths[index], clip)
        scene["transform"] = transform
        complete += 1
        await report(0.53 + 0.34 * complete / len(timeline), f"镜头生成 {complete}/{len(timeline)}", "Video", page=page["number"])
    scene_tasks = [asyncio.create_task(make_scene(i, scene)) for i, scene in enumerate(timeline)]
    try:
        await asyncio.gather(*scene_tasks)
    except BaseException:
        # A rejected clip must not leave local sibling generators running after
        # the job is reported failed. Already submitted cloud tasks retain IDs.
        for task in scene_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*scene_tasks, return_exceptions=True)
        raise
    await report(0.9, "一次编排自动合成整书视频并检查格式", "Video")
    output = book_dir / "book.mp4"
    probe = await concatenate_segments([segments_dir / f"{i + 1:04}.mp4" for i in range(len(timeline))], output, settings)
    media_info = check_video_metadata(probe)
    write_json(book_dir / "video_probe.json", probe)
    if not reviewer.strip():
        warnings.append("尚未指定内容安全复核负责人；当前作品只可作为待审核成果。")
    if mode == "cloud":
        warnings.append("生成视频中对象可能移动；触控必须基于点击时刻的实际视频帧重新识别，不能套用 PDF 坐标。")
    book = {"schema_version": SCHEMA_VERSION, "id": book_dir.name, "title": title,
            "language": actual_language, "pages": pages, "scenes": timeline,
            "video_url": f"/media/{book_dir.name}/book.mp4", "duration": media_info["duration"],
            "video_metadata": media_info, "mode": mode, "review": review, "warnings": list(dict.fromkeys(warnings)),
            "summary": str(story.get("summary", "")), "character_bible": story.get("character_bible", ""),
            "provenance": {**source_info, "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "generation_seconds": round(time.perf_counter() - started, 3),
                "output_sha256": sha256_file(output), "single_pipeline_run": True,
                "manual_post_edit": False, "excluded_pages": [p["number"] for p in pages if p["kind"] == "copyright"],
                "page_coverage": [s["page_number"] for s in timeline],
                "vision_used": provider.available("vision"), "story_model_used": provider.available("chat"),
                "generative_video_used": mode == "cloud", "model_calls": "agents/ 与 raw_clips/ 中保存提示和原始结果"}}
    book["provenance"]["providers"] = {
        name: {key: config.get(key, "") for key in ("kind", "base_url", "model", "voice")}
        for name, config in getattr(settings, "providers", {}).items()
        if name in ("vision", "chat", "asr", "tts", "video") and isinstance(config, dict)
    }
    write_json(book_dir / "book.json", book)
    await report(1.0, "视频已生成；等待人工内容复核", "Review", status=review["status"])
    return book
