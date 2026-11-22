"""Bounded source-pixel protection; never a substitute for output inspection.

The reference canvas is mapped to the generated frame under a *fixed camera*
assumption. It is not measured tracking, and final quality verification must
explicitly check alignment, duplicates and masking of real moving objects.
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

from .schema import sha256_file, write_json
from .video import media_duration, probe_media, run_process


PROTECTION_VERSION = 'source-detail-protection-v1'
# Engineering policies, not competition metrics. Refuse broad replacement.
MAX_COVERAGE = .25
SMALL_OBJECT_AREA = .02
MIN_OBJECT_CONFIDENCE = .8


class ProtectionPlanningError(ValueError):
    """Unsafe/overbroad source geometry: keep the rejected raw result blocked."""


def _box(value):
    if not isinstance(value, (list,tuple)) or len(value) != 4:
        return None
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in value):
        return None
    x,y,w,h = value
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x+w > 1.000001 or y+h > 1.000001:
        return None
    return list(value)


def prepare_protection_overlay(reference: Path, page: dict, transform: dict,
                               output: Path, video_size: tuple[int,int]) -> dict:
    """Keep original typography/props as pixels; don't regenerate OCR strings."""
    with Image.open(reference) as source:
        canvas = source.convert('RGBA')
    if (transform.get('kind') != 'reference_contain_pad'
            or transform.get('canvas_size') != list(canvas.size)
            or transform.get('source_size') != [page.get('image_width'),page.get('image_height')]):
        raise ProtectionPlanningError('原始参考图变换不匹配，不能安全合成保护区域。')
    vw,vh = video_size
    if (type(vw) is not int or type(vh) is not int or min(vw,vh) < 1
            or abs((vw/vh)/(canvas.width/canvas.height)-1) > .05):
        raise ProtectionPlanningError('视频与参考图比例差异过大，不能假定保护区域对齐。')
    left,top,cw,ch = transform['content_rect']
    if min(cw,ch) <= 0 or min(left,top) < 0 or left+cw > canvas.width or top+ch > canvas.height:
        raise ProtectionPlanningError('原始参考图内容区域无效。')
    mask = Image.new('L',canvas.size,0)
    draw = ImageDraw.Draw(mask)
    regions=[]
    items = [('source_text', word, f'word-{index+1}') for index,word in enumerate(page.get('words',[]))]
    for obj in page.get('objects',[]):
        box = _box(obj.get('bbox'))
        confidence = obj.get('confidence',0)
        if (box and box[2]*box[3] <= SMALL_OBJECT_AREA and type(confidence) in (float,int)
                and math.isfinite(confidence) and confidence >= MIN_OBJECT_CONFIDENCE):
            items.append(('small_object',obj,str(obj.get('id',''))))
    for kind,item,source_id in items:
        box = _box(item.get('bbox'))
        if not box or (kind == 'source_text' and not str(item.get('text','')).strip()):
            continue
        x,y,w,h = box
        # Include a small context border, particularly around tiny prop tips.
        # This is bounded source restoration, not a mask estimated by the VLM.
        px = max(3/cw,.006,w*.25 if kind == 'small_object' else 0)
        py = max(3/ch,.006,h*.25 if kind == 'small_object' else 0)
        x1=max(left,math.floor(left+(x-px)*cw)); y1=max(top,math.floor(top+(y-py)*ch))
        x2=min(left+cw,math.ceil(left+(x+w+px)*cw)); y2=min(top+ch,math.ceil(top+(y+h+py)*ch))
        draw.rectangle((x1,y1,x2-1,y2-1),fill=255)
        regions.append({'kind':kind,'source_id':source_id,'source':item.get('source','page_understanding'),
                        'source_bbox':box,'reference_rect':[x1,y1,x2-x1,y2-y1]})
    covered=sum(count for value,count in enumerate(mask.histogram()) if value==255)
    coverage=covered/(cw*ch)
    if coverage > MAX_COVERAGE:
        raise ProtectionPlanningError('保护区域覆盖过大，已停止合成；需要复核文字/对象框，不能用整页静态图冒充生成动画。')
    canvas.putalpha(mask)
    output.parent.mkdir(parents=True,exist_ok=True)
    if canvas.size != video_size:
        colour=canvas.convert('RGB').resize(video_size,Image.Resampling.LANCZOS)
        alpha=mask.resize(video_size,Image.Resampling.NEAREST)
        canvas=colour.convert('RGBA'); canvas.putalpha(alpha)
    canvas.save(output,format='PNG')
    return {'version':PROTECTION_VERSION,'status':'planned' if regions else 'not_needed',
            'regions':regions,'coverage_fraction':round(coverage,6),
            'max_coverage_policy':MAX_COVERAGE,'small_object_area_policy':SMALL_OBJECT_AREA,
            'min_object_confidence_policy':MIN_OBJECT_CONFIDENCE,
            'reference_transform':transform,'video_size':list(video_size),
            'mapping':'fixed_reference_normalized_to_generated_frame_assumption',
            'mapping_requires_quality_review':True,'protected_regions_static':True,
            'not_valid_for_generated_frame_grounding':True}


async def protect_video_details(reference: Path, clip: Path, output: Path,
                                page: dict, transform: dict, settings) -> dict:
    """Encode once locally, preserving the paid original and a verifiable audit."""
    if clip.resolve() == output.resolve() or reference.resolve() == output.resolve():
        raise ValueError('保护合成不能覆盖原片或原始参考图。')
    probe=await probe_media(clip,settings.ffprobe)
    stream=next((s for s in probe.get('streams',[]) if s.get('codec_type')=='video'),None)
    duration=media_duration(probe)
    if not stream or not math.isfinite(duration) or duration <= 0:
        raise ValueError('生成原片无有效视频或时长，不能保护细节。')
    overlay=output.with_suffix('.overlay.png')
    output.parent.mkdir(parents=True,exist_ok=True)
    plan=prepare_protection_overlay(reference,page,transform,overlay,(stream['width'],stream['height']))
    plan.update(raw_clip_sha256=sha256_file(clip),reference_sha256=sha256_file(reference),
                overlay_sha256=sha256_file(overlay),raw_clip_duration=duration)
    audit=output.with_suffix('.protection.json')
    write_json(audit,plan)
    if plan['status']=='not_needed':
        return plan
    try:
        await run_process([settings.ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(clip),
            '-loop','1','-i',str(overlay),'-filter_complex_threads','1',
            '-filter_complex','[0:v:0][1:v:0]overlay=0:0:shortest=1:format=auto,format=yuv420p[v]',
            '-map','[v]','-an','-t',f'{duration:.6f}','-c:v','libx264','-preset','veryfast','-crf','18',
            '-threads','2','-movflags','+faststart',str(output)],timeout=max(180,duration*12))
        processed=await probe_media(output,settings.ffprobe)
        if abs(media_duration(processed)-duration) > .08 or sha256_file(clip)!=plan['raw_clip_sha256']:
            raise ValueError('保护合成时长或原片指纹不匹配，已停止。')
        plan.update(status='composited',output_sha256=sha256_file(output),
                    output_duration=media_duration(processed))
    except BaseException:
        plan['status']='failed'
        output.unlink(missing_ok=True)
        write_json(audit,plan)
        raise
    write_json(audit,plan)
    return plan
