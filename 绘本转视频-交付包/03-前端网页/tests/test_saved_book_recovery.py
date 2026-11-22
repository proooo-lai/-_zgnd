"""Saved media may be reused only with matching evidence, audio and quality."""
import asyncio
import json
import shutil
import wave

import pytest
from PIL import Image

from storybook.schema import sha256_file, write_json


def fixture(tmp_path):
    from storybook.settings import Settings
    settings=Settings(data_dir=tmp_path/'data',config_file=tmp_path/'none.json')
    source=settings.data_dir/'source';source.mkdir()
    (source/'input.pdf').write_bytes(b'original synthetic input')
    page={'number':1,'text':'A green bird.','description':'A bird.','kind':'story',
          'image_width':120,'image_height':80,'image_url':'/media/source/pages/0001.png','words':[],'objects':[]}
    (source/'pages').mkdir();Image.new('RGB',(120,80),'green').save(source/'pages/0001.png')
    write_json(source/'agents/ocr.json',[page]);write_json(source/'agents/vision-0001.json',
        {'text':page['text'],'description':page['description'],'kind':'story','objects':[],'words':[]})
    write_json(source/'agents/story.json',{'title':'Original bird story','scenes':[{'page_number':1,'narration':'A green bird.'}]})
    audio=source/'audio/0001.wav';audio.parent.mkdir()
    with wave.open(str(audio),'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\x20\x10'*2400)
    clip=source/'raw_clips/0001.mp4';clip.parent.mkdir();clip.write_bytes(b'synthetic encoded boundary')
    write_json(clip.with_suffix('.task.json'),{'status':'succeeded','output_sha256':sha256_file(clip)})
    quality=source/'agents/video-quality-0001.json'
    write_json(quality,{'consistent':True,'uncertain':False,'issues':[],
                        'source_sha256':sha256_file(source/'pages/0001.png'),'clip_sha256':sha256_file(clip)})
    manifest={'source_book_id':'source','source_pdf_sha256':sha256_file(source/'input.pdf'),
              'story_sha256':sha256_file(source/'agents/story.json'),
              'choices':[{'scene':1,'clip_path':str(clip),'quality_path':str(quality),
                'audio_sha256':sha256_file(audio),'rendering':'generative_video','reason':'Reuse verified original.'}]}
    return settings,source,manifest


def test_rejected_clip_cannot_be_assembled_or_published(tmp_path):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path)
    p=source/'agents/video-quality-0001.json';quality=json.loads(p.read_text())
    quality.update(consistent=False,issues=[{'severity':'major','description':'Original object disappeared.'}]);write_json(p,quality)
    with pytest.raises(ValueError,match='质量'):
        prepare_recovery(settings,manifest,'output')
    assert not (settings.data_dir/'output').exists()


@pytest.mark.parametrize('field',['input','story','audio','clip'])
def test_changed_inputs_prevent_reuse_before_writing_output(tmp_path,field):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path)
    paths={'input':'input.pdf','story':'agents/story.json','audio':'audio/0001.wav','clip':'raw_clips/0001.mp4'}
    path=source/paths[field];path.write_bytes(path.read_bytes()+b'changed')
    with pytest.raises(ValueError):prepare_recovery(settings,manifest,'output')
    assert not (settings.data_dir/'output').exists()


def test_source_motion_requires_explicit_opt_in_and_matching_provenance(tmp_path):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path);manifest['choices'][0]['rendering']='source_preserved_motion'
    with pytest.raises(ValueError,match='保真'):prepare_recovery(settings,manifest,'output')
    clip=source/'raw_clips/0001.mp4'
    write_json(clip.with_suffix('.motion.json'),{'version':'source-preserved-motion-v1','status':'completed',
        'source_sha256':sha256_file(source/'pages/0001.png'),'output_sha256':sha256_file(clip)})
    prepared=prepare_recovery(settings,manifest,'output',allow_source_motion=True)
    assert prepared['fallback_pages']==[1] and prepared['audio_lengths']==[.1]


def test_recovery_cannot_overwrite_source_or_a_completed_book(tmp_path):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path)
    with pytest.raises(ValueError):prepare_recovery(settings,manifest,'source')
    (settings.data_dir/'output').mkdir()
    with pytest.raises(ValueError):prepare_recovery(settings,manifest,'output')


def test_manifest_must_cover_every_scene_once_in_order(tmp_path):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path);manifest['choices']*=2
    with pytest.raises(ValueError):prepare_recovery(settings,manifest,'output')


def test_recovery_can_explicitly_select_matching_shortened_narration(tmp_path):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path)
    shortened=source/'agents/story-shortened-1.json'
    story=json.loads((source/'agents/story.json').read_text())
    story['scenes'][0]['narration']='A bird.'
    write_json(shortened,story)
    audio=source/'audio/attempt-2/0001.wav';audio.parent.mkdir()
    shutil.copy2(source/'audio/0001.wav',audio)
    manifest.update(story_path='agents/story-shortened-1.json',story_sha256=sha256_file(shortened))
    manifest['choices'][0]['audio_path']='audio/attempt-2/0001.wav'
    prepared=prepare_recovery(settings,manifest,'output')
    assert prepared['story']['scenes'][0]['narration']=='A bird.'
    assert prepared['items'][0]['audio']==audio.resolve()


@pytest.mark.parametrize('field',['story_path','audio_path'])
def test_selected_narration_paths_cannot_escape_source(tmp_path,field):
    from storybook.recovery import prepare_recovery
    settings,source,manifest=fixture(tmp_path)
    target=manifest if field=='story_path' else manifest['choices'][0]
    target[field]='../outside.json' if field=='story_path' else '../outside.wav'
    with pytest.raises(ValueError,match='目录'):
        prepare_recovery(settings,manifest,'output')


def test_source_motion_keeps_full_page_and_marks_dynamic_geometry(tmp_path):
    from storybook.video import render_source_motion, probe_media
    from storybook.settings import Settings
    settings=Settings(data_dir=tmp_path/'data',config_file=tmp_path/'none.json')
    source=tmp_path/'original.png';Image.new('RGB',(500,800),'green').save(source)
    output=tmp_path/'motion.mp4'
    audit=asyncio.run(render_source_motion(source,output,1,settings))
    assert audit['status']=='completed' and audit['source_sha256']==sha256_file(source)
    assert audit['source_rect'][2]<settings.width and audit['source_rect'][3]<settings.height
    assert audit['requires_frame_grounding'] and audit['generative_video_used'] is False
    probe=asyncio.run(probe_media(output,settings.ffprobe))
    assert next(s for s in probe['streams'] if s['codec_type']=='video')['width']==1280


def test_assembled_recovery_has_audio_aligned_timeline_and_disclosed_fallback(tmp_path):
    from storybook.recovery import assemble_recovered_book
    from storybook.video import render_source_motion,probe_media
    settings,source,manifest=fixture(tmp_path);settings.target_duration=100
    clip=source/'raw_clips/0001.mp4';clip.unlink()
    asyncio.run(render_source_motion(source/'pages/0001.png',clip,1,settings))
    quality_path=source/'agents/video-quality-0001.json';quality=json.loads(quality_path.read_text())
    quality['clip_sha256']=sha256_file(clip);write_json(quality_path,quality)
    manifest['choices'][0]['rendering']='source_preserved_motion'
    book=asyncio.run(assemble_recovered_book(settings,manifest,'output',allow_source_motion=True))
    assert book['mode']=='hybrid_test' and book['review']['status']=='pending'
    assert book['provenance']['source_motion_pages']==[1]
    assert book['provenance']['audio_reused'] and not book['provenance']['single_pipeline_run']
    assert book['duration']==100 and book['scenes'][0]['end']==100
    assert book['scenes'][0]['transform']['requires_frame_grounding']
    assert book['scenes'][0]['audio_duration']==.1 and book['warnings']
    probe=asyncio.run(probe_media(settings.data_dir/'output/book.mp4',settings.ffprobe))
    assert sum(s['codec_type']=='audio' for s in probe['streams'])==1
    assert (settings.data_dir/'output/book.json').is_file()
    assert not (source/'book.json').exists()
