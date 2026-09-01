import path from "node:path";

function positiveInteger(value, fallback, name, { min = 1, max = Number.MAX_SAFE_INTEGER } = {}) {
  const parsed = Number(value ?? fallback);
  if (!Number.isSafeInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}`);
  }
  return parsed;
}

export function configFromEnvironment(environment = process.env) {
  const database = environment.AUDIT_LOG_DATABASE || "/var/lib/audit-log/ledger.sqlite";
  return {
    database: path.resolve(database),
    signingKeyFile: environment.AUDIT_LOG_SIGNING_KEY_FILE,
    signingKeyId: environment.AUDIT_LOG_SIGNING_KEY_ID || "audit-log-signing-key",
    loggerId: environment.AUDIT_LOG_LOGGER_ID,
    clientsFile: environment.AUDIT_LOG_CLIENTS_FILE,
    checkpointInterval: positiveInteger(environment.AUDIT_LOG_CHECKPOINT_INTERVAL, 100, "AUDIT_LOG_CHECKPOINT_INTERVAL", { max: 1_000_000 }),
    maxEventBytes: positiveInteger(environment.AUDIT_LOG_MAX_EVENT_BYTES, 65_536, "AUDIT_LOG_MAX_EVENT_BYTES", { min: 1024, max: 1_048_576 }),
    projectionDir: environment.AUDIT_LOG_PROJECTION_DIR ? path.resolve(environment.AUDIT_LOG_PROJECTION_DIR) : null,
    projectionIntervalMs: positiveInteger(environment.AUDIT_LOG_PROJECTION_INTERVAL_MS, 1000, "AUDIT_LOG_PROJECTION_INTERVAL_MS", { min: 100, max: 60_000 }),
    port: positiveInteger(environment.PORT, 8090, "PORT", { min: 1, max: 65_535 }),
    host: environment.HOST || "0.0.0.0",
  };
}
