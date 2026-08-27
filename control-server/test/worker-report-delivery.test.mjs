import assert from "node:assert/strict";
import test from "node:test";
import {
  deliverWorkerReport,
  reportDeliveryTimeoutMs,
  workerReportEndpoint,
} from "../src/worker-report-delivery.mjs";

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
