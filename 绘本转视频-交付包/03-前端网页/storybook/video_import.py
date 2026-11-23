"""Import an explicit existing narrated film without generating new media.

The local fingerprinted manifest supplies reviewed stable page intervals.
Gaps (crossfades, titles, outros) intentionally carry no current-page claim.
Original PDF understanding and adapted subtitles remain separate evidence.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .pipeline import (_vision_cached, _vision_prompt, assert_allowed_input,
                       detect_language, extract_pdf, merge_visual_page)
from .safety import review_document
from .schema import SCHEMA_VERSION, sha256_file, write_json
from .storage import Store
from .video import check_video_metadata, probe_media, run_process


class ImportScene(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    page_number: StrictInt = Field(ge=1)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    subtitle_indices: list[StrictInt] = Field(max_length=1000)


class ImportManifest(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    title: str = Field(min_length=1, max_length=200)
    pdf_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    video_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    subtitle_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    scenes: list[ImportScene] = Field(min_length=1, max_length=100)


def parse_srt(text: str) -> list[dict]:
    if len(text) > 5_000_000:
        raise ValueError('字幕过大。')
    text = text.lstrip('\ufeff').replace('\r\n', '\n').replace('\r', '\n').strip()
    cues = []
    stamp = r'(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})'
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        match = re.fullmatch(stamp + r'\s+-->\s+' + stamp, lines[1].strip()) if len(lines) >= 3 else None
        if not lines or not lines[0].strip().isdigit() or not match:
            raise ValueError('字幕必须是包含序号、有效时间和正文的 SRT。')
        values = [int(value) for value in match.groups()]
        start = values[0]*3600 + values[1]*60 + values[2] + values[3]/1000
        end = values[4]*3600 + values[5]*60 + values[6] + values[7]/1000
        index, body = int(lines[0].strip()), '\n'.join(lines[2:]).strip()
        if (end <= start or not body or len(body) > 18000 or len(cues) >= 1000
                or (cues and (index <= cues[-1]['index'] or start < cues[-1]['end']))):
            raise ValueError('字幕序号、正文或时间顺序无效。')
        cues.append({'index': index, 'start': start, 'end': end, 'text': body})
    return cues


def validate_mapping(raw, page_numbers, cues, duration) -> list[dict]:
    scenes = [ImportScene.model_validate(item).model_dump() for item in raw]
    if [s['page_number'] for s in scenes] != page_numbers:
        raise ValueError('导入时间线必须按顺序完整覆盖每个原书页一次。')
    indexed = {cue['index']: cue for cue in cues}
    used, previous_end = [], 0.0
    for scene in scenes:
        if not previous_end <= scene['start'] < scene['end'] <= duration:
            raise ValueError('时间线重叠、越界或时长无效。')
        for index in scene['subtitle_indices']:
            cue = indexed.get(index)
            # Narration may start during the preceding crossfade. It cannot
            # start on an earlier stable page or continue onto the next page.
            if not cue or not previous_end <= cue['start'] < cue['end'] <= scene['end']:
                raise ValueError('字幕和对应页时间不匹配。')
            used.append(index)
        previous_end = scene['end']
    if used != [cue['index'] for cue in cues]:
        raise ValueError('每条字幕必须按顺序对应一页，不能重复或遗漏。')
    return scenes


async def import_video_book(settings, provider, pdf, video, subtitles, raw_manifest, book_id,
                            *, reviewer='', age_band='6-8', progress=None, cached_only=False):
    """Publish only after source understanding, media decoding and hash checks.

    Calls the configured source-page vision service, with its existing bounded
    retry/cache rules. Never calls story generation, TTS or video generation.
    A failed directory is retained for diagnosis; use a new id to retry, with
    matching successfully understood pages reusable through the vision cache.
    """
    manifest = ImportManifest.model_validate(raw_manifest)
    Store.identifier(book_id)
    root = assert_allowed_input(Path(settings.data_dir))
    output = root / book_id
    if output.exists():
        raise ValueError('导入必须使用新的绘本目录，不能覆盖原有作品。')
    sources = [assert_allowed_input(Path(path)) for path in (pdf, video, subtitles)]
    fingerprints = [manifest.pdf_sha256, manifest.video_sha256, manifest.subtitle_sha256]
    for path, expected in zip(sources, fingerprints):
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError('导入输入指纹不匹配。')
    if not cached_only and not provider.available('vision'):
        raise ValueError('导入互动绘本需要配置原书读图服务。')
    if sources[2].stat().st_size > 5_000_000:
        raise ValueError('字幕过大。')
    cues = parse_srt(sources[2].read_text(encoding='utf-8-sig'))
    probe = await probe_media(sources[1], settings.ffprobe)
    # Imported films are not constrained to our generated-film time budget.
    metadata = check_video_metadata(probe, minimum=0.01, maximum=100000)
    if metadata['audio_tracks'] != 1:
        raise ValueError('导入全书测试视频必须包含一个完整配音音轨。')
    audio = next(s for s in probe['streams'] if s.get('codec_type') == 'audio')
    if float(audio.get('duration') or 0) < metadata['duration'] - .1:
        raise ValueError('配音音轨未覆盖完整视频时长。')
    output.mkdir(parents=True, exist_ok=False)
    audit = {'version': 'external-video-import-v1', 'status': 'importing',
             'created_at': datetime.now(timezone.utc).isoformat(),
             'manifest': manifest.model_dump(), 'new_video_submissions': 0,
             'new_tts_submissions': 0, 'understood_pages': [],
             'cached_only': cached_only, 'understanding_pending_pages': [],
             'timeline_policy': 'stable_page_intervals_only_gaps_unmapped'}
    write_json(output/'import.json', audit)
    try:
        for source, filename in zip(sources, ('input.pdf', 'book.mp4', 'subtitles.srt')):
            await asyncio.to_thread(shutil.copy2, source, output/filename)
        pages, source_info = await asyncio.to_thread(extract_pdf, output/'input.pdf', output, settings.max_pages)
        write_json(output/'agents/ocr.json', pages)
        scenes = validate_mapping([s.model_dump() for s in manifest.scenes],
                                  [p['number'] for p in pages], cues, metadata['duration'])
        write_json(output/'agents/imported-subtitles.json', cues)
        # A full demux/decode catches truncated media before publishing it.
        await run_process([settings.ffmpeg, '-v', 'error', '-xerror', '-i', str(output/'book.mp4'),
                           '-map', '0:v:0', '-map', '0:a:0', '-f', 'null', '-'],
                          timeout=max(120, min(600, metadata['duration']*3)))
        semaphore = asyncio.Semaphore(min(4, settings.concurrency))
        async def understand(page):
            async with semaphore:
                prompt = _vision_prompt(page)
                (output/'agents'/f"vision-{page['number']:04}.prompt.txt").write_text(prompt, encoding='utf-8')
                result = await _vision_cached(provider, prompt, output/'pages'/f"{page['number']:04}.png",
                                              settings, cache_only=cached_only)
                if result is None:
                    merged = {**page, 'uncertainties': ['未完成本页图像理解；仅保留原图和原始 PDF 文字，文字可能不完整。']}
                    audit['understanding_pending_pages'].append(page['number'])
                else:
                    write_json(output/'agents'/f"vision-{page['number']:04}.json", result)
                    merged = merge_visual_page(page, result)
                    audit['understood_pages'].append(page['number'])
                write_json(output/'import.json', audit)
                if progress:
                    progress(len(audit['understood_pages']) + len(audit['understanding_pending_pages']), len(pages))
                return merged
        tasks = [asyncio.create_task(understand(page)) for page in pages]
        try:
            pages = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        indexed = {cue['index']: cue for cue in cues}
        for scene in scenes:
            scene.update(narration='\n'.join(indexed[i]['text'] for i in scene['subtitle_indices']),
                         narration_source='imported_subtitles_not_source_text',
                         has_narration=bool(scene['subtitle_indices']),
                         transform={'kind': 'generated_frame', 'requires_frame_grounding': True,
                                    'video_width': metadata['width'], 'video_height': metadata['height']})
        review = review_document([p['text'] for p in pages] + [p['description'] for p in pages]
                                 + [c['text'] for c in cues], reviewer, age_band)
        audit['review'] = review
        if review['status'] == 'blocked':
            raise ValueError('导入内容检查未通过，已停止发布。')
        for original, expected, filename in zip(sources, fingerprints, ('input.pdf', 'book.mp4', 'subtitles.srt')):
            if sha256_file(original) != expected or sha256_file(output/filename) != expected:
                raise ValueError('导入期间输入变化，指纹核对失败。')
        book = {'schema_version': SCHEMA_VERSION, 'id': book_id, 'title': manifest.title,
                'language': detect_language(pages), 'age_band': age_band, 'mode': 'imported_video',
                'pages': pages, 'scenes': scenes, 'duration': metadata['duration'],
                'video_url': f'/media/{book_id}/book.mp4', 'video_metadata': metadata, 'review': review,
                'warnings': ['已导入既有成片和配音，未在工作台重新生成。原书图文与改编旁白分开保存。',
                    '转场和片尾不归属于原书页；点击请暂停在清晰的故事画面。',
                    '导入不代表视频内容质量或全流程生成验收通过；成片与年龄适配仍待人工复核。'],
                'provenance': {**source_info, 'execution': 'external_video_import',
                    'output_sha256': manifest.video_sha256, 'subtitle_sha256': manifest.subtitle_sha256,
                    'single_pipeline_run': False, 'vision_used': bool(audit['understood_pages']), 'story_model_used': False,
                    'understanding_pending_pages': sorted(audit['understanding_pending_pages']),
                    'understanding_reused_pages': sorted(audit['understood_pages']) if cached_only else [],
                    'new_video_submissions': 0, 'new_tts_submissions': 0,
                    'page_coverage': [p['number'] for p in pages],
                    'timeline_policy': audit['timeline_policy'], 'manual_content_review': 'pending',
                    'import_audit': 'import.json'}}
        if audit['understanding_pending_pages']:
            book['warnings'].insert(0, '第 ' + '、'.join(map(str, sorted(audit['understanding_pending_pages'])))
                + ' 页读图待完成，目前仅有原始 PDF 文字。全书播放可测，完整图像理解与问答尚未验证。')
        audit.update(status='imported_pending_human_review', video_metadata=metadata)
        write_json(output/'video_probe.json', probe)
        write_json(output/'import.json', audit)
        write_json(output/'book.json', book)
        return book
    except BaseException as error:
        audit.update(status='failed', error_type=type(error).__name__)
        write_json(output/'import.json', audit)
        raise
