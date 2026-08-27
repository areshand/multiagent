import assert from "node:assert/strict";
import test from "node:test";
import { jobPhase, renderSessionTemplate, sessionSecret } from "../src/kubernetes-session.mjs";

test("deployment-owned session templates accept only named bounded substitutions", () => {
  const rendered = renderSessionTemplate({ metadata: { name: "session-{{SESSION_ID}}" }, value: "{{RESUME}}" }, {
    SESSION_ID: "task-1",
    RESUME: "0",
  });
  assert.deepEqual(rendered, { metadata: { name: "session-task-1" }, value: "0" });
  assert.throws(() => renderSessionTemplate("{{IMAGE}}", {}), /unknown placeholder/);
  assert.throws(() => renderSessionTemplate("{{session}}", {}), /invalid placeholder/);
});

test("session bootstrap secrets bind thread and execution lease without placing task data in Job arguments", () => {
  const secret = sessionSecret("task-1", "multiagent", "summarize general", "caller-123", "thread-1", 4);
  assert.equal(secret.metadata.name, "multiagent-session-task-1");
  assert.equal(Buffer.from(secret.data["task.md"], "base64").toString("utf8"), "summarize general");
  assert.equal(Buffer.from(secret.data["thread-id"], "base64").toString("utf8"), "thread-1");
  assert.equal(Buffer.from(secret.data["lease-generation"], "base64").toString("utf8"), "4");
  assert.equal(secret.immutable, true);
});

test("Kubernetes Job status maps to the public session lifecycle", () => {
  assert.equal(jobPhase(null), "pending");
  assert.equal(jobPhase({ status: { active: 1 } }), "running");
  assert.equal(jobPhase({ status: { succeeded: 1 } }), "completed");
  assert.equal(jobPhase({ status: { failed: 1 } }), "failed");
});
