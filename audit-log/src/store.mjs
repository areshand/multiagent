import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { canonicalJson } from "./canonical-json.mjs";
import { sha256 } from "./crypto.mjs";

const GENESIS_HASH = `sha256:${"0".repeat(64)}`;

export class DuplicateEventError extends Error {
  constructor(message) {
    super(message);
    this.code = "event_id_conflict";
    this.statusCode = 409;
  }
}

export class AuditStore {
  constructor({ database, signer, checkpointInterval = 100, projectionEnabled = false, clock = () => new Date() }) {
    fs.mkdirSync(path.dirname(database), { recursive: true, mode: 0o700 });
    this.database = database;
    this.db = new DatabaseSync(database);
    this.signer = signer;
    this.checkpointInterval = checkpointInterval;
    this.projectionEnabled = projectionEnabled;
    this.clock = clock;
    try {
      this.initialize();
      const verification = this.verify();
      if (!verification.ok) throw new Error(`audit ledger integrity check failed: ${verification.errors.join("; ")}`);
    } catch (error) {
      this.db.close();
      throw error;
    }
  }

  initialize() {
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA synchronous = FULL;
      PRAGMA locking_mode = EXCLUSIVE;
      PRAGMA foreign_keys = ON;
      PRAGMA trusted_schema = OFF;
      CREATE TABLE IF NOT EXISTS logs (
        log_id TEXT PRIMARY KEY,
        sequence INTEGER NOT NULL,
        head_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL
      ) STRICT;
      CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY,
        request_digest TEXT NOT NULL,
        log_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        previous_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL UNIQUE,
        committed_at TEXT NOT NULL,
        producer_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        event_json TEXT NOT NULL,
        entry_json TEXT NOT NULL,
        UNIQUE(log_id, sequence),
        FOREIGN KEY(log_id) REFERENCES logs(log_id)
      ) STRICT;
      CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id TEXT PRIMARY KEY,
        log_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        entry_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        checkpoint_json TEXT NOT NULL,
        UNIQUE(log_id, sequence),
        FOREIGN KEY(log_id) REFERENCES logs(log_id)
      ) STRICT;
      CREATE TABLE IF NOT EXISTS projection_queue (
        event_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        FOREIGN KEY(event_id) REFERENCES events(event_id)
      ) STRICT;
      CREATE INDEX IF NOT EXISTS events_log_sequence ON events(log_id, sequence);
      CREATE INDEX IF NOT EXISTS projections_pending ON projection_queue(status, next_attempt_at);
    `);
    const journalMode = this.db.prepare("PRAGMA journal_mode").get().journal_mode;
    if (String(journalMode).toLowerCase() !== "wal") throw new Error("audit ledger requires SQLite WAL mode");
    for (const suffix of ["", "-wal", "-shm"]) {
      const file = `${this.database}${suffix}`;
      if (file && file !== ":memory:" && fs.existsSync(file)) fs.chmodSync(file, 0o600);
    }
  }

  append(event, producerId) {
    const acceptedEvent = { ...event, producerId };
    const requestDigest = sha256(canonicalJson(acceptedEvent));
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const existing = this.db.prepare("SELECT request_digest FROM events WHERE event_id = ?").get(event.eventId);
      if (existing) {
        if (existing.request_digest !== requestDigest) throw new DuplicateEventError("eventId already commits different content");
        this.db.exec("COMMIT");
        return { duplicate: true };
      }

      const head = this.db.prepare("SELECT sequence, head_hash FROM logs WHERE log_id = ?").get(event.sessionId);
      const sequence = Number(head?.sequence || 0) + 1;
      const previousHash = head?.head_hash || GENESIS_HASH;
      const committedAt = this.clock().toISOString();
      const entryBody = {
        logId: event.sessionId,
        sequence,
        previousHash,
        committedAt,
        producerId,
        eventId: event.eventId,
        eventType: event.eventType,
        payloadDigest: event.payloadDigest,
        artifactReferences: event.artifactReferences,
      };
      const entryHash = sha256(canonicalJson(entryBody));
      this.db.prepare(`
        INSERT INTO logs(log_id, sequence, head_hash, updated_at) VALUES(?, ?, ?, ?)
        ON CONFLICT(log_id) DO UPDATE SET sequence=excluded.sequence, head_hash=excluded.head_hash, updated_at=excluded.updated_at
      `).run(event.sessionId, sequence, entryHash, committedAt);
      this.db.prepare(`
        INSERT INTO events(
          event_id, request_digest, log_id, sequence, previous_hash, entry_hash,
          committed_at, producer_id, event_type, payload_digest, event_json, entry_json
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      `).run(
        event.eventId,
        requestDigest,
        event.sessionId,
        sequence,
        previousHash,
        entryHash,
        committedAt,
        producerId,
        event.eventType,
        event.payloadDigest,
        canonicalJson(acceptedEvent),
        canonicalJson({ ...entryBody, entryHash }),
      );
      if (sequence % this.checkpointInterval === 0) this.createCheckpoint(event.sessionId, sequence, entryHash, committedAt);
      if (this.projectionEnabled) {
        this.db.prepare("INSERT INTO projection_queue(event_id, status, next_attempt_at) VALUES(?, 'pending', ?)").run(event.eventId, committedAt);
      }
      this.db.exec("COMMIT");
      return { duplicate: false };
    } catch (error) {
      try { this.db.exec("ROLLBACK"); } catch {}
      throw error;
    }
  }

  createCheckpoint(logId, sequence, entryHash, createdAt) {
    const checkpointId = `checkpoint-${sha256(`${logId}:${sequence}:${entryHash}`).slice("sha256:".length, 55)}`;
    const body = {
      apiVersion: "audit.multiagent.dev/v1",
      kind: "AuditCheckpoint",
      checkpointId,
      logId,
      sequence,
      entryHash,
      createdAt,
      loggerIdentity: this.signer.loggerId,
    };
    const checkpoint = { ...body, loggerSignature: this.signer.sign(body) };
    this.db.prepare(`
      INSERT INTO checkpoints(checkpoint_id, log_id, sequence, entry_hash, created_at, checkpoint_json)
      VALUES(?, ?, ?, ?, ?, ?)
    `).run(checkpointId, logId, sequence, entryHash, createdAt, canonicalJson(checkpoint));
  }

  head(logId) {
    const row = this.db.prepare("SELECT log_id, sequence, head_hash, updated_at FROM logs WHERE log_id = ?").get(logId);
    if (!row) return null;
    return { logId: row.log_id, sequence: Number(row.sequence), entryHash: row.head_hash, updatedAt: row.updated_at };
  }

  entries(logId, { after = 0, limit = 100 } = {}) {
    return this.db.prepare(`
      SELECT entry_json FROM events WHERE log_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?
    `).all(logId, after, limit).map((row) => JSON.parse(row.entry_json));
  }

  checkpoint(checkpointId) {
    const row = this.db.prepare("SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id = ?").get(checkpointId);
    return row ? JSON.parse(row.checkpoint_json) : null;
  }

  checkpointSession(checkpointId) {
    return this.db.prepare("SELECT log_id FROM checkpoints WHERE checkpoint_id = ?").get(checkpointId)?.log_id || null;
  }

  checkpoints(logId, { after = 0, limit = 100 } = {}) {
    return this.db.prepare(`
      SELECT checkpoint_json FROM checkpoints
      WHERE log_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?
    `).all(logId, after, limit).map((row) => JSON.parse(row.checkpoint_json));
  }

  pendingProjections(now = new Date().toISOString(), limit = 100) {
    return this.db.prepare(`
      SELECT q.event_id, q.attempts, e.entry_json
      FROM projection_queue q JOIN events e ON e.event_id = q.event_id
      WHERE q.status = 'pending' AND q.next_attempt_at <= ?
      ORDER BY e.committed_at ASC LIMIT ?
    `).all(now, limit).map((row) => ({ eventId: row.event_id, attempts: Number(row.attempts), entry: JSON.parse(row.entry_json) }));
  }

  markProjected(eventId) {
    this.db.prepare("UPDATE projection_queue SET status='complete', last_error=NULL WHERE event_id = ?").run(eventId);
  }

  markProjectionFailed(eventId, attempts, message) {
    const delay = Math.min(60_000, 1000 * 2 ** Math.min(attempts, 6));
    const next = new Date(Date.now() + delay).toISOString();
    this.db.prepare(`
      UPDATE projection_queue SET attempts=?, next_attempt_at=?, last_error=? WHERE event_id=?
    `).run(attempts, next, String(message).slice(0, 1024), eventId);
  }

  projectionCounts() {
    const rows = this.db.prepare("SELECT status, COUNT(*) AS count FROM projection_queue GROUP BY status").all();
    return Object.fromEntries(rows.map((row) => [row.status, Number(row.count)]));
  }

  verify(logId = null) {
    const errors = [];
    const logs = logId
      ? this.db.prepare("SELECT log_id, sequence, head_hash FROM logs WHERE log_id = ?").all(logId)
      : this.db.prepare("SELECT log_id, sequence, head_hash FROM logs ORDER BY log_id").all();
    let checkedEntries = 0;
    for (const log of logs) {
      let previousHash = GENESIS_HASH;
      let expectedSequence = 1;
      const rows = this.db.prepare("SELECT * FROM events WHERE log_id = ? ORDER BY sequence").all(log.log_id);
      for (const row of rows) {
        checkedEntries += 1;
        const entry = JSON.parse(row.entry_json);
        const { entryHash, ...body } = entry;
        const acceptedEvent = JSON.parse(row.event_json);
        if (Number(row.sequence) !== expectedSequence) errors.push(`${log.log_id}: expected sequence ${expectedSequence}`);
        if (row.previous_hash !== previousHash || entry.previousHash !== previousHash) errors.push(`${log.log_id}:${row.sequence}: previous hash mismatch`);
        const calculated = sha256(canonicalJson(body));
        if (calculated !== row.entry_hash || calculated !== entryHash) errors.push(`${log.log_id}:${row.sequence}: entry hash mismatch`);
        if (sha256(canonicalJson(acceptedEvent)) !== row.request_digest) errors.push(`${log.log_id}:${row.sequence}: accepted event digest mismatch`);
        if (
          acceptedEvent.sessionId !== entry.logId ||
          acceptedEvent.eventId !== entry.eventId ||
          acceptedEvent.producerId !== entry.producerId ||
          acceptedEvent.eventType !== entry.eventType ||
          acceptedEvent.payloadDigest !== entry.payloadDigest ||
          canonicalJson(acceptedEvent.artifactReferences) !== canonicalJson(entry.artifactReferences)
        ) errors.push(`${log.log_id}:${row.sequence}: accepted event does not match chain entry`);
        previousHash = calculated;
        expectedSequence += 1;
      }
      if (Number(log.sequence) !== rows.length || log.head_hash !== previousHash) errors.push(`${log.log_id}: authoritative head mismatch`);
    }
    const checkpoints = logId
      ? this.db.prepare("SELECT checkpoint_json FROM checkpoints WHERE log_id = ? ORDER BY sequence").all(logId)
      : this.db.prepare("SELECT checkpoint_json FROM checkpoints ORDER BY log_id, sequence").all();
    for (const row of checkpoints) {
      const checkpoint = JSON.parse(row.checkpoint_json);
      const { loggerSignature, ...body } = checkpoint;
      if (!this.signer.verify(body, loggerSignature)) errors.push(`${checkpoint.checkpointId}: invalid checkpoint signature`);
      const event = this.db.prepare("SELECT entry_hash FROM events WHERE log_id = ? AND sequence = ?").get(checkpoint.logId, checkpoint.sequence);
      if (!event || event.entry_hash !== checkpoint.entryHash) errors.push(`${checkpoint.checkpointId}: checkpoint entry mismatch`);
    }
    return { ok: errors.length === 0, checkedLogs: logs.length, checkedEntries, checkedCheckpoints: checkpoints.length, errors };
  }

  close() {
    this.db.close();
  }
}

export { GENESIS_HASH };
