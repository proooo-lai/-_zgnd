"""Original synthetic sources only; no competition assets or paid services."""
import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageChops, ImageStat

from storybook.pipeline import merge_visual_page, prepare_video_reference, inspect_video_quality
from storybook.settings import Settings


def fixture_page(tmp_path):
    source = tmp_path/'source.png'
    image = Image.new('RGB',(600,400),'white')
    draw=ImageDraw.Draw(image)
    draw.text((40,40),'ORIGINAL',fill='black')
    draw.rectangle((420,280,438,296),fill='red')
    image.save(source)
    reference=tmp_path/'reference.png'
    transform=prepare_video_reference(source,reference,640,360)
    page={'number':1,'image_width':600,'image_height':400,
          'words':[{'text':'ORIGINAL','bbox':[.06,.09,.15,.05],'source':'pdf_text_layer'}],
          'objects':[{'id':'prop','label':'original red prop','bbox':[.70,.70,.03,.04],'confidence':.95},
                     {'id':'large','label':'large figure','bbox':[.3,.3,.3,.4],'confidence':.95},
                     {'id':'uncertain','label':'uncertain prop','bbox':[.9,.8,.02,.02],'confidence':.3}]}
    return source,reference,transform,page


def test_native_text_geometry_does_not_discard_visual_only_handwriting():
    page={'number':1,'words':[{'text':'Printed sentence.','bbox':[.1,.1,.3,.04],'source':'pdf_text_layer'}],
          'objects':[],'native_text':'Printed sentence.'}
    visual={'text':'Printed sentence. Handwritten note.','description':'An original fictional page.',
            'words':[{'text':'Printed','bbox':[.1,.1,.1,.04]},
                     {'text':'Handwritten note.','bbox':[.5,.6,.25,.07]},
                     {'text':'Watermark','bbox':[.3,.95,.4,.02]}]}
    merged=merge_visual_page(page,visual)
    assert any(w['text']=='Handwritten note.' and w['source']=='vision' for w in merged['words'])
    assert not any(w['text']=='Watermark' for w in merged['words'])
    assert page['words'][0]['text']=='Printed sentence.' and merged['native_text']==page['native_text']
    assert len(merged['visual_words'])==3  # Raw visual geometry remains distinct from confirmed story words.


@pytest.mark.parametrize('text,word',[("Handwritten\nnote.","Handwritten note."),
                                      ("The child’s note.","child's note.")])
def test_story_word_matching_tolerates_layout_and_quote_variants_without_changing_raw_text(text,word):
    page={'number':1,'words':[{'text':'Printed','bbox':[.1,.1,.2,.05],'source':'pdf_text_layer'}],
          'objects':[],'native_text':'Printed'}
    visual={'text':'Printed. '+text,'description':'Original synthetic page.',
            'words':[{'text':word,'bbox':[.5,.6,.3,.05]}]}
    merged=merge_visual_page(page,visual)
    assert any(w['text']==word for w in merged['words'])
    assert merged['text']==visual['text'] and merged['native_text']=='Printed'


def test_protection_overlay_maps_original_pixels_and_keeps_large_motion_region_clear(tmp_path):
    from storybook.fidelity import prepare_protection_overlay
    source,reference,transform,page=fixture_page(tmp_path)
    overlay=tmp_path/'overlay.png'
    plan=prepare_protection_overlay(reference,page,transform,overlay,(640,360))
    assert plan['status']=='planned' and 0 < plan['coverage_fraction'] < .25
    assert {r['kind'] for r in plan['regions']}=={'source_text','small_object'}
    assert {r['source_id'] for r in plan['regions']} >= {'prop'}
    assert not any(r['source_id'] in ('large','uncertain') for r in plan['regions'])
    with Image.open(overlay) as patch, Image.open(reference) as original:
        # Page (429,288) after contain pad: scale .9, offset (50,0).
        assert patch.getpixel((436,259))[:3]==original.getpixel((436,259)) and patch.getpixel((436,259))[3]==255
        assert patch.getpixel((300,180))[3]==0
    assert plan['mapping_requires_quality_review'] is True


def test_protection_refuses_broad_or_invalid_mapping_instead_of_static_replacement(tmp_path):
    from storybook.fidelity import prepare_protection_overlay
    _,reference,transform,page=fixture_page(tmp_path)
    page['words']=[{'text':'synthetic invalid broad region','bbox':[0,0,1,1]}]
    with pytest.raises(ValueError,match='保护区域'):
        prepare_protection_overlay(reference,page,transform,tmp_path/'broad.png',(640,360))
    with pytest.raises(ValueError,match='参考图'):
        prepare_protection_overlay(reference,page,{**transform,'canvas_size':[1280,720]},tmp_path/'bad.png',(640,360))


def test_encoded_video_preserves_source_details_without_freezing_unprotected_motion(tmp_path):
    from storybook.fidelity import protect_video_details
    from storybook.video import run_process, probe_media, media_duration
    settings=Settings(data_dir=tmp_path/'data',config_file=tmp_path/'absent.json')
    source,reference,transform,page=fixture_page(tmp_path)
    clip=tmp_path/'raw.mp4'; output=tmp_path/'protected.mp4'
    async def scenario():
        await run_process([settings.ffmpeg,'-v','error','-y','-f','lavfi','-i',
                           'testsrc2=size=640x360:rate=24:duration=1','-c:v','libx264','-pix_fmt','yuv420p',str(clip)])
        original=clip.read_bytes()
        plan=await protect_video_details(reference,clip,output,page,transform,settings)
        assert clip.read_bytes()==original
        assert plan['status']=='composited' and plan['raw_clip_sha256']!=plan['output_sha256']
        assert json.loads(output.with_suffix('.protection.json').read_text())['output_sha256']==plan['output_sha256']
        assert media_duration(await probe_media(output,settings.ffprobe))==pytest.approx(1,abs=.05)
        frames=[]
        for index,second in enumerate((0,.8)):
            frame=tmp_path/f'frame-{index}.png'
            await run_process([settings.ffmpeg,'-v','error','-y','-ss',str(second),'-i',str(output),'-frames:v','1',str(frame)])
            with Image.open(frame) as im: frames.append(im.convert('RGB').copy())
        # Protected red prop is stable despite generated test-pattern motion.
        for frame in frames:
            r,g,b=frame.getpixel((436,259)); assert r>190 and g<70 and b<70
        moving=ImageChops.difference(frames[0].crop((220,130,400,230)),frames[1].crop((220,130,400,230)))
        assert sum(ImageStat.Stat(moving).mean)>10
    asyncio.run(scenario())


@pytest.mark.parametrize('alignment',[False,'true',None])
def test_postprocessed_quality_requires_explicit_alignment_and_checks_actual_output(tmp_path,monkeypatch,alignment):
    source=tmp_path/'source.png'; Image.new('RGB',(100,100),'white').save(source)
    clip=tmp_path/'protected.mp4'; clip.write_bytes(b'processed pixels')
    async def extract(args,timeout):
        assert args[args.index('-i')+1]==str(clip)
        Image.new('RGB',(100,100),'green').save(Path(args[-1]))
    class Verifier:
        async def vision(self,prompt,images):
            assert '重影' in prompt and '保护' in prompt
            return {'consistent':True,'issues':[],'uncertain':False,'protection_aligned':alignment}
    monkeypatch.setattr('storybook.pipeline.run_process',extract)
    quality=asyncio.run(inspect_video_quality(source,clip,1,Verifier(),
        type('S',(),{'ffmpeg':'ffmpeg','fps':24})(),tmp_path/'book',0,
        protection={'status':'composited','regions':[]}))
    assert quality['decision']=='rejected'


@pytest.mark.parametrize('raw_decision',['sampled_consistent','needs_review'])
def test_acceptable_original_is_never_changed_by_a_repair_attempt(tmp_path,monkeypatch,raw_decision):
    from storybook.pipeline import review_clip_with_protection
    async def check(*args,**kwargs):
        return {'decision':raw_decision,'issues':[],'consistent':True,'uncertain':raw_decision=='needs_review'}
    async def never_modify(*args,**kwargs):
        raise AssertionError('Passing raw footage must remain unchanged')
    monkeypatch.setattr('storybook.pipeline.inspect_video_quality',check)
    monkeypatch.setattr('storybook.pipeline.protect_video_details',never_modify)
    raw=tmp_path/'raw.mp4'
    clip,audit=asyncio.run(review_clip_with_protection(tmp_path/'page.png',tmp_path/'ref.png',raw,
        {'number':1},{},object(),object(),tmp_path,0,duration=1))
    assert clip==raw and audit['quality']['decision']==raw_decision
    assert audit['detail_protection']['status']=='not_required'


def test_repair_quality_failure_is_not_a_success_or_a_paid_retry(tmp_path,monkeypatch):
    from storybook.pipeline import review_clip_with_protection,require_video_quality
    calls=[]
    async def check(source,clip,*args,**kwargs):
        calls.append((clip,kwargs))
        return {'decision':'rejected','issues':[{'severity':'major','description':'Synthetic mismatch'}],
                'consistent':False,'uncertain':False}
    async def protect(reference,clip,output,*args):
        return {'status':'composited','output_sha256':'synthetic hash'}
    monkeypatch.setattr('storybook.pipeline.inspect_video_quality',check)
    monkeypatch.setattr('storybook.pipeline.protect_video_details',protect)
    raw=tmp_path/'raw.mp4'
    clip,audit=asyncio.run(review_clip_with_protection(tmp_path/'page.png',tmp_path/'ref.png',raw,
        {'number':1},{},object(),object(),tmp_path,0,duration=1))
    assert len(calls)==2 and calls[0][0]==raw and calls[1][0]!=raw
    assert calls[1][1]['protection']['status']=='composited'
    assert audit['raw_quality']['decision']=='rejected'
    with pytest.raises(ValueError,match='未自动重试'):
        require_video_quality(audit['quality'],1)


def test_default_cloud_pipeline_rejects_raw_mismatch_without_repair_or_publish(tmp_path,monkeypatch):
    import pymupdf
    from storybook.pipeline import build_book
    document=pymupdf.open();document.new_page(width=160,height=90).insert_text((10,20),'Original square.')
    pdf=tmp_path/'original.pdf';document.save(pdf);document.close()
    calls=[]
    class Cloud:
        def available(self,name): return True
        async def vision(self,prompt,images):
            return {'text':'Original square.','description':'An original square.','objects':[],'words':[]}
        async def chat(self,prompt):
            return {'title':'Original synthetic book','scenes':[{'page_number':1,'narration':'Original square.','video_prompt':'Small motion.'}]}
        async def tts(self,text,language,output):
            output.write_bytes(b'synthetic audio');return output
        async def generate_clip(self,reference,prompt,output,duration):
            calls.append('generate_once');output.write_bytes(b'synthetic generated raw');return output
    async def probe(*args):return {'format':{'duration':'1'}}
    async def quality(*args,**kwargs):
        calls.append('review_raw');return {'decision':'rejected','consistent':False,'uncertain':False,'issues':[]}
    async def forbidden(*args,**kwargs):
        raise AssertionError('Default pipeline must not run experimental repair, encode or publish rejected footage')
    monkeypatch.setattr('storybook.pipeline.probe_media',probe)
    monkeypatch.setattr('storybook.pipeline.inspect_video_quality',quality)
    for name in ('protect_video_details','review_clip_with_protection','render_segment','concatenate_segments'):
        monkeypatch.setattr('storybook.pipeline.'+name,forbidden)
    settings=Settings(data_dir=tmp_path/'data',config_file=tmp_path/'absent.json')
    with pytest.raises(ValueError,match='未自动重试'):
        asyncio.run(build_book(pdf,tmp_path/'run',Cloud(),settings,mode='cloud'))
    assert calls==['generate_once','review_raw']
    assert not (tmp_path/'run/book.json').exists() and not (tmp_path/'run/book.mp4').exists()


def test_diagnostic_identifier_validation_never_initializes_the_source_library(tmp_path,monkeypatch):
    from tools.inspect_saved_video_fidelity import inspect_saved
    from types import SimpleNamespace
    monkeypatch.setattr('tools.inspect_saved_video_fidelity.Settings',lambda:SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ValueError,match='无效的标识符'):
        asyncio.run(inspect_saved('bad/id',1))
    assert list(tmp_path.iterdir())==[]
