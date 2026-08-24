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
