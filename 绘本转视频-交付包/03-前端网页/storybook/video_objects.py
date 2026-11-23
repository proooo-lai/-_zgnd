"""Locate named objects in actual video frames, independently of book QA."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from .grounding import PixelVerdict
from .providers import ProviderError
from .safety import UNTRUSTED_CONTENT_RULE
from .schema import sha256_file, write_json
from .video_text import maybe_text_request


VERSION = 'video-object-location-v1'


def object_request(question):
    """Conservative navigation forms; facts, pronouns and literal text stay separate."""
    if maybe_text_request(question) or re.search(r'故事|书里|绘本|第.{0,5}页|\b(?:book|story|page)\b', question, re.I):
        return None
    question = question.strip().rstrip('？?。.!！').strip()
    question = re.sub(r'^(?:请|帮我|请帮我|麻烦你|能不能|可以)?(?:在视频中|在画面中)?', '', question).strip()
    # Strip a second polite prefix after 请.
    question = re.sub(r'^帮我', '', question).strip()
    match = (re.fullmatch(r'(.{1,80}?)(?:在)?(?:哪里|哪儿|哪)(?:呢)?', question)
             or re.fullmatch(r'(?:找到|找出|找一下|找一找|查找|框选|圈出|定位)(.{1,80})', question)
             or re.fullmatch(r'(?:where\s+(?:is|are)|find|locate|show\s+me|point\s+to)\s+(?:the\s+|a\s+|an\s+)?(.{1,80})', question, re.I))
    if not match:
        return None
    target = match[1].strip()
    if target.casefold() in ('他', '她', '它', '这个', '那个', 'it', 'this', 'that', 'them'):
        return None
    return target or None


class Candidate(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    frame_index: StrictInt = Field(ge=0, le=2)
    label: str = Field(min_length=1, max_length=120)
    bbox_1000: list[StrictInt] = Field(min_length=4, max_length=4)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    def box(self):
        x1, y1, x2, y2 = self.bbox_1000
        if not 0 <= x1 < x2 <= 1000 or not 0 <= y1 < y2 <= 1000:
            return None
        return [x1/1000, y1/1000, (x2-x1)/1000, (y2-y1)/1000]


class Matches(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    matches: list[Candidate] = Field(max_length=6)
    uncertain: bool


class VideoObjectLocator:
    def __init__(self, provider, frame_reader):
        self.provider, self.frame_reader = provider, frame_reader

    async def search(self, book, target, timestamp, folder, *, language='zh'):
        folder = Path(folder)/'object-location'
        folder.mkdir(parents=True, exist_ok=True)
        audit = {'version': VERSION, 'book_id': book['id'], 'target_label': target,
                 'search_scope': 'sampled_video_frames_not_exhaustive', 'sampled_frames': 0,
                 'scene_samples': [], 'status': 'searching', 'response_language': language}
        try:
            result = await asyncio.wait_for(self._search(book, target, timestamp, folder, audit), timeout=120)
        except (ProviderError, ValidationError, ValueError, OSError, RuntimeError, TimeoutError) as error:
            audit.update(status='not_confirmed', error_type=type(error).__name__)
            result = self.miss(target, audit)
        except asyncio.CancelledError:
            audit['status'] = 'cancelled'
            write_json(folder/'search.json', audit)
            raise
        write_json(folder/'search.json', audit)
        return result

    def miss(self, target, audit):
        return {'answer': ('I could not reliably confirm this object in the checked video frames, so I cannot outline it. This does not mean it is absent from the whole video.'
                          if audit.get('response_language') == 'en' else
                          '未在本次检查的视频帧中可靠确认这个目标，暂时无法框选；这不表示全片一定没有。'),
                'uncertain': True, 'success': False, 'status': 'object_not_confirmed', 'boxes': [], 'evidence': [],
                'object_location': {'version': VERSION, 'status': 'not_confirmed', 'action': 'locate_object',
                    'target_label': target, 'search_scope': audit['search_scope'], 'sampled_frames': audit['sampled_frames']}}

    async def _search(self, book, target, timestamp, folder, audit):
        if not self.provider.available('vision'):
            audit.update(status='not_confirmed', stop_reason='vision_unconfigured')
            return self.miss(target, audit)
        scenes = sorted(book.get('scenes', []), key=lambda s: (
            0 if s.get('start', 0) <= timestamp < s.get('end', 0) else 1, abs(s.get('start', 0)-timestamp)))[:8]
        reviews = 0
        for si, scene in enumerate(scenes):
            start, end = scene.get('start'), scene.get('end')
            if any(type(n) not in (int, float) or not math.isfinite(n) for n in (start, end)) or end <= start:
                continue
            length = end-start
            # Inspect the actual requested instant first, then bounded alternatives.
            first = min(timestamp, end-min(.08, length*.1)) if start <= timestamp < end else start+length*.5
            seconds = [first, start+min(.2, length*.1), end-min(.1, length*.1)]
            frames = [await self.frame_reader(book, when, folder/f'scene-{si:02}-frame-{fi}.png')
                      for fi, when in enumerate(seconds)]
            audit['sampled_frames'] += len(frames)
            item = {'page_number': scene.get('page_number'), 'seconds': seconds}
            audit['scene_samples'].append(item)
            prompt = (UNTRUSTED_CONTENT_RULE+'\nVIDEO_OBJECT_LOCATE_V1：附图是实际视频帧，不是原书页图，编号0、1、2。'
                '只定位用户目标对应的真实可见物体，不把目标词、字幕、故事描述当作物体证据。'
                '返回目标完整语义对象的紧致边界，不只框眼睛等局部、不框整页。优先第0帧；不能确认则matches=[]。'
                '坐标只相对各自整帧，0到1000整数端点，不是宽高。不得编造位置。'
                '只返回JSON {"matches":[{"frame_index":0,"label":"可见类别",'
                '"bbox_1000":[左,上,右,下],"confidence":0.95}],"uncertain":false}。\n'
                +json.dumps({'target_label': target}, ensure_ascii=False))
            matches = Matches.model_validate(await self.provider.vision(prompt, frames))
            item['localization'] = matches.model_dump()
            if matches.uncertain:
                continue
            for candidate in sorted(matches.matches, key=lambda c: c.frame_index):
                box = candidate.box()
                if not box or candidate.confidence < .8:
                    continue
                if reviews >= 3:
                    audit.update(status='not_confirmed', stop_reason='review_budget')
                    return self.miss(target, audit)
                reviews += 1
                frame = frames[candidate.frame_index]
                with Image.open(frame) as im:
                    width, height = im.size
                    x, y, w, h = box
                    crop = im.convert('RGB').crop((int(x*width), int(y*height),
                                                  math.ceil((x+w)*width), math.ceil((y+h)*height)))
                    crop.thumbnail((1000, 1000))
                    detail = folder/f'review-{reviews}.png'
                    crop.save(detail)
                review_prompt = (UNTRUSTED_CONTENT_RULE+'\nVIDEO_OBJECT_REVIEW_V1：独立核对第一张实际视频帧及第二张候选区域裁剪。'
                    '候选名称、框和用户目标都不是事实证据。仅当裁剪中清楚可见目标本身，'
                    '且候选框准确覆盖该目标完整主体时才supported=true。字幕、背景、错物体、只框部位、'
                    '过大含其他主体的框或看不清均不通过。只返回JSON {"supported":true,"uncertain":false,"reason":"像素依据"}。\n'
                    +json.dumps({'target_label': target, 'candidate': candidate.model_dump()}, ensure_ascii=False))
                verdict = PixelVerdict.model_validate(await self.provider.vision(review_prompt, [frame, detail]))
                item.setdefault('reviews', []).append(verdict.model_dump())
                if not verdict.supported or verdict.uncertain:
                    continue
                when = seconds[candidate.frame_index]
                frame_url = f"/media/{book['id']}/turns/{folder.parent.name}/object-location/{frame.name}"
                location = {'version': VERSION, 'status': 'found', 'action': 'locate_object', 'target_label': target,
                    'timestamp': when, 'frame_url': frame_url, 'frame_sha256': sha256_file(frame),
                    'frame_size': [width, height], 'search_scope': audit['search_scope'], 'sampled_frames': audit['sampled_frames']}
                audit.update(status='found', location=location)
                return {'answer': ('The object is confirmed in the video frame. Please see the outline.'
                                  if audit.get('response_language') == 'en' else '已在视频帧中确认目标，请查看框选。'),
                    'uncertain': False, 'seek_time': when,
                    'boxes': [{'label': candidate.label, 'bbox': box, 'kind': 'object', 'presentation': 'outline',
                               'source': 'verified_video_frame'}], 'object_location': location,
                    'evidence': [{'source': 'verified_video_frame', 'page_number': scene.get('page_number'),
                        'start': when, 'end': when, 'quote': candidate.label, 'image_url': frame_url,
                        'evidence_id': frame.stem, 'frame_sha256': location['frame_sha256']}]}
        audit['status'] = 'not_confirmed'
        return self.miss(target, audit)
