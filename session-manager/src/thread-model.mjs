import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const acceptingSessionStates = new Set(["queued", "starting", "running", "waiting_for_user"]);
const terminalSessionStates = new Set(["completed", "failed", "interrupted", "cancelled"]);
const publicEventTypes = new Set([
  "user_message",
  "system_message",
  "assistant_message",
  "progress",
  "question",
  "review_requested",
  "review_resolved",
  "artifact_available",
  "session_started",
  "session_completed",
  "session_interrupted",
]);

const clone = (value) => structuredClone(value);

export function generateThreadId({ now = Date.now(), randomBytes = crypto.randomBytes } = {}) {
  return `thread-${Number(now).toString(36)}-${randomBytes(5).toString("hex")}`;
}

function requiredString(value, name, max = 32768) {
  if (typeof value !== "string" || !value.trim() || value.length > max) {
    throw new Error(`${name} must contain 1 to ${max} characters`);
  }
  return value.trim();
}

function boundedPayload(payload) {
  const value = payload === undefined ? {} : clone(payload);
  if (Buffer.byteLength(JSON.stringify(value), "utf8") > 64 * 1024) {
    throw new Error("event payload exceeds 64 KiB");
  }
  return value;
}

function boundedRepairPaths(values, required) {
  if (!Array.isArray(values) || values.length > 32
    || (required ? values.length < 1 : values.length !== 0)) {
    throw new Error("repair paths must exactly match the requested source-write effect");
  }
  return [...new Set(values.map((value) => {
    const raw = requiredString(value, "repair path", 512).replaceAll("\\", "/");
    const normalized = path.posix.normalize(raw);
    if (path.posix.isAbsolute(normalized) || normalized === "." || normalized === ".." || normalized.startsWith("../")) {
      throw new Error("repair paths must stay inside the selected repository");
    }
    return normalized;
  }))].sort();
}
const approvedRepairEffects = Object.freeze(["source-write", "reviewed-ops"]);

function boundedRepairEffects(values) {
  if (!Array.isArray(values) || values.length < 1 || values.length > approvedRepairEffects.length
    || new Set(values).size !== values.length
    || values.some((effect) => !approvedRepairEffects.includes(effect))) {
    throw new Error("repair review effects must be source-write and/or reviewed-ops");
  }
  return approvedRepairEffects.filter((effect) => values.includes(effect));
}


function notFound() {
  const error = new Error("thread not found");
  error.statusCode = 404;
  return error;
}

function conflict(message) {
  const error = new Error(message);
  error.statusCode = 409;
  return error;
}

export class InMemoryThreadStore {
  constructor(snapshot = null) {
    this.restoreSnapshot(snapshot);
  }

  restoreSnapshot(snapshot = null) {
    if (snapshot && !new Set([1, 2, 3]).has(snapshot.schemaVersion)) throw new Error("unsupported thread manifest schema");
    const entries = (name) => {
      const value = snapshot?.[name] || [];
      if (!Array.isArray(value)) throw new Error(`thread manifest ${name} must be an array`);
      return value;
    };
    this.threads = new Map(entries("threads"));
    this.sessions = new Map(entries("sessions"));
    this.events = new Map(entries("events"));
    this.idempotency = new Map(entries("idempotency"));
    this.checkpoints = new Map(entries("checkpoints"));
    this.artifacts = new Map(entries("artifacts"));
    this.reviews = new Map(entries("reviews"));
  }

  snapshot() {
    return clone({
      schemaVersion: 3,
      threads: [...this.threads.entries()],
      sessions: [...this.sessions.entries()],
      events: [...this.events.entries()],
      idempotency: [...this.idempotency.entries()],
      checkpoints: [...this.checkpoints.entries()],
      artifacts: [...this.artifacts.entries()],
      reviews: [...this.reviews.entries()],
    });
  }

  createThread({ id, ownerSubject, repository, title = "", source = null, now = new Date().toISOString() }) {
    requiredString(id, "thread id", 63);
    requiredString(ownerSubject, "owner subject", 256);
    requiredString(repository, "repository", 128);
    if (this.threads.has(id)) throw conflict("thread already exists");
    const thread = {
      id,
      ownerSubject,
      repository,
      title: String(title || "").trim().slice(0, 256),
      state: "idle",
      headSequence: 0,
      activeSessionId: null,
      queuedSessionId: null,
      pendingReviewId: null,
      continuationBlocked: false,
      source: source === null ? null : boundedPayload(source),
      leaseGeneration: 0,
      createdAt: now,
      updatedAt: now,
    };
    this.threads.set(id, thread);
    this.events.set(id, []);
    this.artifacts.set(id, []);
    return clone(thread);
  }

  listThreadsForActor(actor) {
    return [...this.threads.values()]
      .filter((thread) => thread.ownerSubject === actor)
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))
      .map(clone);
  }

  getThreadForActor(threadId, actor) {
    return clone(this.#authorizedThread(threadId, actor));
  }

  appendUserMessageAndRoute({
    threadId,
    actor,
    messageId,
    text,
    newSessionId,
    now = new Date().toISOString(),
    leaseTtlMs = 60_000,
  }) {
    const thread = this.#authorizedThread(threadId, actor);
    if (thread.pendingReviewId) throw conflict(`resolve pending review ${thread.pendingReviewId} before continuing`);
    if (thread.continuationBlocked) throw conflict("thread cannot continue after its repair review was rejected");
    requiredString(messageId, "message id", 128);
    const message = requiredString(text, "message");
    const key = `${threadId}:${actor}:${messageId}`;
    const existing = this.idempotency.get(key);
    if (existing) return clone(existing);

    let session = thread.activeSessionId ? this.sessions.get(thread.activeSessionId) : null;
    if (session && !acceptingSessionStates.has(session.status) && thread.queuedSessionId) {
      session = this.sessions.get(thread.queuedSessionId);
    }
    let createdSession = false;
    if (!session || !acceptingSessionStates.has(session.status)) {
      requiredString(newSessionId, "new session id", 63);
      if (this.sessions.has(newSessionId)) throw conflict("session already exists");
      const activateNow = !thread.activeSessionId;
      const generation = activateNow ? thread.leaseGeneration + 1 : null;
      session = {
        id: newSessionId,
        threadId,
        ordinal: [...this.sessions.values()].filter((candidate) => candidate.threadId === threadId).length + 1,
        actorSubject: actor,
        authorityScope: "observe",
        mutationGrant: null,
        triggerMessageId: messageId,
        status: "queued",
        leaseGeneration: generation,
        leaseExpiresAt: activateNow ? new Date(Date.parse(now) + leaseTtlMs).toISOString() : null,
        inboxHeadSequence: 0,
        inboxAckSequence: 0,
        contextHeadSequence: thread.headSequence,
        createdAt: now,
        updatedAt: now,
      };
      this.sessions.set(session.id, session);
      if (activateNow) {
        thread.activeSessionId = session.id;
        thread.leaseGeneration = generation;
        thread.state = "starting";
      } else {
        thread.queuedSessionId = session.id;
      }
      createdSession = true;
    }

    const event = this.#appendEvent(thread, {
      eventId: messageId,
      sessionId: session.id,
      actorSubject: actor,
      type: "user_message",
      payload: { text: message },
      createdAt: now,
    });
    session.inboxHeadSequence = event.sequence;
    session.updatedAt = now;
    thread.updatedAt = now;
    const result = { event, session: clone(session), createdSession };
    this.idempotency.set(key, result);
    return clone(result);
  }

  createExternalThreadAndRoute({
    id,
    ownerSubject,
    repository,
    title = "",
    sourceActor,
    source,
    eventId,
    text,
    newSessionId,
    now = new Date().toISOString(),
    leaseTtlMs = 60_000,
  }) {
    requiredString(sourceActor, "source actor", 256);
    const externalId = requiredString(source?.eventId, "external event id", 256);
    const sourceType = requiredString(source?.type, "external source type", 64);
    const key = `external:${sourceType}:${externalId}`;
    const existing = this.idempotency.get(key);
    if (existing) return clone({ ...existing, duplicate: true });

    const thread = this.createThread({ id, ownerSubject, repository, title, source, now });
    requiredString(eventId, "message id", 128);
    const message = requiredString(text, "message");
    requiredString(newSessionId, "new session id", 63);
    if (this.sessions.has(newSessionId)) throw conflict("session already exists");
    const session = {
      id: newSessionId,
      threadId: thread.id,
      ordinal: 1,
      actorSubject: sourceActor,
      authorityScope: "observe",
      mutationGrant: null,
      triggerMessageId: eventId,
      status: "queued",
      leaseGeneration: 1,
      leaseExpiresAt: new Date(Date.parse(now) + leaseTtlMs).toISOString(),
      inboxHeadSequence: 0,
      inboxAckSequence: 0,
      contextHeadSequence: 0,
      createdAt: now,
      updatedAt: now,
    };
    this.sessions.set(session.id, session);
    const mutableThread = this.threads.get(thread.id);
    mutableThread.activeSessionId = session.id;
    mutableThread.leaseGeneration = 1;
    mutableThread.state = "starting";
    const event = this.#appendEvent(mutableThread, {
      eventId,
      sessionId: session.id,
      actorSubject: sourceActor,
      type: "system_message",
      payload: { text: message, source: boundedPayload(source) },
      createdAt: now,
    });
    session.inboxHeadSequence = event.sequence;
    session.updatedAt = now;
    mutableThread.updatedAt = now;
    const result = { thread: clone(mutableThread), event: clone(event), session: clone(session), createdSession: true, duplicate: false };
    this.idempotency.set(key, result);
    return clone(result);
  }

  markSessionRunning({ threadId, sessionId, generation, now = new Date().toISOString() }) {
    const { thread, session } = this.#currentSession(threadId, sessionId, generation);
    if (!new Set(["queued", "starting"]).has(session.status)) throw conflict("session cannot enter running state");
    session.status = "running";
    session.updatedAt = now;
    thread.state = "running";
    thread.updatedAt = now;
    return clone(session);
  }

  acknowledgeInbox({ threadId, sessionId, generation, throughSequence, now = new Date().toISOString() }) {
    const { session } = this.#currentSession(threadId, sessionId, generation);
    if (!Number.isSafeInteger(throughSequence) || throughSequence < session.inboxAckSequence || throughSequence > session.inboxHeadSequence) {
      throw new Error("invalid inbox acknowledgement sequence");
    }
    session.inboxAckSequence = throughSequence;
    session.updatedAt = now;
    return clone(session);
  }

  markSessionFinishing({ threadId, sessionId, generation, now = new Date().toISOString() }) {
    const { thread, session } = this.#currentSession(threadId, sessionId, generation);
    if (session.inboxAckSequence !== session.inboxHeadSequence) {
      return { finishing: false, reason: "pending_input", session: clone(session) };
    }
    if (!new Set(["running", "waiting_for_user"]).has(session.status)) throw conflict("session cannot enter finishing state");
    session.status = "finishing";
    session.updatedAt = now;
    thread.state = "running";
    thread.updatedAt = now;
    return { finishing: true, session: clone(session) };
  }

  appendFencedSessionEvent({
    threadId,
    sessionId,
    generation,
    eventId,
    type,
    payload,
    now = new Date().toISOString(),
  }) {
    const { thread, session } = this.#currentSession(threadId, sessionId, generation);
    requiredString(eventId, "event id", 128);
    if (!publicEventTypes.has(type) || type === "user_message") throw new Error("unsupported public session event type");
    const event = this.#appendEvent(thread, {
      eventId,
      sessionId,
      actorSubject: session.actorSubject,
      type,
      payload: boundedPayload(payload),
      createdAt: now,
    });
    session.updatedAt = now;
    thread.updatedAt = now;
    return clone(event);
  }

  renewSessionLease({ threadId, sessionId, generation, now = new Date().toISOString(), leaseTtlMs = 60_000 }) {
    const { session } = this.#currentSession(threadId, sessionId, generation);
    session.leaseExpiresAt = new Date(Date.parse(now) + leaseTtlMs).toISOString();
    session.updatedAt = now;
    return clone(session);
  }

  finalizeSession({ threadId, sessionId, generation, status = "completed", now = new Date().toISOString() }) {
    if (!terminalSessionStates.has(status)) throw new Error("invalid terminal session status");
    const { thread, session } = this.#currentSession(threadId, sessionId, generation);
    if (session.status !== "finishing" && status === "completed") throw conflict("completed session must be finishing");
    session.status = status;
    session.completedAt = now;
    session.updatedAt = now;
    let activatedSession = null;
    if (thread.queuedSessionId) {
      activatedSession = this.sessions.get(thread.queuedSessionId);
      activatedSession.leaseGeneration = thread.leaseGeneration + 1;
      activatedSession.leaseExpiresAt = new Date(Date.parse(now) + 60_000).toISOString();
      activatedSession.updatedAt = now;
      thread.leaseGeneration = activatedSession.leaseGeneration;
      thread.activeSessionId = activatedSession.id;
      thread.queuedSessionId = null;
      thread.state = "starting";
    } else {
      thread.activeSessionId = null;
      thread.state = status === "completed" ? "idle" : "interrupted";
    }
    thread.updatedAt = now;
    return clone({ session, activatedSession });
  }

  finalizeSessionWithReview({
    threadId,
    sessionId,
    generation,
    reviewId,
    question,
    sourceEventId,
    repairPaths,
    effects,
    now = new Date().toISOString(),
  }) {
    requiredString(reviewId, "review id", 128);
    const boundedQuestion = requiredString(question, "review question", 2_000);
    const boundedEffects = boundedRepairEffects(effects);
    const boundedPaths = boundedRepairPaths(repairPaths, boundedEffects.includes("source-write"));
    requiredString(sourceEventId, "review source event id", 128);
    if (this.reviews.has(reviewId)) throw conflict("review already exists");
    const finalized = this.finalizeSession({ threadId, sessionId, generation, now });
    if (finalized.activatedSession) throw conflict("human review cannot activate queued input");
    const thread = this.threads.get(threadId);
    const review = {
      id: reviewId,
      threadId,
      ownerSubject: thread.ownerSubject,
      sourceSessionId: sessionId,
      sourceEventId,
      question: boundedQuestion,
      questionSha256: `sha256:${crypto.createHash("sha256").update(boundedQuestion).digest("hex")}`,
      repairPaths: boundedPaths,
      effects: boundedEffects,
      status: "pending",
      requestedAt: now,
      decidedAt: null,
      decidedBy: null,
      decision: null,
      decisionEventId: null,
    };
    this.reviews.set(review.id, review);
    thread.pendingReviewId = review.id;
    thread.state = "review_required";
    thread.updatedAt = now;
    return clone({ ...finalized, review, thread });
  }

  listReviewsForActor({ actor, status = "pending" }) {
    requiredString(actor, "actor", 256);
    const allowed = new Set(["pending", "approved", "rejected", "all"]);
    if (!allowed.has(status)) throw new Error("invalid review status");
    return [...this.reviews.values()]
      .filter((review) => review.ownerSubject === actor && (status === "all" || review.status === status))
      .sort((left, right) => right.requestedAt.localeCompare(left.requestedAt))
      .map(clone);
  }

  decideReviewAndRoute({
    reviewId,
    actor,
    decision,
    decisionId,
    messageId,
    newSessionId,
    now = new Date().toISOString(),
    leaseTtlMs = 60_000,
  }) {
    requiredString(reviewId, "review id", 128);
    requiredString(actor, "actor", 256);
    requiredString(decisionId, "decision id", 128);
    if (!new Set(["approve", "reject"]).has(decision)) throw new Error("decision must be approve or reject");
    const key = `review:${reviewId}:${actor}:${decisionId}`;
    const existing = this.idempotency.get(key);
    if (existing) return clone(existing);
    const review = this.reviews.get(reviewId);
    if (!review || review.ownerSubject !== actor) throw notFound();
    if (review.status !== "pending") throw conflict(`review is already ${review.status}`);
    const thread = this.#authorizedThread(review.threadId, actor);
    if (thread.pendingReviewId !== review.id || thread.activeSessionId) throw conflict("review is not the active thread boundary");

    let approvedEffects = null;
    let approvedPaths = null;
    if (decision === "approve") {
      requiredString(messageId, "message id", 128);
      requiredString(newSessionId, "new session id", 63);
      if (this.sessions.has(newSessionId)) throw conflict("session already exists");
      approvedEffects = boundedRepairEffects(review.effects);
      approvedPaths = boundedRepairPaths(
        review.repairPaths,
        approvedEffects.includes("source-write"),
      );
    }
    review.status = decision === "approve" ? "approved" : "rejected";
    review.decision = decision;
    review.decidedAt = now;
    review.decidedBy = actor;
    review.decisionEventId = decisionId;
    const resolvedEvent = this.#appendEvent(thread, {
      eventId: decisionId,
      sessionId: review.sourceSessionId,
      actorSubject: actor,
      type: "review_resolved",
      payload: { reviewId: review.id, decision, questionSha256: review.questionSha256 },
      createdAt: now,
    });
    thread.pendingReviewId = null;
    thread.updatedAt = now;

    let event = null;
    let session = null;
    if (decision === "approve") {
      const approvalText = [
        `I approve repair review ${review.id} (${review.questionSha256}).`,
        "Continue this durable thread in a fresh execution session, limited to the exact reviewed request:",
        review.question,
      ].join("\n");
      session = {
        id: newSessionId,
        threadId: thread.id,
        ordinal: [...this.sessions.values()].filter((candidate) => candidate.threadId === thread.id).length + 1,
        actorSubject: actor,
        authorityScope: "approved-repair",
        mutationGrant: {
          kind: "review-approved-repair",
          effects: approvedEffects,
          repository: thread.repository,
          paths: approvedPaths,
          reviewId: review.id,
          sourceSessionId: review.sourceSessionId,
          sourceEventId: review.sourceEventId,
          questionSha256: review.questionSha256,
          grantedToSessionId: newSessionId,
          approvedBy: actor,
          approvedAt: now,
        },
        triggerMessageId: messageId,
        status: "queued",
        leaseGeneration: thread.leaseGeneration + 1,
        leaseExpiresAt: new Date(Date.parse(now) + leaseTtlMs).toISOString(),
        inboxHeadSequence: 0,
        inboxAckSequence: 0,
        contextHeadSequence: thread.headSequence,
        createdAt: now,
        updatedAt: now,
      };
      this.sessions.set(session.id, session);
      thread.activeSessionId = session.id;
      thread.leaseGeneration = session.leaseGeneration;
      thread.state = "starting";
      thread.continuationBlocked = false;
      event = this.#appendEvent(thread, {
        eventId: messageId,
        sessionId: session.id,
        actorSubject: actor,
        type: "user_message",
        payload: { text: approvalText, reviewId: review.id, questionSha256: review.questionSha256 },
        createdAt: now,
      });
      session.inboxHeadSequence = event.sequence;
    } else {
      thread.state = "review_rejected";
      thread.continuationBlocked = true;
    }
    thread.updatedAt = now;
    const result = {
      review: clone(review), resolvedEvent: clone(resolvedEvent), event: clone(event),
      session: clone(session), thread: clone(thread),
    };
    this.idempotency.set(key, result);
    return clone(result);
  }

  publishCheckpoint({ threadId, sessionId, generation, throughSequence, content, sourceDigest, now = new Date().toISOString() }) {
    const { thread } = this.#currentSession(threadId, sessionId, generation);
    if (!Number.isSafeInteger(throughSequence) || throughSequence < 0 || throughSequence > thread.headSequence) {
      throw new Error("invalid checkpoint sequence");
    }
    const checkpoint = {
      threadId,
      sessionId,
      throughSequence,
      content: requiredString(content, "checkpoint", 64 * 1024),
      sourceDigest: requiredString(sourceDigest, "source digest", 128),
      createdAt: now,
    };
    this.checkpoints.set(threadId, checkpoint);
    return clone(checkpoint);
  }

  registerArtifact({ threadId, sessionId, generation, artifact, now = new Date().toISOString() }) {
    this.#currentSession(threadId, sessionId, generation);
    const entry = {
      artifactId: requiredString(artifact?.artifactId, "artifact id", 128),
      threadId,
      sourceSessionId: sessionId,
      contentDigest: requiredString(artifact?.contentDigest, "artifact digest", 128),
      size: Number(artifact?.size),
      contentType: requiredString(artifact?.contentType, "artifact content type", 128),
      classification: requiredString(artifact?.classification, "artifact classification", 64),
      storageReference: requiredString(artifact?.storageReference, "artifact storage reference", 1024),
      redactionStatus: requiredString(artifact?.redactionStatus, "artifact redaction status", 64),
      createdAt: now,
    };
    if (!Number.isSafeInteger(entry.size) || entry.size < 0) throw new Error("invalid artifact size");
    const entries = this.artifacts.get(threadId);
    if (entries.some((candidate) => candidate.artifactId === entry.artifactId)) throw conflict("artifact already exists");
    entries.push(entry);
    return clone(entry);
  }

  readEventsAfter({ threadId, actor, afterSequence = 0, limit = 200 }) {
    this.#authorizedThread(threadId, actor);
    const boundedLimit = Math.min(Math.max(Number(limit) || 1, 1), 500);
    return this.events.get(threadId)
      .filter((event) => event.sequence > afterSequence)
      .slice(0, boundedLimit)
      .map(clone);
  }

  listSessionsForActor({ threadId, actor }) {
    this.#authorizedThread(threadId, actor);
    return [...this.sessions.values()]
      .filter((session) => session.threadId === threadId)
      .sort((left, right) => left.ordinal - right.ordinal)
      .map(clone);
  }

  contextEnvelope({ threadId, actor, sessionId, recentLimit = 40 }) {
    const thread = this.#authorizedThread(threadId, actor);
    const session = this.sessions.get(sessionId);
    if (!session || session.threadId !== threadId) throw notFound();
    const checkpoint = this.checkpoints.get(threadId) || null;
    const afterSequence = checkpoint?.throughSequence || 0;
    const recentEvents = this.events.get(threadId).filter((event) => event.sequence > afterSequence).slice(-recentLimit);
    return clone({
      threadId,
      sessionId,
      authorityScope: session.authorityScope || "human",
      mutationGrant: session.mutationGrant || null,
      throughSequence: thread.headSequence,
      checkpoint,
      recentEvents,
      artifacts: this.artifacts.get(threadId),
    });
  }

  #authorizedThread(threadId, actor) {
    const thread = this.threads.get(threadId);
    if (!thread || thread.ownerSubject !== actor) throw notFound();
    return thread;
  }

  #currentSession(threadId, sessionId, generation) {
    const thread = this.threads.get(threadId);
    const session = this.sessions.get(sessionId);
    if (!thread || !session || session.threadId !== threadId || thread.activeSessionId !== sessionId || session.leaseGeneration !== generation) {
      throw conflict("stale session fence");
    }
    return { thread, session };
  }

  #appendEvent(thread, value) {
    const events = this.events.get(thread.id);
    if (events.some((event) => event.eventId === value.eventId)) throw conflict("event already exists");
    const event = {
      threadId: thread.id,
      sequence: thread.headSequence + 1,
      eventId: value.eventId,
      sessionId: value.sessionId,
      actorSubject: value.actorSubject,
      type: value.type,
      payload: boundedPayload(value.payload),
      createdAt: value.createdAt,
    };
    thread.headSequence = event.sequence;
    events.push(event);
    return event;
  }
}

const mutatingMethods = new Set([
  "createThread",
  "createExternalThreadAndRoute",
  "appendUserMessageAndRoute",
  "markSessionRunning",
  "acknowledgeInbox",
  "markSessionFinishing",
  "appendFencedSessionEvent",
  "renewSessionLease",
  "finalizeSession",
  "finalizeSessionWithReview",
  "decideReviewAndRoute",
  "publishCheckpoint",
  "registerArtifact",
]);

async function fileThreadStore(filePath) {
  if (!filePath) throw new Error("file thread store requires a path");
  let snapshot = null;
  try {
    snapshot = JSON.parse(await fs.readFile(filePath, "utf8"));
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
  }
  const store = new InMemoryThreadStore(snapshot);
  let operations = Promise.resolve();
  const persist = async (nextSnapshot) => {
    const encoded = JSON.stringify(nextSnapshot) + "\n";
    await fs.mkdir(path.dirname(filePath), { recursive: true, mode: 0o700 });
    const temporary = `${filePath}.tmp-${process.pid}`;
    await fs.writeFile(temporary, encoded, { mode: 0o600 });
    await fs.rename(temporary, filePath);
  };
  return new Proxy(store, {
    get(target, property) {
      const value = Reflect.get(target, property, target);
      if (typeof value !== "function") return value;
      if (!mutatingMethods.has(property)) {
        return async (...args) => {
          await operations;
          return value.apply(target, args);
        };
      }
      return async (...args) => {
        const operation = operations.then(async () => {
          const candidate = new InMemoryThreadStore(target.snapshot());
          const result = value.apply(candidate, args);
          const nextSnapshot = candidate.snapshot();
          await persist(nextSnapshot);
          target.restoreSnapshot(nextSnapshot);
          return result;
        });
        operations = operation.then(() => undefined, () => undefined);
        return operation;
      };
    },
  });
}

export async function createThreadStore({
  backend = process.env.MULTIAGENT_THREAD_STORE_BACKEND || "memory",
  filePath = process.env.MULTIAGENT_THREAD_STORE_FILE,
} = {}) {
  if (backend === "memory") return new InMemoryThreadStore();
  if (backend === "file") return fileThreadStore(filePath);
  throw new Error(`unsupported thread store backend: ${backend}`);
}
