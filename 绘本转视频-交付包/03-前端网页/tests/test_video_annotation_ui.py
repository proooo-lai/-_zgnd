"""Execute the player annotation controller, including delayed media events."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


WEB = Path(__file__).resolve().parents[1] / 'web'


def run_ui(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for player controller checks')
    source = WEB / 'video-annotations.js'
    setup = r'''
const window=globalThis,location={href:'http://localhost:8765/',origin:'http://localhost:8765'};
window.location=location;
class Element extends EventTarget {
  constructor(){super();this.children=[];this.style={};this.className='';this.classList={items:new Set(['hidden']),add(x){this.items.add(x)},remove(x){this.items.delete(x)},contains(x){return this.items.has(x)}};}
  append(...xs){this.children.push(...xs)} replaceChildren(...xs){this.children=xs}
  getBoundingClientRect(){return {width:800,height:600,left:0,top:0}}
}
const images=[];
class Image extends Element {
  constructor(){super();images.push(this);this.naturalWidth=1280;this.naturalHeight=720;}
  loaded(){this.dispatchEvent(new Event('load'))}
}
const document={createElement:()=>new Element()};
const observers=[];class ResizeObserver{constructor(callback){this.callback=callback;observers.push(this)}observe(){}}
window.addEventListener=()=>{};
class Video extends Element {
  constructor(){super();this.readyState=2;this.videoWidth=1280;this.videoHeight=720;this.duration=120;this._time=0;this.paused=false;this.seeking=false;this.src='/media/book/book.mp4'}
  pause(){this.paused=true}
  get currentTime(){return this._time}
  set currentTime(n){this._time=n;this.seeking=true;this.dispatchEvent(new Event('seeking'))}
  finishSeek(){this.seeking=false;this.dispatchEvent(new Event('seeked'))}
}
const video=new Video(),frame=new Element(),layer=new Element();let bookId='book';
const flush=()=>new Promise(r=>setImmediate(r));
const result=(action='locate_text')=>({seek_time:30,boxes:[{bbox:[.25,.25,.5,.125],label:'APPLE',kind:'text',presentation:action==='highlight_text'?'highlight':'outline',source:'verified_video_frame'}],
text_location:{status:'found',action,target_text:'APPLE',timestamp:30,frame_url:'/media/book/turns/turn/text-location/scene-00-frame-1.png',frame_sha256:'a'.repeat(64),frame_size:[1280,720]}});
'''
    code = setup + (source.read_text() if source.exists() else '') + '\n' + r'''
const annotations=window.VideoAnnotations.create({video,frame,layer,getBookId:()=>bookId});
''' + scenario
    process = subprocess.run([node, '-e', code], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def test_annotation_waits_for_exact_seek_and_uses_contain_geometry():
    value = run_ui(r'''
(async()=>{const pending=annotations.apply(result());images[0].loaded();await flush();
const before={paused:video.paused,boxes:layer.children.length,hidden:frame.classList.contains('hidden'),time:video.currentTime};
video.finishSeek();const applied=await pending;
console.log(JSON.stringify({before,applied,after:{boxes:layer.children.length,hidden:frame.classList.contains('hidden'),style:layer.children[0]?.style,cls:layer.children[0]?.className,sameSource:annotations.source()===images[0]}}));annotations.clear()})()
''')
    assert value['before'] == {'paused': True, 'boxes': 0, 'hidden': True, 'time': 30}
    assert value['applied'] is True and value['after']['sameSource']
    assert value['after']['boxes'] == 1 and not value['after']['hidden']
    # A 16:9 video in an 800x600 stage has 75 px top/bottom letterboxing.
    assert value['after']['style'] == {'left': '200px', 'top': '187.5px', 'width': '400px', 'height': '56.25px'}
    assert 'outline' in value['after']['cls']


def test_highlight_has_distinct_style_and_play_or_seek_clears_frozen_pixels():
    value = run_ui(r'''
(async()=>{const pending=annotations.apply(result('highlight_text'));images[0].loaded();await flush();video.finishSeek();await pending;
const cls=layer.children[0]?.className;video.dispatchEvent(new Event('play'));
const afterPlay={boxes:layer.children.length,hidden:frame.classList.contains('hidden')};
const next=annotations.apply(result());images[1].loaded();await next;video.currentTime=50;
console.log(JSON.stringify({cls,afterPlay,afterSeek:{boxes:layer.children.length,hidden:frame.classList.contains('hidden')}}));annotations.clear()})()
''')
    assert 'highlight' in value['cls'] and 'outline' not in value['cls']
    assert value['afterPlay'] == value['afterSeek'] == {'boxes': 0, 'hidden': True}


def test_late_image_or_seek_cannot_resurrect_annotations_after_clear_or_switch():
    value = run_ui(r'''
(async()=>{const first=annotations.apply(result());annotations.clear();images[0].loaded();video.finishSeek();
const cancelled=await first;const second=annotations.apply(result());images[1].loaded();await flush();bookId='other';video.finishSeek();
console.log(JSON.stringify({cancelled,switched:await second,boxes:layer.children.length,hidden:frame.classList.contains('hidden')}));annotations.clear()})()
''')
    assert value == {'cancelled': False, 'switched': False, 'boxes': 0, 'hidden': True}


def test_frame_error_or_bad_dimensions_does_not_show_an_annotation():
    for event in ('error', 'wrong_dimensions'):
        value = run_ui(r'''
(async()=>{const pending=annotations.apply(result());
''' + ("images[0].dispatchEvent(new Event('error'));" if event == 'error' else "images[0].naturalWidth=100;images[0].loaded();") + r'''
console.log(JSON.stringify({applied:await pending,boxes:layer.children.length,hidden:frame.classList.contains('hidden')}));annotations.clear()})()
''')
        assert value == {'applied': False, 'boxes': 0, 'hidden': True}


def test_external_frame_invalid_box_and_mismatched_time_are_rejected():
    value = run_ui(r'''
(async()=>{const badURL=result();badURL.text_location.frame_url='https://other.test/frame.png';
const badBox=result();badBox.boxes[0].bbox=[.9,.2,.4,.1];
const badTime=result();badTime.seek_time=80;
const accepted=[];for(const item of [badURL,badBox,badTime])accepted.push(await annotations.apply(item));
console.log(JSON.stringify({accepted,images:images.length,boxes:layer.children.length}));annotations.clear()})()
''')
    assert value == {'accepted': [False, False, False], 'images': 0, 'boxes': 0}


def test_user_seeking_away_during_loading_cancels_pending_location():
    value = run_ui(r'''
(async()=>{const pending=annotations.apply(result());images[0].loaded();await flush();video.currentTime=90;video.finishSeek();
console.log(JSON.stringify({applied:await pending,boxes:layer.children.length,hidden:frame.classList.contains('hidden'),time:video.currentTime}));annotations.clear()})()
''')
    assert value == {'applied': False, 'boxes': 0, 'hidden': True, 'time': 90}


def test_verified_object_location_uses_the_same_exact_frame_controller():
    value=run_ui(r'''
(async()=>{const item=result();item.object_location={...item.text_location,action:'locate_object',target_label:'elephant',frame_url:'/media/book/turns/turn/object-location/scene-00-frame-0.png'};delete item.text_location;
item.boxes[0].kind='object';const pending=annotations.apply(item);images[0]?.loaded();await flush();video.finishSeek();
console.log(JSON.stringify({applied:await pending,boxes:layer.children.length,paused:video.paused}));annotations.clear()})()
''')
    assert value=={'applied':True,'boxes':1,'paused':True}


@pytest.mark.parametrize('applied', [True, False])
def test_answer_acceptance_waits_for_annotations_and_reports_display_failure(applied):
    source = (WEB / 'app.js').read_text()
    function = source[source.index('async function accept('):source.index('async function streamRequest(')]
    setup = '''
const events=[],state={boxes:[]},window={ObjectAssistant:{close(){}}},Language={noteHeard(){}},finite=Number.isFinite;
const drawBoxes=()=>{},seek=()=>events.push('incorrect-extra-seek'),status=()=>{},stopAnswer=()=>{};
const renderAssistant=result=>{events.push({answer:result.answer,uncertain:result.uncertain});return {}},evidence=()=>{},speak=()=>events.push('speak');
const videoAnnotations={clear(){},async apply(){events.push('apply');return APPLIED}};
'''.replace('APPLIED', json.dumps(applied))
    scenario = '''
(async()=>{await accept({answer:'Verified text.',seek_time:30,text_location:{status:'found'},boxes:[],evidence:[]},{});console.log(JSON.stringify(events))})()
'''
    process = subprocess.run([shutil.which('node') or 'node', '-e', setup + function + scenario], capture_output=True, text=True)
    assert process.returncode == 0, process.stderr
    events = json.loads(process.stdout)
    assert events[0] == 'apply' and 'incorrect-extra-seek' not in events
    if applied:
        assert events[1]['answer'] == 'Verified text.' and events[-1] == 'speak'
    else:
        assert events[1]['uncertain'] and '未能' in events[1]['answer'] and 'speak' not in events


def test_layout_change_without_window_resize_repositions_the_annotation():
    value=run_ui(r'''
(async()=>{const pending=annotations.apply(result());images[0].loaded();await flush();video.finishSeek();await pending;
layer.getBoundingClientRect=()=>({width:400,height:300});for(const observer of observers)observer.callback();
console.log(JSON.stringify({style:layer.children[0].style}));annotations.clear()})()
''')
    assert value['style']=={'left':'100px','top':'93.75px','width':'200px','height':'28.125px'}


def test_interrupt_during_frame_loading_cancels_annotation_and_late_answer():
    source=(WEB/'app.js').read_text()
    accept=source[source.index('async function accept('):source.index('async function streamRequest(')]
    interrupt=next(line for line in source.splitlines() if line.startswith('function interruptAnswer('))
    fixture=r'''
const state={book:{id:'book'},session:'session',requestEpoch:1,boxes:[]},events=[],window={ObjectAssistant:{close(){}}},Language={noteHeard(){}};
let release;
const videoAnnotations={async apply(){return new Promise(r=>release=r)},clear(){events.push('clear');release?.(false)}};
const stopAnswer=()=>{},busy=()=>{},toast=()=>{},status=()=>{},drawBoxes=()=>{},evidence=()=>{},finite=Number.isFinite,seek=()=>{};
const renderAssistant=()=>{events.push('render');return {}},speak=()=>events.push('speak');
'''
    scenario=r'''
(async()=>{const pending=accept({answer:'Located.',text_location:{status:'found'},boxes:[]},{requestEpoch:1});interruptAnswer();release(false);await pending;console.log(JSON.stringify(events))})()
'''
    process=subprocess.run([shutil.which('node') or 'node','-e',fixture+accept+interrupt+scenario],capture_output=True,text=True)
    assert process.returncode==0,process.stderr
    events=json.loads(process.stdout)
    assert 'clear' in events and 'render' not in events and 'speak' not in events
