import test from "node:test";
import assert from "node:assert/strict";
import { bookInteractionState, questionPayload } from "../src/book-view.mjs";

test("an imported pending book exposes page previews without content interactions", () => {
  const result = bookInteractionState({ id: "new-book", analysis_status: "pending" });
  assert.equal(result.contentReady, false);
  assert.match(result.statusLabel, /待分析/);
  assert.match(result.pageTextFallback, /OCR/);
});

test("only the explicit manual demo and completed analysis allow content interactions", () => {
  assert.equal(bookInteractionState({ id: "demo", analysis_status: "demo-manual" }).contentReady, true);
  assert.equal(bookInteractionState({ id: "ready", analysis_status: "completed" }).contentReady, true);
  assert.equal(bookInteractionState({ id: "unknown", analysis_status: "failed" }).contentReady, false);
});

test("question payload always identifies the selected book", () => {
  assert.deepEqual(questionPayload({ id: "book-b" }, "这是谁？", { inputMode: "voice", asrRaw: "这是谁啊" }), {
    book_id: "book-b",
    question: "这是谁？",
    input_mode: "voice",
    asr_raw: "这是谁啊",
  });
});
