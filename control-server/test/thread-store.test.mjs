import assert from "node:assert/strict";
import test from "node:test";
import { generateThreadId, InMemoryThreadStore } from "../../thread/src/thread-model.mjs";

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

test("messages are idempotent and route across fresh Sessions", () => {
  const store = storeWithThread();
  const first = store.appendUserMessageAndRoute({
    threadId: "thread-1", actor: "user-a", messageId: "message-1", text: "Start", newSessionId: "session-a", now,
  });
  assert.equal(first.createdSession, true);
  assert.equal(first.session.authorityScope, "user");
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

function storeAtPendingSlackReview({ repairPaths = ["deploy/service.yaml"], effects = ["source-write", "reviewed-ops"] } = {}) {
  const store = new InMemoryThreadStore();
  const routed = store.createExternalThreadAndRoute({
    id: "thread-slack",
    ownerSubject: "production-e2e",
    repository: "multiagent",
    title: "Slack alert",
    sourceActor: "integration:slack:T123",
    source: { type: "slack", eventId: "Ev123", workspaceId: "T123", channelId: "C456", messageTs: "1.2" },
    eventId: "slack-message-1",
    text: "Diagnose alert",
    newSessionId: "session-diagnose",
    now,
  });
  store.markSessionRunning({ threadId: routed.thread.id, sessionId: routed.session.id, generation: 1, now });
  store.acknowledgeInbox({ threadId: routed.thread.id, sessionId: routed.session.id, generation: 1, throughSequence: 1, now });
  store.appendFencedSessionEvent({
    threadId: routed.thread.id,
    sessionId: routed.session.id,
    generation: 1,
    eventId: "final-session-diagnose",
    type: "question",
    payload: { text: "Approve restarting service api in testnet?" },
    now,
  });
  store.markSessionFinishing({ threadId: routed.thread.id, sessionId: routed.session.id, generation: 1, now });
  store.finalizeSessionWithReview({
    threadId: routed.thread.id,
    sessionId: routed.session.id,
    generation: 1,
    reviewId: "review-session-diagnose",
    question: "Approve restarting service api in testnet?",
    sourceEventId: "final-session-diagnose",
    repairPaths,
    effects,
    now,
  });
  return store;
}

test("Slack events create idempotent observe-only threads owned by the human reviewer", () => {
  const store = new InMemoryThreadStore();
  const input = {
    id: "thread-slack",
    ownerSubject: "production-e2e",
    repository: "multiagent",
    title: "Slack alert",
    sourceActor: "integration:slack:T123",
    source: { type: "slack", eventId: "Ev123", workspaceId: "T123", channelId: "C456", messageTs: "1.2" },
    eventId: "slack-message-1",
    text: "Diagnose alert",
    newSessionId: "session-diagnose",
    now,
  };
  const first = store.createExternalThreadAndRoute(input);
  const duplicate = store.createExternalThreadAndRoute({ ...input, id: "unused", newSessionId: "unused" });
  assert.equal(first.duplicate, false);
  assert.equal(duplicate.duplicate, true);
  assert.equal(duplicate.thread.id, "thread-slack");
  assert.equal(first.session.actorSubject, "integration:slack:T123");
  assert.equal(first.session.authorityScope, "observe");
  assert.equal(store.getThreadForActor("thread-slack", "production-e2e").source.eventId, "Ev123");
  assert.throws(() => store.getThreadForActor("thread-slack", "integration:slack:T123"), /thread not found/);
});

test("approving a repair review creates a fresh path-bound repair Session", () => {
  const store = storeAtPendingSlackReview();
  const reviews = store.listReviewsForActor({ actor: "production-e2e" });
  assert.equal(reviews.length, 1);
  assert.equal(store.getThreadForActor("thread-slack", "production-e2e").state, "review_required");
  assert.throws(() => store.appendUserMessageAndRoute({
    threadId: "thread-slack", actor: "production-e2e", messageId: "bypass", text: "continue", newSessionId: "bypass",
  }), /resolve pending review/);

  const approved = store.decideReviewAndRoute({
    reviewId: reviews[0].id,
    actor: "production-e2e",
    decision: "approve",
    decisionId: "decision-approve",
    messageId: "message-approve",
    newSessionId: "session-repair",
    now,
  });
  assert.equal(approved.review.status, "approved");
  assert.equal(approved.session.id, "session-repair");
  assert.equal(approved.session.ordinal, 2);
  assert.equal(approved.session.actorSubject, "production-e2e");
  assert.equal(approved.session.authorityScope, "user");
  assert.deepEqual(approved.session.mutationGrant.paths, ["deploy/service.yaml"]);
  assert.deepEqual(approved.session.mutationGrant.effects, ["source-write", "reviewed-ops"]);
  assert.equal(approved.session.mutationGrant.grantedToSessionId, "session-repair");
  assert.match(approved.event.payload.text, /exact reviewed request/);
  assert.match(approved.event.payload.text, /Approve restarting service api in testnet\?/);
  assert.equal(approved.thread.state, "starting");
});

test("an operations-only approval grants reviewed ops without workspace writes", () => {
  const store = storeAtPendingSlackReview({ repairPaths: [], effects: ["reviewed-ops"] });
  const approved = store.decideReviewAndRoute({
    reviewId: "review-session-diagnose",
    actor: "production-e2e",
    decision: "approve",
    decisionId: "decision-ops-approve",
    messageId: "message-ops-approve",
    newSessionId: "session-ops-repair",
    now,
  });
  assert.deepEqual(approved.session.mutationGrant.paths, []);
  assert.deepEqual(approved.session.mutationGrant.effects, ["reviewed-ops"]);
  assert.equal(approved.session.authorityScope, "user");
  assert.throws(
    () => storeAtPendingSlackReview({ repairPaths: [], effects: ["source-write"] }),
    /repair paths/);
});


test("approval fails atomically when a restored review grant is invalid", () => {
  const snapshot = storeAtPendingSlackReview().snapshot();
  snapshot.reviews[0][1].effects = ["admin"];
  const store = new InMemoryThreadStore(snapshot);
  assert.throws(() => store.decideReviewAndRoute({
    reviewId: "review-session-diagnose",
    actor: "production-e2e",
    decision: "approve",
    decisionId: "decision-invalid-approve",
    messageId: "message-invalid-approve",
    newSessionId: "session-invalid-repair",
    now,
  }), /effects/);
  assert.equal(
    store.listReviewsForActor({ actor: "production-e2e" })[0].status,
    "pending",
  );
  const thread = store.getThreadForActor("thread-slack", "production-e2e");
  assert.equal(thread.pendingReviewId, "review-session-diagnose");
  assert.equal(thread.activeSessionId, null);
});
test("rejecting a repair review closes the thread and starts no session", () => {
  const store = storeAtPendingSlackReview();
  const rejected = store.decideReviewAndRoute({
    reviewId: "review-session-diagnose",
    actor: "production-e2e",
    decision: "reject",
    decisionId: "decision-reject",
    now,
  });
  assert.equal(rejected.review.status, "rejected");
  assert.equal(rejected.session, null);
  assert.equal(rejected.thread.state, "review_rejected");
  assert.equal(rejected.thread.continuationBlocked, true);
  assert.throws(() => store.appendUserMessageAndRoute({
    threadId: "thread-slack", actor: "production-e2e", messageId: "later", text: "continue", newSessionId: "later",
  }), /cannot continue/);
});
