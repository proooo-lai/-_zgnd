"""Click-triggered story background stays in the object's own dialogue."""
import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from storybook.providers import ProviderError
from storybook.schema import write_json
from storybook.server import create_app
from tests.test_server import settings_for
from tests.test_interaction import ScriptedProvider,make_book
from tests.test_object_agents import create
from storybook.providers import ProviderError


def test_background_uses_frozen_target_and_source_evidence_and_is_reused(tmp_path):
    provider=ScriptedProvider([{'answer':'The apple is under the tree.','evidence_scope':'book',
        'evidence_pages':[1],'uncertain':False}],enabled=('chat',))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        child=create(client);agent=child['agent']['id']
        assert child['agent']['background_available'] is True
        path=f'/api/books/synthetic/object-agents/{agent}/background'
        body={'session_id':'main','request_id':str(uuid.uuid4())}
        first=client.post(path,json=body)
        assert first.status_code==200 and not first.json()['uncertain']
        assert first.json()['background_state']=='supported'
        assert first.json()['background_execution']=='live'
        assert first.json()['evidence'][0]['page_number']==1
        second=client.post(path,json={**body,'request_id':str(uuid.uuid4())})
        assert second.status_code==200 and second.json()['background_execution']=='reused_same_object'
        assert second.json()['turn_id']==first.json()['turn_id']
        assert len(provider.calls)==1
        prompt=provider.calls[0]['prompt']
        assert 'selected_object' in prompt and 'apple' in prompt
        context=json.loads(next(line for line in prompt.splitlines() if line.startswith('{"book_title"')))
        assert context['current_time']==10 and context['selected_object']['label']=='apple'
        assert 'original book' in context['question'] and 'story' in context['question']
        assert app.state.store.history('synthetic','main')==[]
        turns=app.state.store.history('synthetic',agent)
        assert len(turns)==2 and turns[-1]['input_kind']=='object_background'
        assert turns[-1]['background_state']=='supported'
        assert client.post(path,json={**body,'session_id':'other','request_id':str(uuid.uuid4())}).status_code==404


def test_background_does_not_invent_unprovided_story_facts(tmp_path):
    provider=ScriptedProvider([{'answer':'Unknown origin.','evidence_scope':'book',
        'evidence_pages':[],'uncertain':True}],enabled=('chat',))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert result['uncertain'] and result['background_state']=='unconfirmed' and result['evidence']==[]
        assert app.state.object_agents.get('synthetic',agent,'main')['status']=='ready'


def test_background_preserves_clicked_visual_reference_as_context_not_story_evidence(tmp_path):
    provider=ScriptedProvider([{'answer':'The apple is under the tree.','evidence_scope':'book',
        'evidence_pages':[1],'uncertain':False}],enabled=('chat',))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())})
        assert result.status_code==200 and result.json()['background_state']=='supported'
        context=json.loads(next(line for line in provider.calls[0]['prompt'].splitlines()
                                if line.startswith('{"book_title"')))
        assert context['selected_object']['visual_reference_only']=='It is red.'
        assert context['book_evidence'][0]['text']=='A red apple is under the tree.'
        assert app.state.store.history('synthetic','main')==[]


def test_old_background_without_current_context_version_is_not_reused(tmp_path):
    provider=ScriptedProvider([{'answer':'The apple is under the tree.','evidence_scope':'book',
        'evidence_pages':[1],'uncertain':False}],enabled=('chat',))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id'];agents=app.state.object_agents
        context=agents.get('synthetic',agent,'main')
        context['background_result']={'answer':'Old missing identity result.','uncertain':True,'evidence':[]}
        write_json(agents.directory('synthetic',agent)/'context.json',context)
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert result['background_execution']=='live' and result['background_state']=='supported'
        assert result['answer']=='The apple is under the tree.'


def test_temporary_verification_failure_can_be_retried_for_the_same_object(tmp_path):
    class Recovery(ScriptedProvider):
        failed=False
        async def chat(self,prompt):
            if 'ANSWER_VERIFICATION_V1' in prompt and not self.failed:
                self.failed=True
                raise ProviderError('Temporary review outage')
            return await super().chat(prompt)
    reply={'answer':'The apple is under the tree.','evidence_scope':'book','evidence_pages':[1],'uncertain':False}
    provider=Recovery([reply,reply]);app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id'];path=f'/api/books/synthetic/object-agents/{agent}/background'
        first=client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        second=client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert first['uncertain'] and first['evidence_validation']['reason']=='verifier_unavailable_or_invalid'
        assert not second['uncertain'] and second['background_execution']=='live' and second['turn_id']!=first['turn_id']


def test_vision_only_background_uses_original_book_images_and_is_available(tmp_path):
    provider=ScriptedProvider([{'answer':'The apple is under the tree.','evidence_scope':'book','evidence_pages':[1],'uncertain':False},
        {'supported':True,'evidence_scope':'book','uncertain':False,'evidence_pages':[1],'reason':'Original text.'}],enabled=('vision',))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        child=create(client);assert child['agent']['background_available'] is True
        result=client.post(f"/api/books/synthetic/object-agents/{child['agent']['id']}/background",json={
            'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert not result['uncertain'] and result['background_state']=='supported'
        for call in provider.calls:
            assert call['images'][0].name == 'frame.jpg'
            assert any(image.parent.name == 'pages' for image in call['images'])
            assert 'reference_only_not_book_evidence' in call['prompt']


@pytest.mark.parametrize('extra', [{'question':'Invent a past.'}, {'time':70}, {'frame':'spoof'}])
def test_background_body_cannot_replace_the_frozen_context_or_internal_question(tmp_path,extra):
    provider=ScriptedProvider(enabled=())
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        child=create(client)
        assert child['agent']['background_available'] is False
        result=client.post(f"/api/books/synthetic/object-agents/{child['agent']['id']}/background",
            json={'session_id':'main','request_id':str(uuid.uuid4()),**extra})
        assert result.status_code==422
        assert provider.calls==[]


def test_failed_background_keeps_ready_target_and_can_be_retried(tmp_path):
    class FailsOnce(ScriptedProvider):
        async def chat(self,prompt):
            if not getattr(self,'failed',False):
                self.failed=True
                raise ProviderError('Background service unavailable.')
            return await super().chat(prompt)

    provider=FailsOnce([{'answer':'The apple is under the tree.','evidence_scope':'book',
        'evidence_pages':[1],'uncertain':False}])
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        path=f'/api/books/synthetic/object-agents/{agent}/background'
        assert client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())}).status_code==502
        context=app.state.object_agents.get('synthetic',agent,'main')
        assert context['status']=='ready' and context['label']=='apple'
        result=client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())})
        assert result.status_code==200 and result.json()['background_execution']=='live'
        assert result.json()['background_state']=='supported'
        assert app.state.store.history('synthetic','main')==[]


def test_cancelled_background_keeps_recognized_target_without_caching_a_result(tmp_path):
    class WaitingProvider(ScriptedProvider):
        async def chat(self,prompt):
            self.entered.set()
            await asyncio.Future()

    provider=WaitingProvider()
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
    agents=app.state.object_agents

    async def exercise():
        provider.entered=asyncio.Event()
        request=str(uuid.uuid4())
        task=asyncio.create_task(agents.background('synthetic',agent,'main',request))
        await asyncio.wait_for(provider.entered.wait(),1)
        await agents.cancel('synthetic',agent,'main',request)
        result=await asyncio.gather(task,return_exceptions=True)
        assert isinstance(result[0],asyncio.CancelledError)

    asyncio.run(exercise())
    context=agents.get('synthetic',agent,'main')
    assert context['status']=='ready' and 'background_result' not in context
    assert app.state.store.history('synthetic','main')==[]


def test_background_checks_target_against_source_images_even_when_chat_is_available(tmp_path,monkeypatch):
    class VisualBackground(ScriptedProvider):
        def __init__(self):
            super().__init__(enabled=('vision','chat'));self.drafts=[];self.reviews=[]
        async def chat(self,prompt):
            raise AssertionError('Object identity needs source-image comparison, not text-only inference')
        async def vision(self,prompt,images):
            record={'prompt':prompt,'images':list(images)}
            if 'ANSWER_VERIFICATION_V1' in prompt:
                self.reviews.append(record)
                return {'supported':True,'evidence_scope':'book','uncertain':False,
                        'evidence_pages':[1],'reason':'The source illustration and text support the scene.'}
            self.drafts.append(record)
            return {'answer':'The apple is under the tree.','evidence_scope':'book','evidence_pages':[1],'uncertain':False}
    provider=VisualBackground();app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    monkeypatch.setattr('storybook.interaction.rank_pages',lambda pages,*args: list(reversed(pages)))
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
    assert result['background_state']=='supported',result
    assert len(provider.drafts)==len(provider.reviews)==1
    for call in provider.drafts+provider.reviews:
        assert call['images'][0].name=='frame.jpg'
        assert any(p.name=='0001.png' and p.parent.name=='pages' for p in call['images'])
        assert 'reference_only_not_book_evidence' in call['prompt']
    draft=json.loads(next(l for l in provider.drafts[0]['prompt'].splitlines() if l.startswith('{"book_title"')))
    assert draft['priority_pages'][0]==1
    assert not result.get('seek_time') and not result['boxes']
    assert app.state.store.history('synthetic','main')==[]


def test_unconfirmed_background_is_retryable_and_not_saved_as_permanent_answer(tmp_path):
    provider=ScriptedProvider([
        {'answer':'I cannot confirm its name.','evidence_scope':'book','evidence_pages':[1],'uncertain':True},
        {'answer':'The apple is under the tree.','evidence_scope':'book','evidence_pages':[1],'uncertain':False}])
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id'];path=f'/api/books/synthetic/object-agents/{agent}/background'
        first=client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert first['background_state']=='unconfirmed'
        assert 'background_result' not in app.state.object_agents.get('synthetic',agent,'main')
        second=client.post(path,json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
        assert second['background_state']=='supported' and second['background_execution']=='live'
        assert second['turn_id']!=first['turn_id']


@pytest.mark.parametrize('scope,supported,uncertain,reason',[
    ('current_frame',True,False,'evidence_scope_mismatch'),
    ('book',False,False,'evidence_does_not_support_answer'),
    ('book',True,True,'evidence_does_not_support_answer'),
])
def test_background_vision_must_independently_confirm_source_evidence(tmp_path,scope,supported,uncertain,reason):
    class WrongScope(ScriptedProvider):
        async def vision(self,prompt,images):
            if 'ANSWER_VERIFICATION_V1' in prompt:
                return {'supported':supported,'evidence_scope':scope,'uncertain':uncertain,
                        'evidence_pages':[1],'reason':'The video target does not establish this book fact.'}
            return {'answer':'The apple is under the tree.','evidence_scope':'book','evidence_pages':[1],'uncertain':False}
    provider=WrongScope(enabled=('vision',));app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
    assert result['background_state']=='unconfirmed' and not result['evidence']
    assert result['evidence_validation']['reason']==reason
    assert 'background_result' not in app.state.object_agents.get('synthetic',agent,'main')


def test_missing_original_images_cannot_promote_video_reference_to_background_evidence(tmp_path):
    provider=ScriptedProvider(enabled=('vision','chat'))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        for image in (app.state.store.directory('synthetic')/'pages').glob('*.png'):
            image.unlink()
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
    assert result['background_state']=='unconfirmed' and result['evidence']==[]
    assert provider.calls==[]
