import assert from "node:assert/strict";
import crypto from "node:crypto";
import test from "node:test";
import { parseChannelAllowlist, parseSlackEnvelope, verifySlackRequest } from "../src/slack-events.mjs";

function signature(secret, timestamp, body) {
  return `v0=${crypto.createHmac("sha256", secret).update(`v0:${timestamp}:${body}`).digest("hex")}`;
}

test("Slack request verification binds timestamp and raw body", () => {
  const timestamp = 2_000_000_000;
  const body = JSON.stringify({ type: "event_callback" });
  assert.equal(verifySlackRequest({
    rawBody: Buffer.from(body),
    timestamp,
    signature: signature("secret", timestamp, body),
    signingSecret: "secret",
    nowMs: timestamp * 1000,
  }), true);
  assert.equal(verifySlackRequest({
    rawBody: Buffer.from(`${body} `),
    timestamp,
    signature: signature("secret", timestamp, body),
    signingSecret: "secret",
    nowMs: timestamp * 1000,
  }), false);
  assert.equal(verifySlackRequest({
    rawBody: Buffer.from(body),
    timestamp,
    signature: signature("secret", timestamp, body),
    signingSecret: "secret",
    nowMs: (timestamp + 301) * 1000,
  }), false);
});

test("only allowed channel messages become normalized events", () => {
  const body = {
    type: "event_callback",
    event_id: "Ev123",
    team_id: "T123",
    event: { type: "message", channel: "C-HANGOUT".replace("-", ""), user: "U123", ts: "123.456", text: "api error rate high" },
  };
  const parsed = parseSlackEnvelope(body, { allowedChannelIds: new Set(["CHANGOUT"]) });
  assert.equal(parsed.kind, "event");
  assert.deepEqual(parsed.event, {
    eventId: "Ev123",
    workspaceId: "T123",
    channelId: "CHANGOUT",
    messageTs: "123.456",
    threadTs: null,
    senderId: "U123",
    text: "api error rate high",
  });
  assert.equal(parseSlackEnvelope(body, { allowedChannelIds: new Set(["COTHER"]) }).reason, "channel-not-allowed");
  assert.equal(parseSlackEnvelope({ ...body, event: { ...body.event, subtype: "message_changed" } }, { allowedChannelIds: new Set(["CHANGOUT"]) }).reason, "message-subtype");
});

test("channel allowlist is required and validates Slack IDs", () => {
  assert.deepEqual([...parseChannelAllowlist("CHANGOUT,COTHER")], ["CHANGOUT", "COTHER"]);
  assert.throws(() => parseChannelAllowlist(""), /at least one/);
  assert.throws(() => parseChannelAllowlist("hangout"), /invalid Slack channel ID/);
});
