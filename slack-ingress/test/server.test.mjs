import assert from "node:assert/strict";
import crypto from "node:crypto";
import { mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createSlackIngress } from "../src/server.mjs";

function signedHeaders(secret, timestamp, rawBody) {
  const signature = `v0=${crypto.createHmac("sha256", secret).update(`v0:${timestamp}:${rawBody}`).digest("hex")}`;
  return {
    "content-type": "application/json",
    "x-slack-request-timestamp": String(timestamp),
    "x-slack-signature": signature,
  };
}

test("HTTP ingress rejects bad signatures then queues and delivers an allowed message", async (context) => {
  const root = await mkdtemp(path.join(os.tmpdir(), "slack-ingress-"));
  const signingSecretFile = path.join(root, "signing-secret");
  const tokenFile = path.join(root, "control-token");
  await writeFile(signingSecretFile, "signing-secret\n", { mode: 0o600 });
  await writeFile(tokenFile, "control-token\n", { mode: 0o600 });
  let deliveredResolve;
  const delivered = new Promise((resolve) => { deliveredResolve = resolve; });
  const calls = [];
  const timestamp = 2_000_000_000;
  const ingress = await createSlackIngress({
    env: {
      PORT: "0",
      HOST: "127.0.0.1",
      SLACK_SIGNING_SECRET_FILE: signingSecretFile,
      MULTIAGENT_SLACK_INGRESS_TOKEN_FILE: tokenFile,
      MULTIAGENT_CONTROL_SERVER_URL: "http://control.internal",
      SLACK_ALLOWED_CHANNEL_IDS: "CHANGOUT",
      SLACK_INGRESS_STATE_DIR: path.join(root, "queue"),
    },
    now: () => timestamp * 1000,
    logger: { error() {} },
    fetchImpl: async (url, options) => {
      calls.push({ url: String(url), options });
      deliveredResolve();
      return new Response(JSON.stringify({ accepted: true }), { status: 202 });
    },
  });
  context.after(() => ingress.stop());
  const address = await ingress.start();
  const endpoint = `http://127.0.0.1:${address.port}/slack/events`;
  const payload = {
    type: "event_callback",
    event_id: "Ev123",
    team_id: "T123",
    event: { type: "message", channel: "CHANGOUT", user: "U123", ts: "123.456", text: "api error rate high" },
  };
  const rawBody = JSON.stringify(payload);

  const rejected = await fetch(endpoint, { method: "POST", headers: signedHeaders("wrong", timestamp, rawBody), body: rawBody });
  assert.equal(rejected.status, 401);

  const accepted = await fetch(endpoint, { method: "POST", headers: signedHeaders("signing-secret", timestamp, rawBody), body: rawBody });
  assert.equal(accepted.status, 200);
  assert.deepEqual(await accepted.json(), { ok: true, queued: true, duplicate: false });
  await delivered;
  await ingress.drain();
  assert.equal(await ingress.queue.size(), 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://control.internal/internal/integrations/slack/events");
  assert.equal(calls[0].options.headers.authorization, "Bearer control-token");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    eventId: "Ev123",
    workspaceId: "T123",
    channelId: "CHANGOUT",
    messageTs: "123.456",
    threadTs: null,
    senderId: "U123",
    text: "api error rate high",
  });
});
