import { findObjectAt, mapDisplayPoint } from "/src/domain.mjs";
import { bookInteractionState, questionPayload } from "/src/book-view.mjs";

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
  bookRequest: 0,
};

const pageImage = $("#page-image");
const readerStage = $("#reader-stage");
const objectOverlay = $("#object-overlay");
const feedback = $("#reader-feedback");
const questionInput = $("#question-input");
const sendButton = $(".send-button");
const conversation = $("#conversation");
const workflowOutput = $("#workflow-output");
const bookSelector = $("#book-selector");
const uploadForm = $("#book-upload-form");
const analyzeButton = $("#analyze-book-button");
const modelNotice = $("#model-notice");

function analysisStatus(book) {
  return book?.analysis_status ?? (book?.mode === "synthetic-local-demo" ? "demo-manual" : "pending");
}

function contentReady() {
  return bookInteractionState(state.book).contentReady;
}

function updateBookControls() {
  const ready = contentReady();
  const page = currentPage();
  const status = analysisStatus(state.book);
  $("#read-button").disabled = !ready || !page?.text;
  $("#recognize-button").disabled = !ready || !(page?.objects?.length);
  $("#preview-button").disabled = state.book?.mode !== "synthetic-local-demo";
  questionInput.disabled = !ready;
  $("#voice-button").disabled = !ready;
  sendButton.disabled = !ready;
  $("#motion-label").hidden = state.book?.mode !== "synthetic-local-demo";
  $("#input-status").textContent = ready
    ? "语音由浏览器提供，处理方式因浏览器而异"
    : "内容待分析：问答、朗读、识物尚不可用";
  analyzeButton.hidden = !state.book || state.book.mode === "synthetic-local-demo" || status === "completed";
  analyzeButton.disabled = status === "analyzing";
  analyzeButton.textContent = status === "failed" ? "重试内容分析" : status === "analyzing" ? "内容分析中" : "开始内容分析";
  if (modelNotice) {
    modelNotice.textContent = status === "demo-manual"
      ? "本地演示 · 人工内容"
      : status === "completed"
        ? `百炼视觉分析 · ${state.book.analysis_version ?? "已记录模型版本"}`
        : status === "analyzing" ? "百炼视觉分析 · 分析中" : status === "failed" ? "百炼视觉分析 · 分析失败" : "百炼视觉分析 · 待执行";
  }
}

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
  $("#page-text").textContent = page.text || bookInteractionState(state.book).pageTextFallback;
  $("#previous-page").disabled = state.pageIndex === 0;
  $("#next-page").disabled = state.pageIndex === state.book.pages.length - 1;
  renderPageChoices();
  renderObjectOverlay();
  updateBookControls();
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
  if (mode === "recognize" && !contentReady()) return;
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
  $("#run-summary").textContent = run.status === "awaiting-analysis"
    ? "页面已导入 · 待分析"
    : run.status === "running" ? "内容分析中"
      : run.status === "failed" ? "分析失败"
        : run.outcome === "refused" ? "已拒答" : "已完成";

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
  if (!contentReady()) return;
  const askedBookId = state.book.id;
  appendMessage("user", question);
  sendButton.disabled = true;
  $("#input-status").textContent = "正在查找页面原文";

  try {
    const response = await fetch("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(questionPayload(state.book, question, { inputMode, asrRaw })),
    });
    const result = await response.json();
    if (state.book?.id !== askedBookId) return;
    if (!response.ok) throw new Error(result.error ?? "暂时无法处理这个问题。");

    const answerMessage = appendMessage("assistant", result.answer, result.evidence);
    const runResponse = await fetch(`/api/runs/${encodeURIComponent(result.run_id)}`);
    if (runResponse.ok) {
      const run = await runResponse.json();
      if (state.book?.id !== askedBookId) return;
      renderRun(run);
    }
    $("#workflow-details").open = true;
    $("#input-status").textContent = result.answerable ? "已附页面来源" : "没有找到页面依据";
    if (inputMode === "voice") {
      speakMessage(result.answer, answerMessage.querySelector(".answer-speech"), { automatic: true });
    }
  } catch (error) {
    if (state.book?.id !== askedBookId) return;
    appendMessage("assistant", error.message || "暂时无法连接本地服务。");
    $("#input-status").textContent = "请求未完成";
  } finally {
    sendButton.disabled = !contentReady();
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
  if (state.book?.mode !== "synthetic-local-demo") return;
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
  if (!contentReady() || !currentPage()?.text) return;
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
  if (!contentReady()) return;
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
  if (!contentReady() || state.mode !== "recognize" || event.target.closest("button")) return;
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

function renderBookList(books, selectedId) {
  bookSelector.replaceChildren();
  for (const book of books) {
    const option = document.createElement("option");
    option.value = book.id;
    option.textContent = book.title + (book.analysis_status === "pending" ? " · 待分析" : "");
    bookSelector.append(option);
  }
  bookSelector.value = selectedId ?? books[0]?.id ?? "";
}

async function refreshBookList(selectedId) {
  const response = await fetch("/api/books");
  if (!response.ok) throw new Error("无法获取绘本列表。");
  const { books } = await response.json();
  renderBookList(books, selectedId);
}

function showBook(book) {
  if (state.previewTimer) stopPreview();
  if (state.recognition) state.recognition.stop();
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
  state.book = book;
  state.pageIndex = 0;
  state.mode = "read";
  state.inputMode = "text";
  state.asrRaw = "";
  questionInput.value = "";
  conversation.replaceChildren();
  workflowOutput.replaceChildren();
  $("#workflow-details").open = false;
  $("#run-summary").textContent = "尚无记录";
  $("#recognize-button").setAttribute("aria-pressed", "false");
  $("#read-button").querySelector("span").textContent = "朗读";
  $("#book-title").textContent = book.title;
  $(".topbar-book").textContent = "绘本 · " + book.title;
  $("#source-notice").textContent = book.source_notice;
  $("#book-analysis-status").textContent = bookInteractionState(book).statusLabel;
  document.title = book.title + " · 纸书智伴";
  bookSelector.value = book.id;
  selectPage(0);

  if (bookInteractionState(book).contentReady && book.mode === "synthetic-local-demo") {
    const opening = book.pages[0];
    if (opening?.text) {
      const firstPassage = opening.text.split("。", 1)[0] + "。";
      appendMessage("assistant", firstPassage,
        [{ evidence_id: opening.evidence_id, page_number: opening.page_number, excerpt: firstPassage }]);
    }
  } else if (!contentReady()) {
    appendMessage("assistant", "页面预览已就绪。OCR/VLM 尚未分析这本书，因此暂不能问答、识物或朗读。");
  }
}

async function loadBook(bookId) {
  const requestId = ++state.bookRequest;
  const response = await fetch("/api/books/" + encodeURIComponent(bookId));
  if (!response.ok) throw new Error("无法载入所选绘本。");
  const book = await response.json();
  if (requestId === state.bookRequest) showBook(book);
}

async function analyzeBook() {
  if (!state.book || analyzeButton.disabled || analyzeButton.hidden) return;
  const bookId = state.book.id;
  analyzeButton.disabled = true;
  $("#book-analysis-status").textContent = "分析中 · 仅可预览";
  $("#book-upload-status").textContent = "正在调用百炼视觉模型逐页分析，期间不会启用问答或识物……";
  try {
    const response = await fetch(`/api/books/${encodeURIComponent(bookId)}/analyze`, { method: "POST" });
    const result = await response.json();
    if (result.book && state.book?.id === bookId) {
      showBook(result.book);
      if (result.run) {
        renderRun(result.run);
        $("#workflow-details").open = true;
      }
      await refreshBookList(bookId);
    }
    if (!response.ok) throw new Error(result.error ?? "内容分析未完成。");
    $("#book-upload-status").textContent = "内容分析完成：已保存模型版本和逐页结果。";
  } catch (error) {
    if (state.book?.id === bookId) {
      $("#book-analysis-status").textContent = "分析失败 · 可重试";
      $("#book-upload-status").textContent = error.message || "内容分析未完成，请重试。";
    }
  } finally {
    if (state.book?.id === bookId) updateBookControls();
  }
}

async function initialize() {
  try {
    await refreshBookList();
    await loadBook(bookSelector.value);
  } catch {
    $("#book-title").textContent = "绘本载入失败";
    setFeedback("请确认本地演示服务正在运行。", "warning");
  }
}

bookSelector.addEventListener("change", async () => {
  try {
    await loadBook(bookSelector.value);
  } catch (error) {
    setFeedback(error.message, "warning");
  }
});

uploadForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = $("#book-upload-file").files[0];
  const title = $("#book-upload-title").value.trim();
  const status = $("#book-upload-status");
  if (!file || !title || !$("#rights-confirmed").checked) return;
  if (file.size > 20 * 1024 * 1024 || file.type !== "application/pdf") {
    status.textContent = "请选择不超过 20 MB 的 PDF 文件。";
    return;
  }
  const button = $("#book-upload-button");
  button.disabled = true;
  status.textContent = "正在校验 PDF 并生成逐页预览……";
  try {
    const response = await fetch("/api/books?title=" + encodeURIComponent(title), {
      method: "POST",
      headers: { "content-type": "application/pdf", "x-rights-confirmed": "true" },
      body: file,
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error ?? "导入未完成。");
    await refreshBookList(result.book_id);
    ++state.bookRequest;
    showBook(result.book);
    const runResponse = await fetch("/api/runs/" + encodeURIComponent(result.run_id));
    if (runResponse.ok) {
      const run = await runResponse.json();
      if (state.book?.id === result.book_id) {
        renderRun(run);
        $("#workflow-details").open = true;
      }
    }
    uploadForm.reset();
    status.textContent = "导入完成：已生成逐页预览，内容理解仍待模型处理。";
  } catch (error) {
    status.textContent = error.message || "导入未完成，请重试。";
  } finally {
    button.disabled = false;
  }
});

analyzeButton.addEventListener("click", analyzeBook);

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
  if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLSelectElement) return;
  if (event.key === "ArrowLeft") selectPage(state.pageIndex - 1);
  if (event.key === "ArrowRight") selectPage(state.pageIndex + 1);
});

initialize();
