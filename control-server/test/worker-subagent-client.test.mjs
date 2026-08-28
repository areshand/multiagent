import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";
import { fetchWorkerSubagents } from "../src/worker-subagent-client.mjs";

test("gateway fetches a bounded authenticated subagent snapshot from the session worker", async (context) => {
  const server = http.createServer((request, response) => {
    assert.equal(request.url, "/api/sessions/session-1/agents");
    assert.equal(request.headers.authorization, "Bearer scoped-token");
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ agents: [{ name: "reader", status: "working" }] }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => server.close());
  const address = server.address();
  const agents = await fetchWorkerSubagents({
    sessionId: "session-1",
    hostname: "127.0.0.1",
    port: address.port,
    token: "scoped-token",
  });
  assert.deepEqual(agents, [{ name: "reader", status: "working" }]);
});

test("gateway rejects malformed worker subagent snapshots", async (context) => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.end(JSON.stringify({ agents: "not-an-array" }));
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => server.close());
  const address = server.address();
  await assert.rejects(fetchWorkerSubagents({
    sessionId: "session-1",
    hostname: "127.0.0.1",
    port: address.port,
    token: "scoped-token",
  }), /invalid subagent snapshot/);
});
