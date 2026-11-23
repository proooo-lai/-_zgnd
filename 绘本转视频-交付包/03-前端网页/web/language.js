/* 语言中枢（前端架构层）
 *
 * 一本绘本只有一种语言：导入时由服务端识别，写进 book.language；前端只在这里定一次，
 * 其它地方一律读这里，不再出现「语言」选项。
 *
 *   BOOK  绘本语言   ← book.language（服务端识别中文 / English）
 *   HEARD 提问语言   ← 当前文字问题或语音转写，只影响本轮回答
 *
 * 下游约定：
 *   - 成片：旁白、字幕、视频提示词都用 promptLanguage()（= BOOK）
 *   - 回答：answerLanguage()（= HEARD ?? BOOK），提问用什么语言就用什么语言回答
 *   - 朗读：ttsVoice()（语言 → 默认音色），用户手动选过音色则不覆盖
 *   - ASR：不预设语言，音频交给模型自动识别；转写回来后用 noteHeard() 记录
 */
(function (global) {
  "use strict";

  const LABELS = { zh: "中文", en: "English" };
  const DEFAULT_VOICE = { zh: "Cherry", en: "Ethan" };
  const listeners = new Set();
  const State = { BOOK: null, HEARD: null };

  function normalize(code) {
    if (!code) return null;
    const c = String(code).trim().toLowerCase();
    if (c === "cn" || c === "chinese" || c.startsWith("zh")) return "zh";
    if (c === "eng" || c === "english" || c.startsWith("en")) return "en";
    return null;
  }

  function unquoted(text) {
    return text.replace(/“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"/g," ").replace(/(?<!\w)'[^'\n]+'(?!\w)/g," ");
  }

  /** 当前指令语言，引用的目标原文和短的外语角色名不改变回答语言。 */
  function detect(text) {
    if (!text) return null;
    let s = unquoted(String(text));
    if (/用英[文语]|英[文语]回答|(?:answer|reply|respond)\s+in\s+English/i.test(s)) return "en";
    if (/用中[文语]|中文回答|(?:answer|reply|respond)\s+in\s+Chinese/i.test(s)) return "zh";
    if (!/[\u3400-\u9fffA-Za-z]/.test(s)) s=String(text);
    const cjk = (s.match(/[\u3400-\u9fff]/g) || []).length;
    const latin = (s.match(/[A-Za-z]/g) || []).length;
    if (!cjk && !latin) return null;
    return cjk >= latin * 0.3 ? "zh" : "en";
  }

  function snapshot() {
    const answer = State.HEARD || State.BOOK;
    return {
      book: State.BOOK,
      heard: State.HEARD,
      answer: answer,
      bookLabel: LABELS[State.BOOK] || "待识别",
      heardLabel: LABELS[State.HEARD] || null,
      answerLabel: LABELS[answer] || "待识别",
    };
  }

  function emit() {
    const snap = snapshot();
    listeners.forEach((fn) => {
      try { fn(snap); } catch (error) { console.error("[language]", error); }
    });
  }

  function onChange(fn) {
    listeners.add(fn);
    try { fn(snapshot()); } catch (error) { console.error("[language]", error); }
    return () => listeners.delete(fn);
  }

  /** 打开绘本时调用一次：绘本语言确定下来，后续全部跟随。 */
  function setBook(book) {
    State.BOOK = normalize(book && book.language);
    State.HEARD = null;
    emit();
    return State.BOOK;
  }

  /** 每次文字提问或语音转写调用；不改绘本语言。 */
  function noteHeard(text) {
    const code = detect(text);
    if (code && code !== State.HEARD) { State.HEARD = code; emit(); }
    return code;
  }

  function clearHeard() {
    if (State.HEARD) { State.HEARD = null; emit(); }
  }

  function ttsVoice(code) { return DEFAULT_VOICE[code || State.HEARD || State.BOOK] || ""; }
  function answerLanguage() { return State.HEARD || State.BOOK; }
  function promptLanguage() { return State.BOOK; }
  function label(code) { return LABELS[code] || "待识别"; }

  function statusText() {
    if (!State.BOOK) return "语言：自动识别（导入后确定中文 / English），无需手动选择。";
    const heard = State.HEARD && State.HEARD !== State.BOOK ? " · 最近提问用 " + label(State.HEARD) : "";
    return "绘本语言：" + label(State.BOOK) + "（自动识别）" + heard + " · 中文提问用中文回答，英文提问用英文回答，回答语音跟随本轮语言。";
  }

  global.Language = {
    LABELS, DEFAULT_VOICE,
    detect, normalize, setBook, noteHeard, clearHeard,
    ttsVoice, answerLanguage, promptLanguage, label, statusText,
    snapshot, onChange,
    get BOOK() { return State.BOOK; },
    get HEARD() { return State.HEARD; },
  };
})(window);
