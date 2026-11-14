import { randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdtemp, mkdir, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";

const execute = promisify(execFile);
const MAX_PAGES = 40;
const PROCESS_TIMEOUT_MS = 30_000;
const BOOK_ID = /^[0-9a-f-]{36}$/u;

function inputError(message) {
  const error = new Error(message);
  error.status = 400;
  return error;
}

async function command(binary, args) {
  return execute(binary, args, { timeout: PROCESS_TIMEOUT_MS, maxBuffer: 1024 * 1024 });
}

async function readManifest(storageDir, bookId) {
  if (!BOOK_ID.test(bookId)) return null;
  try {
    return JSON.parse(await readFile(join(storageDir, bookId, "book.json"), "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

export function createBookStore({ storageDir, defaultBook }) {
  async function getBook(bookId) {
    if (bookId === defaultBook.id) return defaultBook;
    return readManifest(storageDir, bookId);
  }

  async function listBooks() {
    let entries = [];
    try {
      entries = await readdir(storageDir, { withFileTypes: true });
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    const imported = await Promise.all(entries
      .filter((entry) => entry.isDirectory() && BOOK_ID.test(entry.name))
      .map((entry) => readManifest(storageDir, entry.name)));
    return [defaultBook, ...imported.filter(Boolean)]
      .map((book) => ({
        id: book.id,
        title: book.title,
        page_count: book.pages.length,
        analysis_status: book.analysis_status ?? "demo-manual",
        mode: book.mode,
      }));
  }

  async function importPdf({ title, bytes }) {
    if (typeof title !== "string" || !title.trim() || [...title.trim()].length > 100) {
      throw inputError("书名不能为空，且最多 100 个字符。");
    }
    if (!Buffer.isBuffer(bytes) || bytes.length < 20 || bytes.subarray(0, 5).toString() !== "%PDF-") {
      throw inputError("文件不是有效的 PDF。");
    }

    await mkdir(storageDir, { recursive: true });
    const temporaryDir = await mkdtemp(join(storageDir, ".incoming-"));
    const bookId = randomUUID();
    const sourcePath = join(temporaryDir, "source.pdf");
    try {
      await writeFile(sourcePath, bytes);
      let metadata;
      try {
        metadata = (await command("pdfinfo", [sourcePath])).stdout;
      } catch (error) {
        if (error.code === "ENOENT") throw new Error("服务器缺少 pdfinfo，无法导入 PDF。");
        throw inputError("PDF 无法读取、已加密或文件损坏。");
      }
      const pageCount = Number(metadata.match(/^Pages:\s*(\d+)\s*$/mu)?.[1]);
      if (!Number.isInteger(pageCount) || pageCount < 1 || pageCount > MAX_PAGES) {
        throw inputError("PDF 页数必须介于 1 到 " + MAX_PAGES + " 页之间。");
      }

      try {
        await command("pdftoppm", ["-f", "1", "-l", String(pageCount), "-scale-to", "1200", "-png", sourcePath, join(temporaryDir, "page")]);
      } catch (error) {
        if (error.code === "ENOENT") throw new Error("服务器缺少 pdftoppm，无法生成页面预览。");
        throw inputError("PDF 页面渲染失败。");
      }

      const pages = [];
      for (let index = 1; index <= pageCount; index += 1) {
        const renderedName = "page-" + String(index).padStart(String(pageCount).length, "0") + ".png";
        const storedName = "page-" + String(index).padStart(2, "0") + ".png";
        const renderedPath = join(temporaryDir, renderedName);
        const png = await readFile(renderedPath);
        if (png.subarray(0, 8).toString("hex") !== "89504e470d0a1a0a") throw inputError("生成的页面预览无效。");
        if (renderedName !== storedName) await rename(renderedPath, join(temporaryDir, storedName));
        pages.push({
          id: bookId + "-page-" + String(index).padStart(3, "0"),
          evidence_id: null,
          page_number: index,
          asset: "/api/books/" + bookId + "/pages/" + index + ".png",
          width: png.readUInt32BE(16),
          height: png.readUInt32BE(20),
          text: "",
          text_source: "pending-ocr",
          objects: [],
          analysis_status: "pending",
        });
      }
      const book = {
        id: bookId,
        title: title.trim(),
        edition: "用户本地导入",
        mode: "imported-unprocessed",
        analysis_status: "pending",
        imported_at: new Date().toISOString(),
        source_notice: "PDF 已保存并生成逐页预览；OCR、VLM、问答证据、识物和视频生成尚待模型处理。",
        pages,
      };
      await writeFile(join(temporaryDir, "book.json"), JSON.stringify(book, null, 2));
      await rename(temporaryDir, join(storageDir, bookId));
      return book;
    } catch (error) {
      await rm(temporaryDir, { recursive: true, force: true });
      throw error;
    }
  }

  async function getPageAsset(bookId, pageNumber) {
    const book = await getBook(bookId);
    if (!book || book.id === defaultBook.id || !Number.isInteger(pageNumber) || pageNumber < 1 || pageNumber > book.pages.length) return null;
    return readFile(join(storageDir, bookId, "page-" + String(pageNumber).padStart(2, "0") + ".png"));
  }

  return { getBook, listBooks, importPdf, getPageAsset };
}
