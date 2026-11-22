"""Frame actions and story facts must have independent evidence boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from storybook.interaction import Interactor
from storybook.storage import Store
from tests.test_interaction import make_book, encoded_frame


class SplitProvider:
    def __init__(self, reject=''):
        self.calls=[];self.reject=reject
    def available(self,name):return name in ('vision','chat')
    def mode(self,name):return 'mock'
    async def vision(self,prompt,images):
        self.calls.append(('vision',prompt,list(images)))
        if 'ANSWER_VERIFICATION_V1' in prompt:
            return {'supported':self.reject!='frame','evidence_scope':'current_frame','uncertain':False,'evidence_pages':[1],'reason':'Only actual frame supports this posture.'}
        return {'answer':'苹果在树下。','uncertain':False}
    async def chat(self,prompt):
        self.calls.append(('chat',prompt,[]))
        if 'ANSWER_VERIFICATION_V1' in prompt:
            return {'supported':self.reject!='story','evidence_scope':'book','uncertain':False,'evidence_pages':[2],'reason':'Only original book supports the shared apple.'}
        return {'answer':'小鸟把苹果分享给兔子。','evidence_pages':[2],'uncertain':False}


@pytest.fixture
def exercise(tmp_path):
    store=Store(tmp_path);make_book(store)
    async def run(provider):
        reader=Interactor(store,provider,SimpleNamespace())
        await reader.touch('synthetic',.47,.55,10,'s1')
        return await reader.ask('synthetic','它在干什么？','s1',10,encoded_frame('green'))
    return run


def test_story_followup_routes_frame_and_story_to_separate_models(exercise):
    p=SplitProvider();result=asyncio.run(exercise(p))
    assert result['answer']=='苹果在树下。故事里，小鸟把苹果分享给兔子。'
    assert result['uncertain'] is False
    assert {e['page_number'] for e in result['evidence']}=={1,2}
    vd=[c for c in p.calls if c[0]=='vision' and 'ANSWER_VERIFICATION_V1' not in c[1]]
    cd=[c for c in p.calls if c[0]=='chat' and 'ANSWER_VERIFICATION_V1' not in c[1]]
    assert len(vd)==len(cd)==1
    assert len(vd[0][2])==4
    assert 'The bird shares the apple with a rabbit.' not in vd[0][1]
    assert 'The bird shares the apple with a rabbit.' in cd[0][1]
    vr=next(c for c in p.calls if c[0]=='vision' and 'ANSWER_VERIFICATION_V1' in c[1])
    cr=next(c for c in p.calls if c[0]=='chat' and 'ANSWER_VERIFICATION_V1' in c[1])
    assert len(vr[2])==4
    assert 'A red apple is under the tree.' not in vr[1]
    assert '小鸟把苹果分享给兔子。' not in vr[1]
    assert '小鸟把苹果分享给兔子。' in cr[1]
    assert '苹果在树下。' not in cr[1]
    assert result['evidence_validation']['story_review']['evidence_scope']=='book'


@pytest.mark.parametrize('reject',['frame','story'])
def test_either_failed_component_rejects_combined_followup(exercise,reject):
    p=SplitProvider(reject);result=asyncio.run(exercise(p))
    assert result['uncertain'] is True and result['success'] is False
    assert '苹果在树下' not in result['answer']
    assert not result.get('story_context')


def test_selected_point_is_available_to_both_action_and_action_review(tmp_path):
    store=Store(tmp_path);make_book(store)
    p=SplitProvider();reader=Interactor(store,p,SimpleNamespace())
    async def run():
        await reader.touch('synthetic',.47,.55,10,'s1')
        return await reader.ask('synthetic','它在干什么？','s1',10,encoded_frame('green'))
    result=asyncio.run(run())
    assert result['uncertain'] is False
    calls=[c for c in p.calls if c[0]=='vision']
    assert len(calls)==2
    for _,prompt,images in calls:
        assert '0.47' in prompt and '0.55' in prompt
        assert len(images)==4
        with Image.open(images[1]) as marked:
            px=marked.convert('RGB').getpixel((round(.47*(marked.width-1)),round(.55*(marked.height-1))))
            assert px[0]>180 and px[1]<70


def test_moved_video_does_not_reuse_previous_point_as_current_target(tmp_path):
    from tests.test_story_followup import ReferenceProvider
    store=Store(tmp_path);make_book(store)
    p=ReferenceProvider();reader=Interactor(store,p,SimpleNamespace())
    async def run():
        await reader.touch('synthetic',.47,.55,10,'s1')
        return await reader.ask('synthetic','它是什么？','s1',20,encoded_frame())
    asyncio.run(run())
    assert p.questions[0]['selected_object'] is None


@pytest.mark.parametrize('parent_cancelled', [False, True])
def test_parallel_followup_does_not_leave_sibling_requests_running(parent_cancelled):
    from storybook.followup import joined
    async def run():
        entered = asyncio.Event()
        finished = asyncio.Event()
        async def waiting_request():
            entered.set()
            try:
                await asyncio.Future()
            finally:
                finished.set()
        async def failing_request():
            await entered.wait()
            raise RuntimeError('Model request failed')
        task = asyncio.create_task(joined(waiting_request(), waiting_request() if parent_cancelled else failing_request()))
        await entered.wait()
        if parent_cancelled:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if parent_cancelled else RuntimeError):
            await task
        assert finished.is_set()
        assert not [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    asyncio.run(run())


def test_story_review_cannot_fall_back_to_current_frame_when_chat_becomes_unavailable(tmp_path):
    class ChangingProvider(SplitProvider):
        chat_ready = True
        def available(self, name):
            return name == 'vision' or name == 'chat' and self.chat_ready
        async def chat(self, prompt):
            result = await super().chat(prompt)
            if 'ANSWER_VERIFICATION_V1' not in prompt:
                self.chat_ready = False
            return result
    store=Store(tmp_path);make_book(store)
    p=ChangingProvider();reader=Interactor(store,p,SimpleNamespace())
    async def run():
        await reader.touch('synthetic',.47,.55,10,'s1')
        return await reader.ask('synthetic','它在干什么？','s1',10,encoded_frame())
    result=asyncio.run(run())
    assert result['uncertain'] is True
    reviews=[c for c in p.calls if 'ANSWER_VERIFICATION_V1' in c[1]]
    assert all('story_background' not in c[1] for c in reviews if c[0]=='vision')
