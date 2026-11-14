import test from "node:test";
import assert from "node:assert/strict";
import { createServer } from "../server.mjs";

async function withServer(run) {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();

  try {
    await run(`http://127.0.0.1:${address.port}`);
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}

test("serves an original public demo book by default", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/book`);
    const book = await response.json();

    assert.equal(response.status, 200);
    assert.equal(book.id, "original-garden-demo");
    assert.deepEqual(book.pages.map((page) => page.page_number), [1, 2]);
    assert.ok(book.pages.every((page) => page.evidence_id && page.asset));
    assert.ok(book.pages.every((page) => page.asset.endsWith(".svg")));
  });
});

test("serves the reading interface, shared domain logic, and original illustrations", async () => {
  await withServer(async (baseUrl) => {
    const [documentResponse, moduleResponse, imageResponse] = await Promise.all([
      fetch(baseUrl),
      fetch(`${baseUrl}/src/domain.mjs`),
      fetch(`${baseUrl}/assets/garden-01.svg`),
    ]);
    const document = await documentResponse.text();
    const moduleSource = await moduleResponse.text();

    assert.equal(documentResponse.status, 200);
    assert.match(document, /id="reader-stage"/);
    assert.equal(moduleResponse.status, 200);
    assert.match(moduleSource, /export function mapDisplayPoint/);
    assert.equal(imageResponse.status, 200);
    assert.match(imageResponse.headers.get("content-type"), /image\/svg\+xml/);
    assert.ok(Number(imageResponse.headers.get("content-length")) > 100);
  });
});

test("serves book selection, upload controls, and shared interaction rules", async () => {
  await withServer(async (baseUrl) => {
    const [htmlResponse, rulesResponse] = await Promise.all([
      fetch(baseUrl),
      fetch(baseUrl + "/src/book-view.mjs"),
    ]);
    const html = await htmlResponse.text();
    assert.equal(htmlResponse.status, 200);
    assert.match(html, /id="book-selector"/);
    assert.match(html, /id="book-upload-form"/);
    assert.match(html, /id="rights-confirmed"/);
    assert.equal(rulesResponse.status, 200);
    assert.match(await rulesResponse.text(), /bookInteractionState/);
  });
});

test("returns grounded answers and a retrievable workflow run", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "小鹿捡到红色风筝" }),
    });
    const result = await response.json();

    assert.equal(response.status, 200);
    assert.equal(result.status, "completed");
    assert.match(result.answer, /红色风筝/);
    assert.equal(result.evidence[0].page_number, 1);
    assert.ok(result.run_id);

    const runResponse = await fetch(`${baseUrl}/api/runs/${result.run_id}`);
    const run = await runResponse.json();

    assert.equal(run.run_id, result.run_id);
    assert.equal(run.status, "completed");
    assert.ok(run.steps.some((step) => step.id === "evidence-search"));
  });
});

test("preserves the raw browser transcript separately from the submitted voice question", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        question: "小鹿捡到红色风筝",
        input_mode: "voice",
        asr_raw: "小鹿捡到红色风筝呀",
      }),
    });
    const result = await response.json();
    const runResponse = await fetch(`${baseUrl}/api/runs/${result.run_id}`);
    const run = await runResponse.json();

    assert.equal(response.status, 200);
    assert.equal(run.input.mode, "voice");
    assert.equal(run.input.raw_asr_transcript, "小鹿捡到红色风筝呀");
    assert.equal(run.input.normalized_question, "小鹿捡到红色风筝");
    assert.ok(run.steps.some((step) => step.id === "speech-recognition" && step.status === "client-reported"));
  });
});

test("rejects an invalid voice transcript contract", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "风筝在哪里", input_mode: "voice", asr_raw: 42 }),
    });

    assert.equal(response.status, 400);
  });
});

test("refuses questions without page evidence", async () => {
  await withServer(async (baseUrl) => {
    const response = await fetch(`${baseUrl}/api/ask`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question: "蓝色火箭在哪里" }),
    });
    const result = await response.json();

    assert.equal(response.status, 200);
    assert.equal(result.answerable, false);
    assert.deepEqual(result.evidence, []);
    assert.match(result.answer, /找不到/);
  });
});
