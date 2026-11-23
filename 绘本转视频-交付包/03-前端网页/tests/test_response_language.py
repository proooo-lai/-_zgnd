"""Current question controls replies, including cross-language book evidence."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from storybook.interaction import Interactor, response_language
from storybook.server import create_app
from storybook.storage import Store
from tests.test_interaction import ScriptedProvider, make_book
from tests.test_object_agents import create
from tests.test_server import settings_for
from tests.test_server import wav_bytes


CASES = [
    ('What happens to 小满 in the story?', 'en'),
    ('Highlight “夏天菜园” in the video.', 'en'),
    ('请找到“green vegetables in summer”这句话。', 'zh'),
    ('这是什么？请用英语回答。', 'en'),
    ('What happened? Please answer in Chinese.', 'zh'),
    ('Why is the tomato moving?', 'en'),
    ('它为什么在动？', 'zh'),
]


@pytest.mark.parametrize('question,want', CASES)
def test_question_language_ignores_quoted_targets_and_short_foreign_names(question, want):
    assert response_language(question) == want


def test_browser_language_switches_each_turn_and_honors_explicit_preferences():
    runtime = shutil.which('node')
    if not runtime:
        pytest.skip('Node.js required')
    js = (Path(__file__).resolve().parents[1]/'web/language.js').read_text()
    scenario = '''const result=[];Language.setBook({language:'zh'});
    for(const [question] of cases){Language.noteHeard(question);result.push([Language.answerLanguage(),Language.ttsVoice()]);}
    console.log(JSON.stringify(result));'''
    process = subprocess.run([runtime, '-e', 'global.window=global;const cases='+json.dumps(CASES)+ ';'+js+scenario],
                             capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == [[want, 'Cherry' if want == 'zh' else 'Ethan'] for _, want in CASES]


@pytest.mark.parametrize('question,bad,want', [
    ('What happens in the story?', '苹果在树下。', 'en'),
    ('故事里发生了什么？', 'The apple is under the tree.', 'zh'),
])
def test_wrong_language_draft_is_not_displayed_or_spoken(tmp_path, question, bad, want):
    store=Store(tmp_path);make_book(store)
    provider=ScriptedProvider([{'answer':bad,'evidence_scope':'book','evidence_pages':[1]}])
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic',question,'s',5))
    assert result['answer'] != bad
    assert result['success'] is False
    assert result['evidence_validation']['reason'] == 'answer_language_mismatch'
    assert response_language(result['answer']) == want


@pytest.mark.parametrize('book_language,want', [('en','en'), ('zh','zh')])
def test_automatic_object_background_uses_book_language(tmp_path,book_language,want):
    answer='The apple is under the tree.' if want=='en' else '苹果在树下。'
    provider=ScriptedProvider([{'answer':answer,'evidence_scope':'book','evidence_pages':[1]}])
    app=create_app(settings_for(tmp_path),provider)
    book=make_book(app.state.store);book['language']=book_language;app.state.store.save(book)
    with TestClient(app) as client:
        agent=create(client)['agent']['id']
        result=client.post(f'/api/books/synthetic/object-agents/{agent}/background',
            json={'session_id':'main','request_id':str(uuid.uuid4())}).json()
    assert result['background_state']=='supported',result
    assert response_language(app.state.store.history('synthetic',agent)[-1]['question'])==want


def test_english_text_and_object_navigation_return_english_without_a_model(tmp_path):
    store=Store(tmp_path);make_book(store)
    reader=Interactor(store,ScriptedProvider(enabled=()),SimpleNamespace())
    for question in ['Highlight “苹果”', 'Where is the bird?']:
        result=asyncio.run(reader.ask('synthetic',question,'s',5))
        assert result['uncertain'] is True
        assert response_language(result['answer'])=='en',result


@pytest.mark.parametrize('question,answer,want', [
    ('What happens in the story?', 'The apple is under the tree.', 'en'),
    ('故事里发生了什么？', '苹果在树下。', 'zh'),
])
def test_answer_text_and_speech_language_follow_question_even_for_other_language_book(tmp_path, question, answer, want):
    store=Store(tmp_path);book=make_book(store)
    book['language']='zh' if want=='en' else 'en';store.save(book)
    spoken=[]
    class Speech(ScriptedProvider):
        async def tts(self, text, language, output, voice=None):
            spoken.append((text,language,voice));output.write_bytes(b'original synthetic audio');return output
        def mode(self,name):
            return 'dashscope' if name=='tts' else super().mode(name)
    provider=Speech([{'answer':answer,'evidence_scope':'book','evidence_pages':[1]}],enabled=('chat','tts'))
    result=asyncio.run(Interactor(store,provider,SimpleNamespace()).ask('synthetic',question,'s',5))
    assert result['answer']==answer and result['audio_complete'] is True
    assert spoken==[(answer,want,'Ethan' if want=='en' else 'Cherry')]


def test_text_submission_updates_current_language_before_selecting_automatic_voice():
    runtime=shutil.which('node')
    if not runtime:pytest.skip('Node.js required')
    web=Path(__file__).resolve().parents[1]/'web'
    source=next(line for line in (web/'app.js').read_text().splitlines() if line.startswith('async function ask('))
    setup="""global.window=global;const state={book:{id:'b'},session:'s'},bodies=[];
    const video={currentTime:0,pause(){}},videoAnnotations={clear(){}};
    const busy=x=>state.busy=x,openAssistant=()=>{},stopAnswer=()=>{},unlockAudio=()=>{},captureFrame=()=>null;
    const addTurn=()=>{},status=()=>{},$=()=>({value:''}),bookPath=x=>x,toast=()=>{};
    const streamRequest=async(path,body)=>{bodies.push(body);return true;};
    """
    scenario="""Language.setBook({language:'zh'});(async()=>{await ask('What happened?');await ask('发生了什么？');
    console.log(JSON.stringify(bodies.map(b=>[b.question,b.voice])));})();"""
    result=subprocess.run([runtime,'-e',setup+(web/'language.js').read_text()+source+scenario],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)==[['What happened?','Ethan'],['发生了什么？','Cherry']]


def test_object_introduction_uses_localized_label_without_wrong_language_description():
    from storybook.interaction import object_introduction
    obj={'label':'苹果','label_en':'apple','label_zh':'苹果','description':'这是红色的。'}
    assert object_introduction(obj,'en')=='This is apple.'
    assert object_introduction(obj,'zh')=='这是苹果。这是红色的。'


@pytest.mark.parametrize('route', ['voice','voice/stream'])
@pytest.mark.parametrize('question,answer,want', [
    ('What happens in the story?', 'The apple is under the tree.', 'en'),
    ('故事里发生了什么？', '苹果在树下。', 'zh'),
])
def test_asr_result_controls_reply_and_speech_language_in_both_routes(tmp_path,route,question,answer,want):
    spoken=[]
    class Voice(ScriptedProvider):
        async def asr(self, path):return question
        async def tts(self, text, language, output, voice=None):
            spoken.append(language);output.write_bytes(b'synthetic speech');return output
    provider=Voice([{'answer':answer,'evidence_scope':'book','evidence_pages':[1]}],enabled=('asr','chat','tts'))
    app=create_app(settings_for(tmp_path),provider);make_book(app.state.store)
    with TestClient(app) as client:
        reply=client.post(f'/api/books/synthetic/{route}',data={'session_id':'s','time':'5'},
                          files={'audio':('question.wav',wav_bytes(),'audio/wav')})
    assert reply.status_code==200,reply.text
    result=reply.json() if route=='voice' else next(json.loads(block.split('data: ',1)[1])
        for block in reply.text.split('\n\n') if block.startswith('event: result'))
    assert result['transcript']==question and result['answer']==answer
    assert result['audio_complete'] is True and spoken==[want]


def test_english_book_blank_touch_returns_english_guidance(tmp_path):
    store=Store(tmp_path);make_book(store)
    reader=Interactor(store,ScriptedProvider(enabled=()),SimpleNamespace())
    result=asyncio.run(reader.touch('synthetic',.02,.5,5,'s'))
    assert response_language(result['answer'])=='en'
    assert result['uncertain'] is True and not result.get('object')
