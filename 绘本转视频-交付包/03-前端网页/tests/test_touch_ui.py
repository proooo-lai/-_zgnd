"""A click owns its paused frame even when main dialogue or seeking is busy."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


def run_touch(scenario):
    runtime = shutil.which("node")
    if not runtime:
        pytest.skip("Node.js required")
    source = (Path(__file__).resolve().parents[1] / "web/app.js").read_text()
    start = "function waitForClickFrame(" if "function waitForClickFrame(" in source else "async function touch("
    body = source[source.index(start):source.index("function encodeWav(")]
    setup = r'''
const state={book:{id:'book'},session:'main',busy:false,requestEpoch:1},opened=[],events=[];
const video=new EventTarget();Object.assign(video,{src:'/book.mp4',currentTime:10,readyState:4,seeking:false,pause(){events.push('pause')}});
let rect={stage:{left:0,top:0},left:0,top:0,width:100,height:100};
const contentRect=()=>rect;
const captureFrame=()=> 'frame-at-'+video.currentTime;
const $=()=>({}),toast=x=>events.push(x),stopAnswer=()=>events.push('stop'),status=()=>{},drawBoxes=()=>{};
const busy=x=>{state.busy=x;},videoAnnotations={clear(){events.push('clear')}};
let stopMic=async()=>{state.mic=null};
const window={ObjectAssistant:{close(){events.push('close')},async open(data){opened.push(data)}}};
const flush=()=>new Promise(r=>setImmediate(r));
const click={clientX:50,clientY:50};
'''
    result = subprocess.run([runtime, "-e", setup + body + scenario], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_click_interrupts_pending_main_request_and_opens_object():
    result = run_touch(r'''
(async()=>{state.busy=true;state.abort={abort(){events.push('abort')}};await touch(click);console.log(JSON.stringify({opened,events,epoch:state.requestEpoch,busy:state.busy}))})()
''')
    assert len(result["opened"]) == 1
    assert "abort" in result["events"]
    assert result["epoch"] == 2 and not result["busy"]


def test_click_snapshots_time_before_waiting_for_microphone_shutdown():
    result = run_touch(r'''
(async()=>{state.mic={};stopMic=async()=>{state.mic=null;video.currentTime=20};await touch(click);console.log(JSON.stringify(opened))})()
''')
    assert len(result) == 1
    assert result[0]["time"] == 10 and result[0]["frame"] == "frame-at-10"


def test_newer_click_wins_when_older_click_is_waiting_for_microphone():
    result = run_touch(r'''
(async()=>{let release;state.mic={};stopMic=()=>{state.mic=null;return new Promise(r=>release=r)};
const first=touch(click);await flush();video.currentTime=20;await touch(click);release();await first;console.log(JSON.stringify(opened))})()
''')
    assert len(result) == 1 and result[0]["time"] == 20


def test_seeking_click_waits_for_decoded_frame():
    result = run_touch(r'''
(async()=>{video.seeking=true;video.readyState=1;const pending=touch(click);await flush();const before=opened.length;
video.seeking=false;video.readyState=4;video.dispatchEvent(new Event('seeked'));await pending;console.log(JSON.stringify({before,opened}))})()
''')
    assert result["before"] == 0 and len(result["opened"]) == 1
    assert result["opened"][0]["frame"] == "frame-at-10"


def test_click_outside_picture_does_not_cancel_main_question():
    result = run_touch(r'''
(async()=>{state.busy=true;state.abort={abort(){events.push('abort')}};await touch({clientX:-1,clientY:10});console.log(JSON.stringify({opened,events}))})()
''')
    assert result["opened"] == [] and "abort" not in result["events"]


def test_new_main_question_supersedes_a_click_waiting_for_decode():
    result = run_touch(r'''
(async()=>{video.seeking=true;const pending=touch(click);await flush();state.requestEpoch++;
video.seeking=false;video.dispatchEvent(new Event('seeked'));await pending;console.log(JSON.stringify(opened))})()
''')
    assert result == []


def test_new_seek_supersedes_a_click_waiting_for_decode():
    result = run_touch(r'''
(async()=>{video.seeking=true;const pending=touch(click);await flush();state.touchEpoch++;
video.seeking=false;video.dispatchEvent(new Event('seeked'));await pending;console.log(JSON.stringify(opened))})()
''')
    assert result == []


def test_first_portrait_frame_rechecks_actual_letterbox_after_decode():
    result = run_touch(r'''
(async()=>{video.readyState=1;rect={stage:{left:0,top:0},left:0,top:0,width:160,height:90};
const pending=touch({clientX:20,clientY:45});await flush();
rect={stage:{left:0,top:0},left:54.6875,top:0,width:50.625,height:90};
video.readyState=4;video.dispatchEvent(new Event('loadeddata'));await pending;console.log(JSON.stringify(opened))})()
''')
    assert result == []


def test_first_portrait_frame_uses_actual_content_coordinates():
    result = run_touch(r'''
(async()=>{video.readyState=1;rect={stage:{left:0,top:0},left:0,top:0,width:160,height:90};
const pending=touch({clientX:67.34375,clientY:45});await flush();
rect={stage:{left:0,top:0},left:54.6875,top:0,width:50.625,height:90};
video.readyState=4;video.dispatchEvent(new Event('loadeddata'));await pending;console.log(JSON.stringify(opened))})()
''')
    assert len(result) == 1 and result[0]['x'] == .25 and result[0]['y'] == .5


def test_resuming_playback_cancels_click_waiting_for_microphone():
    result = run_touch(r'''
(async()=>{let release;state.mic={};stopMic=()=>{state.mic=null;return new Promise(r=>release=r)};
const pending=touch(click);await flush();video.dispatchEvent(new Event('play'));
release();await pending;console.log(JSON.stringify(opened))})()
''')
    assert result == []
