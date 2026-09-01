import http from "node:http";
import { fileURLToPath } from "node:url";
import { ClientAuthorizer, AuthenticationError } from "./auth.mjs";
import { configFromEnvironment } from "./config.mjs";
import { Ed25519Signer } from "./crypto.mjs";
import { Metrics } from "./metrics.mjs";
import { ProjectionWorker } from "./projection.mjs";
import { AuditStore } from "./store.mjs";
import { validateEvent, validateIdentifier, ValidationError } from "./validation.mjs";

export function createAuditLogApplication(config) {
  const signer = new Ed25519Signer({
    privateKeyFile: config.signingKeyFile,
    keyId: config.signingKeyId,
    loggerId: config.loggerId,
  });
  const authorizer = new ClientAuthorizer(config.clientsFile);
  const metrics = new Metrics();
  const store = new AuditStore({
    database: config.database,
    signer,
    checkpointInterval: config.checkpointInterval,
    projectionEnabled: Boolean(config.projectionDir),
  });
  const projection = new ProjectionWorker({
    store,
    directory: config.projectionDir,
    intervalMs: config.projectionIntervalMs,
    metrics,
  });
  let ready = true;

  const server = http.createServer(async (request, response) => {
    try {
      setSecurityHeaders(response);
      const url = new URL(request.url, "http://audit-log.invalid");
      if (request.method === "GET" && url.pathname === "/healthz") return json(response, 200, { live: true });
      if (request.method === "GET" && url.pathname === "/readyz") return json(response, ready ? 200 : 503, { ready });
      if (request.method === "GET" && url.pathname === "/v1/public-key") return json(response, 200, signer.publicDescriptor());

      const client = authorizer.authenticate(request.headers.authorization);
      if (request.method === "GET" && url.pathname === "/metrics") {
        authorizer.requirePermission(client, "read");
        response.writeHead(200, { "content-type": "text/plain; version=0.0.4; charset=utf-8" });
        return response.end(metrics.render(store.projectionCounts()));
      }

      if (request.method === "POST" && url.pathname === "/v1/events") {
        if (!ready) throw new RequestError("integrity_unavailable", "authoritative append is disabled until ledger integrity is restored", 503);
        authorizer.requirePermission(client, "append");
        const event = validateEvent(await readJson(request, config.maxEventBytes));
        authorizer.authorizeSession(client, event.sessionId);
        authorizer.authorizeEvent(client, event.eventType);
        const result = store.append(event, client.id);
        metrics.increment(result.duplicate ? "audit_log_duplicate_appends_total" : "audit_log_appends_total");
        return noContent(response);
      }

      const headMatch = /^\/v1\/logs\/([^/]+)\/head$/.exec(url.pathname);
      if (request.method === "GET" && headMatch) {
        authorizer.requirePermission(client, "read");
        const logId = validateIdentifier(decodeSegment(headMatch[1]), "logId");
        authorizer.authorizeSession(client, logId);
        const head = store.head(logId);
        return head ? json(response, 200, head) : notFound(response);
      }

      const entriesMatch = /^\/v1\/logs\/([^/]+)\/entries$/.exec(url.pathname);
      if (request.method === "GET" && entriesMatch) {
        authorizer.requirePermission(client, "read");
        const logId = validateIdentifier(decodeSegment(entriesMatch[1]), "logId");
        authorizer.authorizeSession(client, logId);
        const after = queryInteger(url, "after", 0, { min: 0, max: Number.MAX_SAFE_INTEGER });
        const limit = queryInteger(url, "limit", 100, { min: 1, max: 1000 });
        return json(response, 200, { logId, entries: store.entries(logId, { after, limit }) });
      }

      const checkpointsMatch = /^\/v1\/logs\/([^/]+)\/checkpoints$/.exec(url.pathname);
      if (request.method === "GET" && checkpointsMatch) {
        authorizer.requirePermission(client, "read");
        const logId = validateIdentifier(decodeSegment(checkpointsMatch[1]), "logId");
        authorizer.authorizeSession(client, logId);
        const after = queryInteger(url, "after", 0, { min: 0, max: Number.MAX_SAFE_INTEGER });
        const limit = queryInteger(url, "limit", 100, { min: 1, max: 1000 });
        return json(response, 200, { logId, checkpoints: store.checkpoints(logId, { after, limit }) });
      }

      const checkpointMatch = /^\/v1\/checkpoints\/([^/]+)$/.exec(url.pathname);
      if (request.method === "GET" && checkpointMatch) {
        authorizer.requirePermission(client, "read");
        const checkpointId = validateIdentifier(decodeSegment(checkpointMatch[1]), "checkpointId");
        const sessionId = store.checkpointSession(checkpointId);
        if (!sessionId) return notFound(response);
        authorizer.authorizeSession(client, sessionId);
        return json(response, 200, store.checkpoint(checkpointId));
      }

      if (request.method === "POST" && url.pathname === "/v1/verify") {
        authorizer.requirePermission(client, "verify");
        const body = await readJson(request, 4096);
        if (!body || typeof body !== "object" || Array.isArray(body)) throw new ValidationError("verify request must be an object");
        for (const key of Object.keys(body)) {
          if (key !== "logId") throw new ValidationError(`verify request contains unsupported property ${key}`);
        }
        const logId = body.logId === undefined ? null : validateIdentifier(body.logId, "logId");
        if (logId) authorizer.authorizeSession(client, logId);
        const result = store.verify(logId);
        metrics.integrity = result.ok ? 1 : 0;
        ready = result.ok;
        return json(response, result.ok ? 200 : 503, result);
      }

      return notFound(response);
    } catch (error) {
      metrics.increment("audit_log_rejected_requests_total");
      const statusCode = error.statusCode || (error instanceof ValidationError ? 400 : 500);
      const code = error.code || (statusCode === 500 ? "internal_error" : "invalid_request");
      if (statusCode === 500) console.error("audit logger request failed", error);
      return json(response, statusCode, { error: { code, message: statusCode === 500 ? "internal server error" : error.message } });
    }
  });

  server.on("clientError", (_error, socket) => socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"));
  return {
    server,
    store,
    projection,
    start() { projection.start(); },
    async close() {
      projection.stop();
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
      store.close();
    },
  };
}

async function readJson(request, maxBytes) {
  const declared = Number(request.headers["content-length"] || 0);
  if (Number.isFinite(declared) && declared > maxBytes) throw new RequestError("request_too_large", "request body exceeds size limit", 413);
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maxBytes) throw new RequestError("request_too_large", "request body exceeds size limit", 413);
    chunks.push(chunk);
  }
  if (size === 0) throw new ValidationError("request body is required");
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); }
  catch { throw new ValidationError("request body must be valid JSON"); }
}

class RequestError extends Error {
  constructor(code, message, statusCode) {
    super(message);
    this.code = code;
    this.statusCode = statusCode;
  }
}

function queryInteger(url, name, fallback, { min, max }) {
  const value = url.searchParams.get(name);
  if (value === null) return fallback;
  if (!/^\d+$/.test(value)) throw new ValidationError(`${name} must be an integer`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) throw new ValidationError(`${name} is outside the allowed range`);
  return parsed;
}

function decodeSegment(value) {
  try { return decodeURIComponent(value); }
  catch { throw new ValidationError("URL path contains invalid encoding"); }
}

function setSecurityHeaders(response) {
  response.setHeader("cache-control", "no-store");
  response.setHeader("x-content-type-options", "nosniff");
}

function json(response, statusCode, body) {
  response.writeHead(statusCode, { "content-type": "application/json; charset=utf-8" });
  response.end(`${JSON.stringify(body)}\n`);
}

function noContent(response) {
  response.writeHead(204);
  response.end();
}

function notFound(response) {
  return json(response, 404, { error: { code: "not_found", message: "resource not found" } });
}

async function main() {
  process.umask(0o077);
  const config = configFromEnvironment();
  const application = createAuditLogApplication(config);
  application.server.listen(config.port, config.host, () => {
    application.start();
    console.log(`audit logger listening on ${config.host}:${config.port}`);
  });
  const stop = async (signal) => {
    console.log(`received ${signal}; stopping audit logger`);
    await application.close();
    process.exit(0);
  };
  process.on("SIGINT", () => void stop("SIGINT"));
  process.on("SIGTERM", () => void stop("SIGTERM"));
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error("audit logger failed to start", error);
    process.exit(1);
  });
}

export { AuthenticationError };
