"""Selected objects are reference context, never substitute story evidence."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from storybook.interaction import Interactor
from storybook.server import create_app
from storybook.storage import Store
from tests.test_interaction import make_book, encoded_frame
from tests.test_server import OfflineProvider, settings_for, seed_book, wav_bytes


def payload(prompt):
    return json.JSONDecoder().raw_decode(next(line for line in prompt.splitlines() if line.startswith('{"book_title"')))[0]


class ReferenceProvider:
    def __init__(self):
        self.questions, self.reviews = [], []

    def available(self, name):
        return name == "chat"

    def mode(self, name):
        return "mock"

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            data = json.loads(prompt.rsplit("\n", 1)[1]); self.reviews.append(data)
            selected = data["context_not_evidence"].get("selected_object")
            return {"supported": bool(selected), "uncertain": False, "evidence_scope": "book",
                    "evidence_pages": [7], "reason": "The cited original story explains what happens to the selected apple."}
        data = payload(prompt); self.questions.append(data)
        return {"answer": "故事里，小鸟把苹果带给了等待的朋友。", "evidence_scope": "book", "evidence_pages": [7]}


def seed_story(store):
    book = make_book(store)
    # The relevant action lives beyond the top three default pages and beyond
    # the short outline window. No fixed sample page can stand in for retrieval.
    for n in range(3, 8):
        book['pages'].append({'number': n, 'text': '风吹过山坡。' * 80,
                              'description': '宁静的原野。', 'objects': [],
                              'image_url': f'/media/synthetic/pages/{n:04}.png'})
    book['pages'][-1]['text'] += '小鸟把苹果带给了等待的朋友。'
    store.save(book)
    return book


def test_touch_followup_retrieves_story_and_keeps_reference_in_verifier(tmp_path):
    store = Store(tmp_path); seed_story(store)
    provider = ReferenceProvider(); reader = Interactor(store, provider, SimpleNamespace())
    async def run():
        await reader.touch('synthetic', .47, .55, 10, 's1')
        return await reader.ask('synthetic', '它在故事里怎么了？', 's1', 10)
    result = asyncio.run(run())
    assert result['answer'] == '故事里，小鸟把苹果带给了等待的朋友。'
    assert result['uncertain'] is False
    selected = provider.questions[0]['selected_object']
    assert selected['label'] == 'apple' and selected['page_number'] == 1
    assert 7 in provider.questions[0]['priority_pages'][:3]
    assert '小鸟把苹果带给了等待的朋友。' in next(p['text'] for p in provider.questions[0]['book_evidence'] if p['page_number'] == 7)
    assert provider.reviews[0]['context_not_evidence']['selected_object'] == selected
    assert 'description' not in selected and 'bbox' not in selected
    # A source-only page has no invented video timestamp or seek target.
    assert result['evidence'][0]['start'] is None
    assert 'seek_time' not in result


@pytest.mark.parametrize('case', ['blank_touch', 'other_session', 'other_page'])
def test_selected_object_never_leaks_to_unrelated_followup(tmp_path, case):
    store = Store(tmp_path); seed_story(store)
    provider = ReferenceProvider(); reader = Interactor(store, provider, SimpleNamespace())
    async def run():
        await reader.touch('synthetic', .47, .55, 10, 's1')
        if case == 'blank_touch':
            await reader.touch('synthetic', .5, .02, 10, 's1')
        return await reader.ask('synthetic', '它在故事里怎么了？', 's2' if case == 'other_session' else 's1', 70 if case == 'other_page' else 10)
    result = asyncio.run(run())
    assert provider.questions[0].get('selected_object') is None
    assert result['uncertain'] is True


@pytest.mark.parametrize('route', ['voice', 'voice/stream'])
def test_voice_routes_forward_actual_frame_and_chosen_voice(tmp_path, monkeypatch, route):
    app = create_app(settings_for(tmp_path), OfflineProvider()); seed_book(app)
    received = {}
    async def capture(self, book_id, question, session_id, timestamp=0, frame=None, **kwargs):
        received.update(frame=frame, voice=kwargs.get('voice'), session=session_id, time=timestamp)
        return {'turn_id':'voice-turn', 'answer':'ok', 'timings':{}}
    monkeypatch.setattr(Interactor, 'ask', capture)
    frame = encoded_frame('green')
    with TestClient(app) as client:
        response = client.post(f'/api/books/book1/{route}',
                               data={'session_id':'same-session', 'time':'12', 'frame':frame, 'voice':'Cherry'},
                               files={'audio':('q.wav',wav_bytes(),'audio/wav')})
    assert response.status_code == 200
    assert received == {'frame':frame, 'voice':'Cherry', 'session':'same-session', 'time':12}

def test_failed_click_clears_older_selected_object(tmp_path):
    from storybook.providers import ProviderError
    store = Store(tmp_path); seed_story(store)
    class FailingClick(ReferenceProvider):
        def available(self, name):
            return name in ('chat', 'vision')
        async def vision(self, prompt, images):
            raise ProviderError('Vision unavailable')
    provider = FailingClick(); reader = Interactor(store, provider, SimpleNamespace())
    async def run():
        await reader.touch('synthetic', .47, .55, 10, 's1')
        with pytest.raises(ProviderError):
            await reader.touch('synthetic', .5, .02, 10, 's1', encoded_frame())
        provider.available = lambda name: name == 'chat'
        return await reader.ask('synthetic', '它在故事里怎么了？', 's1', 10)
    result = asyncio.run(run())
    assert provider.questions[0].get('selected_object') is None
    assert result['uncertain'] is True
    assert store.history('synthetic','s1')[-2]['status'] == 'failed'


def test_tts_failure_does_not_discard_reliable_object_selection(tmp_path):
    from storybook.providers import ProviderError
    store = Store(tmp_path); seed_story(store)
    class FailingSpeech(ReferenceProvider):
        def available(self, name):
            return name in ('chat', 'tts')
        async def tts(self, *args):
            raise ProviderError('Speech unavailable')
    provider = FailingSpeech(); reader = Interactor(store, provider, SimpleNamespace())
    async def run():
        touch = await reader.touch('synthetic', .47, .55, 10, 's1')
        assert touch['status'] == 'partial_audio'
        return await reader.ask('synthetic', '它在故事里怎么了？', 's1', 10)
    result = asyncio.run(run())
    assert result['answer'] == '故事里，小鸟把苹果带给了等待的朋友。'
    assert result['uncertain'] is False
    assert provider.questions[0]['selected_object']['label'] == 'apple'


class StoryPartsProvider(ReferenceProvider):
    def __init__(self, story_context):
        super().__init__()
        self.story_context = story_context

    async def chat(self, prompt):
        if "ANSWER_VERIFICATION_V1" in prompt:
            data = json.loads(prompt.rsplit(chr(10), 1)[1])
            self.reviews.append(data)
            return {"supported": True, "uncertain": False, "evidence_scope": "book",
                    "evidence_pages": [p["page_number"] for p in data["cited_evidence"]],
                    "reason": "The supplied original fixture supports the answer components."}
        self.questions.append(payload(prompt))
        result = {"answer": "苹果在树下。", "evidence_scope": "book", "evidence_pages": [1]}
        if self.story_context is not None:
            result["story_context"] = self.story_context
        return result


def test_selected_action_followup_combines_verified_story_with_direct_answer(tmp_path):
    store = Store(tmp_path)
    seed_story(store)
    story = {"answer": "小鸟把苹果带给了等待的朋友。", "evidence_pages": [7], "uncertain": False}
    provider = StoryPartsProvider(story)
    reader = Interactor(store, provider, SimpleNamespace())

    async def run():
        await reader.touch("synthetic", .47, .55, 10, "s1")
        return await reader.ask("synthetic", "它在干什么？", "s1", 10)

    result = asyncio.run(run())
    assert result["answer"] == "苹果在树下。故事里，小鸟把苹果带给了等待的朋友。"
    assert {e["page_number"] for e in result["evidence"]} == {1, 7}
    assert result["story_context"] == story
    assert provider.reviews[0]["candidate_answer"] == result["answer"]
    assert result["uncertain"] is False


@pytest.mark.parametrize("story", [
    None,
    {"answer": "它去了月球。", "evidence_pages": [999], "uncertain": False},
    {"answer": "它去了月球。", "evidence_pages": [7], "uncertain": True},
])
def test_description_alone_or_unverified_story_is_not_a_completed_story_followup(tmp_path, story):
    store = Store(tmp_path)
    seed_story(store)
    provider = StoryPartsProvider(story)
    reader = Interactor(store, provider, SimpleNamespace())

    async def run():
        await reader.touch("synthetic", .47, .55, 10, "s1")
        return await reader.ask("synthetic", "它在干什么？", "s1", 10)

    result = asyncio.run(run())
    assert result["uncertain"] is True
    assert result["success"] is False
    assert "月球" not in result["answer"]
    assert provider.reviews == []


def test_optional_story_text_is_not_exposed_when_it_was_not_validated(tmp_path):
    store = Store(tmp_path)
    seed_story(store)
    provider = StoryPartsProvider({"answer": "未经复核的额外内容", "evidence_pages": [7], "uncertain": False})
    result = asyncio.run(Interactor(store, provider, SimpleNamespace()).ask(
        "synthetic", "苹果在哪里？", "s1", 10))
    assert result["answer"] == "苹果在树下。"
    assert not result.get("story_context")


def test_review_service_failure_is_distinct_from_missing_story_evidence(tmp_path):
    from storybook.providers import ProviderError
    store = Store(tmp_path)
    seed_story(store)
    class UnavailableReview(ReferenceProvider):
        async def chat(self, prompt):
            if "ANSWER_VERIFICATION_V1" in prompt:
                raise ProviderError("Temporary service failure")
            return await super().chat(prompt)
    result = asyncio.run(Interactor(store, UnavailableReview(), SimpleNamespace()).ask(
        "synthetic", "苹果在哪里？", "s1", 10))
    assert result["uncertain"] is True and result["success"] is False
    assert "复核服务" in result["answer"]
    assert result["evidence_validation"]["reason"] == "verifier_unavailable_or_invalid"
