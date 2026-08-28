import crypto from "node:crypto";

const base64url = (value) => Buffer.from(value).toString("base64url");

export function issueWorkerToken({ sessionSecret, sessionId, ttlMs, now = Date.now() }) {
  const payload = base64url(JSON.stringify({
    audience: "multiagent-session-worker",
    sessionId,
    expiresAt: now + ttlMs,
    nonce: crypto.randomBytes(12).toString("hex"),
  }));
  const signature = crypto.createHmac("sha256", sessionSecret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

export function verifyWorkerAuthorization({
  serverMode,
  authorization,
  sessionSecret,
  sessionId,
  now = Date.now(),
}) {
  if (!["gateway", "session-worker"].includes(serverMode) || typeof authorization !== "string" || !authorization.startsWith("Bearer ")) return false;
  const token = authorization.slice(7);
  const [payload, signature] = token.split(".", 2);
  if (!payload || !signature) return false;
  const expected = crypto.createHmac("sha256", sessionSecret).update(payload).digest();
  let supplied;
  try { supplied = Buffer.from(signature, "base64url"); } catch { return false; }
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) return false;
  try {
    const value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return value.audience === "multiagent-session-worker" && value.sessionId === sessionId && value.expiresAt > now;
  } catch {
    return false;
  }
}
