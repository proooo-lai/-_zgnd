/* Annotations are attached only to the actual, reviewed video frame. */
"use strict";
window.VideoAnnotations = (() => {
  const finite = n => typeof n === "number" && Number.isFinite(n);

  function create({video, frame, layer, getBookId}) {
    let pending = null, active = null;
    function clear() {
      const previous = pending; pending = null; active = null;
      previous?.controller.abort();
      frame.classList.add("hidden"); frame.replaceChildren(); layer.replaceChildren();
    }
    const live = operation => pending === operation && !operation.controller.signal.aborted && getBookId() === operation.book;

    function wait(target, event, signal, start) {
      return new Promise((resolve, reject) => {
        let timer;
        function done(error) {
          clearTimeout(timer); target.removeEventListener(event, success); target.removeEventListener("error", failure);
          signal.removeEventListener("abort", failure); error ? reject(error) : resolve();
        }
        const success = () => done(), failure = () => done(new Error("媒体载入或定位未完成"));
        target.addEventListener(event, success, {once:true}); target.addEventListener("error", failure, {once:true});
        signal.addEventListener("abort", failure, {once:true}); timer = setTimeout(failure, 12000);
        if (signal.aborted) { failure(); return; }
        try { start?.(); } catch (error) { done(error); }
      });
    }
    function validate(result, book) {
      const location = result?.text_location;
      if (!book || location?.status !== "found" || !["locate_text","highlight_text"].includes(location.action)
          || !finite(location.timestamp) || location.timestamp < 0 || result.seek_time !== location.timestamp
          || !Array.isArray(location.frame_size) || location.frame_size.length !== 2
          || !location.frame_size.every(n => Number.isInteger(n) && n > 0 && n <= 12000)
          || !/^[a-f0-9]{64}$/i.test(location.frame_sha256 || "")) return null;
      let url;
      try { url = new URL(location.frame_url, window.location.href); } catch { return null; }
      if (url.origin !== window.location.origin || !url.pathname.startsWith("/media/"+encodeURIComponent(book)+"/turns/")
          || !/\/text-location\/[^/]+\.png$/.test(url.pathname)) return null;
      const presentation = location.action === "highlight_text" ? "highlight" : "outline";
      const boxes = result.boxes;
      if (!Array.isArray(boxes) || !boxes.length || !boxes.every(box => {
        const b = box.bbox;
        return box.kind === "text" && box.source === "verified_video_frame" && box.presentation === presentation
          && Array.isArray(b) && b.length === 4 && b.every(finite) && b[0] >= 0 && b[1] >= 0 && b[2] > 0 && b[3] > 0
          && b[0]+b[2] <= 1.000001 && b[1]+b[3] <= 1.000001;
      })) return null;
      return {location, url:url.href, boxes, presentation};
    }
    function redraw() {
      layer.replaceChildren();
      if (!active) return;
      if (getBookId() !== active.book) { clear(); return; }
      const rect = layer.getBoundingClientRect(), [w,h] = active.location.frame_size;
      const scale = Math.min(rect.width/w,rect.height/h), width = w*scale, height = h*scale;
      const left = (rect.width-width)/2, top = (rect.height-height)/2;
      for (const box of active.boxes) {
        const [x,y,bw,bh] = box.bbox, element = document.createElement("div");
        element.className = "video-text-"+active.presentation; element.title = box.label || active.location.target_text;
        Object.assign(element.style,{left:left+x*width+"px",top:top+y*height+"px",width:bw*width+"px",height:bh*height+"px"});
        layer.append(element);
      }
    }
    async function apply(result) {
      clear();
      const book = getBookId(), value = validate(result,book);
      if (!value || !video.src) return false;
      const operation = {book,controller:new AbortController(),expected:null}; pending = operation;
      const image = new Image(); image.alt = "已核对的实际视频暂停帧";
      try {
        video.pause();
        await Promise.all([
          wait(image,"load",operation.controller.signal,()=>{image.src=value.url;}),
          video.readyState >= 1 ? Promise.resolve() : wait(video,"loadedmetadata",operation.controller.signal)
        ]);
        if (!live(operation) || image.naturalWidth !== value.location.frame_size[0] || image.naturalHeight !== value.location.frame_size[1]
            || video.videoWidth !== image.naturalWidth || video.videoHeight !== image.naturalHeight
            || !finite(video.duration) || value.location.timestamp >= video.duration) throw new Error("视频帧尺寸或时间不匹配");
        const when = value.location.timestamp;
        operation.expected = when;
        if (video.seeking || Math.abs(video.currentTime-when) > .02) {
          await wait(video,"seeked",operation.controller.signal,()=>{video.currentTime=when;});
        }
        if (!live(operation) || video.seeking || Math.abs(video.currentTime-when) > .05) throw new Error("未暂停到目标帧");
        video.pause(); pending = null; active = {...value,book,image};
        frame.replaceChildren(image); frame.classList.remove("hidden"); redraw(); return true;
      } catch {
        if (pending === operation) clear();
        return false;
      }
    }
    video.addEventListener("seeking",()=>{
      if (pending?.expected !== null && pending?.expected !== undefined && Math.abs(video.currentTime-pending.expected) <= .05) return;
      clear();
    });
    for (const event of ["play","emptied","loadstart","error"]) video.addEventListener(event,clear);
    window.addEventListener("resize",redraw);
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(redraw).observe(layer);
    document.addEventListener?.("fullscreenchange",redraw);
    return {apply,clear,redraw,source:()=>active?.image || video,get active(){return Boolean(active);}};
  }
  return {create};
})();
