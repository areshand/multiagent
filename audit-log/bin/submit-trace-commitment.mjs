#!/usr/bin/env node
import { createHash } from "node:crypto";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { canonicalJson } from "../src/canonical-json.mjs";
import { submitAuditEvent } from "../src/producer-client.mjs";

export async function buildTraceCommitment({ eventId, sessionId, file, storageReference, mediaType = "application/octet-stream" }) {
  if (!eventId || !sessionId || !file || !storageReference) {
    throw new Error("trace commitment requires eventId, sessionId, file, and storageReference");
  }
  const stat = fs.statSync(file);
  if (!stat.isFile()) throw new Error("trace commitment source must be a regular file");
  const hash = createHash("sha256");
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  const artifactDigest = `sha256:${hash.digest("hex")}`;
  const commitment = { artifactDigest, storageReference, size: stat.size, mediaType };
  return {
    eventId,
    sessionId,
    eventType: "trace.artifact_exported",
    payloadDigest: `sha256:${createHash("sha256").update(canonicalJson(commitment)).digest("hex")}`,
    artifactReferences: [{ uri: storageReference, digest: artifactDigest, size: stat.size, mediaType }],
  };
}

function options(args) {
  const result = new Map();
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    const value = args[index + 1];
    if (!key?.startsWith("--") || value === undefined) throw new Error("trace commitment arguments must be --name value pairs");
    if (result.has(key)) throw new Error(`duplicate argument ${key}`);
    result.set(key, value);
  }
  return result;
}

async function main() {
  const values = options(process.argv.slice(2));
  const event = await buildTraceCommitment({
    eventId: values.get("--event-id"),
    sessionId: values.get("--session-id"),
    file: values.get("--file"),
    storageReference: values.get("--storage-reference"),
    mediaType: values.get("--media-type") || "application/octet-stream",
  });
  await submitAuditEvent({
    url: process.env.AUDIT_LOG_URL,
    tokenFile: process.env.AUDIT_LOG_BEARER_TOKEN_FILE,
    event,
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(error.message);
    process.exit(1);
  });
}
