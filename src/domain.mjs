const QUESTION_FILLERS = [
  "为什么", "为何", "怎么回事", "怎么样", "怎么", "什么", "哪里", "哪儿",
  "多少", "谁", "请问", "告诉我", "讲讲", "说说", "吗", "呢", "呀", "啊",
];

const STOP_CHARACTERS = new Set(["的", "了", "是", "在", "和", "与", "吗", "呢", "呀", "啊"]);

function compactText(value) {
  return String(value ?? "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]/gu, "");
}

function queryTerms(query) {
  let compact = compactText(query);
  for (const filler of QUESTION_FILLERS) {
    compact = compact.replaceAll(compactText(filler), "");
  }

  const terms = new Set();
  const chinese = [...compact].filter((character) => /\p{Script=Han}/u.test(character));
  for (let index = 0; index < chinese.length - 1; index += 1) {
    const pair = chinese[index] + chinese[index + 1];
    if (![...pair].some((character) => STOP_CHARACTERS.has(character))) terms.add(pair);
  }
  for (const character of chinese) {
    if (!STOP_CHARACTERS.has(character)) terms.add(character);
  }

  const latinWords = compact.match(/[a-z0-9]+/g) ?? [];
  for (const word of latinWords) terms.add(word);
  return terms;
}

function passageList(text) {
  return String(text ?? "")
    .split(/(?<=[。！？!?；;])/u)
    .map((passage) => passage.trim())
    .filter(Boolean);
}

function scorePassage(passage, terms) {
  const normalizedPassage = compactText(passage);
  let score = 0;
  let matchedPairs = 0;
  let matchedCharacters = 0;

  for (const term of terms) {
    if (!normalizedPassage.includes(term)) continue;
    if ([...term].length === 1) {
      score += 0.55;
      matchedCharacters += 1;
    } else {
      score += 2.6;
      matchedPairs += 1;
    }
  }

  return { score, matchedPairs, matchedCharacters };
}

export function findEvidence(query, pages, { limit = 3 } = {}) {
  if (typeof query !== "string" || !Array.isArray(pages)) return [];
  const terms = queryTerms(query);
  if (!terms.size) return [];

  const matches = [];
  for (const page of pages) {
    const rankedPassages = passageList(page.text)
      .map((excerpt) => ({ excerpt, ...scorePassage(excerpt, terms) }))
      .filter(({ matchedPairs, matchedCharacters, score }) =>
        (matchedPairs > 0 && score >= 2.6) || (matchedCharacters >= 4 && score >= 2.2))
      .sort((left, right) => right.score - left.score);

    const best = rankedPassages[0];
    if (!best) continue;

    matches.push({
      evidence_id: page.evidence_id,
      page_number: page.page_number,
      excerpt: best.excerpt,
      source: "book-text",
      score: best.score,
    });
  }

  return matches.sort((left, right) => right.score - left.score).slice(0, limit);
}

export function mapDisplayPoint(clientX, clientY, rect, imageWidth, imageHeight) {
  if (
    !rect ||
    !Number.isFinite(clientX) ||
    !Number.isFinite(clientY) ||
    !Number.isFinite(rect.left) ||
    !Number.isFinite(rect.top) ||
    !Number.isFinite(rect.width) ||
    !Number.isFinite(rect.height) ||
    rect.width <= 0 ||
    rect.height <= 0 ||
    imageWidth <= 0 ||
    imageHeight <= 0
  ) {
    return null;
  }

  const scale = Math.min(rect.width / imageWidth, rect.height / imageHeight);
  const renderedWidth = imageWidth * scale;
  const renderedHeight = imageHeight * scale;
  const offsetX = (rect.width - renderedWidth) / 2;
  const offsetY = (rect.height - renderedHeight) / 2;
  const x = (clientX - rect.left - offsetX) / renderedWidth;
  const y = (clientY - rect.top - offsetY) / renderedHeight;

  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y };
}

export function findObjectAt(point, objects) {
  if (
    !point ||
    !Number.isFinite(point.x) ||
    !Number.isFinite(point.y) ||
    point.x < 0 ||
    point.x > 1 ||
    point.y < 0 ||
    point.y > 1 ||
    !Array.isArray(objects)
  ) {
    return null;
  }

  return objects
    .filter((object) => {
      const box = object?.box;
      return (
        Array.isArray(box) &&
        box.length === 4 &&
        box.every(Number.isFinite) &&
        box[2] > 0 &&
        box[3] > 0 &&
        point.x >= box[0] &&
        point.x <= box[0] + box[2] &&
        point.y >= box[1] &&
        point.y <= box[1] + box[3]
      );
    })
    .sort((left, right) => left.box[2] * left.box[3] - right.box[2] * right.box[3])[0] ?? null;
}
