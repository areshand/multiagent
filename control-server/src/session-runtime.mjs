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

export function ownsThreadProjection(serverMode) {
  return serverMode !== "session-worker";
}

export function acceptsLiveInput(live, headless) {
  return Boolean(live) && !headless;
}

const terminalOutcomes = new Set(["succeeded", "failed", "review_requested"]);

export function executionTerminalOutcome({ phase, outcome, live }) {
  if (phase === "complete") {
    if (terminalOutcomes.has(outcome)) return outcome;
    // Workflows created before terminal_outcome was introduced completed only
    // through supervisor-owned success gates.
    return outcome ? "failed" : "succeeded";
  }
  return live ? null : "failed";
}

export function sessionStatusForTerminalOutcome(outcome) {
  if (!terminalOutcomes.has(outcome)) throw new Error(`invalid terminal outcome: ${outcome}`);
  return outcome === "failed" ? "failed" : "completed";
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

export function responseTypeForMessage(message, completionRoute = "") {
  if (!["direct-response", "human-review"].includes(completionRoute)) return "assistant_message";
  const text = String(message || "").trim();
  const questions = [...text].filter((character) => character === "?" || character === "？").length;
  const tail = text.replace(/[\s*_`"')\]]+$/g, "");
  return Buffer.byteLength(text, "utf8") <= 2000
    && questions >= 1
    && questions <= 3
    && (tail.endsWith("?") || tail.endsWith("？"))
    ? "question"
    : "assistant_message";
}

export async function submitLocalFollowup({ id, text, actor, live, sendInput, restart, sessionView }) {
  if (live) {
    await sendInput(id, text);
    return { mode: "live-input", session: sessionView(id) };
  }
  return { mode: "supervisor-resume", session: restart(id, text, actor) };
}

export function normalizeWorkerReport(value) {
  if (!value || typeof value !== "object" || typeof value.report !== "string" || !value.report.trim()) return null;
  if (Buffer.byteLength(value.report, "utf8") > 64 * 1024) return null;
  const transcript = value.transcript === undefined ? null : value.transcript;
  if (Buffer.byteLength(JSON.stringify(transcript), "utf8") > 64 * 1024) return null;
  const message = typeof value.message === "string" && value.message.trim() ? value.message.trim() : null;
  if (message && Buffer.byteLength(message, "utf8") > 6000) return null;
  const completionRoute = new Set(["direct-response", "read-only", "external-only", "human-review", "source"])
    .has(value.completionRoute) ? value.completionRoute : null;
  if (value.terminalOutcome !== undefined && !terminalOutcomes.has(value.terminalOutcome)) return null;
  const terminalOutcome = terminalOutcomes.has(value.terminalOutcome)
    ? value.terminalOutcome
    : value.status === "failed" ? "failed"
      : completionRoute === "human-review" ? "review_requested" : "succeeded";
  const responseType = responseTypeForMessage(message, completionRoute);
  if ((completionRoute === "human-review") !== (terminalOutcome === "review_requested")) return null;
  if (terminalOutcome === "review_requested" && responseType !== "question") return null;
  return {
    report: value.report,
    transcript,
    message,
    completionRoute,
    terminalOutcome,
    responseType,
  };
}

export function scopedThreadTranscript(sessionId, transcript) {
  if (!transcript || typeof transcript !== "object") return null;
  const traceReferences = Array.isArray(transcript.traceReferences)
    ? transcript.traceReferences.map((reference) => {
      const normalized = path.posix.normalize(path.posix.join("logs", String(reference)));
      if (path.posix.isAbsolute(normalized) || normalized === ".." || normalized.startsWith("../")) return null;
      return `trace://session/${sessionId}/${normalized}`;
    }).filter(Boolean)
    : [];
  return { ...transcript, traceReferences };
}

export function workerReportPublicEvent(sessionId, report) {
  return {
    type: report.responseType,
    payload: {
      text: selectFinalMessage(report.message, report.report),
      transcript: scopedThreadTranscript(sessionId, report.transcript),
    },
  };
}

export function workerReportInterruptedEvent(sessionId, report, fallback) {
  return {
    type: "session_interrupted",
    payload: {
      text: report ? selectFinalMessage(report.message, fallback) : String(fallback || "").trim(),
      transcript: report ? scopedThreadTranscript(sessionId, report.transcript) : null,
    },
  };
}
