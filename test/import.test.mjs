import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
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

async function withServer(storageDir, run) {
  const server = createServer({ bookStorageDir: storageDir });
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

async function importPdf(baseUrl, title, pageCount, rightsConfirmed = true) {
  return fetch(`${baseUrl}/api/books?title=${encodeURIComponent(title)}`, {
    method: "POST",
    headers: { "content-type": "application/pdf", "x-rights-confirmed": String(rightsConfirmed) },
    body: makePdf(pageCount),
  });
}

test("imports distinct PDFs without shared page data and persists their manifests", async () => {
  const storageDir = await mkdtemp(join(tmpdir(), "book-import-test-"));
  try {
    let firstId;
    await withServer(storageDir, async (baseUrl) => {
      const firstResponse = await importPdf(baseUrl, "原创甲", 2);
      assert.equal(firstResponse.status, 201);
      const first = await firstResponse.json();
      firstId = first.book_id;
      assert.equal(first.book.pages.length, 2);
      assert.equal(first.book.analysis_status, "pending");
      assert.ok(first.book.pages.every((page) => page.text === "" && page.objects.length === 0));
      assert.ok(first.book.pages.every((page) => page.id.startsWith(firstId)));
      const secondResponse = await importPdf(baseUrl, "原创乙", 1);
      assert.equal(secondResponse.status, 201);
      const second = await secondResponse.json();
      assert.notEqual(second.book_id, firstId);
      assert.equal(second.book.pages.length, 1);
      const listing = await (await fetch(`${baseUrl}/api/books`)).json();
      assert.ok(listing.books.some((item) => item.id === firstId && item.page_count === 2));
      assert.ok(listing.books.some((item) => item.id === second.book_id && item.page_count === 1));
      const imageResponse = await fetch(`${baseUrl}${first.book.pages[1].asset}`);
      assert.equal(imageResponse.status, 200);
      assert.match(imageResponse.headers.get("content-type"), /image\/png/);
      assert.ok((await imageResponse.arrayBuffer()).byteLength > 100);
      const askResponse = await fetch(`${baseUrl}/api/ask`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ book_id: firstId, question: "书里是什么？" }),
      });
      assert.equal(askResponse.status, 409);
      assert.equal((await askResponse.json()).code, "analysis_pending");
      const runResponse = await fetch(`${baseUrl}/api/runs/${first.run_id}`);
      assert.equal((await runResponse.json()).result.book_id, firstId);
    });
    await withServer(storageDir, async (baseUrl) => {
      const restored = await (await fetch(`${baseUrl}/api/books/${firstId}`)).json();
      assert.equal(restored.id, firstId);
      assert.equal(restored.pages.length, 2);
    });
  } finally {
    await rm(storageDir, { recursive: true, force: true });
  }
});

test("rejects unconfirmed or invalid PDF inputs without creating books", async () => {
  const storageDir = await mkdtemp(join(tmpdir(), "book-import-test-"));
  try {
    await withServer(storageDir, async (baseUrl) => {
      assert.equal((await importPdf(baseUrl, "未确认", 1, false)).status, 400);
      const invalid = await fetch(`${baseUrl}/api/books?title=bad`, {
        method: "POST",
        headers: { "content-type": "application/pdf", "x-rights-confirmed": "true" },
        body: Buffer.from("not a PDF"),
      });
      assert.equal(invalid.status, 400);
      assert.deepEqual((await (await fetch(`${baseUrl}/api/books`)).json()).books.map((book) => book.id), ["original-garden-demo"]);
    });
  } finally {
    await rm(storageDir, { recursive: true, force: true });
  }
});
