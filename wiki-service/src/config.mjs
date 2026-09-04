import path from "node:path";

function boundedInteger(value, fallback, minimum, maximum) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed)) return fallback;
  return Math.min(Math.max(parsed, minimum), maximum);
}

export function loadConfig(environment = process.env) {
  return Object.freeze({
    root: path.resolve(environment.WIKI_ROOT || "/var/lib/wiki"),
    host: environment.HOST || "0.0.0.0",
    port: boundedInteger(environment.PORT, 8080, 1, 65535),
    maxRequestBytes: boundedInteger(environment.WIKI_MAX_REQUEST_BYTES, 256 * 1024, 1024, 1024 * 1024),
    maxCorpusFiles: boundedInteger(environment.WIKI_MAX_CORPUS_FILES, 10_000, 1, 100_000),
    maxCorpusBytes: boundedInteger(environment.WIKI_MAX_CORPUS_BYTES, 64 * 1024 * 1024, 1024, 512 * 1024 * 1024),
    maxFallbackFiles: boundedInteger(environment.WIKI_MAX_FALLBACK_FILES, 500, 1, 10_000),
    maxFallbackBytes: boundedInteger(environment.WIKI_MAX_FALLBACK_BYTES, 8 * 1024 * 1024, 1024, 128 * 1024 * 1024),
  });
}
