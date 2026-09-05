async function readBoundedJson(response, maximum) {
  const reader = response.body?.getReader?.();
  if (!reader) throw new Error("wiki response body is not a readable stream");
  const chunks = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      total += chunk.byteLength;
      if (total > maximum) {
        await reader.cancel("response size limit exceeded");
        throw new Error(`wiki response exceeds ${maximum} bytes`);
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder().decode(bytes));
}

export class WikiClient {
  constructor({ baseUrl, fetchImpl = globalThis.fetch, timeoutMs = 5000, maxResponseBytes = 1024 * 1024 }) {
    if (!baseUrl) throw new Error("baseUrl is required");
    if (typeof fetchImpl !== "function") throw new Error("fetch implementation is required");
    if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000) {
      throw new Error("timeoutMs must be an integer from 1 through 60000");
    }
    if (!Number.isInteger(maxResponseBytes) || maxResponseBytes < 1024 || maxResponseBytes > 4 * 1024 * 1024) {
      throw new Error("maxResponseBytes must be an integer from 1024 through 4194304");
    }
    this.baseUrl = String(baseUrl).replace(/\/$/, "");
    this.fetchImpl = fetchImpl;
    this.timeoutMs = timeoutMs;
    this.maxResponseBytes = maxResponseBytes;
  }

  async request(path, body) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response;
    let payload;
    try {
      response = await this.fetchImpl(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      payload = await readBoundedJson(response, this.maxResponseBytes);
    } catch (error) {
      if (controller.signal.aborted) {
        throw new Error(`wiki request timed out after ${this.timeoutMs} ms`, { cause: error });
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      const error = new Error(payload.error || `wiki request failed with HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  query(input) { return this.request("/v1/query", input); }
  refresh() { return this.request("/v1/refresh", {}); }
}
