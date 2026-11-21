"""Execute the browser submission/render functions with lightweight DOM seams."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / 'web' / 'app.js'


def run_browser_function(name, fixture):
    runtime = shutil.which('node')
    if not runtime:
        pytest.skip('Browser-function checks require Node.js')
    source = next(line for line in APP.read_text().splitlines() if line.startswith(f'function {name}(') or line.startswith(f'async function {name}('))
    result = subprocess.run([runtime, '-e', fixture.replace('/* FUNCTION */', source)], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_voice_submission_keeps_frame_session_time_and_voice_in_both_routes():
    result = run_browser_function('sendVoice', '''
const state={book:{id:'b'},session:'s',busy:false,voice:'Cherry'},video={currentTime:8,pause(){}},Language={ttsVoice:()=> 'Serena'};
let forms=[];const busy=()=>{},stopAnswer=()=>{},unlockAudio=()=>{},addTurn=()=>({querySelector:()=>({})}),status=()=>{},captureFrame=()=> 'data:image/png;base64,frame',bookPath=x=>x;
const streamRequest=async(path,d)=>{forms.push([path,Object.fromEntries([...d].filter(([k])=>k!=='audio'))]);return false;};
const api=async(path,{body})=>{forms.push([path,Object.fromEntries([...body].filter(([k])=>k!=='audio'))]);return {};},accept=()=>{},toast=()=>{};
/* FUNCTION */
sendVoice(new Blob(['wav']),{kind:'audio_file'},'q.wav').then(()=>console.log(JSON.stringify(forms)));
''')
    assert [item[0] for item in result] == ['/voice/stream', '/voice']
    for _, form in result:
        assert form == {'frame':'data:image/png;base64,frame', 'session_id':'s', 'time':'8', 'voice':'Cherry'}


def test_source_only_evidence_opens_page_without_inventing_video_time():
    result = run_browser_function('evidence', '''
const children=[],actions=[];const root={replaceChildren(){},append(x){children.push(x)}};
const $=()=>root,node=(tag,cls,text)=>({tag,text,children:[],append(...xs){this.children.push(...xs)}});
const finite=n=>typeof n==='number'&&Number.isFinite(n),fmt=n=>n==null?'00:00':String(n),seek=x=>actions.push(['seek',x]),showPage=x=>actions.push(['page',x]);
/* FUNCTION */
evidence([{page_number:7,start:null,end:null,quote:'Original story.'}]);
children[0].children[2].onclick();
console.log(JSON.stringify({title:children[0].children[0].text,button:children[0].children[2].text,actions}));
''')
    assert '00:00' not in result['title']
    assert result['button'] == '查看原书这一页 →'
    assert result['actions'] == [['page',7]]
