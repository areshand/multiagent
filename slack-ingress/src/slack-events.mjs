import crypto from "node:crypto";

function safeEqual(left, right) {
  const first = Buffer.from(String(left || ""));
  const second = Buffer.from(String(right || ""));
  return first.length > 0 && first.length === second.length && crypto.timingSafeEqual(first, second);
}

export function verifySlackRequest({ rawBody, timestamp, signature, signingSecret, nowMs = Date.now() }) {
  const seconds = Number(timestamp);
  if (!Number.isSafeInteger(seconds) || Math.abs(Math.floor(nowMs / 1000) - seconds) > 300) return false;
  if (!String(signingSecret || "")) return false;
  const base = `v0:${seconds}:${Buffer.from(rawBody).toString("utf8")}`;
  const expected = `v0=${crypto.createHmac("sha256", signingSecret).update(base).digest("hex")}`;
  return safeEqual(signature, expected);
}

function required(value, name, max) {
  const text = String(value || "").trim();
  if (!text || text.length > max) throw new Error(`${name} must contain 1 to ${max} characters`);
  return text;
}

export function parseSlackEnvelope(body, { allowedChannelIds, ignoredBotUserId = "" } = {}) {
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Slack request must be an object");
  if (body.type === "url_verification") {
    return { kind: "challenge", challenge: required(body.challenge, "Slack challenge", 512) };
  }
  if (body.type !== "event_callback") return { kind: "ignored", reason: "unsupported-envelope" };
  const eventId = required(body.event_id, "Slack event ID", 256);
  const workspaceId = required(body.team_id, "Slack workspace ID", 128);
  const event = body.event;
  if (!event || event.type !== "message") return { kind: "ignored", reason: "not-a-message" };
  if (event.subtype && event.subtype !== "bot_message") return { kind: "ignored", reason: "message-subtype" };
  const channelId = required(event.channel, "Slack channel ID", 128);
  const allowlist = allowedChannelIds instanceof Set ? allowedChannelIds : new Set(allowedChannelIds || []);
  if (!allowlist.size || !allowlist.has(channelId)) return { kind: "ignored", reason: "channel-not-allowed" };
  if (ignoredBotUserId && (event.user === ignoredBotUserId || event.bot_id === ignoredBotUserId)) {
    return { kind: "ignored", reason: "self-message" };
  }
  const text = required(event.text, "Slack message text", 16 * 1024);
  return {
    kind: "event",
    event: {
      eventId,
      workspaceId,
      channelId,
      messageTs: required(event.ts, "Slack message timestamp", 64),
      threadTs: event.thread_ts ? required(event.thread_ts, "Slack thread timestamp", 64) : null,
      senderId: event.user || event.bot_id || null,
      text,
    },
  };
}

export function parseChannelAllowlist(value) {
  const channels = String(value || "").split(",").map((item) => item.trim()).filter(Boolean);
  if (!channels.length) throw new Error("SLACK_ALLOWED_CHANNEL_IDS must contain at least one channel ID");
  for (const channel of channels) {
    if (!/^[A-Z0-9]+$/.test(channel)) throw new Error(`invalid Slack channel ID: ${channel}`);
  }
  return new Set(channels);
}
