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

export function findActiveSession(sessionIds, candidate, isAlive) {
  return sessionIds.find((id) => id !== candidate && isAlive(id)) || null;
}
