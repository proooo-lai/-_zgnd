"""Text must exist in sampled video pixels, not merely in source-page OCR."""
import asyncio
import json
from types import SimpleNamespace

from PIL import Image
import pytest

from storybook.interaction import Interactor
from storybook.storage import Store
from tests.test_interaction import make_book, ScriptedProvider, encoded_frame


def test_quoted_text_requests_route_to_video_pixels_not_story_answers(tmp_path,monkeypatch):
    from storybook.video_text import VideoTextLocator
    store=Store(tmp_path);book=make_book(store,generated=True)
    provider=ScriptedProvider(enabled=('vision',))
    observed=[]
    async def search(self,book,request,timestamp,folder):
        observed.append(request.model_dump())
        return {'answer':'Located in the actual video.','uncertain':False,'boxes':[],
                'text_location':{'status':'found','action':request.action,'target_text':request.target_text}}
    monkeypatch.setattr(VideoTextLocator,'search',search)
    for question,action in [('找到视频中的“ORIGINAL WORD”','locate_text'),('高亮“ORIGINAL WORD”','highlight_text')]:
        result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic',question,'s1',12,encoded_frame()))
        assert result['text_location']['action']==action
    assert [r['target_text'] for r in observed]==['ORIGINAL WORD']*2
    assert provider.calls==[]


def search_fixture(tmp_path,reply,verification):
    from storybook.video_text import VideoTextLocator,TextRequest
    store=Store(tmp_path);book=make_book(store,generated=True)
    provider=ScriptedProvider([reply,verification],enabled=('vision',))
    times=[]
    async def frame(book,timestamp,output,uploaded=None):
        times.append(timestamp);Image.new('RGB',(600,400),'white').save(output);return output
    locator=VideoTextLocator(provider,frame)
    folder=store.directory('synthetic')/'turns/test';folder.mkdir(parents=True)
    result=asyncio.run(locator.search(book,TextRequest(action='highlight_text',target_text='apple'),0,folder))
    return result,times,provider,folder


def candidate(text='apple',box=None):
    return {'uncertain':False,'matches':[{'frame_index':1,'text':text,'bbox_1000':box or [300,400,500,500],'confidence':.96}]}


def verified(text='apple',visible=True):
    return {'visible':visible,'uncertain':not visible,'text':text,'bbox_1000':[300,400,500,500],'confidence':.96}


def test_actual_frame_time_and_verified_geometry_drive_location_not_scene_start(tmp_path):
    result,times,provider,folder=search_fixture(tmp_path,candidate(),verified())
    location=result['text_location']
    assert location['status']=='found' and location['timestamp']==times[1]
    assert result['seek_time']==times[1] and times[1] > 1
    assert result['boxes'][0]['bbox']==pytest.approx([.3,.4,.2,.1])
    assert result['boxes'][0]['presentation']=='highlight'
    assert location['frame_sha256'] and location['frame_size']==[600,400]
    assert result['evidence'][0]['source']=='verified_video_frame'
    assert '不是原书' in provider.calls[0]['prompt']
    assert len(provider.calls)==2


@pytest.mark.parametrize('reply,review',[
    (candidate(text='apples'),verified()),
    (candidate(box=[.3,.4,.5,.5]),verified()),
    (candidate(),verified(visible=False)),
    (candidate(),verified(text='ample')),
])
def test_wrong_text_or_geometry_never_becomes_a_location(tmp_path,reply,review):
    # Limit the book to one scene: a miss is a bounded sample result, not a whole-video assertion.
    from storybook.video_text import VideoTextLocator,TextRequest
    store=Store(tmp_path);book=make_book(store,generated=True);book['scenes']=book['scenes'][:1]
    provider=ScriptedProvider([reply,review],enabled=('vision',))
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(600,400),'white').save(output);return output
    folder=store.directory('synthetic')/'turns/test';folder.mkdir(parents=True)
    result=asyncio.run(VideoTextLocator(provider,frame).search(book,TextRequest(action='locate_text',target_text='apple'),0,folder))
    assert result['uncertain'] and result['boxes']==[] and 'seek_time' not in result
    assert result['text_location']['status']=='not_confirmed'
    assert result['text_location']['search_scope']=='sampled_video_frames_not_exhaustive'


def test_source_text_without_video_text_is_not_a_positive_match(tmp_path):
    from storybook.video_text import VideoTextLocator,TextRequest
    store=Store(tmp_path);book=make_book(store,generated=True)
    provider=ScriptedProvider([{'matches':[],'uncertain':False}]*2,enabled=('vision',))
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(600,400),'white').save(output);return output
    folder=store.directory('synthetic')/'turns/test';folder.mkdir(parents=True)
    result=asyncio.run(VideoTextLocator(provider,frame).search(book,TextRequest(action='locate_text',target_text='apple'),0,folder))
    assert result['text_location']['status']=='not_confirmed' and 'seek_time' not in result
    assert result['text_location']['sampled_frames']>=6


def test_ordinary_fact_and_object_search_are_not_text_intents():
    from storybook.video_text import quoted_request,maybe_text_request
    assert quoted_request('What color is the apple?') is None
    assert not maybe_text_request('Find the elephant')
    assert not maybe_text_request('这个字是什么意思？')
    assert maybe_text_request('帮我找到这句话在哪里')


def test_request_parser_cannot_invent_target_text_not_present_in_query():
    from storybook.video_text import validate_text_request
    assert validate_text_request({'action':'locate_text','target_text':'secret answer'},'请找到这句话') is None


def test_plain_single_quotes_are_an_explicit_text_search():
    from storybook.video_text import quoted_request
    request=quoted_request("find 'a red apple'")
    assert request and request.target_text=='a red apple'


def test_possessive_apostrophe_is_not_a_text_search_marker():
    from storybook.video_text import maybe_text_request
    assert not maybe_text_request("Find the bird's house")


def test_word_navigation_never_speaks_an_unlocated_story_draft(tmp_path,monkeypatch):
    from storybook.video_text import VideoTextLocator
    class Speech(ScriptedProvider):
        async def tts(self,text,language,output,voice=None):
            spoken.append(text);output.write_bytes(b'original-test-audio');return output
    spoken=[]
    provider=Speech([{'answer':'Here is the apple.','evidence_scope':'book','evidence_pages':[1],
        'uncertain':False,'seek_page':1,'target_label':'apple','target_kind':'word'}],enabled=('chat','tts'))
    store=Store(tmp_path);make_book(store,generated=True)
    async def miss(self,*args):
        return {'answer':'The sampled video frames do not confirm the word.','uncertain':True,'boxes':[],'evidence':[]}
    async def emit(*args): pass
    monkeypatch.setattr(VideoTextLocator,'search',miss)
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic','Jump to apple','main',emit=emit))
    assert spoken==[result['answer']]
    assert result['answer']=='The sampled video frames do not confirm the word.'


def test_fallback_cannot_search_a_word_the_user_never_requested(tmp_path,monkeypatch):
    from storybook.video_text import VideoTextLocator
    calls=[]
    async def search(self,*args): calls.append(True);return {}
    monkeypatch.setattr(VideoTextLocator,'search',search)
    store=Store(tmp_path);make_book(store,generated=True)
    provider=ScriptedProvider([{'answer':'Here is the apple.','evidence_scope':'book','evidence_pages':[1],
        'uncertain':False,'seek_page':1,'target_label':'banana','target_kind':'word'}])
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic','Jump to apple','main'))
    assert not calls and result['uncertain'] and 'seek_time' not in result


def test_review_budget_stops_further_model_calls(tmp_path):
    from storybook.video_text import VideoTextLocator,TextRequest
    store=Store(tmp_path);book=make_book(store,generated=True)
    book['scenes']=[{**book['scenes'][0],'start':n*10,'end':(n+1)*10} for n in range(6)]
    provider=ScriptedProvider([reply for _ in range(6) for reply in (candidate(),verified(visible=False))],enabled=('vision',))
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(600,400),'white').save(output);return output
    folder=store.directory('synthetic')/'turns/test';folder.mkdir(parents=True)
    result=asyncio.run(VideoTextLocator(provider,frame).search(book,TextRequest(action='locate_text',target_text='apple'),0,folder))
    assert result['uncertain'] and len(provider.calls)==6
    assert json.loads((folder/'text-location/search.json').read_text())['stop_reason']=='review_budget'


def test_english_word_boundaries_cannot_be_removed_to_force_a_match(tmp_path):
    from storybook.video_text import VideoTextLocator,TextRequest,text_key
    assert text_key('now here') != text_key('nowhere')
    assert text_key('星星\n亮了') == text_key('星星亮了')
    store=Store(tmp_path);book=make_book(store,generated=True);book['scenes']=book['scenes'][:1]
    provider=ScriptedProvider([candidate('nowhere'),verified('nowhere')],enabled=('vision',))
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(600,400),'white').save(output);return output
    result=asyncio.run(VideoTextLocator(provider,frame).search(book,TextRequest(action='locate_text',target_text='now here'),0,store.directory('synthetic')/'turns/test'))
    assert result['uncertain'] and result['boxes']==[] and 'seek_time' not in result


def test_cancelled_video_search_records_cancellation_without_a_false_match(tmp_path):
    from storybook.video_text import VideoTextLocator,TextRequest
    class Waiting(ScriptedProvider):
        async def vision(self,prompt,images):
            entered.set()
            await asyncio.Event().wait()
    store=Store(tmp_path);book=make_book(store,generated=True)
    folder=store.directory('synthetic')/'turns/test'
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(100,100),'white').save(output);return output
    async def cancel():
        nonlocal entered
        entered=asyncio.Event()
        task=asyncio.create_task(VideoTextLocator(Waiting(enabled=('vision',)),frame).search(
            book,TextRequest(action='locate_text',target_text='apple'),0,folder))
        await entered.wait();task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
    entered=None;asyncio.run(cancel())
    audit=json.loads((folder/'text-location/search.json').read_text())
    assert audit['status']=='cancelled' and 'location' not in audit


def test_vision_service_timeout_is_an_unconfirmed_search_not_a_positive(tmp_path):
    from storybook.video_text import VideoTextLocator,TextRequest
    class Failed(ScriptedProvider):
        async def vision(self,prompt,images):raise TimeoutError()
    store=Store(tmp_path);book=make_book(store,generated=True)
    async def frame(book,timestamp,output,uploaded=None):
        Image.new('RGB',(100,100),'white').save(output);return output
    folder=store.directory('synthetic')/'turns/test'
    result=asyncio.run(VideoTextLocator(Failed(enabled=('vision',)),frame).search(
        book,TextRequest(action='locate_text',target_text='apple'),0,folder))
    assert result['uncertain'] and not result['success'] and result['boxes']==[] and 'seek_time' not in result
    assert json.loads((folder/'text-location/search.json').read_text())['error_type']=='TimeoutError'
