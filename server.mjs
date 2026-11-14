import { createServer as createHttpServer } from "node:http";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";
import { findEvidence } from "./src/domain.mjs";
import { createBookStore } from "./src/books.mjs";

const bookFile = process.env.BOOK_DATA_FILE
  ? resolve(process.env.BOOK_DATA_FILE)
  : fileURLToPath(new URL("./data/demo-book.json", import.meta.url));
const book = JSON.parse(readFileSync(bookFile, "utf8"));
const runs = new Map();
const MAX_RUNS = 100;
const MAX_BODY_BYTES = 4096;
const MAX_PDF_BYTES = 20 * 1024 * 1024;

const staticFiles = new Map([
  ["/", ["index.html", "text/html; charset=utf-8"]],
  ["/index.html", ["index.html", "text/html; charset=utf-8"]],
  ["/src/domain.mjs", ["../src/domain.mjs", "text/javascript; charset=utf-8"]],
  ["/src/book-view.mjs", ["../src/book-view.mjs", "text/javascript; charset=utf-8"]],
  ["/app.js", ["app.js", "text/javascript; charset=utf-8"]],
  ["/styles.css", ["styles.css", "text/css; charset=utf-8"]],
]);
for (const page of book.pages) {
  if (typeof page.asset !== "string" || !/^\/assets\/[a-zA-Z0-9_-]+\.(?:svg|png)$/u.test(page.asset)) {
    throw new Error("绘本页面资源路径无效。");
  }
  const type = page.asset.endsWith(".svg") ? "image/svg+xml" : "image/png";
  staticFiles.set(page.asset, [page.asset.slice(1), type]);
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

async function readJson(request) {
  let body = "";
  for await (const chunk of request) {
    body += chunk;
    if (Buffer.byteLength(body) > MAX_BODY_BYTES) {
      const error = new Error("请求内容过大。最多支持 4 KB。");
      error.status = 413;
      throw error;
    }
  }

  try {
    return JSON.parse(body);
  } catch {
    const error = new Error("请求内容不是有效 JSON。");
    error.status = 400;
    throw error;
  }
}

async function readPdf(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > MAX_PDF_BYTES) {
      const error = new Error("PDF 最多支持 20 MB。");
      error.status = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function buildAnswer(question, evidence) {
  if (!evidence.length) {
    return "在目前加载的绘本页面中找不到可支持这个问题的原文，我暂时无法确认。";
  }

  const first = evidence[0];
  const answer = `绘本原文写到：“${first.excerpt}”`;
  if (/为什么|为何|原因/u.test(question) && !/因为|由于|所以/u.test(first.excerpt)) {
    return `${answer} 当前样本页没有给出更具体的原因说明。`;
  }
  return answer;
}

function createQuestionRun(question, book, { inputMode, rawQuestion, asrRaw }) {
  const startedAt = performance.now();
  const runId = randomUUID();
  const evidenceStartedAt = performance.now();
  const evidence = findEvidence(question, book.pages);
  const evidenceDuration = Math.round(performance.now() - evidenceStartedAt);
  const answerable = evidence.length > 0;
  const answer = buildAnswer(question, evidence);
  const totalDuration = Math.round(performance.now() - startedAt);

  const run = {
    run_id: runId,
    created_at: new Date().toISOString(),
    status: "completed",
    outcome: answerable ? "answered" : "refused",
    mode: "local-evidence-demo",
    input: {
      book_id: book.id,
      mode: inputMode,
      raw_question: rawQuestion,
      raw_asr_transcript: inputMode === "voice" ? asrRaw : null,
      normalized_question: question,
    },
    steps: [
      {
        id: "speech-recognition",
        status: inputMode === "voice" ? "client-reported" : "skipped",
        duration_ms: 0,
        detail: inputMode === "voice"
          ? "客户端报告了浏览器语音转写；服务未接收或核验原始音频。"
          : "本次使用文字输入。",
      },
      {
        id: "input-validation",
        status: "completed",
        duration_ms: 0,
        detail: "输入长度与类型校验通过。",
      },
      {
        id: "evidence-search",
        status: "completed",
        duration_ms: evidenceDuration,
        output: { evidence_count: evidence.length },
      },
      {
        id: "evidence-check",
        status: "completed",
        duration_ms: 0,
        output: { answerable },
      },
      {
        id: "response",
        status: "completed",
        duration_ms: Math.max(0, totalDuration - evidenceDuration),
        detail: answerable ? "仅根据页面原文返回。" : "没有足够原文，已拒答。",
      },
    ],
    result: { answer, answerable, evidence },
    total_duration_ms: totalDuration,
  };

  runs.set(runId, run);
  if (runs.size > MAX_RUNS) runs.delete(runs.keys().next().value);

  return run;
}

async function serveStatic(pathname, response) {
  const entry = staticFiles.get(pathname);
  if (!entry) return false;

  const [relativePath, contentType] = entry;
  const filePath = relativePath.startsWith("../")
    ? fileURLToPath(new URL(relativePath, new URL("./public/", import.meta.url)))
    : fileURLToPath(new URL(`./public/${relativePath}`, import.meta.url));
  const contents = readFileSync(filePath);
  response.writeHead(200, {
    "content-type": contentType,
    "content-length": contents.length,
    "cache-control": "no-cache",
    "x-content-type-options": "nosniff",
    "content-security-policy": "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
  });
  response.end(contents);
  return true;
}

export function createServer({ bookStorageDir = resolve(".local-books") } = {}) {
  const bookStore = createBookStore({ storageDir: bookStorageDir, defaultBook: book });
  return createHttpServer(async (request, response) => {
    const url = new URL(request.url ?? "/", "http://127.0.0.1");
    const { pathname } = url;

    try {
      if (request.method === "GET" && pathname === "/api/health") {
        sendJson(response, 200, { status: "ok", mode: book.mode });
        return;
      }

      if (request.method === "GET" && pathname === "/api/book") {
        sendJson(response, 200, book);
        return;
      }

      if (request.method === "GET" && pathname === "/api/books") {
        sendJson(response, 200, { books: await bookStore.listBooks() });
        return;
      }

      if (request.method === "POST" && pathname === "/api/books") {
        if (request.headers["x-rights-confirmed"] !== "true") {
          sendJson(response, 400, { error: "请先确认拥有本赛事开发与演示所需的使用授权。" });
          return;
        }
        if (request.headers["content-type"]?.split(";", 1)[0] !== "application/pdf") {
          sendJson(response, 415, { error: "目前只支持 PDF 绘本。" });
          return;
        }
        const imported = await bookStore.importPdf({ title: url.searchParams.get("title"), bytes: await readPdf(request) });
        const runId = randomUUID();
        runs.set(runId, {
          run_id: runId,
          created_at: new Date().toISOString(),
          status: "awaiting-analysis",
          mode: "local-pdf-import",
          input: { book_id: imported.id, title: imported.title, page_count: imported.pages.length },
          steps: [
            { id: "pdf-validation", status: "completed" },
            { id: "page-preview", status: "completed" },
            { id: "ocr-vlm", status: "pending", detail: "尚未接入模型 API。" },
          ],
          result: { book_id: imported.id, analysis_status: imported.analysis_status },
        });
        if (runs.size > MAX_RUNS) runs.delete(runs.keys().next().value);
        sendJson(response, 201, { book_id: imported.id, run_id: runId, book: imported });
        return;
      }

      const pageMatch = pathname.match(new RegExp("^/api/books/([0-9a-f-]{36})/pages/([0-9]+)\\.png$", "u"));
      if (request.method === "GET" && pageMatch) {
        const image = await bookStore.getPageAsset(pageMatch[1], Number(pageMatch[2]));
        if (!image) {
          sendJson(response, 404, { error: "未找到此页。" });
          return;
        }
        response.writeHead(200, {
          "content-type": "image/png",
          "content-length": image.length,
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
        });
        response.end(image);
        return;
      }

      const bookId = pathname.match(new RegExp("^/api/books/([0-9a-f-]{36}|original-garden-demo)$", "u"))?.[1];
      if (request.method === "GET" && bookId) {
        const selectedBook = await bookStore.getBook(bookId);
        if (!selectedBook) {
          sendJson(response, 404, { error: "未找到这本绘本。" });
          return;
        }
        sendJson(response, 200, selectedBook);
        return;
      }

      if (request.method === "POST" && pathname === "/api/ask") {
        const body = await readJson(request);
        const rawQuestion = typeof body?.question === "string" ? body.question : "";
        const question = rawQuestion.trim();
        const inputMode = body?.input_mode ?? "text";
        const asrRaw = body?.asr_raw;
        if (!question || [...question].length > 200) {
          sendJson(response, 400, { error: "问题不能为空，且最多 200 个字符。" });
          return;
        }
        if (
          !["text", "voice"].includes(inputMode) ||
          (inputMode === "voice" && (typeof asrRaw !== "string" || [...asrRaw].length > 200))
        ) {
          sendJson(response, 400, { error: "输入来源或语音转写格式无效。" });
          return;
        }

        const selectedBook = body?.book_id ? await bookStore.getBook(body.book_id) : book;
        if (!selectedBook) {
          sendJson(response, 404, { error: "未找到这本绘本。" });
          return;
        }
        if (selectedBook.analysis_status === "pending") {
          sendJson(response, 409, { code: "analysis_pending", error: "这本绘本尚未完成 OCR/VLM 分析，目前无法基于它回答。" });
          return;
        }
        const run = createQuestionRun(question, selectedBook, { inputMode, rawQuestion, asrRaw });
        sendJson(response, 200, {
          run_id: run.run_id,
          status: run.status,
          mode: run.mode,
          answerable: run.result.answerable,
          answer: run.result.answer,
          evidence: run.result.evidence,
        });
        return;
      }

      const runId = pathname.match(/^\/api\/runs\/([0-9a-f-]{36})$/iu)?.[1];
      if (request.method === "GET" && runId) {
        const run = runs.get(runId);
        if (!run) {
          sendJson(response, 404, { error: "找不到这次运行记录。" });
          return;
        }
        sendJson(response, 200, run);
        return;
      }

      if (request.method === "GET" && await serveStatic(pathname, response)) return;
      sendJson(response, 404, { error: "未找到请求的内容。" });
    } catch (error) {
      sendJson(response, error.status ?? 500, {
        error: error.status ? error.message : "处理请求时发生错误。",
      });
    }
  });
}

async function listenWithFallback(server, startPort) {
  for (let port = startPort; port < startPort + 12; port += 1) {
    try {
      await new Promise((resolveListen, rejectListen) => {
        const onError = (error) => {
          server.off("listening", onListening);
          rejectListen(error);
        };
        const onListening = () => {
          server.off("error", onError);
          resolveListen();
        };
        server.once("error", onError);
        server.once("listening", onListening);
        server.listen(port, "127.0.0.1");
      });
      return port;
    } catch (error) {
      if (error.code !== "EADDRINUSE" || port === startPort + 11) throw error;
    }
  }
  throw new Error("没有可用的本地端口。");
}

const invokedPath = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (invokedPath === import.meta.url) {
  const server = createServer();
  const requestedPort = Number(process.env.PORT ?? 4173);
  listenWithFallback(server, Number.isInteger(requestedPort) ? requestedPort : 4173)
    .then((port) => console.log(`纸书智伴演示已启动：http://127.0.0.1:${port}`))
    .catch((error) => {
      console.error(`启动失败：${error.message}`);
      process.exitCode = 1;
    });
}
