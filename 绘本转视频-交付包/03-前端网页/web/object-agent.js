/* Object-scoped UI, transport, recording and playback. Never writes main chat. */
"use strict";
window.ObjectAssistant = (() => {
  let current = null, sequence = 0, pcm = null, audioEpoch = 0;
  const audio = new Audio();
  const panel = $("object-agent");
  const handle = $("object-drag-handle");
  let drag = null;
  function place(left, top) {
    const viewport=window.visualViewport;
    const x=viewport?.offsetLeft||0,y=viewport?.offsetTop||0;
    const width=viewport?.width||window.innerWidth,height=viewport?.height||window.innerHeight;
    panel.style.maxWidth=Math.max(1,width-20)+"px";
    panel.style.maxHeight="min(650px, "+Math.max(1,height-20)+"px)";
    const rect=panel.getBoundingClientRect();
    panel.style.left=Math.max(x+10,Math.min(left,x+width-rect.width-10))+"px";
    panel.style.top=Math.max(y+10,Math.min(top,y+height-rect.height-10))+"px";
    panel.style.right="auto";panel.style.bottom="auto";
  }
  function keepVisible() {
    if(panel.classList.contains("hidden"))return;
    const rect=panel.getBoundingClientRect();place(rect.left,rect.top);
  }
  function endDrag(event) {
    if(!drag||event&&event.pointerId!==drag.id)return;
    const id=drag.id;drag=null;panel.classList.remove("dragging");
    if(handle.hasPointerCapture?.(id))handle.releasePointerCapture(id);
  }
  handle.onpointerdown=event=>{
    if(event.button!==0||event.isPrimary===false||event.target.closest("button,a,input,textarea,select"))return;
    const rect=panel.getBoundingClientRect();drag={id:event.pointerId,x:event.clientX-rect.left,y:event.clientY-rect.top};
    handle.setPointerCapture(event.pointerId);panel.classList.add("dragging");event.preventDefault();
  };
  handle.onpointermove=event=>{
    if(drag&&event.pointerId===drag.id){event.preventDefault();place(event.clientX-drag.x,event.clientY-drag.y);}
  };
  handle.onpointerup=handle.onpointercancel=handle.onlostpointercapture=endDrag;
  handle.onkeydown=event=>{
    if(event.target!==handle)return;
    const delta={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[event.key];
    if(!delta)return;
    event.preventDefault();event.stopPropagation();const rect=panel.getBoundingClientRect(),step=event.shiftKey?40:12;
    place(rect.left+delta[0]*step,rect.top+delta[1]*step);
  };
  window.addEventListener("resize",keepVisible);
  window.visualViewport?.addEventListener("resize",keepVisible);
  window.visualViewport?.addEventListener("scroll",keepVisible);
  document.addEventListener("fullscreenchange",keepVisible);
  if(window.ResizeObserver)new ResizeObserver(keepVisible).observe(panel);
  const live = (view, revision = view.revision) => current === view && view.revision === revision;
  const message = text => { $("object-status").textContent = text; };
  const path = (view, suffix = "") => "/api/books/" + encodeURIComponent(view.bookId) + "/object-agents" + (view.agentId && (view.ready || suffix === "/cancel") ? "/" + encodeURIComponent(view.agentId) : "") + suffix;

  function controls(view, working) {
    view.working = working;
    $("object-send").disabled = working || !view.ready || Boolean(view.mic);
    $("object-question").disabled = !view.ready;
    $("object-microphone").disabled = working || !view.ready;
    $("object-background-retry").disabled = working || !view.ready || Boolean(view.mic);
    $("object-stop").classList.toggle("hidden", !working);
  }
  function turn(role, text, uncertain = false) {
    const row = node("article", "turn " + role);
    row.append(node("div", "turn-label", role === "user" ? "你" : "对象小助手"), node("div", "turn-bubble" + (uncertain ? " uncertain" : ""), text));
    $("object-history").append(row);
    $("object-history").scrollTop = $("object-history").scrollHeight;
    return row;
  }
  function stopAudio() {
    audioEpoch++;
    audio.onended = audio.onplaying = audio.onerror = null;
    audio.pause(); audio.removeAttribute("src"); audio.load();
    if (pcm) {
      for (const timer of pcm.timers) clearTimeout(timer);
      pcm.timers.clear(); pcm.pending = []; pcm.next = 0;
      for (const source of pcm.sources) { source.onended = null; try { source.stop(); } catch {} source.disconnect(); }
      pcm.sources.clear();
    }
  }
  function unlock() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!pcm || pcm.ctx.state === "closed") pcm = {ctx:new Ctx({latencyHint:"interactive"}),pending:[],sources:new Set(),timers:new Set(),next:0};
    if (pcm.ctx.state !== "running") pcm.ctx.resume().then(drain).catch(() => message("点击“播放回答”后收听。"));
  }
  function metric(view, context, turnId, event) {
    if (!live(view) || context.marked || !turnId) return;
    context.marked = true;
    const now = performance.now();
    const entry = {book_id:view.bookId,session_id:view.agent.id,turn_id:turnId,input_kind:context.kind,
      latency_ms:Math.max(0,Math.round(now-context.started)),playback_event:event,
      measurement_source:"browser_playback_event",measurement_kind:"browser_playback_event",
      clock_domain:"object-window-performance-"+view.parent,warmup_complete:false,same_clock:true,
      speech_end_ms:context.kind==="mic"?context.started:null,first_audible_ms:now};
    fetch("/api/metrics",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(entry)}).catch(()=>{});
  }
  function drain() {
    if (!pcm || pcm.ctx.state !== "running") return;
    while (pcm.pending.length) {
      const item = pcm.pending.shift();
      if (!live(item.view) || item.epoch !== audioEpoch) continue;
      const buffer = pcm.ctx.createBuffer(1,item.samples.length,item.rate); buffer.copyToChannel(item.samples,0);
      const source = pcm.ctx.createBufferSource(); source.buffer=buffer; source.connect(pcm.ctx.destination);
      const start = Math.max(pcm.next,pcm.ctx.currentTime+.008); pcm.next=start+buffer.duration;
      pcm.sources.add(source); source.onended=()=>{pcm.sources.delete(source);source.disconnect();}; source.start(start);
      if (item.first < 0 || item.context.scheduled) continue;
      item.context.scheduled = true;
      const audible = start+item.first/item.rate;
      const check = () => {
        if (!live(item.view) || item.epoch !== audioEpoch) return;
        if (pcm.ctx.state!=="running" || pcm.ctx.currentTime<audible) {
          const timer=setTimeout(()=>{pcm.timers.delete(timer);check();},50);pcm.timers.add(timer);return;
        }
        metric(item.view,item.context,item.turnId,"web_audio_pcm_start");
      };
      const timer=setTimeout(()=>{pcm.timers.delete(timer);check();},Math.max(0,(audible-pcm.ctx.currentTime)*1000));pcm.timers.add(timer);
    }
  }
  function pcmChunk(data, view, context, epoch) {
    unlock();
    if (!pcm) throw new Error("浏览器不支持实时音频，请查看文字或重听已生成音频。");
    const raw=atob(data.data||""),rate=Number(data.sample_rate);
    if (!raw.length || raw.length%2 || !Number.isInteger(rate) || rate<8000 || rate>96000) throw new Error("回答音频格式无效");
    const bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);
    const reader=new DataView(bytes.buffer),samples=new Float32Array(raw.length/2);let first=-1;
    for(let i=0;i<samples.length;i++){samples[i]=reader.getInt16(i*2,true)/32768;if(first<0&&Math.abs(samples[i])>.005)first=i;}
    pcm.pending.push({view,context,epoch,samples,rate,first,turnId:data.turn_id});drain();
  }
  function replayButton(view, url, label, container) {
    url=safeURL(url);if(!url)return;
    const button=node("button","button subtle",label);button.type="button";
    button.onclick=()=>{if(!live(view))return;stopAnswer();stopAudio();audio.src=url;audio.play().catch(()=>message("请检查浏览器的声音权限。"));};container.append(button);
  }
  function render(view, result, label = null) {
    const partial=["partial","failed"].includes(result.status),audioFailed=result.audio_error||["partial","failed"].includes(result.audio_status);
    const row=turn("assistant",partial?(result.answer_partial||"本轮回答未完成。"):(result.answer||"没有获得可确认的回答。"),result.uncertain||partial);
    if(label)row.children[0].textContent=label;
    if(result.background_execution)row.append(node("p","small",result.background_execution==="reused_same_object"?"已复用本对象的背景介绍。":"本轮生成的原书背景介绍。"));
    if(result.uncertain)row.append(node("p","small","这条回答存在不确定性，请核对原书。"));
    if(partial||audioFailed)row.append(node("p","turn-warning",result.error||result.audio_error||"本轮音频不完整。"));
    for(const evidence of result.evidence||[]) {
      const details=node("details","object-evidence"),summary=node("summary","","原书第 "+evidence.page_number+" 页依据");
      details.append(summary,node("blockquote","",evidence.quote||evidence.description||"请对照该页核验。"));row.append(details);
    }
    replayButton(view,result.audio_url,"播放回答",row);
    if(!result.audio_url)for(const chunk of result.audio_chunks||[])replayButton(view,typeof chunk==="string"?chunk:chunk.url||chunk.audio_url,"重听已生成片段",row);
    return !partial&&!audioFailed;
  }
  async function request(view, suffix, body, context, onTranscript, streaming=true) {
    const revision=view.revision,controller=new AbortController();view.controller=controller;
    const requestId=crypto.randomUUID();view.requestId=requestId;
    if(body instanceof FormData)body.append("request_id",requestId);else body={...body,request_id:requestId};
    const timer=setTimeout(()=>controller.abort(),180000),epoch=audioEpoch;
    const isForm=body instanceof FormData;let reader=null;
    let received=false,gotPCM=false,queue=[],playing=false,result=null,failure=null;
    function playNext() {
      if(!live(view,revision)||epoch!==audioEpoch||playing||!queue.length)return;
      const chunk=queue.shift(),url=safeURL(chunk.url||chunk.audio_url);if(!url){playNext();return;}
      playing=true;audio.src=url;audio.onplaying=()=>metric(view,context,chunk.turn_id,"audio_playing");
      audio.onended=()=>{playing=false;playNext();};audio.onerror=()=>{playing=false;message("音频片段播放失败，请核对文字。");playNext();};
      audio.play().catch(()=>message("浏览器暂停了音频，回答完成后可点击“播放回答”。"));
    }
    function dispatch(type,data) {
      if(!live(view,revision))return;
      if(type==="stage")message(data.message||"正在核对绘本依据…");
      else if(type==="transcript")onTranscript?.(data.text||data.transcript||"");
      else if(type==="audio_pcm"&&epoch===audioEpoch) {
        if(!gotPCM){gotPCM=true;queue=[];audio.pause();}received=true;pcmChunk(data,view,context,epoch);
      } else if(type==="audio"&&!gotPCM&&epoch===audioEpoch){received=true;queue.push(data);playNext();}
      else if(type==="result")result=data;
      else if(type==="error")failure=data.detail||"对象助手未完成回答。";
    }
    try {
      const response=await fetch(path(view,suffix),{method:"POST",headers:isForm?{}:{"Content-Type":"application/json"},body:isForm?body:JSON.stringify(body),signal:controller.signal});
      if(!live(view,revision))return null;
      if(!response.ok){
        const data=await response.json().catch(()=>({}));
        if(response.status===404&&suffix===""&&data.detail==="Not Found"){
          const error=new Error("对象识别接口尚未加载，请重启工作台，然后刷新页面。");
          error.code="object_interface_unavailable";throw error;
        }
        throw new Error(typeof data.detail==="string"?data.detail:"请求失败，请稍后重试。");
      }
      if(!streaming||!response.headers.get("content-type")?.includes("text/event-stream"))return await response.json();
      reader=response.body.getReader();const decoder=new TextDecoder();let buffer="";
      const parse=block=>{let type="message",lines=[];for(const line of block.split("\n")){if(line.startsWith("event:"))type=line.slice(6).trim();if(line.startsWith("data:"))lines.push(line.slice(5).trimStart());}if(lines.length)dispatch(type,JSON.parse(lines.join("\n")));};
      while(live(view,revision)) {
        const part=await reader.read();buffer+=decoder.decode(part.value||new Uint8Array(),{stream:!part.done}).replace(/\r\n/g,"\n");let end;
        while((end=buffer.indexOf("\n\n"))>=0){parse(buffer.slice(0,end));buffer=buffer.slice(end+2);}
        if(part.done){if(buffer.trim())parse(buffer);break;}
      }
      if(!live(view,revision))return null;
      if(failure)throw new Error(failure);
      if(!result)throw new Error("回答提前结束，未收到完整结果。");
      result._windowStreamedAudio=received;
      return result;
    } finally {
      clearTimeout(timer);if(reader)reader.releaseLock();if(view.controller===controller)view.controller=null;
    }
  }
  async function background(view) {
    const revision=view.revision;
    $("object-background-retry").classList.add("hidden");
    message("正在查找这个对象的原书背景与关系…");
    try {
      const result=await request(view,"/background",{session_id:view.parent,voice:view.voice},{kind:"object_background",started:performance.now()},null,false);
      if(!live(view,revision)||!result)return;
      render(view,result,"相关介绍与背景");
      if(result.background_state!=="supported")$("object-background-retry").classList.remove("hidden");
      message(result.background_state==="supported"?"背景介绍完成，可展开原书依据核对，或继续提问。":"原书背景暂无法确认，可核对原书或继续提问。");
    } catch(error) {
      if(live(view,revision)){
        const row=turn("assistant","背景介绍暂未完成："+(error.name==="AbortError"?"等待超时，可继续提问。":error.message),true);
        row.children[0].textContent="相关介绍与背景";
        $("object-background-retry").classList.remove("hidden");
        message("对象已识别，背景介绍暂不可用，仍可向它提问。");
      }
    }
  }
  async function open(snapshot) {
    close(false);stopAnswer();
    const view={...snapshot,revision:++sequence,agentId:"object-"+crypto.randomUUID().replaceAll("-",""),agent:null,ready:false,working:false,mic:null,openingMic:false};current=view;
    const revision=view.revision;
    view.agent={id:view.agentId,session_id:view.agentId};
    panel.classList.remove("hidden");$("object-title").textContent="正在认识这个朋友…";$("object-avatar").removeAttribute("src");
    $("object-background-retry").classList.add("hidden");keepVisible();
    $("object-history").replaceChildren();$("object-question").value="";controls(view,true);message("视频已暂停，正在识别点选对象…");
    const context={kind:"touch",started:performance.now()};unlock();
    try {
      const data=await request(view,"",{x:view.x,y:view.y,time:view.time,frame:view.frame,session_id:view.parent,voice:view.voice,agent_id:view.agentId},context,null,false);
      if(!live(view,revision)||!data)return;
      if(!data.agent){view.agent=null;$("object-title").textContent="还没有认清目标";render(view,data.result);message("请点击另一个清晰的人物或物品。");return;}
      view.agent=data.agent;
      view.ready=true;
      $("object-title").textContent=view.agent.label+" · 小助手";
      $("object-meta").textContent="第 "+view.agent.page_number+" 页 · "+fmt(view.agent.time)+" 的画面";
      const avatar=imageURL(view.agent.avatar_url);if(avatar)$("object-avatar").src=avatar;
      render(view,data.result);message("只在这个窗口聊天。可输入问题，或点击“说话”。");
      if(data.result.audio_url){audio.src=safeURL(data.result.audio_url)||"";audio.onplaying=()=>metric(view,context,data.result.turn_id,"audio_playing");audio.play().catch(()=>message("点击“播放回答”收听介绍。"));}
      $("object-question").focus();
      if(view.agent.background_available===true)await background(view);
    } catch(error) {if(live(view,revision)){
      view.agent=null;const unavailable=error.code==="object_interface_unavailable";
      $("object-title").textContent=unavailable?"对象助手需要重启工作台":"本轮识别未完成";
      turn("assistant","本轮识别未完成："+(error.name==="AbortError"?"等待超时，请重新点选。":error.message),true);
      message(unavailable?"请重启工作台并刷新页面，再点选目标。":"请重新点击目标。");
    }}
    finally {if(live(view,revision))controls(view,false);}
  }
  function cancelServer(view, requestId=view?.requestId) {
    if(!view?.agentId||!requestId)return Promise.resolve();
    return fetch(path(view,"/cancel")+"?session_id="+encodeURIComponent(view.parent)+"&request_id="+encodeURIComponent(requestId),{method:"POST",keepalive:true}).catch(()=>{});
  }
  function close(focus=true) {
    endDrag();
    const view=current;current=null;
    if(view){view.revision++;view.controller?.abort();cancelServer(view);stopMic(view,true);}
    stopAudio();panel.classList.add("hidden");
    if(focus&&view?.returnFocus)view.returnFocus.focus?.();
  }
  async function interrupt() {
    const view=current;if(!view)return;
    const requestId=view.requestId;view.revision++;const revision=view.revision;view.controller?.abort();stopMic(view,true);stopAudio();message("正在中断本轮回答…");
    await cancelServer(view,requestId);
    if(current===view&&view.revision===revision){controls(view,false);message("已中断。你可以继续向这个对象提问。");}
  }
  async function submit(view, suffix, body, context, row) {
    if(!live(view)||view.working)return;
    view.revision++;const revision=view.revision;stopAnswer();stopAudio();unlock();controls(view,true);message("正在查找故事依据并核对回答…");
    try {
      const result=await request(view,suffix,body,context,text=>{if(text){Language.noteHeard(text);row.children[1].textContent=text;}});
      if(!live(view,revision)||!result)return;
      if(result.transcript){Language.noteHeard(result.transcript);row.children[1].textContent=result.transcript;}
      const complete=render(view,result);message(result.uncertain?"无法完全确认，请展开原书依据核对。":"回答完成 · 语音播放事件仅作计时参考。");
      if(complete&&!result._windowStreamedAudio&&result.audio_url){audio.src=safeURL(result.audio_url)||"";audio.onplaying=()=>metric(view,context,result.turn_id,"audio_playing");audio.play().catch(()=>message("点击“播放回答”收听。"));}
    } catch(error) {
      if(live(view,revision)){stopAudio();turn("assistant","本轮未完成："+(error.name==="AbortError"?"等待超时，可重试。":error.message),true);message("请稍后重试；未把失败当作成功回答。");}
    } finally {if(live(view,revision))controls(view,false);}
  }
  async function startMic(view) {
    if(!live(view)||view.working||!view.agent||view.openingMic)return;
    if(!navigator.mediaDevices?.getUserMedia){message("无法录音，请使用 localhost 或 HTTPS 并允许麦克风。");return;}
    const revision=view.revision;view.openingMic=true;stopAnswer();stopAudio();unlock();
    let stream,ctx;
    try {
      stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true},video:false});
      if(!live(view,revision)){stream.getTracks().forEach(t=>t.stop());return;}
      const Ctx=window.AudioContext||window.webkitAudioContext;ctx=new Ctx();await ctx.resume();
      if(!live(view,revision)){stream.getTracks().forEach(t=>t.stop());await ctx.close();return;}
      const source=ctx.createMediaStreamSource(stream),processor=ctx.createScriptProcessor(2048,1,1),silent=ctx.createGain();silent.gain.value=0;
      source.connect(processor);processor.connect(silent);silent.connect(ctx.destination);
      const mic={stream,ctx,source,processor,silent,chunks:[],length:0,started:performance.now(),lastVoice:null,heard:false};view.mic=mic;
      processor.onaudioprocess=event=>{
        if(!live(view,revision)||view.mic!==mic)return;
        const chunk=new Float32Array(event.inputBuffer.getChannelData(0));mic.chunks.push(chunk);mic.length+=chunk.length;
        let power=0;for(const sample of chunk)power+=sample*sample;
        if(Math.sqrt(power/chunk.length)>.018){let last=chunk.length-1;while(last>=0&&Math.abs(chunk[last])<=.018)last--;if(last>=0){mic.heard=true;mic.lastVoice=performance.now()-(chunk.length-1-last)/ctx.sampleRate*1000;}}
      };
      mic.timer=setInterval(()=>{const now=performance.now();if(mic.heard&&now-mic.lastVoice>450||now-mic.started>60000)stopMic(view);else if(!mic.heard&&now-mic.started>10000){message("未检测到清晰话音，请再试一次。");stopMic(view,true);}},60);
      $("object-microphone").textContent="● 结束并发送";$("object-microphone").classList.add("recording");$("object-send").disabled=true;
      message("正在听…说完后自动发送，只进入对象对话。");
    } catch(error){stream?.getTracks().forEach(t=>t.stop());if(ctx&&ctx.state!=="closed")await ctx.close();if(live(view,revision))message("无法使用麦克风："+error.message);}
    finally{view.openingMic=false;}
  }
  async function stopMic(view,cancel=false) {
    const mic=view.mic;if(!mic)return;view.mic=null;clearInterval(mic.timer);mic.processor.onaudioprocess=null;
    mic.source.disconnect();mic.processor.disconnect();mic.silent.disconnect();mic.stream.getTracks().forEach(t=>t.stop());
    const rate=mic.ctx.sampleRate,revision=view.revision;await mic.ctx.close();
    if(!live(view,revision))return;
    $("object-microphone").textContent="● 说话";$("object-microphone").classList.remove("recording");controls(view,false);
    if(cancel||!mic.heard)return;
    const samples=new Float32Array(mic.length);let offset=0;for(const chunk of mic.chunks){samples.set(chunk,offset);offset+=chunk.length;}
    const form=new FormData();form.append("audio",encodeWav(samples,rate),"object-question.wav");form.append("session_id",view.parent);if(view.voice)form.append("voice",view.voice);
    const row=turn("user","语音提问，正在转写…");await submit(view,"/voice/stream",form,{kind:"mic",started:mic.lastVoice},row);
  }
  $("object-form").onsubmit=async event=>{event.preventDefault();const view=current,question=$("object-question").value.trim();if(!view?.ready||view.working||view.mic||!question)return;$("object-question").value="";Language.noteHeard(question);await submit(view,"/ask/stream",{question,session_id:view.parent,voice:view.voice||Language.ttsVoice()},{kind:"text",started:performance.now()},turn("user",question));};
  $("object-microphone").onclick=async()=>{const view=current;if(view)await(view.mic?stopMic(view):startMic(view));};
  $("object-close").onclick=()=>close();$("object-stop").onclick=interrupt;
  $("object-background-retry").onclick=async()=>{
    const view=current;if(!view?.ready||view.working||view.mic||view.openingMic)return;
    view.revision++;const revision=view.revision;stopAudio();controls(view,true);
    try{await background(view);}finally{if(live(view,revision))controls(view,false);}
  };
  panel.onclick=event=>event.stopPropagation();panel.onkeydown=event=>{event.stopPropagation();if(event.key==="Escape"){event.preventDefault();close();}};
  window.addEventListener("beforeunload",()=>close(false));
  return {open,close,stopAudio};
})();
