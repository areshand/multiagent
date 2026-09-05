import assert from "node:assert/strict";
import test from "node:test";
import { jobPhase, renderSessionTemplate, sessionSecret } from "../src/kubernetes-session.mjs";

test("deployment-owned session templates accept only named bounded substitutions", () => {
  const rendered = renderSessionTemplate({ metadata: { name: "session-{{SESSION_ID}}" }, value: "{{RESUME}}", authentication: "{{REPOSITORY_AUTHENTICATION}}" }, {
    SESSION_ID: "task-1",
    RESUME: "0",
    REPOSITORY_AUTHENTICATION: "github-app",
  });
  assert.deepEqual(rendered, { metadata: { name: "session-task-1" }, value: "0", authentication: "github-app" });
  assert.throws(() => renderSessionTemplate("{{IMAGE}}", {}), /unknown placeholder/);
  assert.throws(() => renderSessionTemplate("{{session}}", {}), /invalid placeholder/);
});

test("session bootstrap secrets bind thread, execution lease, scoped token, and mutation grant", () => {
  const grant = { kind: "review-approved-repair", paths: ["config/service.yaml"] };
  const secret = sessionSecret("task-1", "multiagent", "summarize general", "caller-123", "thread-1", 4, "message-1", "scoped.token", "user", grant);
  assert.equal(secret.metadata.name, "multiagent-session-task-1");
  assert.equal(Buffer.from(secret.data["task.md"], "base64").toString("utf8"), "summarize general");
  assert.equal(Buffer.from(secret.data["thread-id"], "base64").toString("utf8"), "thread-1");
  assert.equal(Buffer.from(secret.data["lease-generation"], "base64").toString("utf8"), "4");
  assert.equal(Buffer.from(secret.data["authorizing-event-id"], "base64").toString("utf8"), "message-1");
  assert.equal(Buffer.from(secret.data["gateway-token"], "base64").toString("utf8"), "scoped.token");
  assert.equal(secret.immutable, true);
  assert.equal(Buffer.from(secret.data["authority-scope"], "base64").toString("utf8"), "user");
  assert.deepEqual(JSON.parse(Buffer.from(secret.data["mutation-grant.json"], "base64").toString("utf8")), grant);
  const defaultSecret = sessionSecret("task-2", "multiagent", "read", "caller-456");
  assert.equal(
    Buffer.from(defaultSecret.data["authority-scope"], "base64").toString("utf8"), "user",
  );
});

test("Kubernetes Job status maps to the public session lifecycle", () => {
  assert.equal(jobPhase(null), "pending");
  assert.equal(jobPhase({ status: { active: 1 } }), "running");
  assert.equal(jobPhase({ status: { succeeded: 1 } }), "completed");
  assert.equal(jobPhase({ status: { failed: 1 } }), "failed");
});
