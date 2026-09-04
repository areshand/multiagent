function tokensOf(value) {
  return String(value)
    .replace(/([\p{Ll}\d])([\p{Lu}])/gu, "$1 $2")
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .match(/[\p{L}\p{N}]+/gu) || [];
}

function countOccurrences(haystack, needle) {
  let count = 0;
  let offset = 0;
  while ((offset = haystack.indexOf(needle, offset)) >= 0) {
    count += 1;
    offset += needle.length;
  }
  return count;
}

function documentScore(document, query, queryTokens) {
  const normalizedQuery = query.normalize("NFKC").toLocaleLowerCase("en-US");
  const path = tokensOf(document.path).join(" ");
  const title = tokensOf(document.title).join(" ");
  const body = tokensOf(document.text).join(" ");
  let score = body.includes(normalizedQuery) ? 20 : 0;
  let matched = 0;
  for (const token of queryTokens) {
    const titleCount = countOccurrences(title, token);
    const pathCount = countOccurrences(path, token);
    const bodyCount = Math.min(countOccurrences(body, token), 10);
    if (titleCount || pathCount || bodyCount) matched += 1;
    score += titleCount * 8 + pathCount * 4 + bodyCount;
  }
  if (matched === queryTokens.length) score += 10;
  return score;
}

function excerptOf(text, queryTokens, maximum) {
  const lower = text.normalize("NFKC").toLocaleLowerCase("en-US");
  let offset = Number.POSITIVE_INFINITY;
  for (const token of queryTokens) {
    const candidate = lower.indexOf(token);
    if (candidate >= 0) offset = Math.min(offset, candidate);
  }
  if (!Number.isFinite(offset)) offset = 0;
  const start = Math.max(0, offset - Math.floor(maximum / 4));
  const end = Math.min(text.length, start + maximum);
  return `${start > 0 ? "…" : ""}${text.slice(start, end).trim()}${end < text.length ? "…" : ""}`;
}

function rank(documents, query, queryTokens) {
  return documents
    .map((document) => ({ document, score: documentScore(document, query, queryTokens) }))
    .filter((result) => result.score > 0)
    .sort((left, right) => right.score - left.score || left.document.path.localeCompare(right.document.path, "en"));
}

export function searchCorpus(corpus, request, bounds) {
  const queryTokens = [...new Set(tokensOf(request.query))];
  const indexedResults = rank(corpus.indexed, request.query, queryTokens);
  const needFallback = indexedResults.length < request.limit;
  let fallbackResults = [];
  let fallbackFiles = 0;
  let fallbackBytes = 0;
  let fallbackTruncated = false;
  if (needFallback) {
    const bounded = [];
    for (const document of corpus.fallback) {
      if (fallbackFiles >= bounds.maxFallbackFiles || fallbackBytes + document.bytes > bounds.maxFallbackBytes) {
        fallbackTruncated = true;
        break;
      }
      bounded.push(document);
      fallbackFiles += 1;
      fallbackBytes += document.bytes;
    }
    fallbackResults = rank(bounded, request.query, queryTokens);
  }
  const ranked = [...indexedResults.map((result) => ({ ...result, source: "index" })), ...fallbackResults.map((result) => ({ ...result, source: "fallback" }))]
    .sort((left, right) => right.score - left.score || (left.source === right.source ? 0 : left.source === "index" ? -1 : 1) || left.document.path.localeCompare(right.document.path, "en"))
    .slice(0, request.limit);
  return Object.freeze({
    results: ranked.map(({ document, score, source }) => Object.freeze({
      title: document.title,
      path: document.path,
      sha256: document.sha256,
      score,
      source,
      excerpt: excerptOf(document.text, queryTokens, request.maxExcerptChars),
    })),
    retrieval: Object.freeze({
      mode: needFallback ? "index+fallback" : "index",
      indexedCandidates: corpus.indexed.length,
      fallbackFilesScanned: fallbackFiles,
      fallbackBytesScanned: fallbackBytes,
      fallbackTruncated,
      indexDigest: corpus.indexDigest,
    }),
  });
}
