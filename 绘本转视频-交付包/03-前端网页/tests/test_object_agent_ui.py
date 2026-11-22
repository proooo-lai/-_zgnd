"""Execute the actual object-window controller with DOM and transport seams."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "web" / "object-agent.js"


def run_ui(scenario):
    runtime = shutil.which("node")
    if not runtime:
        pytest.skip("Node.js is needed for UI controller checks")
    setup = r'''
const elements=new Map();
function element(){return {children:[],classList:{items:new Set(['hidden']),add(x){this.items.add(x)},remove(x){this.items.delete(x)},toggle(x,v){if(v)this.items.add(x);else this.items.delete(x)},contains(x){return this.items.has(x)}},value:'',textContent:'',disabled:false,append(...items){this.children.push(...items)},replaceChildren(...items){this.children=[...items]},setAttribute(){},removeAttribute(){},focus(){},contains(){return false},querySelector(){return null}}}
const $=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id)};
const node=(tag,cls,text)=>Object.assign(element(),{tag,className:cls,textContent:text||''});
const window=globalThis;window.addEventListener=()=>{};
const document={activeElement:null,addEventListener(){}};
const location={href:'http://localhost/'};
const safeURL=x=>x,imageURL=x=>x,fmt=x=>String(x),stopAnswer=()=>{};
const addTurn=()=>{throw new Error('wrote main history')},accept=addTurn,status=addTurn,api=addTurn;
const navigator={mediaDevices:{getUserMedia:async()=>{throw new Error('not configured')}}};
class Audio{pause(){}removeAttribute(){}load(){}play(){return Promise.resolve()}}
const encodeWav=()=>new Blob(['wav']);
const snapshot={bookId:'book',parent:'main',x:.4,y:.5,time:8,frame:'frozen',voice:'Cherry'};
const response=data=>({ok:true,status:200,headers:{get:()=> 'application/json'},json:async()=>data});
const child=(id,label)=>({agent:{id,session_id:id,label,time:8,page_number:1,avatar_url:'/avatar.jpg'},result:{answer:label,boxes:[]}});
const flush=()=>new Promise(r=>setImmediate(r));
'''
    source = SOURCE.read_text() if SOURCE.exists() else ""
    process = subprocess.run([runtime, "-e", setup + source + "\n" + scenario], text=True, capture_output=True)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_click_opens_only_object_window_and_child_text_uses_own_route():
    result = run_ui(r'''
const calls=[];
fetch=async(path,options)=>{if(path.endsWith('/cancel'))return response({cancelled:true});const body=JSON.parse(options.body);calls.push({path,body});return response(path.endsWith('/object-agents')?child(body.agent_id,'apple'):{answer:'Story-backed answer.',evidence:[{page_number:2,quote:'Source text.'}]})};
(async()=>{await ObjectAssistant.open(snapshot);$('object-question').value='What is its story?';await $('object-form').onsubmit({preventDefault(){}});const texts=$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '));console.log(JSON.stringify({calls,texts,visible:!$('object-agent').classList.contains('hidden')}));ObjectAssistant.close()})()
''')
    assert result["visible"]
    assert result["calls"][0]["body"]["session_id"] == "main"
    assert result["calls"][0]["body"]["agent_id"].startswith("object-")
    assert result["calls"][0]["body"]["request_id"]
    assert result["calls"][1]["path"].endswith("/object-agents/" + result["calls"][0]["body"]["agent_id"] + "/ask/stream")
    assert result["calls"][1]["body"]["question"] == "What is its story?"
    assert result["calls"][1]["body"]["session_id"] == "main"
    assert result["calls"][1]["body"]["voice"] == "Cherry"
    assert result["calls"][1]["body"]["request_id"]
    assert len(result["calls"]) == 2
    assert any("Story-backed answer." in text for text in result["texts"])


def test_recognized_object_automatically_loads_evidenced_background_without_playing_over_intro():
    result = run_ui(r'''
const calls=[],played=[];Audio.prototype.play=function(){played.push(this.src);return Promise.resolve()};
fetch=async(path,options)=>{if(path.includes('/cancel?'))return response({cancelled:true});const body=JSON.parse(options.body);calls.push({path,body});if(path.endsWith('/background'))return response({answer:'The bird shares the apple.',background_state:'supported',background_execution:'live',evidence:[{page_number:2,quote:'The bird shares the apple with a rabbit.'}],audio_url:'/background.wav'});const data=child(body.agent_id,'apple');data.agent.background_available=true;data.result.audio_url='/intro.wav';return response(data)};
(async()=>{await ObjectAssistant.open(snapshot);const texts=$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '));const details=$('object-history').children.flatMap(x=>x.children.filter(y=>y.tag==='details').map(y=>y.children.map(z=>z.textContent).join(' ')));console.log(JSON.stringify({calls,texts,details,played,disabled:$('object-send').disabled}));ObjectAssistant.close()})()
''')
    assert len(result["calls"]) == 2
    assert result["calls"][1]["path"].endswith("/object-agents/" + result["calls"][0]["body"]["agent_id"] + "/background")
    assert set(result["calls"][1]["body"]) == {"session_id", "request_id", "voice"}
    assert result["calls"][1]["body"]["session_id"] == "main"
    assert result["calls"][1]["body"]["voice"] == "Cherry"
    assert any("相关介绍与背景" in text and "The bird shares the apple." in text for text in result["texts"])
    assert any("2" in text and "The bird shares the apple with a rabbit." in text for text in result["details"])
    assert result["played"] == ["/intro.wav"]
    assert result["disabled"] is False


def test_background_failure_preserves_recognized_object_and_question_controls():
    result = run_ui(r'''
const calls=[];fetch=async(path,options)=>{if(path.includes('/cancel?'))return response({cancelled:true});calls.push(path);if(path.endsWith('/background'))return {ok:false,status:502,json:async()=>({detail:'Background unavailable.'})};const body=JSON.parse(options.body);if(path.endsWith('/ask/stream'))return response({answer:'Still the apple.'});const data=child(body.agent_id,'apple');data.agent.background_available=true;return response(data)};
(async()=>{await ObjectAssistant.open(snapshot);const title=$('object-title').textContent,disabled=$('object-send').disabled;$('object-question').value='What color?';await $('object-form').onsubmit({preventDefault(){}});console.log(JSON.stringify({calls,title,disabled,texts:$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '))}));ObjectAssistant.close()})()
''')
    assert any(path.endswith("/background") for path in result["calls"])
    assert "apple" in result["title"] and "未完成" not in result["title"]
    assert result["disabled"] is False
    assert any("Background unavailable." in text for text in result["texts"])
    assert any("Still the apple." in text for text in result["texts"])


@pytest.mark.parametrize("action", ["close", "new_target", "interrupt"])
def test_late_background_is_cancelled_and_cannot_change_the_current_window(action):
    result = run_ui(r'''
const calls=[];let finish,aborted=false;
fetch=async(path,options)=>{calls.push({path,body:options.body?JSON.parse(options.body):null});if(path.includes('/cancel?'))return response({cancelled:true});if(path.endsWith('/background')){options.signal.addEventListener('abort',()=>aborted=true);return new Promise(resolve=>finish=resolve)}const body=JSON.parse(options.body),data=child(body.agent_id,body.x===.7?'rabbit':'apple');data.agent.background_available=body.x!==.7;return response(data)};
(async()=>{const opening=ObjectAssistant.open(snapshot);await flush();const started=Boolean(finish);
''' + ("ObjectAssistant.close();" if action == "close" else
       "await ObjectAssistant.open({...snapshot,x:.7});" if action == "new_target" else
       "await $('object-stop').onclick();") + r'''
if(finish)finish(response({answer:'Late background must disappear.',background_state:'supported'}));await opening;
console.log(JSON.stringify({started,aborted,calls,title:$('object-title').textContent,hidden:$('object-agent').classList.contains('hidden'),disabled:$('object-send').disabled,texts:$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '))}));ObjectAssistant.close()})()
''')
    assert result["started"] and result["aborted"]
    assert any("/cancel?" in call["path"] and "request_id=" + result["calls"][1]["body"]["request_id"] in call["path"] for call in result["calls"])
    assert not any("Late background must disappear." in text for text in result["texts"])
    if action == "close":
        assert result["hidden"]
    elif action == "new_target":
        assert "rabbit" in result["title"]
    else:
        assert "apple" in result["title"] and result["disabled"] is False


def test_new_target_ignores_late_old_recognition_and_close_ignores_late_answer():
    result = run_ui(r'''
const pending=[];fetch=(path,options)=>path.includes('/cancel?')||path.endsWith('/cancel')?Promise.resolve(response({cancelled:true})):new Promise(resolve=>pending.push(resolve));
(async()=>{const first=ObjectAssistant.open(snapshot);const second=ObjectAssistant.open({...snapshot,x:.7});pending[1](response(child('new','rabbit')));await second;pending[0](response(child('old','apple')));await first;
const label=$('object-title').textContent;$('object-question').value='Why?';const ask=$('object-form').onsubmit({preventDefault(){}});ObjectAssistant.close();pending[2](response({answer:'Late old answer.'}));await ask;
console.log(JSON.stringify({label,hidden:$('object-agent').classList.contains('hidden'),texts:$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '))}));})()
''')
    assert "rabbit" in result["label"]
    assert result["hidden"]
    assert not any("Late old answer." in text for text in result["texts"])


def test_closing_during_microphone_permission_releases_late_stream():
    result = run_ui(r'''
let permit,stopped=0;
navigator.mediaDevices.getUserMedia=()=>new Promise(resolve=>permit=resolve);
fetch=async()=>response(child('object-one','apple'));
(async()=>{await ObjectAssistant.open(snapshot);const recording=$('object-microphone').onclick();ObjectAssistant.close();permit({getTracks:()=>[{stop(){stopped++}}]});await recording;console.log(JSON.stringify({stopped,hidden:$('object-agent').classList.contains('hidden')}))})()
''')
    assert result == {"stopped": 1, "hidden": True}


def test_missing_object_interface_explains_restart_instead_of_retrying_recognition():
    result = run_ui(r'''
fetch=async()=>({ok:false,status:404,json:async()=>({detail:'Not Found'})});
(async()=>{await ObjectAssistant.open(snapshot);console.log(JSON.stringify({title:$('object-title').textContent,status:$('object-status').textContent,disabled:$('object-send').disabled,texts:$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '))}));ObjectAssistant.close()})()
''')
    assert "重启" in result["status"] and "刷新" in result["status"]
    assert "正在认识" not in result["title"]
    assert result["disabled"] is True
    assert any("接口" in text and "重启" in text for text in result["texts"])


def test_missing_book_and_provider_errors_do_not_claim_the_backend_is_outdated():
    for code, detail in [(404, "未找到绘本或所请求的文件"), (502, "视觉服务暂不可用")]:
        result = run_ui('fetch=async()=>({ok:false,status:' + str(code) + ',json:async()=>({detail:'
                        + json.dumps(detail) + r'''})});
(async()=>{await ObjectAssistant.open(snapshot);console.log(JSON.stringify({title:$('object-title').textContent,status:$('object-status').textContent,texts:$('object-history').children.map(x=>x.children.map(y=>y.textContent).join(' '))}));ObjectAssistant.close()})()
''')
        assert any(detail in text for text in result["texts"])
        assert "重启" not in result["status"]
        assert "正在认识" not in result["title"]
