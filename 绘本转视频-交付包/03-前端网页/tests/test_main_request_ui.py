"""Late stream/history events must not change a newer dialogue request."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

APP=Path(__file__).resolve().parents[1]/'web/app.js'


def run(function,start,end,scenario):
    runtime=shutil.which('node')
    if not runtime:pytest.skip('Node.js is required for controller checks')
    source=APP.read_text();body=source[source.index(start):source.index(end)]
    setup=r'''
const state={book:{id:'book'},session:'session',requestEpoch:1,audioGeneration:1,interactionWarning:''},events=[];
const safeURL=x=>x,status=()=>events.push('status'),stopAnswer=()=>events.push('stop'),refreshHistory=()=>events.push('history');
const recordMetric=()=>events.push('metric'),accept=()=>events.push('accept'),enqueuePCM=()=>events.push('pcm');
const responseAudio={pause(){},async play(){events.push('play')}},node=()=>({}),$=()=>({append(){}}),toast=()=>{};
const flush=()=>new Promise(r=>setImmediate(r));
'''
    if function=='history':
        setup=r'''
const state={book:{id:'book'},session:'session',requestEpoch:1},events=[];
const bookPath=x=>x,$=()=>({replaceChildren(){events.push('replaced')}}),addTurn=()=>events.push('turn'),renderAssistant=()=>({}),status=()=>events.push('status');
'''
    process=subprocess.run([runtime,'-e',setup+body+scenario],capture_output=True,text=True)
    assert process.returncode==0,process.stderr
    return json.loads(process.stdout)


def test_old_stream_failure_does_not_refresh_or_warn_in_new_request():
    result=run('stream','async function streamRequest(','async function ask(',r'''
let reject;const fetch=()=>new Promise((r,e)=>reject=e);
(async()=>{const pending=streamRequest('/ask',{}, {requestEpoch:1});state.requestEpoch=2;state.audioGeneration=2;
reject(new Error('old failure'));await pending.catch(()=>{});console.log(JSON.stringify({events,warning:state.interactionWarning}))})()
''')
    assert result=={'events':[],'warning':''}


def test_old_stream_transcript_and_result_are_ignored_after_new_request():
    result=run('stream','async function streamRequest(','async function ask(',r'''
let release,read=0;
const fetch=async()=>({ok:true,status:200,headers:{get:()=> 'text/event-stream'},body:{getReader:()=>({read:()=>read++?Promise.resolve({done:true}):new Promise(r=>release=r)})}});
(async()=>{const pending=streamRequest('/voice',{}, {requestEpoch:1},()=>events.push('transcript'));await flush();
state.requestEpoch=2;state.audioGeneration=2;release({done:false,value:new TextEncoder().encode('event: transcript\ndata: {"text":"old"}\n\nevent: result\ndata: {"answer":"old"}\n\n')});
await pending.catch(()=>{});console.log(JSON.stringify(events))})()
''')
    assert result==[]


def test_history_arriving_after_a_new_question_does_not_replace_its_turns():
    result=run('history','async function refreshHistory(','async function accept(',r'''
let resolve;const api=()=>new Promise(r=>resolve=r);
(async()=>{const pending=refreshHistory();state.requestEpoch=2;resolve({turns:[{question:'old',answer:'old'}]});await pending;console.log(JSON.stringify(events))})()
''')
    assert result==[]
