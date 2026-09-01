#!/usr/bin/env node
import { createHash } from "node:crypto";

const token = process.argv[2];
if (!token || token.length < 20) {
  console.error("usage: hash-token.mjs TOKEN (minimum 20 characters)");
  process.exit(64);
}
console.log(`sha256:${createHash("sha256").update(token, "utf8").digest("hex")}`);
