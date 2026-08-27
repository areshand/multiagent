import http from "node:http";
import https from "node:https";

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export function reportDeliveryTimeoutMs(value = process.env.MULTIAGENT_REPORT_DELIVERY_TIMEOUT_SECONDS) {
  const parsed = value === undefined || value === "" ? 600 : Number(value);
  const seconds = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 30), 1800) : 600;
  return seconds * 1000;
}

export function workerReportEndpoint(gatewayUrl, sessionId) {
  if (typeof gatewayUrl !== "string" || !gatewayUrl.trim()) throw new Error("worker report gateway URL is required");
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(sessionId)) throw new Error("worker report session ID is invalid");
  const endpoint = new URL(`/api/sessions/${sessionId}/report`, gatewayUrl);
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password) {
    throw new Error("worker report gateway URL must be an HTTP(S) origin without credentials");
  }
  return endpoint;
}

export function postWorkerReport(endpoint, token, report) {
  if (typeof token !== "string" || !token.trim() || /\s/.test(token)) throw new Error("worker report token is invalid");
  const encoded = Buffer.from(JSON.stringify(report));
  const transport = endpoint.protocol === "https:" ? https : http;
  return new Promise((resolve, reject) => {
    const request = transport.request(endpoint, {
      method: "POST",
      headers: {
        accept: "application/json",
        authorization: `Bearer ${token}`,
        "content-type": "application/json",
        "content-length": encoded.length,
      },
      timeout: 5000,
    }, (response) => {
      response.resume();
      response.on("end", () => {
        if (response.statusCode >= 200 && response.statusCode < 300) return resolve();
        reject(new Error(`worker report gateway returned ${response.statusCode}`));
      });
    });
    request.on("timeout", () => request.destroy(new Error("worker report gateway timed out")));
    request.on("error", reject);
    request.end(encoded);
  });
}

export async function deliverWorkerReport({
  gatewayUrl,
  sessionId,
  token,
  report,
  timeoutMs = reportDeliveryTimeoutMs(),
  retryDelayMs = 2000,
  post = postWorkerReport,
  wait = sleep,
}) {
  const endpoint = workerReportEndpoint(gatewayUrl, sessionId);
  const deadline = Date.now() + timeoutMs;
  let lastError = new Error("worker report delivery did not run");
  do {
    try {
      await post(endpoint, token, report);
      return;
    } catch (error) {
      lastError = error;
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await wait(Math.min(retryDelayMs, remaining));
  } while (Date.now() < deadline);
  throw lastError;
}
