import http from "node:http";

import { loadCorpus } from "./markdown-store.mjs";
import { searchCorpus } from "./search.mjs";
import {
  RequestError,
  boundedInteger,
  boundedString,
  requireObject,
} from "./validation.mjs";

function json(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
  });
  response.end(body);
}

async function readJson(request, maximum) {
  const contentType = String(request.headers["content-type"] || "").split(";", 1)[0].trim().toLowerCase();
  if (contentType && contentType !== "application/json") throw new RequestError("content-type must be application/json", 415);
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maximum) throw new RequestError("request body too large", 413);
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new RequestError("request body must be valid JSON");
  }
}

function queryRequest(body) {
  requireObject(body);
  const query = boundedString(body.query, "query", { maximum: 500 });
  if (!/[\p{L}\p{N}]/u.test(query)) throw new RequestError("query must contain at least one lexical token");
  return Object.freeze({
    query,
    limit: boundedInteger(body.limit, "limit", { fallback: 5, minimum: 1, maximum: 10 }),
    maxExcerptChars: boundedInteger(body.maxExcerptChars, "maxExcerptChars", { fallback: 1200, minimum: 100, maximum: 4000 }),
  });
}

export async function createWikiApp(config) {
  let corpus = null;
  let refreshPromise = null;
  let lastError = null;

  async function refresh() {
    if (!refreshPromise) {
      refreshPromise = loadCorpus(config.root, {
        maxFiles: config.maxCorpusFiles,
        maxBytes: config.maxCorpusBytes,
        profile: config.profile || "organization",
      }).then((loaded) => {
        corpus = loaded;
        lastError = null;
        return loaded;
      }).catch((error) => {
        lastError = error;
        throw error;
      }).finally(() => {
        refreshPromise = null;
      });
    }
    return refreshPromise;
  }

  await refresh();

  function corpusReady() {
    return Boolean(corpus?.indexDigest && corpus.indexed.length > 0 && !lastError);
  }

  const server = http.createServer(async (request, response) => {
    const requestUrl = new URL(request.url || "/", "http://wiki.invalid");
    try {
      if (requestUrl.pathname === "/healthz" && request.method === "GET") {
        return json(response, 200, { status: "ok" });
      }
      if (requestUrl.pathname === "/readyz" && request.method === "GET") {
        return corpusReady()
          ? json(response, 200, { status: "ready", documents: corpus.documents.length, loadedAt: corpus.loadedAt })
          : json(response, 503, { status: "not-ready", error: lastError?.message || "reviewed index is not loaded" });
      }
      if (requestUrl.pathname === "/v1/query" && request.method === "POST") {
        if (!corpusReady()) throw new RequestError("reviewed Wiki index is not ready", 503);
        const body = queryRequest(await readJson(request, config.maxRequestBytes));
        const result = searchCorpus(corpus, body, config);
        return json(response, 200, { query: body.query, ...result });
      }
      if (requestUrl.pathname === "/v1/refresh" && request.method === "POST") {
        requireObject(await readJson(request, config.maxRequestBytes));
        const loaded = await refresh();
        return json(response, 200, { status: "refreshed", documents: loaded.documents.length, loadedAt: loaded.loadedAt, indexDigest: loaded.indexDigest });
      }
      if (["/healthz", "/readyz", "/v1/query", "/v1/refresh"].includes(requestUrl.pathname)) {
        response.setHeader("allow", requestUrl.pathname.startsWith("/v1/") ? "POST" : "GET");
        return json(response, 405, { error: "method not allowed" });
      }
      return json(response, 404, { error: "not found" });
    } catch (error) {
      if (error instanceof RequestError) return json(response, error.status, { error: error.message });
      console.error("wiki request failed", error);
      return json(response, 500, { error: "internal server error" });
    }
  });

  return Object.freeze({ server, refresh, snapshot: () => corpus });
}
