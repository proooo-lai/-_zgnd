const DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1";
const DEFAULT_MODEL = "qwen3-vl-plus";
const DEFAULT_TIMEOUT_MS = 45_000;
const DEFAULT_MAX_RETRIES = 2;

function clientError(code, message, status = 502, cause) {
  const error = new Error(message, cause ? { cause } : undefined);
  error.code = code;
  error.status = status;
  return error;
}

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function validateTextItem(value, fields, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw clientError("model_output_invalid", `${label} 必须是对象。`);
  }
  for (const field of fields) {
    if (typeof value[field] !== "string" || !value[field].trim()) {
      throw clientError("model_output_invalid", `${label}.${field} 缺失或无效。`);
    }
  }
}

function validateAnalysis(value, expectedPageNumber) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw clientError("model_output_invalid", "模型结果必须是 JSON 对象。");
  }
  if (value.page_number !== expectedPageNumber) {
    throw clientError("model_output_invalid", "模型结果页码与请求页码不一致。");
  }
  if (typeof value.text !== "string") {
    throw clientError("model_output_invalid", "模型结果缺少页面文字字段。");
  }
  if (!Array.isArray(value.characters) || !Array.isArray(value.scenes) || !Array.isArray(value.objects)
    || !Array.isArray(value.relations) || !Array.isArray(value.uncertainties)) {
    throw clientError("model_output_invalid", "模型结果数组字段不完整。");
  }
  for (const item of value.characters) validateTextItem(item, ["id", "name", "description"], "characters 项");
  for (const item of value.scenes) validateTextItem(item, ["id", "label", "description"], "scenes 项");
  const entityIds = new Set([
    ...value.characters.map((item) => item.id),
    ...value.objects.map((item) => item.id),
  ]);
  for (const object of value.objects) {
    validateTextItem(object, ["id", "label", "description"], "objects 项");
    if (!Array.isArray(object.box) || object.box.length !== 4 || !object.box.every(finiteNumber)) {
      throw clientError("model_output_invalid", "对象坐标必须是四个数字。");
    }
    const [x, y, width, height] = object.box;
    if (x < 0 || y < 0 || width <= 0 || height <= 0 || x + width > 1 || y + height > 1) {
      throw clientError("model_output_invalid", "对象坐标必须是范围内的归一化矩形。");
    }
    if (!finiteNumber(object.confidence) || object.confidence < 0 || object.confidence > 1) {
      throw clientError("model_output_invalid", "对象置信度必须介于 0 和 1 之间。");
    }
  }
  for (const relation of value.relations) {
    validateTextItem(relation, ["subject_id", "predicate", "object_id"], "relations 项");
    if (!entityIds.has(relation.subject_id) || !entityIds.has(relation.object_id)) {
      throw clientError("model_output_invalid", "关系引用了不存在的人物或对象 ID。");
    }
  }
  for (const uncertainty of value.uncertainties) {
    if (typeof uncertainty !== "string" || !uncertainty.trim()) {
      throw clientError("model_output_invalid", "uncertainties 项必须是非空文字。");
    }
  }
  return value;
}

function extractJson(content) {
  if (Array.isArray(content)) {
    content = content.map((part) => typeof part === "string" ? part : part?.text ?? "").join("");
  }
  if (typeof content !== "string") throw clientError("model_output_invalid", "模型没有返回文字结果。");
  const trimmed = content.trim().replace(/^```(?:json)?\s*/iu, "").replace(/\s*```$/u, "").trim();
  try {
    return JSON.parse(trimmed);
  } catch (error) {
    throw clientError("model_output_invalid", "模型返回的内容不是有效 JSON。", 502, error);
  }
}

function isRetryableStatus(status) {
  return status === 408 || status === 409 || status === 425 || status === 429 || status >= 500;
}

const ANALYSIS_INSTRUCTION = `你是绘本内容理解模型。只输出一个 JSON 对象，不要 Markdown，不要解释。严格使用以下字段：
{"page_number":整数,"text":"页面可读文字，无法辨认时为空字符串","characters":[{"id":"稳定短 ID","name":"人物名","description":"可观察描述"}],"scenes":[{"id":"稳定短 ID","label":"场景名","description":"可观察描述"}],"objects":[{"id":"稳定短 ID","label":"对象名","description":"可观察描述","box":[x,y,width,height],"confidence":0.0}],"relations":[{"subject_id":"已有人物或对象 ID","predicate":"关系","object_id":"已有人物或对象 ID"}],"uncertainties":["无法确认的内容"]}
box 必须是相对于整张图片左上角的归一化矩形，x、y、width、height 均在 0 到 1 范围内且不越界。只报告图片中实际可观察的内容，不用常识补写；没有内容时使用空数组。`;

export function createBailianVisionClient({
  apiKey = process.env.DASHSCOPE_API_KEY ?? "",
  baseUrl = process.env.DASHSCOPE_BASE_URL || DEFAULT_BASE_URL,
  model = process.env.BAILIAN_VISION_MODEL || DEFAULT_MODEL,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  maxRetries = DEFAULT_MAX_RETRIES,
  retryDelayMs = 250,
  fetchImpl = globalThis.fetch,
} = {}) {
  const normalizedBaseUrl = String(baseUrl).replace(/\/+$/u, "");
  if (typeof fetchImpl !== "function") throw new Error("当前运行环境缺少 fetch。 ");

  async function analyzePage({ page, imageBytes }) {
    if (!apiKey) throw clientError("api_not_configured", "尚未配置百炼 API Key。", 503);
    if (!page || !Number.isInteger(page.page_number) || !Buffer.isBuffer(imageBytes)) {
      throw clientError("model_input_invalid", "模型分析输入无效。", 400);
    }
    const payload = {
      model,
      messages: [{
        role: "user",
        content: [
          { type: "text", text: `${ANALYSIS_INSTRUCTION}\n当前请求页码：${page.page_number}` },
          { type: "image_url", image_url: { url: `data:image/png;base64,${imageBytes.toString("base64")}` } },
        ],
      }],
      temperature: 0,
    };

    let lastError;
    for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      try {
        const response = await fetchImpl(`${normalizedBaseUrl}/chat/completions`, {
          method: "POST",
          headers: {
            authorization: `Bearer ${apiKey}`,
            "content-type": "application/json",
          },
          body: JSON.stringify(payload),
          signal: controller.signal,
        });
        if (!response.ok) {
          const error = clientError("model_api_failed", `百炼模型请求失败（HTTP ${response.status}）。`, 502);
          if (!isRetryableStatus(response.status) || attempt === maxRetries) throw error;
          lastError = error;
        } else {
          let result;
          try {
            result = await response.json();
          } catch (error) {
            throw clientError("model_api_failed", "百炼模型返回了无效响应。", 502, error);
          }
          const content = result?.choices?.[0]?.message?.content;
          return validateAnalysis(extractJson(content), page.page_number);
        }
      } catch (error) {
        if (error.code === "model_output_invalid") throw error;
        lastError = error.code === "model_api_failed"
          ? error
          : clientError("model_api_failed", error.name === "AbortError" ? "百炼模型请求超时。" : "百炼模型请求失败。", 502, error);
        if (attempt === maxRetries) throw lastError;
      } finally {
        clearTimeout(timer);
      }
      if (retryDelayMs > 0) await new Promise((resolve) => setTimeout(resolve, retryDelayMs * (attempt + 1)));
    }
    throw lastError ?? clientError("model_api_failed", "百炼模型请求失败。", 502);
  }

  return { analyzePage, model, baseUrl: normalizedBaseUrl };
}

export { validateAnalysis };
