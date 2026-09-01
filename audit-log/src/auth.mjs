import { createHash, timingSafeEqual } from "node:crypto";
import fs from "node:fs";
import { validateIdentifier } from "./validation.mjs";

const PERMISSIONS = new Set(["append", "read", "verify"]);

export class AuthenticationError extends Error {
  constructor(message, statusCode = 401, code = "unauthorized") {
    super(message);
    this.statusCode = statusCode;
    this.code = code;
  }
}

export class ClientAuthorizer {
  constructor(file) {
    if (!file) throw new Error("AUDIT_LOG_CLIENTS_FILE is required");
    const decoded = JSON.parse(fs.readFileSync(file, "utf8"));
    if (!decoded || !Array.isArray(decoded.clients) || decoded.clients.length === 0) {
      throw new Error("audit clients file must contain a non-empty clients array");
    }
    this.clients = decoded.clients.map(validateClient);
    if (new Set(this.clients.map((client) => client.id)).size !== this.clients.length) {
      throw new Error("audit client IDs must be unique");
    }
    if (new Set(this.clients.map((client) => client.tokenDigest.toString("hex"))).size !== this.clients.length) {
      throw new Error("audit client token digests must be unique");
    }
  }

  authenticate(header) {
    const match = /^Bearer ([A-Za-z0-9._~+\/=\-]{20,512})$/.exec(header || "");
    if (!match) throw new AuthenticationError("bearer authentication is required");
    const digest = createHash("sha256").update(match[1], "utf8").digest();
    let selected = null;
    for (const client of this.clients) {
      if (timingSafeEqual(digest, client.tokenDigest)) selected = client;
    }
    if (!selected) throw new AuthenticationError("invalid bearer token");
    return selected;
  }

  requirePermission(client, permission) {
    if (!client.permissions.has(permission)) {
      throw new AuthenticationError(`client ${client.id} lacks ${permission} permission`, 403, "forbidden");
    }
  }

  authorizeSession(client, sessionId) {
    if (!matchesAny(client.sessions, sessionId)) {
      throw new AuthenticationError(`client ${client.id} is not authorized for this session`, 403, "session_forbidden");
    }
  }

  authorizeEvent(client, eventType) {
    if (!matchesAny(client.eventTypes, eventType)) {
      throw new AuthenticationError(`client ${client.id} is not authorized for this event type`, 403, "event_type_forbidden");
    }
  }
}

function validateClient(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("audit client must be an object");
  const allowed = new Set(["id", "tokenSha256", "permissions", "eventTypes", "sessions"]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new Error(`audit client contains unsupported property ${key}`);
  }
  const id = validateIdentifier(value.id, "audit client ID");
  if (typeof value.tokenSha256 !== "string" || !/^sha256:[a-f0-9]{64}$/.test(value.tokenSha256)) {
    throw new Error(`audit client ${id} tokenSha256 must be a lowercase sha256 digest`);
  }
  if (!Array.isArray(value.permissions) || value.permissions.length === 0) {
    throw new Error(`audit client ${id} must have permissions`);
  }
  const permissions = new Set(value.permissions);
  for (const permission of permissions) {
    if (!PERMISSIONS.has(permission)) throw new Error(`audit client ${id} has unsupported permission ${permission}`);
  }
  const eventTypes = patterns(value.eventTypes, `audit client ${id} eventTypes`);
  const sessions = patterns(value.sessions, `audit client ${id} sessions`);
  return {
    id,
    tokenDigest: Buffer.from(value.tokenSha256.slice("sha256:".length), "hex"),
    permissions,
    eventTypes,
    sessions,
  };
}

function patterns(value, name) {
  if (!Array.isArray(value) || value.length === 0 || value.length > 128) {
    throw new Error(`${name} must be a non-empty array of at most 128 patterns`);
  }
  return value.map((pattern) => {
    if (typeof pattern !== "string" || pattern.length === 0 || pattern.length > 128) {
      throw new Error(`${name} contains an invalid pattern`);
    }
    if (pattern !== "*" && pattern.includes("*") && !pattern.endsWith("*")) {
      throw new Error(`${name} supports only exact values or trailing wildcards`);
    }
    if ((pattern.match(/\*/g) || []).length > 1) throw new Error(`${name} contains an invalid wildcard pattern`);
    return pattern;
  });
}

function matchesAny(patterns, value) {
  return patterns.some((pattern) => pattern === "*" || pattern === value || (pattern.endsWith("*") && value.startsWith(pattern.slice(0, -1))));
}
