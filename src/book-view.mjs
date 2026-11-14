export function bookInteractionState(book) {
  const status = book?.analysis_status ?? (book?.mode === "synthetic-local-demo" ? "demo-manual" : "pending");
  if (status === "demo-manual") {
    return { contentReady: true, statusLabel: "人工演示内容", pageTextFallback: "" };
  }
  if (status === "completed") {
    return { contentReady: true, statusLabel: "内容已分析", pageTextFallback: "" };
  }
  if (status === "analyzing") {
    return { contentReady: false, statusLabel: "分析中 · 仅可预览", pageTextFallback: "内容分析进行中，暂无本页原文。" };
  }
  if (status === "failed") {
    return { contentReady: false, statusLabel: "分析失败 · 可重试", pageTextFallback: "内容分析失败，暂无本页原文。" };
  }
  return {
    contentReady: false,
    statusLabel: status === "pending" ? "待分析 · 仅可预览" : "分析未完成 · 仅可预览",
    pageTextFallback: "尚未接入 OCR，暂无本页原文。",
  };
}

export function questionPayload(book, question, { inputMode = "text", asrRaw = "" } = {}) {
  return { book_id: book.id, question, input_mode: inputMode, asr_raw: asrRaw };
}
