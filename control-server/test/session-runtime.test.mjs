import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import {
  completionExitDelayMs,
  controlMode,
  findActiveSession,
  normalizeWorkerReport,
  selectFinalMessage,
  sessionControlInvocation,
  sessionLaunchInvocation,
  validResourceId,
} from "../src/session-runtime.mjs";

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
  });
  assert.equal(normalizeWorkerReport({ report: "" }), null);
  assert.equal(normalizeWorkerReport({ report: "x".repeat(64 * 1024 + 1) }), null);
});

test("session completion grace is bounded and has a stable default", () => {
  assert.equal(completionExitDelayMs(), 30_000);
  assert.equal(completionExitDelayMs("1"), 10_000);
  assert.equal(completionExitDelayMs("999"), 120_000);
  assert.equal(completionExitDelayMs("invalid"), 30_000);
});
