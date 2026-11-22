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
    assert any("Story-backed answer." in text for text in result["texts"])


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
