import assert from "node:assert/strict";
import { createHash, generateKeyPairSync } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { DatabaseSync } from "node:sqlite";
import { Ed25519Signer } from "../src/crypto.mjs";
import { Metrics } from "../src/metrics.mjs";
import { ProjectionWorker } from "../src/projection.mjs";
import { buildTraceCommitment } from "../bin/submit-trace-commitment.mjs";
import { AuditStore, GENESIS_HASH } from "../src/store.mjs";

const DIGEST_A = `sha256:${"a".repeat(64)}`;
const DIGEST_B = `sha256:${"b".repeat(64)}`;

function fixture() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "audit-log-store-"));
  const keyFile = path.join(directory, "signing.pem");
  const { privateKey } = generateKeyPairSync("ed25519");
  fs.writeFileSync(keyFile, privateKey.export({ type: "pkcs8", format: "pem" }), { mode: 0o600 });
  const signer = new Ed25519Signer({ privateKeyFile: keyFile, keyId: "test-key", loggerId: "test-logger" });
  const database = path.join(directory, "ledger.sqlite");
  const store = new AuditStore({ database, signer, checkpointInterval: 2 });
  return { directory, database, signer, store };
}

function event(eventId, payloadDigest = DIGEST_A) {
  return {
    eventId,
    sessionId: "session-123",
    eventType: "reviewer.verdict",
    payloadDigest,
    artifactReferences: [],
  };
}

test("serialized appends build a verifiable chain and signed checkpoint", (t) => {
  const value = fixture();
  t.after(() => { value.store.close(); fs.rmSync(value.directory, { recursive: true, force: true }); });
  value.store.append(event("event-1"), "reviewer");
  value.store.append(event("event-2", DIGEST_B), "supervisor");
  const [first, second] = value.store.entries("session-123");
  assert.equal(first.sequence, 1);
  assert.equal(first.previousHash, GENESIS_HASH);
  assert.equal(second.sequence, 2);
  assert.equal(second.previousHash, first.entryHash);
  assert.equal(value.store.head("session-123").entryHash, second.entryHash);
  const verification = value.store.verify("session-123");
  assert.deepEqual(verification, {
    ok: true,
    checkedLogs: 1,
    checkedEntries: 2,
    checkedCheckpoints: 1,
    errors: [],
  });
  const checkpointId = value.store.db.prepare("SELECT checkpoint_id FROM checkpoints").get().checkpoint_id;
  const checkpoint = value.store.checkpoint(checkpointId);
  assert.deepEqual(value.store.checkpoints("session-123"), [checkpoint]);
  const { loggerSignature, ...checkpointBody } = checkpoint;
  assert.equal(value.signer.verify(checkpointBody, loggerSignature), true);
});

test("an exact duplicate is idempotent and conflicting content is rejected", (t) => {
  const value = fixture();
  t.after(() => { value.store.close(); fs.rmSync(value.directory, { recursive: true, force: true }); });
  const first = value.store.append(event("event-1"), "reviewer");
  const duplicate = value.store.append(event("event-1"), "reviewer");
  assert.equal(first.duplicate, false);
  assert.equal(duplicate.duplicate, true);
  assert.equal(value.store.head("session-123").sequence, 1);
  assert.throws(() => value.store.append(event("event-1", DIGEST_B), "reviewer"), /different content/);
  assert.equal(value.store.head("session-123").sequence, 1);
});

test("startup and explicit verification detect ledger tampering", (t) => {
  const value = fixture();
  value.store.append(event("event-1"), "reviewer");
  value.store.close();
  const database = new DatabaseSync(value.database);
  database.prepare("UPDATE events SET previous_hash = ? WHERE event_id = ?").run(DIGEST_B, "event-1");
  database.close();
  t.after(() => fs.rmSync(value.directory, { recursive: true, force: true }));
  assert.throws(
    () => new AuditStore({ database: value.database, signer: value.signer, checkpointInterval: 2 }),
    /integrity check failed/,
  );
});

test("projection work is durable until explicitly completed", (t) => {
  const value = fixture();
  value.store.close();
  value.store = new AuditStore({ database: value.database, signer: value.signer, checkpointInterval: 2, projectionEnabled: true });
  t.after(() => { value.store.close(); fs.rmSync(value.directory, { recursive: true, force: true }); });
  value.store.append(event("event-1"), "sidecar");
  const pending = value.store.pendingProjections();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].eventId, "event-1");
  value.store.markProjected("event-1");
  assert.deepEqual(value.store.projectionCounts(), { complete: 1 });
});

test("projection worker exports derived JSONL after the authoritative append", async (t) => {
  const value = fixture();
  value.store.close();
  value.store = new AuditStore({ database: value.database, signer: value.signer, checkpointInterval: 2, projectionEnabled: true });
  t.after(() => { value.store.close(); fs.rmSync(value.directory, { recursive: true, force: true }); });
  value.store.append(event("event-1"), "sidecar");
  const projections = path.join(value.directory, "projections");
  const metrics = new Metrics();
  const worker = new ProjectionWorker({ store: value.store, directory: projections, intervalMs: 100, metrics });
  await worker.flush();
  const lines = fs.readFileSync(path.join(projections, "session-123.jsonl"), "utf8").trim().split("\n");
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).eventId, "event-1");
  assert.deepEqual(value.store.projectionCounts(), { complete: 1 });
  assert.match(metrics.render(value.store.projectionCounts()), /audit_log_projection_success_total 1/);
});

test("public signing descriptor is stable and contains no private material", (t) => {
  const value = fixture();
  t.after(() => { value.store.close(); fs.rmSync(value.directory, { recursive: true, force: true }); });
  const descriptor = value.signer.publicDescriptor();
  assert.match(descriptor.publicKeyPem, /BEGIN PUBLIC KEY/);
  assert.doesNotMatch(descriptor.publicKeyPem, /PRIVATE/);
  assert.equal(createHash("sha256").update(descriptor.publicKeyPem).digest("hex").length, 64);
});

test("trace commitment helper hashes bulk artifacts without embedding their bodies", async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "audit-trace-commitment-"));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const trace = path.join(directory, "trace.tar.gz");
  fs.writeFileSync(trace, "private trace body\n");
  const commitment = await buildTraceCommitment({
    eventId: "trace-export-1",
    sessionId: "session-123",
    file: trace,
    storageReference: "s3://audit-bucket/session-123.tar.gz",
    mediaType: "application/gzip",
  });
  assert.equal(commitment.eventType, "trace.artifact_exported");
  assert.equal(commitment.artifactReferences[0].size, 19);
  assert.match(commitment.artifactReferences[0].digest, /^sha256:[a-f0-9]{64}$/);
  assert.doesNotMatch(JSON.stringify(commitment), /private trace body/);
});
