import assert from "node:assert/strict";
import test from "node:test";
import { findActiveSession, tmuxInvocation } from "../src/session-runtime.mjs";

test("UID-isolated control uses the per-session socket and orchestrator identity", () => {
  assert.deepEqual(tmuxInvocation("/var/lib/multiagent/state", "task-1", ["has-session", "-t", "task-1"], true), {
    args: [
      "-S",
      "/var/lib/multiagent/state/sessions/task-1/runtime_state/tmux.sock",
      "has-session",
      "-t",
      "task-1",
    ],
    options: { uid: 10001, gid: 10001 },
  });
});

test("UID-isolated control rejects a second active session", () => {
  const alive = new Set(["task-1"]);
  assert.equal(findActiveSession(["task-1", "task-2"], "task-2", (id) => alive.has(id)), "task-1");
  assert.equal(findActiveSession(["task-1"], "task-1", (id) => alive.has(id)), null);
});
