import test from "node:test";
import assert from "node:assert/strict";
import { findEvidence, mapDisplayPoint, findObjectAt } from "../src/domain.mjs";
import { readFileSync } from "node:fs";

const demoBook = JSON.parse(readFileSync(new URL("../data/demo-book.json", import.meta.url), "utf8"));

const pages = [
  {
    page_number: 1,
    text: "小鹿在花园里捡到红色风筝。小鸟站在树枝上唱歌。",
  },
  {
    page_number: 2,
    text: "一阵风吹来，红色风筝挂在树枝上。小鸟帮小鹿把风筝取下来。",
  },
];

test("returns a passage and page citation only when the page contains matching evidence", () => {
  const matches = findEvidence("红色风筝挂在树枝上", pages);

  assert.equal(matches[0].page_number, 2);
  assert.match(matches[0].excerpt, /树枝/);
  assert.deepEqual(findEvidence("火箭是什么颜色", pages), []);
});

test("maps clicks through object-fit contain and rejects letterboxed space", () => {
  const rect = { left: 0, top: 0, width: 400, height: 400 };

  assert.deepEqual(mapDisplayPoint(200, 200, rect, 800, 400), { x: 0.5, y: 0.5 });
  assert.equal(mapDisplayPoint(200, 50, rect, 800, 400), null);
});

test("chooses the smallest containing object when annotation boxes overlap", () => {
  const objects = [
    { id: "scene", label: "场景", box: [0.1, 0.1, 0.8, 0.8] },
    { id: "kite", label: "风筝", box: [0.35, 0.35, 0.3, 0.3] },
  ];

  assert.equal(findObjectAt({ x: 0.5, y: 0.5 }, objects).id, "kite");
  assert.equal(findObjectAt({ x: 0.95, y: 0.95 }, objects), null);
});

test("original demo annotations match the illustrated kite and bird regions", () => {
  const firstPage = demoBook.pages.find((page) => page.page_number === 1);
  const secondPage = demoBook.pages.find((page) => page.page_number === 2);

  assert.equal(findObjectAt({ x: 0.72, y: 0.45 }, firstPage.objects)?.label, "红色风筝");
  assert.equal(findObjectAt({ x: 0.68, y: 0.36 }, secondPage.objects)?.label, "小鸟");
  assert.equal(findObjectAt({ x: 0.93, y: 0.8 }, secondPage.objects), null);
});
