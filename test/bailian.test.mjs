import test from "node:test";
import assert from "node:assert/strict";
import { createBailianVisionClient, validateAnalysis } from "../src/bailian.mjs";

const validAnalysis = {
  page_number: 1,
  text: "小鹿走进花园。",
  characters: [{ id: "character-1", name: "小鹿", description: "一只小鹿。" }],
  scenes: [{ id: "scene-1", label: "花园", description: "有花和树的花园。" }],
  objects: [{
    id: "object-1",
    label: "风筝",
    description: "红色风筝。",
    box: [0.1, 0.2, 0.3, 0.4],
    confidence: 0.92,
  }],
  relations: [{ subject_id: "character-1", predicate: "看着", object_id: "object-1" }],
  uncertainties: [],
};

function responseFor(content, status = 200) {
  return new Response(JSON.stringify({
    choices: [{ message: { content } }],
  }), { status, headers: { "content-type": "application/json" } });
}

test("sends a page image to the configured Qwen-VL chat endpoint and parses JSON", async () => {
  const calls = [];
  const client = createBailianVisionClient({
    apiKey: "test-key",
    baseUrl: "https://example.test/api/v1",
    model: "qwen3-vl-plus",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return responseFor(JSON.stringify(validAnalysis));
    },
  });

  const result = await client.analyzePage({
    page: { page_number: 1, width: 10, height: 10 },
    imageBytes: Buffer.from("png-bytes"),
  });

  assert.deepEqual(result, validAnalysis);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://example.test/api/v1/chat/completions");
  assert.equal(calls[0].options.headers.authorization, "Bearer test-key");
  const payload = JSON.parse(calls[0].options.body);
  assert.equal(payload.model, "qwen3-vl-plus");
  assert.equal(payload.messages[0].content[1].type, "image_url");
  assert.match(payload.messages[0].content[1].image_url.url, /^data:image\/png;base64,/u);
});
test("retries a transient model response once and rejects invalid structured output", async () => {
  let calls = 0;
  const client = createBailianVisionClient({
    apiKey: "test-key",
    baseUrl: "https://example.test/api/v1",
    maxRetries: 1,
    retryDelayMs: 0,
    fetchImpl: async () => {
      calls += 1;
      if (calls === 1) return responseFor("暂时不可用", 503);
      return responseFor('{"page_number":1}');
    },
  });

  await assert.rejects(
    client.analyzePage({ page: { page_number: 1 }, imageBytes: Buffer.from("png") }),
    (error) => error.code === "model_output_invalid",
  );
  assert.equal(calls, 2);
});

test("reports missing API configuration without making a request", async () => {
  let called = false;
  const client = createBailianVisionClient({
    apiKey: "",
    fetchImpl: async () => {
      called = true;
      return responseFor(JSON.stringify(validAnalysis));
    },
  });

  await assert.rejects(
    client.analyzePage({ page: { page_number: 1 }, imageBytes: Buffer.from("png") }),
    (error) => error.code === "api_not_configured",
  );
  assert.equal(called, false);
});

test("rejects out-of-range boxes and dangling relation references", () => {
  assert.throws(
    () => validateAnalysis({ ...validAnalysis, objects: [{ ...validAnalysis.objects[0], box: [0.8, 0, 0.4, 0.2] }] }, 1),
    (error) => error.code === "model_output_invalid",
  );
  assert.throws(
    () => validateAnalysis({ ...validAnalysis, relations: [{ subject_id: "missing", predicate: "看着", object_id: "object-1" }] }, 1),
    (error) => error.code === "model_output_invalid",
  );
});
