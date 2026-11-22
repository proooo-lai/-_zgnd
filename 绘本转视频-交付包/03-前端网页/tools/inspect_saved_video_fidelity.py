"""Explicit local diagnostic of saved video; never generates or publishes video.

Default: local pixel-protection experiment only. --review invokes the configured
vision service on actual frames, preserving independent raw/processed reviews.
All source-derived outputs remain in ignored artifacts/, never the book library.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from PIL import Image

from storybook.fidelity import protect_video_details
from storybook.pipeline import (merge_visual_page,prepare_video_reference,review_clip_with_protection,
                                VisualPageResult,assert_allowed_input)
from storybook.providers import Provider
from storybook.schema import sha256_file,write_json
from storybook.settings import Settings
from storybook.storage import Store
from storybook.video import media_duration,probe_media


async def inspect_saved(book_id,scene_index,review=False):
    settings=Settings()
    Store.identifier(book_id)
    book=assert_allowed_input(settings.data_dir/book_id)
    story=json.loads((book/'agents/story.json').read_text())
    if not 1 <= scene_index <= len(story['scenes']):
        raise ValueError('镜头编号超出已保存故事范围。')
    number=story['scenes'][scene_index-1]['page_number']
    original=next(p for p in json.loads((book/'agents/ocr.json').read_text()) if p['number']==number)
    visual=VisualPageResult.model_validate(json.loads((book/'agents'/f'vision-{number:04}.json').read_text()))
    page=merge_visual_page(original,visual.model_dump())
    source=book/'pages'/f'{number:04}.png'
    raw=book/'raw_clips'/f'{scene_index:04}.mp4'
    task=json.loads(raw.with_suffix('.task.json').read_text())
    if task.get('status') != 'succeeded' or task.get('output_sha256') != sha256_file(raw):
        raise ValueError('原片未确认成功或指纹不匹配，不能进行修复试验。')
    reference=book/'raw_clips'/f'reference-{scene_index:04}.png'
    folder=ROOT/'artifacts'/('fidelity-'+uuid.uuid4().hex)
    folder.mkdir(parents=True)
    with Image.open(reference) as im: width,height=im.size
    transform=prepare_video_reference(source,folder/'reference.png',width,height)
    if sha256_file(reference)!=sha256_file(folder/'reference.png'):
        raise ValueError('重建参考图与生成时保存的参考图不同，不能假定坐标对应。')
    audit={'status':'started','scope':'diagnostic_only_not_published','video_submissions':0,
           'book_id':book_id,'scene_index':scene_index,'vision_review_requested':review}
    write_json(folder/'diagnostic.json',audit)
    provider=None
    try:
        if review:
            provider=Provider(settings)
            duration=media_duration(await probe_media(raw,settings.ffprobe))
            _,result=await review_clip_with_protection(source,reference,raw,page,transform,
                provider,settings,folder,scene_index-1,duration=duration)
            audit.update(status='checked',decision=result['quality']['decision'],
                         protection_status=result['detail_protection']['status'])
        else:
            plan=await protect_video_details(reference,raw,folder/'protected.mp4',page,transform,settings)
            audit.update(status='unreviewed_diagnostic',protection_status=plan['status'])
    except BaseException as error:
        audit.update(status='failed',error_type=type(error).__name__)
        raise
    finally:
        write_json(folder/'diagnostic.json',audit)
        if provider: await provider.close()
    print(json.dumps({'output_folder':str(folder),'status':audit['status'],
                      'decision':audit.get('decision'),'video_submissions':0},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--book-id',required=True)
    parser.add_argument('--scene',type=int,required=True,help='1-based saved scene index')
    parser.add_argument('--review',action='store_true',help='Invoke configured vision review; no video generation')
    args=parser.parse_args()
    asyncio.run(inspect_saved(args.book_id,args.scene,args.review))
