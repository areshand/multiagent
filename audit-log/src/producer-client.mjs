import fs from "node:fs";

export async function submitAuditEvent({ url, tokenFile, event, timeoutMs = 10_000, fetchImpl = fetch }) {
  if (!url || !/^https?:\/\//.test(url)) throw new Error("AUDIT_LOG_URL must use HTTP or HTTPS");
  if (!tokenFile) throw new Error("AUDIT_LOG_BEARER_TOKEN_FILE is required");
  const token = fs.readFileSync(tokenFile, "utf8").trim();
  if (token.length < 20) throw new Error("audit log bearer token is invalid");
  const response = await fetchImpl(new URL("/v1/events", url), {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: typeof event === "string" || Buffer.isBuffer(event) ? event : JSON.stringify(event),
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`audit logger rejected event (${response.status}): ${text.trim()}`);
  }
}
