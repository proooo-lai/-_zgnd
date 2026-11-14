import { findObjectAt, mapDisplayPoint } from "/src/domain.mjs";

const $ = (selector) => document.querySelector(selector);
const state = {
  book: null,
  pageIndex: 0,
  mode: "read",
  previewTimer: null,
  previewIndex: 0,
  recognition: null,
  inputMode: "text",
  asrRaw: "",
};

const pageImage = $("#page-image");
const readerStage = $("#reader-stage");
const objectOverlay = $("#object-overlay");
const feedback = $("#reader-feedback");
const questionInput = $("#question-input");
const sendButton = $(".send-button");
const conversation = $("#conversation");
const workflowOutput = $("#workflow-output");

function currentPage() {
  return state.book?.pages[state.pageIndex] ?? null;
}

function setFeedback(message, tone = "") {
  feedback.textContent = message;
  feedback.dataset.tone = tone;
}

function selectPage(index, { preserveFeedback = false } = {}) {
  if (!state.book?.pages.length) return;
  state.pageIndex = Math.max(0, Math.min(index, state.book.pages.length - 1));
  const page = currentPage();
  pageImage.src = page.asset;
  pageImage.alt = `《${state.book.title}》第 ${page.page_number} 页`;
  $("#page-number").textContent = `第 ${page.page_number} 页`;
  $("#page-position").textContent = `${state.pageIndex + 1} / ${state.book.pages.length}`;
  $("#page-text").textContent = page.text;
  $("#previous-page").disabled = state.pageIndex === 0;
  $("#next-page").disabled = state.pageIndex === state.book.pages.length - 1;
  renderPageChoices();
  renderObjectOverlay();
  readerStage.classList.remove("is-wobbling");
  if (!preserveFeedback) setFeedback("", "");
}

function renderPageChoices() {
  const choices = $("#page-choices");
  choices.replaceChildren();
  state.book.pages.forEach((page, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "page-choice";
    button.textContent = String(page.page_number);
    button.setAttribute("aria-label", `第 ${page.page_number} 页`);
    if (index === state.pageIndex) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => selectPage(index));
    choices.append(button);
  });
}

function renderObjectOverlay() {
  objectOverlay.replaceChildren();
  if (state.mode !== "recognize") return;

  const page = currentPage();
  const stageRect = readerStage.getBoundingClientRect();
  const scale = Math.min(stageRect.width / page.width, stageRect.height / page.height);
  const renderedWidth = page.width * scale;
  const renderedHeight = page.height * scale;
  objectOverlay.style.left = `${(stageRect.width - renderedWidth) / 2}px`;
  objectOverlay.style.top = `${(stageRect.height - renderedHeight) / 2}px`;
  objectOverlay.style.width = `${renderedWidth}px`;
  objectOverlay.style.height = `${renderedHeight}px`;

  for (const object of page.objects ?? []) {
    const box = document.createElement("div");
    box.className = "object-box";
    box.style.left = `${object.box[0] * 100}%`;
    box.style.top = `${object.box[1] * 100}%`;
    box.style.width = `${object.box[2] * 100}%`;
    box.style.height = `${object.box[3] * 100}%`;

    const label = document.createElement("span");
    label.textContent = object.label;
    box.append(label);
    objectOverlay.append(box);
  }
}

function setMode(mode) {
  state.mode = mode;
  const active = mode === "recognize";
  const button = $("#recognize-button");
  button.setAttribute("aria-pressed", String(active));
  button.title = active ? "退出画面识物" : "切换画面识物";
  renderObjectOverlay();
  setFeedback(active ? "人工标注演示" : "", active ? "warning" : "");
}

function appendMessage(role, text, evidence = [], { manual = false } = {}) {
  const article = document.createElement("article");
  article.className = `chat-message ${role === "user" ? "chat-user" : "chat-assistant"}`;

  if (role !== "user") {
    const meta = document.createElement("div");
    meta.className = "chat-meta";
    meta.textContent = manual ? "人工演示标注" : "页面原文检索";
    article.append(meta);
  }

  const paragraph = document.createElement("p");
  paragraph.textContent = text;
  article.append(paragraph);

  if (role !== "user") {
    const speechButton = document.createElement("button");
    speechButton.type = "button";
    speechButton.className = "icon-button answer-speech";
    speechButton.setAttribute("aria-label", "朗读这条回答");
    speechButton.setAttribute("aria-pressed", "false");
    speechButton.title = "朗读这条回答";
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#icon-volume");
    icon.append(use);
    speechButton.append(icon);
    speechButton.addEventListener("click", () => speakMessage(text, speechButton));
    article.append(speechButton);
  }

  if (evidence.length) {
    const citation = document.createElement("div");
    citation.className = "chat-citation";
    for (const item of evidence) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "citation-button";
      button.textContent = `原文 · 第 ${item.page_number} 页`;
      button.addEventListener("click", () => {
        const index = state.book.pages.findIndex((page) => page.page_number === item.page_number);
        if (index >= 0) selectPage(index);
      });
      const excerpt = document.createElement("span");
      excerpt.className = "citation-excerpt";
      excerpt.textContent = item.excerpt;
      citation.append(button, excerpt);
    }
    article.append(citation);
  }

  conversation.append(article);
  conversation.scrollTop = conversation.scrollHeight;
  return article;
}

function renderRun(run) {
  workflowOutput.replaceChildren();
  $("#run-summary").textContent = run.outcome === "refused" ? "已拒答" : "已完成";

  const id = document.createElement("p");
  id.className = "workflow-id";
  id.textContent = `run_id · ${run.run_id}`;
  workflowOutput.append(id);

  const steps = document.createElement("ol");
  steps.className = "workflow-steps";
  for (const step of run.steps) {
    const row = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = step.id;
    const status = document.createElement("span");
    status.textContent = step.status;
    row.append(label, status);
    steps.append(row);
  }
  workflowOutput.append(steps);
}

function speakMessage(text, button, { automatic = false } = {}) {
  if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {
    $("#input-status").textContent = "此浏览器不支持语音朗读，回答仍可查看文字。";
    return;
  }

  const wasActive = button.getAttribute("aria-pressed") === "true";
  if (window.speechSynthesis.speaking || window.speechSynthesis.pending) {
    window.speechSynthesis.cancel();
  }
  document.querySelectorAll(".answer-speech").forEach((control) => {
    control.setAttribute("aria-pressed", "false");
    control.title = "朗读这条回答";
  });
  if (wasActive) {
    $("#input-status").textContent = "朗读已停止";
    return;
  }

  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "zh-CN";
  utterance.rate = 0.92;
  utterance.onstart = () => {
    button.setAttribute("aria-pressed", "true");
    button.title = "停止朗读";
    $("#input-status").textContent = "正在使用浏览器朗读回答";
  };
  const finish = (message) => {
    button.setAttribute("aria-pressed", "false");
    button.title = "朗读这条回答";
    $("#input-status").textContent = message;
  };
  utterance.onend = () => finish("回答朗读完成");
  utterance.onerror = () => finish("浏览器朗读未完成，可查看文字回答");
  window.speechSynthesis.speak(utterance);
  if (automatic) $("#input-status").textContent = "回答已生成，正在请求浏览器朗读";
}

async function askQuestion(question, { inputMode = "text", asrRaw = "" } = {}) {
  appendMessage("user", question);
  sendButton.disabled = true;
  $("#input-status").textContent = "正在查找页面原文";

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question, input_mode: inputMode, asr_raw: asrRaw }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error ?? "暂时无法处理这个问题。");

    const answerMessage = appendMessage("assistant", result.answer, result.evidence);
    const runResponse = await fetch(`/api/runs/${encodeURIComponent(result.run_id)}`);
    if (runResponse.ok) renderRun(await runResponse.json());
    $("#workflow-details").open = true;
    $("#input-status").textContent = result.answerable ? "已附页面来源" : "没有找到页面依据";
    if (inputMode === "voice") {
      speakMessage(result.answer, answerMessage.querySelector(".answer-speech"), { automatic: true });
    }
  } catch (error) {
    appendMessage("assistant", error.message || "暂时无法连接本地服务。");
    $("#input-status").textContent = "请求未完成";
  } finally {
    sendButton.disabled = false;
  }
}

function setPreviewButton(active) {
  const button = $("#preview-button");
  const use = button.querySelector("use");
  use.setAttribute("href", active ? "#icon-stop" : "#icon-play");
  button.querySelector("span").textContent = active ? "停止预览" : "互动预览";
  button.title = active ? "停止页面动效演示" : "播放页面动效演示";
}

function stopPreview(message = "") {
  if (state.previewTimer) clearTimeout(state.previewTimer);
  state.previewTimer = null;
  state.previewIndex = 0;
  setPreviewButton(false);
  readerStage.classList.remove("is-wobbling");
  if (message) setFeedback(message, "active");
}

function runPreviewStep() {
  const index = state.previewIndex;
  selectPage(index, { preserveFeedback: true });
  if (index === state.book.pages.length - 1) {
    readerStage.classList.add("is-wobbling");
    setFeedback("页面动效演示 · 不会生成视频文件", "warning");
  }
  state.previewIndex += 1;

  if (state.previewIndex >= state.book.pages.length) {
    state.previewTimer = setTimeout(() => stopPreview("页面动效演示已结束"), 2600);
    return;
  }
  state.previewTimer = setTimeout(runPreviewStep, 2300);
}

function togglePreview() {
  if (state.previewTimer) {
    stopPreview("已停止页面动效演示");
    return;
  }
  setPreviewButton(true);
  state.previewIndex = 0;
  setFeedback("本地页面动效演示", "active");
  runPreviewStep();
}

function speakPage() {
  if (!("speechSynthesis" in window) || !("SpeechSynthesisUtterance" in window)) {
    setFeedback("此浏览器不支持朗读，可展开“本页原文”查看文字。", "warning");
    return;
  }

  if (window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
    $("#read-button").querySelector("span").textContent = "朗读";
    setFeedback("朗读已停止", "");
    return;
  }

  const utterance = new SpeechSynthesisUtterance(currentPage().text);
  utterance.lang = "zh-CN";
  utterance.rate = 0.92;
  utterance.onstart = () => {
    $("#read-button").querySelector("span").textContent = "停止朗读";
    setFeedback("使用浏览器朗读", "active");
  };
  utterance.onend = () => {
    $("#read-button").querySelector("span").textContent = "朗读";
    setFeedback("朗读完成", "");
  };
  utterance.onerror = () => {
    $("#read-button").querySelector("span").textContent = "朗读";
    setFeedback("浏览器朗读未完成", "warning");
  };
  window.speechSynthesis.speak(utterance);
}

function startVoiceInput() {
  const Recognition = window.SpeechRecognition ?? window.webkitSpeechRecognition;
  if (!Recognition) {
    $("#input-status").textContent = "此浏览器没有语音输入，可直接输入文字";
    questionInput.focus();
    return;
  }

  if (state.recognition) {
    state.recognition.stop();
    return;
  }

  const recognition = new Recognition();
  state.recognition = recognition;
  recognition.lang = "zh-CN";
  recognition.interimResults = false;
  recognition.maxAlternatives = 1;
  $("#voice-button").classList.add("is-listening");
  $("#input-status").textContent = "正在听取问题";

  recognition.onresult = (event) => {
    const transcript = event.results?.[0]?.[0]?.transcript ?? "";
    questionInput.value = transcript;
    state.inputMode = "voice";
    state.asrRaw = transcript;
    questionInput.focus();
    $("#input-status").textContent = "识别结果可编辑后发送";
  };
  recognition.onerror = () => {
    state.inputMode = "text";
    state.asrRaw = "";
    $("#input-status").textContent = "语音输入未完成，可直接输入文字";
  };
  recognition.onend = () => {
    state.recognition = null;
    $("#voice-button").classList.remove("is-listening");
  };
  try {
    recognition.start();
  } catch {
    state.recognition = null;
    state.inputMode = "text";
    state.asrRaw = "";
    $("#voice-button").classList.remove("is-listening");
    $("#input-status").textContent = "无法启动语音输入，可直接输入文字";
  }
}

function recognizeObject(event) {
  if (state.mode !== "recognize" || event.target.closest("button")) return;
  const page = currentPage();
  const point = mapDisplayPoint(
    event.clientX,
    event.clientY,
    pageImage.getBoundingClientRect(),
    pageImage.naturalWidth || page.width,
    pageImage.naturalHeight || page.height,
  );
  const object = point ? findObjectAt(point, page.objects ?? []) : null;

  if (!object) {
    setFeedback("此处没有已标注对象，本次未命中。", "warning");
    return;
  }

  setFeedback(`已命中：${object.label} · 人工演示标注`, "active");
  appendMessage("assistant", `${object.label}。${object.description}`, [], { manual: true });
}

async function initialize() {
  try {
    const response = await fetch("/api/book");
    if (!response.ok) throw new Error("无法载入绘本页面。");
    state.book = await response.json();
    $("#book-title").textContent = state.book.title;
    $(".topbar-book").textContent = `绘本演示 · ${state.book.title}`;
    $("#source-notice").textContent = state.book.source_notice;
    document.title = `${state.book.title} · 纸书智伴`;
    selectPage(0);
    const opening = state.book.pages[0];
    const firstPassage = opening.text.split("。", 1)[0] + "。";
    appendMessage(
      "assistant",
      firstPassage,
      [{ evidence_id: opening.evidence_id, page_number: opening.page_number, excerpt: firstPassage }],
    );
  } catch {
    $("#book-title").textContent = "绘本载入失败";
    setFeedback("请确认本地演示服务正在运行。", "warning");
  }
}

$("#previous-page").addEventListener("click", () => selectPage(state.pageIndex - 1));
$("#next-page").addEventListener("click", () => selectPage(state.pageIndex + 1));
$("#read-button").addEventListener("click", speakPage);
$("#recognize-button").addEventListener("click", () => setMode(state.mode === "recognize" ? "read" : "recognize"));
$("#preview-button").addEventListener("click", togglePreview);
$("#voice-button").addEventListener("click", startVoiceInput);
readerStage.addEventListener("click", recognizeObject);

$("#question-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question || sendButton.disabled) return;
  const inputMode = state.inputMode;
  const asrRaw = state.asrRaw;
  state.inputMode = "text";
  state.asrRaw = "";
  questionInput.value = "";
  await askQuestion(question, { inputMode, asrRaw });
});

document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
  if (event.key === "ArrowLeft") selectPage(state.pageIndex - 1);
  if (event.key === "ArrowRight") selectPage(state.pageIndex + 1);
});

initialize();
