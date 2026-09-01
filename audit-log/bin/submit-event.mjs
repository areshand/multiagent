#!/usr/bin/env node
import { submitAuditEvent } from "../src/producer-client.mjs";

const url = process.env.AUDIT_LOG_URL;
const tokenFile = process.env.AUDIT_LOG_BEARER_TOKEN_FILE;
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const body = Buffer.concat(chunks);
try {
  await submitAuditEvent({ url, tokenFile, event: body });
} catch (error) {
  console.error(error.message);
  process.exit(1);
}
