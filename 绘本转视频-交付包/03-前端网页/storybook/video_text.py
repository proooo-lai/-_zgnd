"""Locate text in actual video frames; source OCR is only a search hint."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import re
from typing import Literal
import unicodedata

from PIL import Image
from pydantic import BaseModel,ConfigDict,Field,StrictInt,ValidationError

from .providers import ProviderError
from .safety import UNTRUSTED_CONTENT_RULE
from .schema import sha256_file,write_json


VERSION='video-text-location-v1'
MAX_SCENES=8
MAX_REVIEWS=3
SINGLE_QUOTED=re.compile(r"(?<!\w)'([^'\n]+)'(?!\w)")


def text_key(text):
    text=unicodedata.normalize('NFKC',text).casefold()
    text=re.sub(r'\s+',' ',text).strip()
    # Chinese line wrapping does not create words; English spaces do.
    return re.sub(r'(?<=[\u3400-\u9fff]) (?=[\u3400-\u9fff])','',text)


class TextRequest(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid')
    action: Literal['locate_text','highlight_text']
    target_text: str=Field(min_length=1,max_length=500)


def maybe_text_request(question):
    action=re.search(r'查找|找到|找出|找一下|找一找|框选|圈出|定位|高亮|标亮|在哪|哪里|\b(?:find|locate|highlight|search|where)\b',question,re.I)
    text=re.search(r'文字|文本|句子|词语|单词|这个字|这句话|这行字|[“「『"]|\b(?:text|word|phrase|sentence|line)\b',question,re.I)
    return bool(action and (text or SINGLE_QUOTED.search(question) or text_action(question)=='highlight_text'))


def text_action(question):
    return 'highlight_text' if re.search(r'高亮|标亮|\bhighlight\b',question,re.I) else 'locate_text'


def validate_text_request(raw,question):
    try:
        result=TextRequest.model_validate(raw)
    except ValidationError:
        return None
    if not text_key(result.target_text) or text_key(result.target_text) not in text_key(question):
        return None
    return result.model_copy(update={'action':text_action(question)})


def quoted_request(question):
    if not maybe_text_request(question): return None
    quotes=re.findall(r'[“「『"]([^”」』"\n]+)[”」』"]',question)
    if not quotes:
        quotes=SINGLE_QUOTED.findall(question)
    if len(quotes)!=1: return None
    return validate_text_request({'action':text_action(question),'target_text':quotes[0]},question)


async def resolve_text_request(provider,question):
    direct=quoted_request(question)
    if direct: return direct
    if not maybe_text_request(question) or not provider.available('chat'): return None
    raw=await asyncio.wait_for(provider.chat(UNTRUSTED_CONTENT_RULE+'\nVIDEO_TEXT_REQUEST_V1：只提取用户明确要求在视频中查找/框选/高亮的文字。'
        'target_text必须逐字来自用户问题，不能推断、翻译或补写答案。没有明确文字则target_text为空。'
        '返回JSON {"action":"locate_text或highlight_text","target_text":"原样目标文字"}。\n'+json.dumps({'question':question},ensure_ascii=False)),timeout=15)
    return validate_text_request(raw,question)


class TextBox(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid')
    text: str=Field(min_length=1,max_length=500)
    bbox_1000: list[StrictInt]=Field(min_length=4,max_length=4)
    confidence: float=Field(ge=0,le=1,allow_inf_nan=False)

    def box(self):
        x1,y1,x2,y2=self.bbox_1000
        if not 0 <= x1 < x2 <= 1000 or not 0 <= y1 < y2 <= 1000: return None
        return [x1/1000,y1/1000,(x2-x1)/1000,(y2-y1)/1000]


class Match(TextBox):
    frame_index: StrictInt=Field(ge=0,le=2)


class Matches(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid')
    matches: list[Match]=Field(max_length=12)
    uncertain: bool


class Review(TextBox):
    visible: bool
    uncertain: bool


def overlap(a,b):
    intersection=max(0,min(a[0]+a[2],b[0]+b[2])-max(a[0],b[0]))*max(0,min(a[1]+a[3],b[1]+b[3])-max(a[1],b[1]))
    return intersection/(a[2]*a[3]+b[2]*b[3]-intersection) if intersection else 0


class VideoTextLocator:
    def __init__(self,provider,frame_reader):
        self.provider,self.frame_reader=provider,frame_reader

    async def search(self,book,request,timestamp,folder):
        folder=Path(folder)/'text-location';folder.mkdir(parents=True,exist_ok=True)
        audit={'version':VERSION,'book_id':book['id'],'request':request.model_dump(),
               'search_scope':'sampled_video_frames_not_exhaustive','sampled_frames':0,'scene_samples':[],
               'max_scene_policy':MAX_SCENES,'max_review_policy':MAX_REVIEWS,'status':'searching'}
        try:
            result=await asyncio.wait_for(self._search(book,request,timestamp,folder,audit),timeout=120)
        except (ProviderError,ValidationError,ValueError,OSError,RuntimeError,TimeoutError) as error:
            audit.update(status='not_confirmed',error_type=type(error).__name__)
            result=self.miss(request,audit,'服务或视频帧核对未能完成，无法可靠定位。')
        except asyncio.CancelledError:
            audit['status']='cancelled';write_json(folder/'search.json',audit);raise
        write_json(folder/'search.json',audit)
        return result

    def miss(self,request,audit,reason='未在本次检查的视频帧中确认目标文字；这不表示全片一定没有。'):
        return {'answer':reason,'uncertain':True,'success':False,'status':'text_not_confirmed','boxes':[], 'evidence':[],
                'text_location':{'version':VERSION,'status':'not_confirmed','action':request.action,
                    'target_text':request.target_text,'search_scope':audit['search_scope'],'sampled_frames':audit['sampled_frames']}}

    async def _search(self,book,request,timestamp,folder,audit):
        if not self.provider.available('vision'):
            audit.update(status='not_confirmed',stop_reason='vision_unconfigured')
            return self.miss(request,audit,'需要可用的互动视觉服务才能核对视频中的文字。')
        pages={p['number']:p for p in book['pages']}
        key=text_key(request.target_text)
        def priority(scene):
            page=pages.get(scene.get('page_number'),{})
            hint=' '.join([page.get('text',''),scene.get('narration','')]+[w.get('text','') for w in page.get('words',[])])
            return (0 if key in text_key(hint) else 1,abs(scene.get('start',0)-timestamp))
        scenes=sorted(book.get('scenes',[]),key=priority)[:MAX_SCENES]
        reviews=0
        for si,scene in enumerate(scenes):
            if reviews>=MAX_REVIEWS:
                audit['stop_reason']='review_budget';break
            start,end=scene.get('start'),scene.get('end')
            if any(type(v) not in (int,float) or not math.isfinite(v) for v in (start,end)) or end<=start: continue
            length=end-start
            seconds=[start+min(.2,length*.1),start+length*.5,end-min(.1,length*.1)]
            frames=[]
            for fi,second in enumerate(seconds):
                frame=folder/f'scene-{si:02}-frame-{fi}.png'
                frames.append(await self.frame_reader(book,second,frame))
            audit['sampled_frames']+=len(frames)
            observation={'scene_page':scene.get('page_number'),'seconds':seconds}
            audit['scene_samples'].append(observation)
            prompt=(UNTRUSTED_CONTENT_RULE+'\nVIDEO_TEXT_LOCATE_V1：附图是实际视频帧，不是原书页图。'
                '只找实际像素里清楚可读、与目标完全相符的文字，包括原画文字或视频字幕。'
                '不能根据故事内容补字，不能将同义词、物体或疑似文字当命中。逐帧编号为0、1、2。'
                '只框目标文字本身，不框整页或旁边词句；端点按各自整帧0到1000整数，不是宽高或小数。'
                '输出JSON {"matches":[{"frame_index":0,"text":"实际目标原文","bbox_1000":[左,上,右,下],"confidence":0.95}],"uncertain":false}。'
                '没有确认命中时matches=[]，看不清则uncertain=true。\n'+json.dumps({'target_text':request.target_text},ensure_ascii=False))
            matches=Matches.model_validate(await self.provider.vision(prompt,frames))
            observation['localization']=matches.model_dump()
            write_json(folder/'search.json',audit)
            if matches.uncertain: continue
            for match in matches.matches:
                box=match.box()
                if not box or match.confidence<.8 or text_key(match.text)!=key: continue
                if reviews>=MAX_REVIEWS: break
                reviews+=1
                frame=frames[match.frame_index]
                with Image.open(frame) as im:
                    fw,fh=im.size
                    x,y,w,h=box
                    crop=im.convert('RGB').crop((max(0,int(x*fw)-5),max(0,int(y*fh)-5),min(fw,math.ceil((x+w)*fw)+5),min(fh,math.ceil((y+h)*fh)+5)))
                    crop.thumbnail((1200,600))
                    # Small lettering is enlarged for reading, without changing the full-frame contract.
                    factor=min(160/crop.height,1200/crop.width)
                    if factor>1: crop=crop.resize((round(crop.width*factor),round(crop.height*factor)),Image.Resampling.LANCZOS)
                    detail=folder/f'review-{reviews}.png';crop.save(detail)
                review_prompt=(UNTRUSTED_CONTENT_RULE+'\nVIDEO_TEXT_REVIEW_V1：第一张为实际视频帧，第二张只放大候选文字区域。'
                    '独立逐字核对，目标文字不是证据。原画文字和视频字幕都可以，但缺字、错字、遮挡或看不清不能通过。'
                    '同时重新定位目标文字的完整紧致边界，坐标必须相对第一张整帧的0到1000整数端点。'
                    '返回JSON {"visible":true,"uncertain":false,"text":"实际读到的目标文字",'
                    '"bbox_1000":[左,上,右,下],"confidence":0.95}。不确认时visible=false、uncertain=true。'
                    '\n'+json.dumps({'target_text':request.target_text},ensure_ascii=False))
                review=Review.model_validate(await self.provider.vision(review_prompt,[frame,detail]))
                observation.setdefault('reviews',[]).append(review.model_dump())
                verified=review.box()
                if not (review.visible and not review.uncertain and review.confidence>=.8 and verified
                        and text_key(review.text)==key and overlap(box,verified)>=.3): continue
                when=seconds[match.frame_index]
                relative=frame.relative_to(folder.parent).as_posix()
                frame_url=f"/media/{book['id']}/turns/{folder.parent.name}/{relative}"
                box_result={'bbox':verified,'label':review.text,'kind':'text',
                            'presentation':'highlight' if request.action=='highlight_text' else 'outline',
                            'source':'verified_video_frame'}
                location={'version':VERSION,'status':'found','action':request.action,'target_text':request.target_text,
                    'timestamp':when,'frame_url':frame_url,'frame_sha256':sha256_file(frame),'frame_size':[fw,fh],
                    'search_scope':audit['search_scope'],'sampled_frames':audit['sampled_frames']}
                audit.update(status='found',location=location)
                return {'answer':('已在视频帧中确认目标文字，请查看高亮。' if request.action=='highlight_text' else '已在视频帧中确认目标文字，请查看框选。'),
                    'uncertain':False,'seek_time':when,'boxes':[box_result],'text_location':location,
                    'evidence':[{'source':'verified_video_frame','page_number':scene.get('page_number'),
                        'start':when,'end':when,'quote':review.text,'image_url':frame_url,
                        'evidence_id':frame.stem,'frame_sha256':location['frame_sha256']}]}
        audit['status']='not_confirmed'
        return self.miss(request,audit)
