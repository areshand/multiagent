import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  acceptsLiveInput,
  automaticResumeLimit,
  completionExitDelayMs,
  controlMode,
  executionTerminalOutcome,
  findActiveSession,
  normalizeWorkerReport,
  ownsThreadProjection,
  responseTypeForMessage,
  scopedThreadTranscript,
  selectFinalMessage,
  sessionControlInvocation,
  sessionLaunchInvocation,
  sessionStatusForTerminalOutcome,
  shouldAutomaticallyResume,
  submitLocalFollowup,
  validResourceId,
  workerReportInterruptedEvent,
  workerReportPublicEvent,
} from "../src/session-runtime.mjs";

test("session workers report outcomes to the gateway instead of projecting a private thread store", () => {
  assert.equal(ownsThreadProjection("session-worker"), false);
  assert.equal(ownsThreadProjection("gateway"), true);
  assert.equal(ownsThreadProjection("local"), true);
});

test("headless sessions restart for follow-ups and recover incomplete lifecycle passes within a bound", () => {
  assert.equal(acceptsLiveInput(true, false), true);
  assert.equal(acceptsLiveInput(true, true), false);
  assert.equal(acceptsLiveInput(false, false), false);
  assert.equal(automaticResumeLimit(), 3);
  assert.equal(automaticResumeLimit("0"), 0);
  assert.equal(automaticResumeLimit("99"), 10);
  assert.equal(automaticResumeLimit("invalid"), 3);
  assert.equal(shouldAutomaticallyResume({ status: "running", autoResume: true, automaticResumeAttempts: 2 }, 3), true);
  assert.equal(shouldAutomaticallyResume({ status: "running", autoResume: true, automaticResumeAttempts: 3 }, 3), false);
  assert.equal(shouldAutomaticallyResume({ status: "completed", autoResume: true, automaticResumeAttempts: 0 }, 3), false);
});

test("every stopped execution maps immediately to one terminal outcome", () => {
  assert.equal(executionTerminalOutcome({ phase: "complete", outcome: "succeeded", live: false }), "succeeded");
  assert.equal(executionTerminalOutcome({ phase: "complete", outcome: "failed", live: true }), "failed");
  assert.equal(executionTerminalOutcome({ phase: "complete", outcome: "review_requested", live: false }), "review_requested");
  assert.equal(executionTerminalOutcome({ phase: "complete", outcome: "", live: false }), "succeeded");
  assert.equal(executionTerminalOutcome({ phase: "complete", outcome: "corrupt", live: false }), "failed");
  assert.equal(executionTerminalOutcome({ phase: "implementation", outcome: "", live: false }), "failed");
  assert.equal(executionTerminalOutcome({ phase: "implementation", outcome: "", live: true }), null);
  assert.equal(sessionStatusForTerminalOutcome("succeeded"), "completed");
  assert.equal(sessionStatusForTerminalOutcome("review_requested"), "completed");
  assert.equal(sessionStatusForTerminalOutcome("failed"), "failed");
  assert.throws(() => sessionStatusForTerminalOutcome("retrying"), /invalid terminal outcome/);
});

test("control server session IDs match the shared Rust contract", async () => {
  const vectors = JSON.parse(
    await readFile(new URL("../../contracts/session-id-vectors.json", import.meta.url), "utf8"),
  );
  for (const value of vectors.valid) assert.equal(validResourceId(value), true, value);
  for (const value of vectors.invalid) assert.equal(validResourceId(value), false, value);
});

test("UID-isolated control uses semantic setuid-gated session operations", () => {
  assert.deepEqual(sessionControlInvocation("task-1", "capture", ["120"]), {
    command: "/opt/multiagent/bin/multiagent",
    args: ["session-control", "task-1", "capture", "120"],
    options: {},
  });
});

test("UID-isolated control rejects a second active session", () => {
  const alive = new Set(["task-1"]);
  assert.equal(findActiveSession(["task-1", "task-2"], "task-2", (id) => alive.has(id)), "task-1");
  assert.equal(findActiveSession(["task-1"], "task-1", (id) => alive.has(id)), null);
});

test("session workers preserve the launch.sh compatibility callsite", () => {
  assert.deepEqual(sessionLaunchInvocation("/opt/multiagent", "task-1", "/work/repository", false), {
    command: "bash",
    args: ["/opt/multiagent/launch.sh", "--session", "task-1", "--root", "/work/repository", "--no-attach"],
  });
  assert.equal(sessionLaunchInvocation("/opt/multiagent", "task-1", "/work/repository", true).args.at(-1), "--resume");
});

test("control mode defaults to local and accepts only explicit deployment modes", () => {
  assert.equal(controlMode(""), "local");
  assert.equal(controlMode("gateway"), "gateway");
  assert.equal(controlMode("session-worker"), "session-worker");
  assert.throws(() => controlMode("kubernetes"), /unsupported control mode/);
});

test("completed session reports prefer the explicit bounded caller result", () => {
  assert.equal(selectFinalMessage("caller result\n", "procedural fallback"), "caller result");
  assert.equal(selectFinalMessage("", "procedural fallback\n"), "procedural fallback");
  assert.deepEqual(normalizeWorkerReport({ report: "complete result", transcript: { taskId: "task-1" } }), {
    report: "complete result",
    transcript: { taskId: "task-1" },
    message: null,
    completionRoute: null,
    terminalOutcome: "succeeded",
    responseType: "assistant_message",
  });
  assert.deepEqual(normalizeWorkerReport({
    report: "completed report",
    transcript: null,
    message: "Which repository should I check?",
    completionRoute: "direct-response",
    terminalOutcome: "succeeded",
  }), {
    report: "completed report",
    transcript: null,
    message: "Which repository should I check?",
    completionRoute: "direct-response",
    terminalOutcome: "succeeded",
    responseType: "question",
  });
  assert.equal(normalizeWorkerReport({ report: "" }), null);
  assert.equal(normalizeWorkerReport({ report: "x".repeat(64 * 1024 + 1) }), null);
  assert.equal(normalizeWorkerReport({ report: "bad", terminalOutcome: "retrying" }), null);
  assert.equal(normalizeWorkerReport({ report: "bad", completionRoute: "human-review", terminalOutcome: "succeeded" }), null);
  assert.equal(normalizeWorkerReport({ report: "bad", completionRoute: "human-review", terminalOutcome: "review_requested", message: "not a question" }), null);
});

test("production-shaped reports publish the user result instead of lifecycle metadata", () => {
  const report = normalizeWorkerReport({
    report: "# thread-latest-open-pr\n\nStatus: completed\nWorkflow: run-1\n\n## Final agent message\nLatest open PR: #421\n\n## Trace references\n- agents/ops-01/events.jsonl",
    message: "Latest open PR: #421 — fix: remove global waypoint signature-verification bypass",
    completionRoute: "external-only",
    terminalOutcome: "succeeded",
    transcript: { traceReferences: ["agents/ops-01/events.jsonl"] },
  });
  assert.deepEqual(workerReportPublicEvent("session-1", report), {
    type: "assistant_message",
    payload: {
      text: "Latest open PR: #421 — fix: remove global waypoint signature-verification bypass",
      transcript: { traceReferences: ["trace://session/session-1/logs/agents/ops-01/events.jsonl"] },
    },
  });
});

test("failed sessions publish their bounded blocker instead of a generic interruption", () => {
  const report = normalizeWorkerReport({
    report: "# session-1\n\nStatus: failed\n\n## Final agent message\nThe Grafana read is blocked by an operation version mismatch.",
    message: "The Grafana read is blocked: the runbook requests 1.0.0 but prod-mcp certifies 1.1.0.",
    completionRoute: "external-only",
    terminalOutcome: "failed",
    transcript: { traceReferences: ["agents/ops-01/events.jsonl"] },
  });
  assert.deepEqual(workerReportInterruptedEvent("session-1", report, "Execution session failed"), {
    type: "session_interrupted",
    payload: {
      text: "The Grafana read is blocked: the runbook requests 1.0.0 but prod-mcp certifies 1.1.0.",
      transcript: { traceReferences: ["trace://session/session-1/logs/agents/ops-01/events.jsonl"] },
    },
  });
});

test("only bounded direct-response questions project as clarification events", () => {
  assert.equal(responseTypeForMessage("Which repository should I check?", "direct-response"), "question");
  assert.equal(responseTypeForMessage("你希望检查哪个仓库？", "direct-response"), "question");
  assert.equal(responseTypeForMessage("The latest PR is #421.", "direct-response"), "assistant_message");
  assert.equal(responseTypeForMessage("Which repository should I check?", "external-only"), "assistant_message");
  assert.equal(responseTypeForMessage("One? Two? Three? Four?", "direct-response"), "assistant_message");
});

test("thread transcript references remain bound to their originating session", () => {
  assert.deepEqual(scopedThreadTranscript("session-a", {
    taskId: "session-a",
    traceReferences: ["agents/orchestrator/attempt-0001/events.jsonl", "../workflows/run-1/lifecycle/events.log", "../../../escape"],
  }), {
    taskId: "session-a",
    traceReferences: [
      "trace://session/session-a/logs/agents/orchestrator/attempt-0001/events.jsonl",
      "trace://session/session-a/workflows/run-1/lifecycle/events.log",
    ],
  });
});

test("session completion grace is bounded and has a stable default", () => {
  assert.equal(completionExitDelayMs(), 30_000);
  assert.equal(completionExitDelayMs("1"), 10_000);
  assert.equal(completionExitDelayMs("999"), 120_000);
  assert.equal(completionExitDelayMs("invalid"), 30_000);
});

test("live local follow-ups stay in the active execution instead of restarting it", async () => {
  const calls = [];
  const accepted = await submitLocalFollowup({
    id: "session-a",
    text: "continue",
    actor: "user-a",
    live: true,
    sendInput: (...args) => calls.push(["input", ...args]),
    restart: (...args) => calls.push(["restart", ...args]),
    sessionView: (id) => ({ id, live: true }),
  });
  assert.deepEqual(accepted, { mode: "live-input", session: { id: "session-a", live: true } });
  assert.deepEqual(calls, [["input", "session-a", "continue"]]);
});

test("stopped local executions resume only when a follow-up arrives", async () => {
  const calls = [];
  const accepted = await submitLocalFollowup({
    id: "session-a",
    text: "continue",
    actor: "user-a",
    live: false,
    sendInput: (...args) => calls.push(["input", ...args]),
    restart: (...args) => {
      calls.push(["restart", ...args]);
      return { id: args[0], live: true };
    },
    sessionView: () => null,
  });
  assert.deepEqual(accepted, { mode: "supervisor-resume", session: { id: "session-a", live: true } });
  assert.deepEqual(calls, [["restart", "session-a", "continue", "user-a"]]);
});
