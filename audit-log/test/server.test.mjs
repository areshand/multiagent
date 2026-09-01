import assert from "node:assert/strict";
import { createHash, generateKeyPairSync } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createAuditLogApplication } from "../src/server.mjs";

const TOKEN = "test-audit-token-that-is-long-enough";
const OTHER_TOKEN = "different-audit-token-long-enough";
const DIGEST = `sha256:${"1".repeat(64)}`;

function digest(value) {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

async function fixture(t, clientOverrides = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "audit-log-server-"));
  const keyFile = path.join(directory, "signing.pem");
  const clientsFile = path.join(directory, "clients.json");
  const { privateKey } = generateKeyPairSync("ed25519");
  fs.writeFileSync(keyFile, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  fs.writeFileSync(clientsFile, JSON.stringify({
    clients: [{
      id: "reviewer",
      tokenSha256: digest(TOKEN),
      permissions: ["append", "read", "verify"],
      eventTypes: ["reviewer.*"],
      sessions: ["session-*"],
      ...clientOverrides,
    }],
  }));
  const application = createAuditLogApplication({
    database: path.join(directory, "ledger.sqlite"),
    signingKeyFile: keyFile,
    signingKeyId: "test-key",
    loggerId: "test-logger",
    clientsFile,
    checkpointInterval: 2,
    maxEventBytes: 65_536,
    projectionDir: null,
    projectionIntervalMs: 100,
  });
  await new Promise((resolve) => application.server.listen(0, "127.0.0.1", resolve));
  application.start();
  const address = application.server.address();
  const base = `http://127.0.0.1:${address.port}`;
  t.after(async () => { await application.close(); fs.rmSync(directory, { recursive: true, force: true }); });
  return { application, base };
}

function event(eventId = "event-1", eventType = "reviewer.verdict") {
  return { eventId, sessionId: "session-123", eventType, payloadDigest: DIGEST, artifactReferences: [] };
}

async function request(base, pathname, { method = "GET", token = TOKEN, body } = {}) {
  return fetch(`${base}${pathname}`, {
    method,
    headers: token ? { authorization: `Bearer ${token}`, ...(body ? { "content-type": "application/json" } : {}) } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
}

test("HTTP service authenticates, authorizes, appends, and exposes verification evidence", async (t) => {
  const { base } = await fixture(t);
  const append = await request(base, "/v1/events", { method: "POST", body: event() });
  assert.equal(append.status, 204);
  assert.equal(await append.text(), "");

  const head = await request(base, "/v1/logs/session-123/head");
  assert.equal(head.status, 200);
  assert.equal((await head.json()).sequence, 1);
  const entries = await request(base, "/v1/logs/session-123/entries?after=0&limit=10");
  const entry = (await entries.json()).entries[0];
  assert.equal(entry.producerId, "reviewer");
  assert.match(entry.entryHash, /^sha256:[a-f0-9]{64}$/);
  const verify = await request(base, "/v1/verify", { method: "POST", body: { logId: "session-123" } });
  assert.equal(verify.status, 200);
  assert.equal((await verify.json()).ok, true);
  const metrics = await request(base, "/metrics");
  assert.match(await metrics.text(), /audit_log_appends_total 1/);
});

test("health and public key are public while ledger APIs require authentication", async (t) => {
  const { base } = await fixture(t);
  assert.equal((await request(base, "/healthz", { token: null })).status, 200);
  assert.equal((await request(base, "/readyz", { token: null })).status, 200);
  const publicKey = await request(base, "/v1/public-key", { token: null });
  assert.match((await publicKey.json()).publicKeyPem, /BEGIN PUBLIC KEY/);
  assert.equal((await request(base, "/v1/logs/session-123/head", { token: null })).status, 401);
  assert.equal((await request(base, "/v1/logs/session-123/head", { token: OTHER_TOKEN })).status, 401);
});

test("producer event and session restrictions are enforced before append", async (t) => {
  const { base } = await fixture(t);
  const wrongType = await request(base, "/v1/events", { method: "POST", body: event("event-1", "permit.issued") });
  assert.equal(wrongType.status, 403);
  const wrongSession = await request(base, "/v1/events", { method: "POST", body: { ...event("event-2"), sessionId: "other-123" } });
  assert.equal(wrongSession.status, 403);
  assert.equal((await request(base, "/v1/logs/session-123/head")).status, 404);
});

test("idempotent replay is an empty successful acknowledgement", async (t) => {
  const { base } = await fixture(t);
  const first = await request(base, "/v1/events", { method: "POST", body: event() });
  assert.equal(first.status, 204);
  assert.equal(await first.text(), "");
  const replay = await request(base, "/v1/events", { method: "POST", body: event() });
  assert.equal(replay.status, 204);
  assert.equal(await replay.text(), "");
  assert.equal((await (await request(base, "/v1/logs/session-123/head")).json()).sequence, 1);
  const conflict = await request(base, "/v1/events", { method: "POST", body: { ...event(), payloadDigest: `sha256:${"2".repeat(64)}` } });
  assert.equal(conflict.status, 409);
});

test("readers can discover and verify periodic checkpoints without producer receipts", async (t) => {
  const { base } = await fixture(t);
  assert.equal((await request(base, "/v1/events", { method: "POST", body: event("event-1") })).status, 204);
  assert.equal((await request(base, "/v1/events", { method: "POST", body: event("event-2") })).status, 204);
  const response = await request(base, "/v1/logs/session-123/checkpoints?after=0&limit=10");
  assert.equal(response.status, 200);
  const checkpoints = (await response.json()).checkpoints;
  assert.equal(checkpoints.length, 1);
  assert.equal(checkpoints[0].sequence, 2);
  assert.equal(checkpoints[0].loggerSignature.algorithm, "Ed25519");
});

test("oversized and structurally invalid events fail closed", async (t) => {
  const { base } = await fixture(t);
  const unsupported = await request(base, "/v1/events", { method: "POST", body: { ...event(), unexpected: true } });
  assert.equal(unsupported.status, 400);
  const response = await fetch(`${base}/v1/events`, {
    method: "POST",
    headers: { authorization: `Bearer ${TOKEN}`, "content-type": "application/json" },
    body: JSON.stringify({ ...event(), artifactReferences: [{ uri: "x".repeat(70_000) }] }),
  });
  assert.equal(response.status, 413);
});

test("failed integrity verification disables subsequent authoritative appends", async (t) => {
  const { application, base } = await fixture(t);
  assert.equal((await request(base, "/v1/events", { method: "POST", body: event() })).status, 204);
  application.store.db.prepare("UPDATE events SET previous_hash = ? WHERE event_id = ?").run(`sha256:${"9".repeat(64)}`, "event-1");
  const verify = await request(base, "/v1/verify", { method: "POST", body: {} });
  assert.equal(verify.status, 503);
  assert.equal((await request(base, "/readyz", { token: null })).status, 503);
  const append = await request(base, "/v1/events", { method: "POST", body: event("event-2") });
  assert.equal(append.status, 503);
  assert.equal((await append.json()).error.code, "integrity_unavailable");
});
