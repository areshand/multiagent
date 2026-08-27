import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  QueryCommand,
  TransactWriteCommand,
  UpdateCommand,
} from "@aws-sdk/lib-dynamodb";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";

const acceptingSessionStates = new Set(["queued", "starting", "running", "waiting_for_user"]);
const terminalSessionStates = new Set(["completed", "failed", "interrupted", "cancelled"]);
const publicEventTypes = new Set([
  "assistant_message", "progress", "question", "artifact_available",
  "session_started", "session_completed", "session_interrupted",
]);

const pk = (threadId) => `THREAD#${threadId}`;
const eventSk = (sequence) => `EVENT#${String(sequence).padStart(20, "0")}`;
const sessionSk = (sessionId) => `SESSION#${sessionId}`;
const idempotencySk = (actor, messageId) => `IDEMPOTENCY#${actor}#${messageId}`;
const clone = (value) => structuredClone(value);

function requiredString(value, name, max = 32768) {
  if (typeof value !== "string" || !value.trim() || value.length > max) throw new Error(`${name} must contain 1 to ${max} characters`);
  return value.trim();
}

function boundedPayload(payload) {
  const value = payload === undefined ? {} : clone(payload);
  if (Buffer.byteLength(JSON.stringify(value), "utf8") > 64 * 1024) throw new Error("event payload exceeds 64 KiB");
  return value;
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

function publicThread(item) {
  if (!item) return null;
  const { pk: ignoredPk, sk: ignoredSk, gsi1pk, gsi1sk, version, ...thread } = item;
  return thread;
}

function publicSession(item) {
  if (!item) return null;
  const { pk: ignoredPk, sk: ignoredSk, ...session } = item;
  return session;
}

function publicEvent(item) {
  if (!item) return null;
  const { pk: ignoredPk, sk: ignoredSk, ...event } = item;
  return event;
}

function isConditionalFailure(error) {
  return error?.name === "TransactionCanceledException" || error?.name === "ConditionalCheckFailedException";
}

export class DynamoThreadStore {
  constructor({ tableName, region, endpoint, client } = {}) {
    this.tableName = requiredString(tableName, "thread store table name", 255);
    this.client = client || DynamoDBDocumentClient.from(new DynamoDBClient({ region, ...(endpoint ? { endpoint } : {}) }), {
      marshallOptions: { removeUndefinedValues: true },
    });
  }

  async createThread({ id, ownerSubject, repository, title = "", now = new Date().toISOString() }) {
    requiredString(id, "thread id", 63);
    requiredString(ownerSubject, "owner subject", 256);
    requiredString(repository, "repository", 128);
    const item = {
      pk: pk(id), sk: "META", id, ownerSubject, repository,
      title: String(title || "").trim().slice(0, 256),
      state: "idle", headSequence: 0, activeSessionId: null, queuedSessionId: null,
      leaseGeneration: 0, version: 1, createdAt: now, updatedAt: now,
      gsi1pk: `OWNER#${ownerSubject}`, gsi1sk: `${now}#${id}`,
    };
    try {
      await this.client.send(new PutCommand({ TableName: this.tableName, Item: item, ConditionExpression: "attribute_not_exists(pk)" }));
    } catch (error) {
      if (isConditionalFailure(error)) throw conflict("thread already exists");
      throw error;
    }
    return publicThread(item);
  }

  async listThreadsForActor(actor) {
    const result = await this.client.send(new QueryCommand({
      TableName: this.tableName,
      IndexName: "owner-updated-index",
      KeyConditionExpression: "gsi1pk = :owner",
      ExpressionAttributeValues: { ":owner": `OWNER#${actor}` },
      ScanIndexForward: false,
    }));
    return (result.Items || []).map(publicThread);
  }

  async getThreadForActor(threadId, actor) {
    const thread = await this.#thread(threadId);
    if (!thread || thread.ownerSubject !== actor) throw notFound();
    return publicThread(thread);
  }

  async appendUserMessageAndRoute(input) {
    requiredString(input.messageId, "message id", 128);
    const text = requiredString(input.text, "message");
    const now = input.now || new Date().toISOString();
    const leaseTtlMs = input.leaseTtlMs || 60_000;
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const existing = await this.#get(input.threadId, idempotencySk(input.actor, input.messageId));
      if (existing?.result) return clone(existing.result);
      const thread = await this.#thread(input.threadId);
      if (!thread || thread.ownerSubject !== input.actor) throw notFound();
      let session = thread.activeSessionId ? await this.#session(input.threadId, thread.activeSessionId) : null;
      if (session && !acceptingSessionStates.has(session.status) && thread.queuedSessionId) {
        session = await this.#session(input.threadId, thread.queuedSessionId);
      }
      let createdSession = false;
      let activateNow = false;
      if (!session || !acceptingSessionStates.has(session.status)) {
        requiredString(input.newSessionId, "new session id", 63);
        if (await this.#session(input.threadId, input.newSessionId)) throw conflict("session already exists");
        activateNow = !thread.activeSessionId;
        session = {
          id: input.newSessionId,
          threadId: input.threadId,
          ordinal: Number(thread.sessionCount || 0) + 1,
          actorSubject: input.actor,
          triggerMessageId: input.messageId,
          status: "queued",
          leaseGeneration: activateNow ? thread.leaseGeneration + 1 : null,
          leaseExpiresAt: activateNow ? new Date(Date.parse(now) + leaseTtlMs).toISOString() : null,
          inboxHeadSequence: 0,
          inboxAckSequence: 0,
          contextHeadSequence: thread.headSequence,
          createdAt: now,
          updatedAt: now,
        };
        createdSession = true;
      }
      const sequence = thread.headSequence + 1;
      const event = {
        threadId: input.threadId, sequence, eventId: input.messageId, sessionId: session.id,
        actorSubject: input.actor, type: "user_message", payload: { text }, createdAt: now,
      };
      session.inboxHeadSequence = sequence;
      session.updatedAt = now;
      const result = { event, session: publicSession(session), createdSession };
      const names = { "#version": "version", "#head": "headSequence", "#updated": "updatedAt", "#gsi": "gsi1sk" };
      const values = {
        ":version": thread.version, ":nextVersion": thread.version + 1, ":head": sequence,
        ":updated": now, ":gsi": `${now}#${thread.id}`,
      };
      let update = "SET #version = :nextVersion, #head = :head, #updated = :updated, #gsi = :gsi";
      if (createdSession) {
        names["#count"] = "sessionCount";
        values[":count"] = session.ordinal;
        update += ", #count = :count";
        if (activateNow) {
          Object.assign(names, { "#active": "activeSessionId", "#lease": "leaseGeneration", "#state": "state" });
          Object.assign(values, { ":active": session.id, ":lease": session.leaseGeneration, ":state": "starting" });
          update += ", #active = :active, #lease = :lease, #state = :state";
        } else {
          names["#queued"] = "queuedSessionId";
          values[":queued"] = session.id;
          update += ", #queued = :queued";
        }
      }
      const sessionWrite = createdSession
        ? { Put: { TableName: this.tableName, Item: { pk: pk(input.threadId), sk: sessionSk(session.id), ...session }, ConditionExpression: "attribute_not_exists(sk)" } }
        : { Update: {
          TableName: this.tableName,
          Key: { pk: pk(input.threadId), sk: sessionSk(session.id) },
          UpdateExpression: "SET inboxHeadSequence = :head, updatedAt = :updated",
          ExpressionAttributeValues: { ":head": sequence, ":updated": now },
        } };
      try {
        await this.client.send(new TransactWriteCommand({ TransactItems: [
          { Update: {
            TableName: this.tableName, Key: { pk: pk(input.threadId), sk: "META" },
            ConditionExpression: "#version = :version", UpdateExpression: update,
            ExpressionAttributeNames: names, ExpressionAttributeValues: values,
          } },
          { Put: { TableName: this.tableName, Item: { pk: pk(input.threadId), sk: eventSk(sequence), ...event }, ConditionExpression: "attribute_not_exists(sk)" } },
          { Put: { TableName: this.tableName, Item: {
            pk: pk(input.threadId), sk: idempotencySk(input.actor, input.messageId), result,
          }, ConditionExpression: "attribute_not_exists(sk)" } },
          sessionWrite,
        ] }));
        return clone(result);
      } catch (error) {
        if (!isConditionalFailure(error)) throw error;
      }
    }
    throw conflict("thread was modified concurrently; retry the message");
  }

  async markSessionRunning({ threadId, sessionId, generation, now = new Date().toISOString() }) {
    const { thread, session } = await this.#currentSession(threadId, sessionId, generation);
    if (!["queued", "starting"].includes(session.status)) throw conflict("session cannot enter running state");
    await this.client.send(new TransactWriteCommand({ TransactItems: [
      { Update: {
        TableName: this.tableName, Key: { pk: pk(threadId), sk: "META" },
        ConditionExpression: "activeSessionId = :session AND leaseGeneration = :generation",
        UpdateExpression: "SET #state = :state, updatedAt = :now",
        ExpressionAttributeNames: { "#state": "state" },
        ExpressionAttributeValues: { ":session": sessionId, ":generation": generation, ":state": "running", ":now": now },
      } },
      { Update: {
        TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(sessionId) },
        ConditionExpression: "leaseGeneration = :generation",
        UpdateExpression: "SET #status = :status, updatedAt = :now",
        ExpressionAttributeNames: { "#status": "status" },
        ExpressionAttributeValues: { ":generation": generation, ":status": "running", ":now": now },
      } },
    ] }));
    return { ...publicSession(session), status: "running", updatedAt: now };
  }

  async acknowledgeInbox({ threadId, sessionId, generation, throughSequence, now = new Date().toISOString() }) {
    const { session } = await this.#currentSession(threadId, sessionId, generation);
    if (!Number.isSafeInteger(throughSequence) || throughSequence < session.inboxAckSequence || throughSequence > session.inboxHeadSequence) {
      throw new Error("invalid inbox acknowledgement sequence");
    }
    await this.client.send(new UpdateCommand({
      TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(sessionId) },
      ConditionExpression: "leaseGeneration = :generation",
      UpdateExpression: "SET inboxAckSequence = :through, updatedAt = :now",
      ExpressionAttributeValues: { ":generation": generation, ":through": throughSequence, ":now": now },
    }));
    return { ...publicSession(session), inboxAckSequence: throughSequence, updatedAt: now };
  }

  async markSessionFinishing({ threadId, sessionId, generation, now = new Date().toISOString() }) {
    const { session } = await this.#currentSession(threadId, sessionId, generation);
    if (session.inboxAckSequence !== session.inboxHeadSequence) return { finishing: false, reason: "pending_input", session: publicSession(session) };
    if (!["running", "waiting_for_user"].includes(session.status)) throw conflict("session cannot enter finishing state");
    await this.client.send(new UpdateCommand({
      TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(sessionId) },
      ConditionExpression: "leaseGeneration = :generation AND inboxAckSequence = inboxHeadSequence",
      UpdateExpression: "SET #status = :status, updatedAt = :now",
      ExpressionAttributeNames: { "#status": "status" },
      ExpressionAttributeValues: { ":generation": generation, ":status": "finishing", ":now": now },
    }));
    return { finishing: true, session: { ...publicSession(session), status: "finishing", updatedAt: now } };
  }

  async appendFencedSessionEvent({ threadId, sessionId, generation, eventId, type, payload, now = new Date().toISOString() }) {
    requiredString(eventId, "event id", 128);
    if (!publicEventTypes.has(type)) throw new Error("unsupported public session event type");
    const value = boundedPayload(payload);
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const { thread, session } = await this.#currentSession(threadId, sessionId, generation);
      const sequence = thread.headSequence + 1;
      const event = { threadId, sequence, eventId, sessionId, actorSubject: session.actorSubject, type, payload: value, createdAt: now };
      try {
        await this.client.send(new TransactWriteCommand({ TransactItems: [
          { Update: {
            TableName: this.tableName, Key: { pk: pk(threadId), sk: "META" },
            ConditionExpression: "#version = :version AND activeSessionId = :session AND leaseGeneration = :generation",
            UpdateExpression: "SET #version = :nextVersion, headSequence = :head, updatedAt = :now, gsi1sk = :gsi",
            ExpressionAttributeNames: { "#version": "version" },
            ExpressionAttributeValues: {
              ":version": thread.version, ":nextVersion": thread.version + 1, ":session": sessionId,
              ":generation": generation, ":head": sequence, ":now": now, ":gsi": `${now}#${threadId}`,
            },
          } },
          { Put: { TableName: this.tableName, Item: { pk: pk(threadId), sk: eventSk(sequence), ...event }, ConditionExpression: "attribute_not_exists(sk)" } },
        ] }));
        return event;
      } catch (error) {
        if (!isConditionalFailure(error)) throw error;
      }
    }
    throw conflict("thread was modified concurrently; retry the event");
  }

  async renewSessionLease({ threadId, sessionId, generation, now = new Date().toISOString(), leaseTtlMs = 60_000 }) {
    const expiresAt = new Date(Date.parse(now) + leaseTtlMs).toISOString();
    try {
      const result = await this.client.send(new UpdateCommand({
        TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(sessionId) },
        ConditionExpression: "leaseGeneration = :generation",
        UpdateExpression: "SET leaseExpiresAt = :expires, updatedAt = :now",
        ExpressionAttributeValues: { ":generation": generation, ":expires": expiresAt, ":now": now },
        ReturnValues: "ALL_NEW",
      }));
      return publicSession(result.Attributes);
    } catch (error) {
      if (isConditionalFailure(error)) throw conflict("stale session fence");
      throw error;
    }
  }

  async finalizeSession({ threadId, sessionId, generation, status = "completed", now = new Date().toISOString() }) {
    if (!terminalSessionStates.has(status)) throw new Error("invalid terminal session status");
    const { thread, session } = await this.#currentSession(threadId, sessionId, generation);
    if (status === "completed" && session.status !== "finishing") throw conflict("completed session must be finishing");
    const queued = thread.queuedSessionId ? await this.#session(threadId, thread.queuedSessionId) : null;
    const nextGeneration = queued ? thread.leaseGeneration + 1 : null;
    const metaNames = { "#state": "state", "#active": "activeSessionId", "#queued": "queuedSessionId", "#lease": "leaseGeneration" };
    const metaValues = {
      ":session": sessionId, ":generation": generation, ":now": now,
      ":state": queued ? "starting" : status === "completed" ? "idle" : "interrupted",
      ":active": queued?.id || null, ":queued": null, ":lease": nextGeneration || generation,
    };
    const writes = [
      { Update: {
        TableName: this.tableName, Key: { pk: pk(threadId), sk: "META" },
        ConditionExpression: "activeSessionId = :session AND leaseGeneration = :generation",
        UpdateExpression: "SET #state = :state, #active = :active, #queued = :queued, #lease = :lease, updatedAt = :now",
        ExpressionAttributeNames: metaNames, ExpressionAttributeValues: metaValues,
      } },
      { Update: {
        TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(sessionId) },
        ConditionExpression: "leaseGeneration = :generation",
        UpdateExpression: "SET #status = :status, completedAt = :now, updatedAt = :now",
        ExpressionAttributeNames: { "#status": "status" },
        ExpressionAttributeValues: { ":generation": generation, ":status": status, ":now": now },
      } },
    ];
    let activatedSession = null;
    if (queued) {
      const expiresAt = new Date(Date.parse(now) + 60_000).toISOString();
      writes.push({ Update: {
        TableName: this.tableName, Key: { pk: pk(threadId), sk: sessionSk(queued.id) },
        ConditionExpression: "attribute_not_exists(leaseGeneration) OR leaseGeneration = :none",
        UpdateExpression: "SET leaseGeneration = :lease, leaseExpiresAt = :expires, updatedAt = :now",
        ExpressionAttributeValues: { ":none": null, ":lease": nextGeneration, ":expires": expiresAt, ":now": now },
      } });
      activatedSession = { ...publicSession(queued), leaseGeneration: nextGeneration, leaseExpiresAt: expiresAt, updatedAt: now };
    }
    try {
      await this.client.send(new TransactWriteCommand({ TransactItems: writes }));
    } catch (error) {
      if (isConditionalFailure(error)) throw conflict("stale session fence");
      throw error;
    }
    return { session: { ...publicSession(session), status, completedAt: now, updatedAt: now }, activatedSession };
  }

  async publishCheckpoint({ threadId, sessionId, generation, throughSequence, content, sourceDigest, now = new Date().toISOString() }) {
    const { thread } = await this.#currentSession(threadId, sessionId, generation);
    if (!Number.isSafeInteger(throughSequence) || throughSequence < 0 || throughSequence > thread.headSequence) throw new Error("invalid checkpoint sequence");
    const checkpoint = {
      threadId, sessionId, throughSequence, content: requiredString(content, "checkpoint", 64 * 1024),
      sourceDigest: requiredString(sourceDigest, "source digest", 128), createdAt: now,
    };
    await this.client.send(new PutCommand({ TableName: this.tableName, Item: { pk: pk(threadId), sk: "CHECKPOINT#LATEST", ...checkpoint } }));
    return checkpoint;
  }

  async registerArtifact({ threadId, sessionId, generation, artifact, now = new Date().toISOString() }) {
    await this.#currentSession(threadId, sessionId, generation);
    const entry = {
      artifactId: requiredString(artifact?.artifactId, "artifact id", 128), threadId, sourceSessionId: sessionId,
      contentDigest: requiredString(artifact?.contentDigest, "artifact digest", 128), size: Number(artifact?.size),
      contentType: requiredString(artifact?.contentType, "artifact content type", 128),
      classification: requiredString(artifact?.classification, "artifact classification", 64),
      storageReference: requiredString(artifact?.storageReference, "artifact storage reference", 1024),
      redactionStatus: requiredString(artifact?.redactionStatus, "artifact redaction status", 64), createdAt: now,
    };
    if (!Number.isSafeInteger(entry.size) || entry.size < 0) throw new Error("invalid artifact size");
    try {
      await this.client.send(new PutCommand({
        TableName: this.tableName, Item: { pk: pk(threadId), sk: `ARTIFACT#${entry.artifactId}`, ...entry },
        ConditionExpression: "attribute_not_exists(sk)",
      }));
    } catch (error) {
      if (isConditionalFailure(error)) throw conflict("artifact already exists");
      throw error;
    }
    return entry;
  }

  async readEventsAfter({ threadId, actor, afterSequence = 0, limit = 200 }) {
    await this.getThreadForActor(threadId, actor);
    const boundedLimit = Math.min(Math.max(Number(limit) || 1, 1), 500);
    const result = await this.client.send(new QueryCommand({
      TableName: this.tableName,
      KeyConditionExpression: "pk = :pk AND sk BETWEEN :after AND :end",
      ExpressionAttributeValues: { ":pk": pk(threadId), ":after": eventSk(Number(afterSequence) + 1), ":end": "EVENT#~" },
      Limit: boundedLimit,
    }));
    return (result.Items || []).map(publicEvent);
  }

  async listSessionsForActor({ threadId, actor }) {
    await this.getThreadForActor(threadId, actor);
    const result = await this.client.send(new QueryCommand({
      TableName: this.tableName,
      KeyConditionExpression: "pk = :pk AND begins_with(sk, :prefix)",
      ExpressionAttributeValues: { ":pk": pk(threadId), ":prefix": "SESSION#" },
    }));
    return (result.Items || []).map(publicSession).sort((left, right) => left.ordinal - right.ordinal);
  }

  async contextEnvelope({ threadId, actor, sessionId, recentLimit = 40 }) {
    const thread = await this.getThreadForActor(threadId, actor);
    const session = await this.#session(threadId, sessionId);
    if (!session) throw notFound();
    const checkpoint = await this.#get(threadId, "CHECKPOINT#LATEST");
    const afterSequence = checkpoint?.throughSequence || 0;
    const events = await this.client.send(new QueryCommand({
      TableName: this.tableName,
      KeyConditionExpression: "pk = :pk AND sk BETWEEN :after AND :end",
      ExpressionAttributeValues: { ":pk": pk(threadId), ":after": eventSk(afterSequence + 1), ":end": "EVENT#~" },
      ScanIndexForward: false,
      Limit: recentLimit,
    }));
    const artifacts = await this.client.send(new QueryCommand({
      TableName: this.tableName,
      KeyConditionExpression: "pk = :pk AND begins_with(sk, :prefix)",
      ExpressionAttributeValues: { ":pk": pk(threadId), ":prefix": "ARTIFACT#" },
    }));
    return {
      threadId, sessionId, throughSequence: thread.headSequence,
      checkpoint: checkpoint ? publicEvent(checkpoint) : null,
      recentEvents: (events.Items || []).reverse().map(publicEvent),
      artifacts: (artifacts.Items || []).map(publicEvent),
    };
  }

  async deleteThreadForTests(threadId) {
    const result = await this.client.send(new QueryCommand({
      TableName: this.tableName, KeyConditionExpression: "pk = :pk", ExpressionAttributeValues: { ":pk": pk(threadId) },
    }));
    await Promise.all((result.Items || []).map((item) => this.client.send(new DeleteCommand({ TableName: this.tableName, Key: { pk: item.pk, sk: item.sk } }))));
  }

  async #get(threadId, sk) {
    return (await this.client.send(new GetCommand({ TableName: this.tableName, Key: { pk: pk(threadId), sk }, ConsistentRead: true }))).Item || null;
  }

  #thread(threadId) { return this.#get(threadId, "META"); }
  #session(threadId, sessionId) { return this.#get(threadId, sessionSk(sessionId)); }

  async #currentSession(threadId, sessionId, generation) {
    const [thread, session] = await Promise.all([this.#thread(threadId), this.#session(threadId, sessionId)]);
    if (!thread || !session || thread.activeSessionId !== sessionId || session.leaseGeneration !== generation) throw conflict("stale session fence");
    return { thread, session };
  }
}
