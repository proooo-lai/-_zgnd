"""Named location questions must locate real video pixels, not source-page prose."""
import asyncio
from types import SimpleNamespace

from PIL import Image
import pytest

from storybook.interaction import Interactor
from storybook.storage import Store
from tests.test_interaction import make_book, ScriptedProvider


def test_named_object_request_is_distinct_from_literal_text_and_fact_questions():
    from storybook.video_objects import object_request
    for question,target in [('大象在哪里','大象'),('大象在哪儿？','大象'),('请帮我找到小兔子','小兔子'),
                            ('Where is the elephant?','elephant'),('Find the red apple','red apple'),
                            ('Show me the elephant','elephant'),('Point to the elephant','elephant')]:
        assert object_request(question)==target
    for question in ['高亮“大象”','找到文字“大象”','大象为什么走了','大象在第几页','他在哪里',
                     'What color is the apple?','大象在哪里吃饭','故事里大象在哪里？']:
        assert object_request(question) is None


def test_user_location_query_does_not_fall_through_to_book_question_answering(tmp_path,monkeypatch):
    from storybook.video_objects import VideoObjectLocator
    store=Store(tmp_path);make_book(store,generated=True)
    seen=[]
    async def search(self,book,target,timestamp,folder):
        seen.append(target)
        return {'answer':'已框选视频中的目标。','uncertain':False,'boxes':[], 'object_location':{'status':'found'}}
    monkeypatch.setattr(VideoObjectLocator,'search',search)
    provider=ScriptedProvider(enabled=('vision',))
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic','大象在哪里','s1',12))
    assert seen==['大象'] and result['object_location']['status']=='found' and provider.calls==[]


def run_search(tmp_path,review=True,candidate=None):
    from storybook.video_objects import VideoObjectLocator
    store=Store(tmp_path);book=make_book(store,generated=True);book['scenes']=book['scenes'][:1]
    candidate=candidate if candidate is not None else {'matches':[{'frame_index':0,'label':'大象',
        'bbox_1000':[530,180,970,680],'confidence':.95}], 'uncertain':False}
    provider=ScriptedProvider([candidate,{'supported':review,'uncertain':not review,'reason':'Synthetic frame review.'}],enabled=('vision',))
    times=[]
    async def frame(book,timestamp,output,uploaded=None):
        times.append(timestamp);Image.new('RGB',(600,400),'white').save(output);return output
    folder=store.directory('synthetic')/'turns/test';folder.mkdir(parents=True)
    result=asyncio.run(VideoObjectLocator(provider,frame).search(book,'大象',12,folder))
    return result,times,provider,folder


def test_verified_actual_frame_is_paused_and_boxed_at_the_requested_time(tmp_path):
    result,times,provider,folder=run_search(tmp_path)
    location=result['object_location']
    assert not result['uncertain'] and location['status']=='found'
    assert result['seek_time']==times[0]==12
    assert result['boxes'][0]['bbox']==pytest.approx([.53,.18,.44,.5])
    assert result['boxes'][0]['kind']=='object' and result['boxes'][0]['source']=='verified_video_frame'
    assert location['frame_size']==[600,400] and location['frame_sha256']
    assert '/object-location/' in location['frame_url']
    assert result['evidence'][0]['source']=='verified_video_frame'
    assert len(provider.calls)==2


@pytest.mark.parametrize('candidate',[
    {'matches':[], 'uncertain':False},
    {'matches':[{'frame_index':0,'label':'大象','bbox_1000':[.5,.2,.9,.7],'confidence':.9}], 'uncertain':False},
    {'matches':[{'frame_index':8,'label':'大象','bbox_1000':[500,200,900,700],'confidence':.9}], 'uncertain':False},
])
def test_missing_or_invalid_video_match_never_becomes_a_location(tmp_path,candidate):
    result,*_=run_search(tmp_path,candidate=candidate)
    assert result['uncertain'] and result['boxes']==[] and 'seek_time' not in result
    assert result['object_location']['status']=='not_confirmed'


def test_object_candidate_must_pass_independent_frame_and_crop_review(tmp_path):
    result,*_=run_search(tmp_path,review=False)
    assert result['uncertain'] and result['boxes']==[] and 'seek_time' not in result
