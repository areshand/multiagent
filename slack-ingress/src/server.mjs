import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";
import { SlackEventFileQueue } from "./file-queue.mjs";
import { parseChannelAllowlist, parseSlackEnvelope, verifySlackRequest } from "./slack-events.mjs";

async function readRawBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error("request body exceeds 1 MiB");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function json(response, status, value) {
  const body = JSON.stringify(value);
  response.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(body) });
  response.end(body);
}

async function secret(file, label) {
  if (!file) throw new Error(`${label} file is required`);
  const value = (await fs.readFile(file, "utf8")).trim();
  if (!value) throw new Error(`${label} is empty`);
  return value;
}

function requiredUrl(value) {
  let url;
  try { url = new URL(String(value || "")); }
  catch { throw new Error("MULTIAGENT_CONTROL_SERVER_URL must be an absolute URL"); }
  if (!new Set(["http:", "https:"]).has(url.protocol)) throw new Error("control-server URL must use HTTP or HTTPS");
  return url;
}

export async function createSlackIngress({
  env = process.env,
  fetchImpl = globalThis.fetch,
  now = () => Date.now(),
  logger = console,
} = {}) {
  const signingSecretFile = path.resolve(String(env.SLACK_SIGNING_SECRET_FILE || ""));
  const controlTokenFile = path.resolve(String(env.MULTIAGENT_SLACK_INGRESS_TOKEN_FILE || ""));
  const controlUrl = requiredUrl(env.MULTIAGENT_CONTROL_SERVER_URL);
  const allowedChannelIds = parseChannelAllowlist(env.SLACK_ALLOWED_CHANNEL_IDS);
  const ignoredBotUserId = String(env.SLACK_APP_BOT_USER_ID || "").trim();
  const queue = new SlackEventFileQueue(env.SLACK_INGRESS_STATE_DIR || "/var/lib/multiagent-slack");
  await queue.initialize();
  let timer = null;

  const deliver = async (event) => {
    const token = await secret(controlTokenFile, "control-server integration token");
    const target = new URL("/internal/integrations/slack/events", controlUrl);
    const response = await fetchImpl(target, {
      method: "POST",
      headers: { authorization: `Bearer ${token}`, "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify(event),
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new Error(`control server returned ${response.status}${body ? `: ${body.slice(0, 500)}` : ""}`);
    }
  };

  const drain = async () => {
    const outcome = await queue.drainOne(deliver);
    if (outcome.error) logger.error("Slack event delivery failed", { error: outcome.error.message });
    return outcome;
  };

  const server = http.createServer(async (request, response) => {
    const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
    try {
      if (request.method === "GET" && url.pathname === "/healthz") return json(response, 200, { ok: true });
      if (request.method === "GET" && url.pathname === "/readyz") {
        const [signing, token, queueDepth] = await Promise.all([
          secret(signingSecretFile, "Slack signing secret"),
          secret(controlTokenFile, "control-server integration token"),
          queue.size(),
        ]);
        return json(response, signing && token ? 200 : 503, { ready: Boolean(signing && token), queueDepth });
      }
      if (request.method !== "POST" || url.pathname !== "/slack/events") return json(response, 404, { error: "not found" });
      const rawBody = await readRawBody(request);
      const signingSecret = await secret(signingSecretFile, "Slack signing secret");
      if (!verifySlackRequest({
        rawBody,
        timestamp: request.headers["x-slack-request-timestamp"],
        signature: request.headers["x-slack-signature"],
        signingSecret,
        nowMs: now(),
      })) return json(response, 401, { error: "invalid Slack signature" });
      let body;
      try { body = JSON.parse(rawBody.toString("utf8")); }
      catch { return json(response, 400, { error: "invalid JSON" }); }
      const parsed = parseSlackEnvelope(body, { allowedChannelIds, ignoredBotUserId });
      if (parsed.kind === "challenge") return json(response, 200, { challenge: parsed.challenge });
      if (parsed.kind === "ignored") return json(response, 200, { ok: true, ignored: parsed.reason });
      const queued = await queue.enqueue(parsed.event);
      setImmediate(() => { void drain(); });
      return json(response, 200, { ok: true, ...queued });
    } catch (error) {
      logger.error("Slack ingress request failed", { error: error.message });
      return json(response, 400, { error: error.message || "request failed" });
    }
  });

  return {
    server,
    queue,
    drain,
    async start() {
      const port = Number(env.PORT || "8080");
      const host = env.HOST || "0.0.0.0";
      await new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(port, host, resolve);
      });
      timer = setInterval(() => { void drain(); }, 1000);
      timer.unref();
      return server.address();
    },
    async stop() {
      if (timer) clearInterval(timer);
      if (!server.listening) return;
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    },
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const ingress = await createSlackIngress();
  const address = await ingress.start();
  console.log(`multiagent Slack ingress listening on ${typeof address === "object" ? address.port : address}`);
  const stop = async () => { await ingress.stop(); process.exit(0); };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
}
