"""Explicit, fingerprinted assembly of existing media; never submits model jobs."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Literal
import wave

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .pipeline import detect_language, merge_visual_page, validate_story, validate_video_quality
from .safety import review_document
from .schema import SCHEMA_VERSION, sha256_file, write_json
from .storage import Store
from .video import allocate_durations, check_video_metadata, concatenate_segments, probe_media, render_segment


class Choice(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid')
    scene: StrictInt=Field(ge=1)
    clip_path: str=Field(min_length=1)
    quality_path: str=Field(min_length=1)
    audio_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    audio_path: str | None=Field(default=None,min_length=1,max_length=500)
    rendering: Literal['generative_video','source_preserved_motion']
    reason: str=Field(min_length=1,max_length=2000)


class RecoveryManifest(BaseModel):
    model_config=ConfigDict(strict=True,extra='forbid')
    source_book_id: str=Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    source_pdf_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    story_sha256: str=Field(pattern=r'^[a-f0-9]{64}$')
    story_path: str=Field(default='agents/story.json',min_length=1,max_length=500)
    choices: list[Choice]=Field(min_length=1,max_length=100)


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def checked_hash(path,expected):
    if not Path(path).is_file() or sha256_file(Path(path))!=expected:
        raise ValueError('恢复输入指纹不匹配，禁止复用。')


def source_file(source,relative,suffix):
    path=Path(relative)
    target=(source/path).resolve()
    if path.is_absolute() or not target.is_relative_to(source.resolve()) or target.suffix.lower()!=suffix:
        raise ValueError('叙事和配音路径必须位于原运行目录内，且格式正确。')
    return target


def prepare_recovery(settings,raw,output_id,*,allow_source_motion=False):
    manifest=RecoveryManifest.model_validate(raw)
    Store.identifier(output_id)
    root=Path(settings.data_dir).resolve()
    source=root/manifest.source_book_id;output=root/output_id
    if source==output or output.exists():
        raise ValueError('恢复必须使用新的独立绘本目录，不能覆盖原运行。')
    checked_hash(source/'input.pdf',manifest.source_pdf_sha256)
    story_path=source_file(source,manifest.story_path,'.json')
    checked_hash(story_path,manifest.story_sha256)
    ocr=read_json(source/'agents/ocr.json')
    pages=[merge_visual_page(page,read_json(source/f"agents/vision-{page['number']:04}.json")) for page in ocr]
    story=read_json(story_path)
    scenes=validate_story(story,[page for page in pages if page.get('kind')!='copyright'])
    if [choice.scene for choice in manifest.choices]!=list(range(1,len(scenes)+1)):
        raise ValueError('恢复清单必须按顺序完整覆盖每个镜头一次。')
    prepared=[];audio_lengths=[];fallback=[]
    for choice,scene in zip(manifest.choices,scenes):
        image=source/f"pages/{scene['page_number']:04}.png"
        clip=Path(choice.clip_path).resolve();quality_path=Path(choice.quality_path).resolve()
        if clip.suffix.lower()!='.mp4' or quality_path.suffix.lower()!='.json':
            raise ValueError('恢复产物必须为已保存的 MP4 和 JSON 核对记录。')
        quality=read_json(quality_path)
        checked_hash(image,quality.get('source_sha256'))
        checked_hash(clip,quality.get('clip_sha256'))
        validated=validate_video_quality(quality)
        if validated['decision']=='rejected':
            raise ValueError('视频质量未通过，禁止合成或发布。')
        if choice.rendering=='source_preserved_motion':
            if not allow_source_motion:
                raise ValueError('原图保真动效需要明确启用，并披露降级。')
            sidecar=read_json(clip.with_suffix('.motion.json'))
            if sidecar.get('status')!='completed' or sidecar.get('version')!='source-preserved-motion-v1':
                raise ValueError('保真动效来源记录无效。')
            checked_hash(image,sidecar.get('source_sha256'))
            fallback.append(scene['page_number'])
        else:
            sidecar=read_json(clip.with_suffix('.task.json'))
            if sidecar.get('status')!='succeeded':
                raise ValueError('视频任务尚未确认成功，不能复用。')
        checked_hash(clip,sidecar.get('output_sha256'))
        audio=source_file(source,choice.audio_path or f'audio/{choice.scene:04}.wav','.wav')
        checked_hash(audio,choice.audio_sha256)
        with wave.open(str(audio),'rb') as stream:
            if stream.getcomptype()!='NONE' or stream.getnchannels()!=1 or stream.getsampwidth()!=2:
                raise ValueError('配音应为有效单声道 PCM16 WAV。')
            frames,rate=stream.getnframes(),stream.getframerate()
            if frames<=0 or rate<8000 or len(stream.readframes(frames))!=frames*2:
                raise ValueError('配音为空或截断，禁止合成。')
            length=frames/rate
        audio_lengths.append(length)
        prepared.append({'choice':choice.model_dump(),'scene':scene,'image':image,'clip':clip,
                         'quality':quality,'quality_path':quality_path,'sidecar':sidecar,'audio':audio})
    review=review_document([p.get('text','')+'\n'+p.get('description','') for p in pages]
                           +[scene['narration'] for scene in scenes],'','6-8')
    if review['status']=='blocked':
        raise ValueError('内容安全初筛未通过，禁止合成。')
    checked_hash(story_path,manifest.story_sha256)
    return {'source':source,'output':output,'manifest':manifest.model_dump(),'pages':pages,'story':story,'story_path':story_path,
            'items':prepared,'audio_lengths':audio_lengths,'fallback_pages':fallback,'review':review}


async def assemble_recovered_book(settings,raw,output_id,*,allow_source_motion=False,progress=None):
    prepared=prepare_recovery(settings,raw,output_id,allow_source_motion=allow_source_motion)
    output=prepared['output'];source=prepared['source'];items=prepared['items']
    durations=allocate_durations([len(item['scene']['narration']) for item in items],prepared['audio_lengths'],
                                  settings.target_duration,settings.fps)
    output.mkdir(parents=True)
    audit={'status':'assembling','source_book_id':source.name,'output_book_id':output_id,
           'single_pipeline_run':False,'model_submissions':0,'manifest':prepared['manifest'],'scenes':[]}
    write_json(output/'recovery.json',audit)
    try:
        shutil.copytree(source/'pages',output/'pages')
        pages=prepared['pages']
        for page in pages:
            page['image_url']=f"/media/{output_id}/pages/{page['number']:04}.png"
        timeline=[];cursor=0;warnings=[]
        for index,(item,duration) in enumerate(zip(items,durations),1):
            choice=item['choice'];scene=dict(item['scene'])
            copied=output/f'clips/{index:04}.mp4';copied.parent.mkdir(exist_ok=True)
            shutil.copy2(item['clip'],copied)
            checked_hash(copied,item['quality']['clip_sha256'])
            audio=output/f'audio/{index:04}.wav';audio.parent.mkdir(exist_ok=True)
            shutil.copy2(item['audio'],audio);checked_hash(audio,choice['audio_sha256'])
            write_json(output/f'agents/quality-{index:04}.json',item['quality'])
            write_json(output/f'clips/{index:04}.source.json',item['sidecar'])
            segment=output/f'segments/{index:04}.mp4'
            page=next(p for p in pages if p['number']==scene['page_number'])
            checked_hash(output/f"pages/{page['number']:04}.png",item['quality']['source_sha256'])
            geometry=await render_segment(output/f"pages/{page['number']:04}.png",segment,duration,settings,
                                           page['image_width'],page['image_height'],audio,copied)
            probe=await probe_media(segment,settings.ffprobe)
            video=next(s for s in probe['streams'] if s.get('codec_type')=='video')
            frame_count=int(video.get('nb_frames') or round(float(video['duration'])*settings.fps))
            if frame_count!=round(duration*settings.fps) or not any(s.get('codec_type')=='audio' for s in probe['streams']):
                raise ValueError('镜头帧数或配音轨与预期不符。')
            quality=validate_video_quality(item['quality'])
            scene.update(index=index-1,start=cursor/settings.fps,end=(cursor+frame_count)/settings.fps,
                         duration=frame_count/settings.fps,transform=geometry,quality=quality,
                         audio_duration=prepared['audio_lengths'][index-1],has_narration=True,
                         audio_url=f'/media/{output_id}/audio/{index:04}.wav',rendering=choice['rendering'])
            cursor+=frame_count
            if quality['decision']=='needs_review':
                warnings.append(f"第 {page['number']} 页采样核对仍有不确定项，待人工复核。")
            timeline.append(scene)
            audit['scenes'].append({'scene':index,'page_number':page['number'],'start':scene['start'],'end':scene['end'],
                'segment_sha256':sha256_file(segment),'clip_sha256':sha256_file(copied),'audio_sha256':sha256_file(audio),
                'source_audio_path':str(item['audio'].relative_to(source.resolve())),
                'audio_duration':scene['audio_duration'],'rendering':choice['rendering'],'quality':quality['decision'],
                'reason':choice['reason']})
            write_json(output/'recovery.json',audit)
            if progress:progress(index,len(items))
        target=output/'book.mp4'
        probe=await concatenate_segments([output/f'segments/{i:04}.mp4' for i in range(1,len(items)+1)],target,settings)
        metadata=check_video_metadata(probe)
        video=next(s for s in probe['streams'] if s.get('codec_type')=='video')
        if metadata['audio_tracks']!=1 or int(video.get('nb_frames',cursor))!=cursor:
            raise ValueError('全书音轨或帧数校验未通过。')
        fallback=prepared['fallback_pages']
        if fallback:
            warnings.insert(0,'全书测试版：第 '+ '、'.join(map(str,fallback))+' 页采用原图保真动效替换失真片段；这些页不是生成式角色动画。其余页复用云端生成动画。')
        warnings.append('本片复用原书理解、原有配音及部分动画；不是一次冷启动生成。自动采样不替代人工内容与全片审核。')
        title=str(prepared['story'].get('title') or '绘本')
        book={'schema_version':SCHEMA_VERSION,'id':output_id,'title':'[全书测试版] '+title,
            'language':detect_language(pages),'pages':pages,'scenes':timeline,'duration':cursor/settings.fps,
            'video_url':f'/media/{output_id}/book.mp4','video_metadata':metadata,
            'mode':'hybrid_test' if fallback else 'cloud','review':prepared['review'],'warnings':warnings,
            'summary':prepared['story'].get('summary',''),'character_bible':prepared['story'].get('character_bible',''),
            'provenance':{'execution':'explicit_saved_media_recovery','source_book_id':source.name,
                'source_pdf_sha256':prepared['manifest']['source_pdf_sha256'],
                'story_sha256':prepared['manifest']['story_sha256'],'single_pipeline_run':False,
                'output_sha256':sha256_file(target),'generative_video_used':any(i['choice']['rendering']=='generative_video' for i in items),
                'source_motion_pages':fallback,'audio_reused':True,'new_model_submissions':0,'manual_content_review':'pending',
                'original_failed_run_unchanged':True,'timeline_frames':cursor,'recovery_audit':'recovery.json'}}
        audit.update(status='assembled_pending_human_review',video_metadata=metadata,output_sha256=sha256_file(target))
        checked_hash(source/'input.pdf',prepared['manifest']['source_pdf_sha256'])
        checked_hash(prepared['story_path'],prepared['manifest']['story_sha256'])
        write_json(output/'video_probe.json',probe);write_json(output/'recovery.json',audit)
        # Publish only after every selected clip, complete audio and final media are valid.
        write_json(output/'book.json',book)
        return book
    except BaseException as error:
        audit.update(status='failed',error_type=type(error).__name__)
        write_json(output/'recovery.json',audit)
        raise
