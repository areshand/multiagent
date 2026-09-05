import assert from "node:assert/strict";
import test from "node:test";
import { Thread } from "../../thread/src/thread.mjs";
import { InMemoryThreadStore } from "../../thread/src/thread-model.mjs";

function fixture() {
  const store = new InMemoryThreadStore();
  const launched = [];
  const projected = [];
  let nextSession = 0;
  const threads = new Thread({
    threadStore: store,
    newThreadId: () => "thread-managed",
    newSessionId: () => `session-${++nextSession}`,
    startExecution: async (execution) => launched.push(execution),
    deliverFollowup: async () => ({ mode: "live-input" }),
    markExecutionProjected: async (record, outcome) => {
      record.threadProjectedAt = "2026-09-05T00:00:00.000Z";
      projected.push(outcome);
    },
  });
  return { threads, launched, projected };
}

test("Thread owns routing, launch context, fencing, and result projection", async () => {
  const { threads, launched, projected } = fixture();
  const thread = threads.createThread({ ownerSubject: "user-a", repository: "multiagent", title: "Managed" });
  const routed = await threads.appendMessage({
    threadId: thread.id,
    actor: "user-a",
    messageId: "message-1",
    text: "Inspect the current implementation",
  });

  assert.equal(routed.delivery.mode, "initial-context");
  assert.equal(routed.session.authorityScope, "user");
  assert.equal(launched.length, 1);
  assert.match(launched[0].task, /Current authenticated user request:/);
  assert.match(launched[0].task, /Inspect the current implementation/);
  assert.equal((await threads.listSessions({ threadId: thread.id, actor: "user-a" }))[0].status, "running");

  const record = {
    id: routed.session.id,
    threadId: thread.id,
    createdBy: "user-a",
    leaseGeneration: routed.session.leaseGeneration,
  };
  const result = await threads.projectExecution({
    record,
    status: "completed",
    report: {
      report: "Evidence-backed answer",
      message: "The implementation is read-only.",
      responseType: "assistant_message",
      terminalOutcome: "succeeded",
      transcript: { traceReferences: ["agents/reader/attempt-1/events.jsonl", "../../escape"] },
    },
  });

  assert.deepEqual(result, { projected: true, terminalOutcome: "succeeded" });
  assert.deepEqual(projected, ["succeeded"]);
  assert.equal((await threads.getThread(thread.id, "user-a")).state, "idle");
  const events = await threads.readEvents({ threadId: thread.id, actor: "user-a" });
  assert.equal(events.at(-1).payload.text, "The implementation is read-only.");
  assert.deepEqual(events.at(-1).payload.transcript.traceReferences, [
    `trace://session/${routed.session.id}/logs/agents/reader/attempt-1/events.jsonl`,
  ]);
});

test("Thread owns review decisions and launches approved continuations", async () => {
  const { threads, launched } = fixture();
  const thread = threads.createThread({ ownerSubject: "user-a", repository: "multiagent" });
  const routed = await threads.appendMessage({
    threadId: thread.id,
    actor: "user-a",
    messageId: "message-1",
    text: "Diagnose and propose any required repair",
  });
  await threads.projectExecution({
    record: {
      id: routed.session.id,
      threadId: thread.id,
      createdBy: "user-a",
      leaseGeneration: routed.session.leaseGeneration,
    },
    status: "completed",
    report: {
      report: "A bounded repair is required.",
      message: "Approve changing the bounded configuration?",
      responseType: "question",
      terminalOutcome: "review_requested",
      reviewRequest: { effects: ["source-write", "reviewed-ops"], paths: ["config/service.yaml"] },
      transcript: null,
    },
  });

  const [review] = await threads.listReviews({ actor: "user-a" });
  const decided = await threads.decideReview({
    reviewId: review.id,
    actor: "user-a",
    decision: "approve",
    decisionId: "decision-1",
    messageId: "approval-1",
  });
  assert.equal(decided.review.status, "approved");
  assert.equal(launched.length, 2);
  assert.equal(decided.session.authorityScope, "user");
  assert.deepEqual(decided.session.mutationGrant.paths, ["config/service.yaml"]);
  assert.match(launched[1].task, /exact reviewed request/);
});
