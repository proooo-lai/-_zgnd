import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer } from "../server.mjs";

function makePdf(pageCount) {
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>"];
  const pageRefs = Array.from({ length: pageCount }, (_, index) => `${index + 3} 0 R`).join(" ");
  objects.push(`<< /Type /Pages /Kids [${pageRefs}] /Count ${pageCount} >>`);
  for (let index = 0; index < pageCount; index += 1) {
    objects.push("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 240] /Resources << >> >>");
  }
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  for (const offset of offsets.slice(1)) pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
  pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}
async function withServer(storageDir, visionClient, run) {
  const server = createServer({ bookStorageDir: storageDir, visionClient });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  try {
    return await run(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

async function importPdf(baseUrl) {
  return fetch(`${baseUrl}/api/books?title=模型测试绘本`, {
    method: "POST",
    headers: { "content-type": "application/pdf", "x-rights-confirmed": "true" },
    body: makePdf(2),
  });
}

function pageAnalysis(pageNumber) {
  return {
    page_number: pageNumber,
    text: `第${pageNumber}页的小鹿。`,
    characters: [{ id: `character-${pageNumber}`, name: "小鹿", description: "一只小鹿。" }],
    scenes: [{ id: `scene-${pageNumber}`, label: "花园", description: "花园。" }],
    objects: [{
      id: `object-${pageNumber}`,
      label: "风筝",
      description: "红色风筝。",
      box: [0.1, 0.2, 0.3, 0.4],
      confidence: 0.9,
    }],
    relations: [],
    uncertainties: [],
  };
}

test("analyzes every imported page, persists a separate analysis, and unlocks grounded Q&A", async () => {
  const storageDir = await mkdtemp(join(tmpdir(), "book-analyze-test-"));
  const visionClient = { analyzePage: async ({ page }) => pageAnalysis(page.page_number) };
  try {
    await withServer(storageDir, visionClient, async (baseUrl) => {
      const imported = await (await importPdf(baseUrl)).json();
      const response = await fetch(`${baseUrl}/api/books/${imported.book_id}/analyze`, { method: "POST" });
      const result = await response.json();

      assert.equal(response.status, 200);
      assert.equal(result.status, "completed");
      assert.ok(result.run_id);
      assert.equal(result.book.analysis_status, "completed");
      assert.equal(result.book.pages[0].text, "第1页的小鹿。");
      assert.equal(result.book.pages[0].objects[0].label, "风筝");

      const rawManifest = JSON.parse(await readFile(join(storageDir, imported.book_id, "book.json"), "utf8"));
      const analysis = JSON.parse(await readFile(join(storageDir, imported.book_id, "analysis.json"), "utf8"));
      assert.equal(rawManifest.pages[0].text, "");
      assert.equal(analysis.pages[0].text, "第1页的小鹿。");

      const run = await (await fetch(`${baseUrl}/api/runs/${result.run_id}`)).json();
      assert.equal(run.status, "completed");
      assert.ok(run.steps.some((step) => step.id === "page-analysis" && step.status === "completed"));

      const answer = await fetch(`${baseUrl}/api/ask`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ book_id: imported.book_id, question: "小鹿在哪里？" }),
      });
      assert.equal(answer.status, 200);
      assert.equal((await answer.json()).evidence[0].page_number, 1);
    });
  } finally {
    await rm(storageDir, { recursive: true, force: true });
  }
});
test("records a model failure and keeps the imported book unavailable for Q&A", async () => {
  const storageDir = await mkdtemp(join(tmpdir(), "book-analyze-failure-test-"));
  const visionClient = {
    analyzePage: async () => {
      const error = new Error("模型服务暂时不可用");
      error.code = "model_api_failed";
      throw error;
    },
  };
  try {
    await withServer(storageDir, visionClient, async (baseUrl) => {
      const imported = await (await importPdf(baseUrl)).json();
      const response = await fetch(`${baseUrl}/api/books/${imported.book_id}/analyze`, { method: "POST" });
      const result = await response.json();

      assert.equal(response.status, 502);
      assert.equal(result.code, "model_api_failed");
      assert.equal(result.book.analysis_status, "failed");
      assert.equal(result.run.status, "failed");

      const book = await (await fetch(`${baseUrl}/api/books/${imported.book_id}`)).json();
      assert.equal(book.analysis_status, "failed");
      const answer = await fetch(`${baseUrl}/api/ask`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ book_id: imported.book_id, question: "小鹿在哪里？" }),
      });
      assert.equal(answer.status, 409);
      assert.equal((await answer.json()).code, "analysis_failed");
    });
  } finally {
    await rm(storageDir, { recursive: true, force: true });
  }
});
