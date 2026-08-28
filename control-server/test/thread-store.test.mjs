import assert from "node:assert/strict";
import test from "node:test";
import { generateThreadId, InMemoryThreadStore } from "../src/thread-store.mjs";

const now = "2026-08-27T00:00:00.000Z";

function storeWithThread() {
  const store = new InMemoryThreadStore();
  store.createThread({ id: "thread-1", ownerSubject: "user-a", repository: "multiagent", title: "Long task", now });
  return store;
}

test("control server thread IDs are valid and independently generated", () => {
  const first = generateThreadId({ now: 1_777_777_777_777, randomBytes: () => Buffer.from("0000000001", "hex") });
  const second = generateThreadId({ now: 1_777_777_777_777, randomBytes: () => Buffer.from("0000000002", "hex") });
  assert.match(first, /^thread-[a-z0-9]+-[a-f0-9]{10}$/);
  assert.notEqual(first, second);
});

test("thread ownership scopes list, history, and direct lookup", () => {
  const store = storeWithThread();
  assert.equal(store.listThreadsForActor("user-a").length, 1);
  assert.equal(store.listThreadsForActor("user-b").length, 0);
  assert.throws(() => store.getThreadForActor("thread-1", "user-b"), (error) => error.statusCode === 404);
  assert.throws(() => store.readEventsAfter({ threadId: "thread-1", actor: "user-b" }), (error) => error.statusCode === 404);
});

test("messages are idempotent and route across fresh execution sessions", () => {
  const store = storeWithThread();
  const first = store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-1", text: "Start", newSessionId: "session-a", now,
  });
  assert.equal(first.createdSession, true);
  assert.equal(first.session.leaseGeneration, 1);
  assert.deepEqual(store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-1", text: "Start", newSessionId: "unused", now,
  }), first);

  store.markSessionRunning({ threadId: "thread-1", sessionId: "session-a", generation: 1, now });
  store.acknowledgeInbox({ threadId: "thread-1", sessionId: "session-a", generation: 1, throughSequence: 1, now });
  const followUp = store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-2", text: "More detail", newSessionId: "unused", now,
  });
  assert.equal(followUp.createdSession, false);
  assert.equal(followUp.session.id, "session-a");
  assert.equal(followUp.session.inboxAckSequence, 1);
  assert.equal(followUp.session.inboxHeadSequence, 2);
  assert.equal(store.markSessionFinishing({ threadId: "thread-1", sessionId: "session-a", generation: 1, now }).reason, "pending_input");

  store.acknowledgeInbox({ threadId: "thread-1", sessionId: "session-a", generation: 1, throughSequence: 2, now });
  assert.equal(store.markSessionFinishing({ threadId: "thread-1", sessionId: "session-a", generation: 1, now }).finishing, true);
  const resumed = store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-3", text: "Resume tomorrow", newSessionId: "session-b", now,
  });
  assert.equal(resumed.createdSession, true);
  assert.equal(resumed.session.id, "session-b");
  assert.equal(resumed.session.leaseGeneration, null);
  const finalized = store.finalizeSession({ threadId: "thread-1", sessionId: "session-a", generation: 1, now });
  assert.equal(finalized.activatedSession.id, "session-b");
  assert.equal(finalized.activatedSession.leaseGeneration, 2);
  assert.deepEqual(store.listSessionsForActor({ threadId: "thread-1", actor: "user-a" }).map((session) => session.id), ["session-a", "session-b"]);
});

test("fences reject stale session writes without revoking issued permits", () => {
  const store = storeWithThread();
  store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-1", text: "Start", newSessionId: "session-a", now,
  });
  assert.throws(() => store.appendFencedSessionEvent({
    threadId: "thread-1", sessionId: "session-a", generation: 0, eventId: "event-1", type: "progress", payload: {}, now,
  }), /stale session fence/);
  const event = store.appendFencedSessionEvent({
    threadId: "thread-1", sessionId: "session-a", generation: 1, eventId: "event-1", type: "progress", payload: { text: "working" }, now,
  });
  assert.equal(event.sequence, 2);
  // Permit validity is intentionally outside ThreadStore. A lease fence controls
  // new thread writes and permit issuance, not an already-issued permit's expiry.
});

test("history replay, checkpoints, and artifact manifests remain thread scoped", () => {
  const store = storeWithThread();
  store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-1", text: "Start", newSessionId: "session-a", now,
  });
  store.acknowledgeInbox({ threadId: "thread-1", sessionId: "session-a", generation: 1, throughSequence: 1, now });
  store.appendFencedSessionEvent({
    threadId: "thread-1", sessionId: "session-a", generation: 1, eventId: "event-2", type: "assistant_message", payload: { text: "Result" }, now,
  });
  store.publishCheckpoint({
    threadId: "thread-1", sessionId: "session-a", generation: 1, throughSequence: 1, content: "Known context", sourceDigest: "sha256:context", now,
  });
  store.registerArtifact({
    threadId: "thread-1",
    sessionId: "session-a",
    generation: 1,
    artifact: {
      artifactId: "artifact-1",
      contentDigest: "sha256:artifact",
      size: 12,
      contentType: "text/plain",
      classification: "internal",
      storageReference: "opaque:artifact-1",
      redactionStatus: "reviewed",
    },
    now,
  });
  assert.deepEqual(store.readEventsAfter({ threadId: "thread-1", actor: "user-a", afterSequence: 1 }).map((event) => event.sequence), [2]);
  const context = store.contextEnvelope({ threadId: "thread-1", actor: "user-a", sessionId: "session-a" });
  assert.equal(context.checkpoint.throughSequence, 1);
  assert.equal(context.recentEvents[0].sequence, 2);
  assert.equal(context.artifacts[0].artifactId, "artifact-1");
});
