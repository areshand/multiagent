import assert from "node:assert/strict";
import test from "node:test";
import {
  deliverWorkerReport,
  reportDeliveryTimeoutMs,
  workerReportEndpoint,
} from "../src/worker-report-delivery.mjs";
import { issueWorkerToken, verifyWorkerAuthorization } from "../src/worker-token.mjs";

test("session worker tokens authenticate only to the matching gateway or worker session", () => {
  const sessionSecret = "test-session-secret-that-is-long-enough";
  const token = issueWorkerToken({ sessionSecret, sessionId: "session-1", ttlMs: 60_000, now: 1_000 });
  const verify = (overrides = {}) => verifyWorkerAuthorization({
    serverMode: "gateway",
    authorization: `Bearer ${token}`,
    sessionSecret,
    sessionId: "session-1",
    now: 2_000,
    ...overrides,
  });

  assert.equal(verify(), true);
  assert.equal(verify({ serverMode: "session-worker" }), true);
  assert.equal(verify({ serverMode: "local" }), false);
  assert.equal(verify({ sessionId: "session-2" }), false);
  assert.equal(verify({ now: 61_001 }), false);
  assert.equal(verify({ authorization: `Bearer ${token}tampered` }), false);
});

test("worker report delivery retries a deployment-owned session endpoint", async () => {
  let attempts = 0;
  const waits = [];
  await deliverWorkerReport({
    gatewayUrl: "http://multiagent.multiagent.svc.cluster.local:8080",
    sessionId: "thread-1-session-2",
    token: "scoped.token",
    report: { report: "complete", transcript: null },
    timeoutMs: 60_000,
    retryDelayMs: 25,
    post: async (endpoint, token, report) => {
      attempts += 1;
      assert.equal(endpoint.href, "http://multiagent.multiagent.svc.cluster.local:8080/api/sessions/thread-1-session-2/report");
      assert.equal(token, "scoped.token");
      assert.equal(report.report, "complete");
      if (attempts < 3) throw new Error("gateway unavailable");
    },
    wait: async (milliseconds) => waits.push(milliseconds),
  });
  assert.equal(attempts, 3);
  assert.deepEqual(waits, [25, 25]);
});

test("worker report delivery configuration is bounded and rejects embedded credentials", () => {
  assert.equal(reportDeliveryTimeoutMs(), 600_000);
  assert.equal(reportDeliveryTimeoutMs("1"), 30_000);
  assert.equal(reportDeliveryTimeoutMs("9999"), 1_800_000);
  assert.throws(() => workerReportEndpoint("http://user:password@gateway:8080", "session-1"), /without credentials/);
  assert.throws(() => workerReportEndpoint("file:///tmp/gateway", "session-1"), /HTTP\(S\)/);
});
