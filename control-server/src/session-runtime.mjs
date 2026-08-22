import path from "node:path";

export const ORCHESTRATOR_UID = 10001;
export const ROLE_GID = 10001;

export function tmuxInvocation(stateRoot, sessionId, args, uidSandbox) {
  if (!uidSandbox) return { args, options: {} };
  return {
    args: ["-S", path.join(stateRoot, "sessions", sessionId, "runtime_state", "tmux.sock"), ...args],
    options: { uid: ORCHESTRATOR_UID, gid: ROLE_GID },
  };
}

export function findActiveSession(sessionIds, candidate, isAlive) {
  return sessionIds.find((id) => id !== candidate && isAlive(id)) || null;
}
