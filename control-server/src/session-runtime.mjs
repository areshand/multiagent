import path from "node:path";

const resourceIdPattern = /^[a-z0-9][a-z0-9-]{0,62}$/;

export function validResourceId(value) {
  return typeof value === "string" && resourceIdPattern.test(value);
}

export function sessionControlInvocation(sessionId, action, args = []) {
  return {
    command: process.env.MULTIAGENT_BIN || "/opt/multiagent/bin/multiagent",
    args: ["session-control", sessionId, action, ...args],
    options: {},
  };
}

export function sessionLaunchInvocation(launcherRoot, sessionId, repositoryRoot, resume = false) {
  const args = [path.join(launcherRoot, "launch.sh"), "--session", sessionId, "--root", repositoryRoot, "--no-attach"];
  if (resume) args.push("--resume");
  return { command: "bash", args };
}

export function controlMode(value = process.env.MULTIAGENT_CONTROL_MODE) {
  const mode = value || "local";
  if (!["local", "gateway", "session-worker"].includes(mode)) throw new Error(`unsupported control mode: ${mode}`);
  return mode;
}

export function findActiveSession(sessionIds, candidate, isAlive) {
  return sessionIds.find((id) => id !== candidate && isAlive(id)) || null;
}

export function completionExitDelayMs(value = process.env.MULTIAGENT_SESSION_COMPLETION_GRACE_SECONDS) {
  const parsed = value === undefined || value === "" ? 30 : Number(value);
  const seconds = Number.isFinite(parsed) ? Math.min(Math.max(parsed, 10), 120) : 30;
  return seconds * 1000;
}

export function selectFinalMessage(result, fallback) {
  return String(result || "").trim() || String(fallback || "").trim();
}

export function normalizeWorkerReport(value) {
  if (!value || typeof value !== "object" || typeof value.report !== "string" || !value.report.trim()) return null;
  if (Buffer.byteLength(value.report, "utf8") > 64 * 1024) return null;
  const transcript = value.transcript === undefined ? null : value.transcript;
  if (Buffer.byteLength(JSON.stringify(transcript), "utf8") > 64 * 1024) return null;
  return { report: value.report, transcript };
}
