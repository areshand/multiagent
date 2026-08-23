import assert from "node:assert/strict";
import test from "node:test";
import { findActiveSession, sessionControlInvocation } from "../src/session-runtime.mjs";

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
