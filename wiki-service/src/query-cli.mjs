import { WikiClient } from "./client.mjs";

function usage() {
  return "usage: wiki-query --query TEXT [--max-results N] [--max-excerpt-chars N]\n";
}

export function parseQueryArguments(arguments_) {
  const result = { query: null, limit: 5, maxExcerptChars: 1200 };
  for (let index = 0; index < arguments_.length; index += 1) {
    const argument = arguments_[index];
    const value = arguments_[index + 1];
    if (argument === "--query" && value !== undefined) {
      result.query = value;
      index += 1;
    } else if (argument === "--max-results" && value !== undefined) {
      result.limit = Number(value);
      index += 1;
    } else if (argument === "--max-excerpt-chars" && value !== undefined) {
      result.maxExcerptChars = Number(value);
      index += 1;
    } else if (argument === "--help" || argument === "-h") {
      return Object.freeze({ help: true });
    } else {
      throw new Error(`unknown or incomplete argument: ${argument}\n${usage()}`);
    }
  }
  if (!result.query?.trim()) throw new Error(`--query is required\n${usage()}`);
  if (!Number.isInteger(result.limit) || result.limit < 1 || result.limit > 10) {
    throw new Error("--max-results must be an integer from 1 through 10");
  }
  if (!Number.isInteger(result.maxExcerptChars) || result.maxExcerptChars < 100 || result.maxExcerptChars > 4000) {
    throw new Error("--max-excerpt-chars must be an integer from 100 through 4000");
  }
  return Object.freeze(result);
}

export async function runWikiQuery({ arguments_, environment = process.env, fetchImpl = globalThis.fetch }) {
  const parsed = parseQueryArguments(arguments_);
  if (parsed.help) return { help: usage() };
  const requestedTimeout = Number(environment.WIKI_QUERY_TIMEOUT_MS || 5000);
  if (!Number.isInteger(requestedTimeout) || requestedTimeout < 100 || requestedTimeout > 60_000) {
    throw new Error("WIKI_QUERY_TIMEOUT_MS must be an integer from 100 through 60000");
  }
  const client = new WikiClient({
    baseUrl: environment.MULTIAGENT_WIKI_URL || "http://127.0.0.1:8080",
    fetchImpl,
    timeoutMs: requestedTimeout,
  });
  return client.query({ query: parsed.query, limit: parsed.limit, maxExcerptChars: parsed.maxExcerptChars });
}

export { usage };
