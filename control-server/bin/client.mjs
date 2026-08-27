#!/usr/bin/env node
import { ClientError, main } from "../src/client.mjs";

main().catch((error) => {
  const prefix = error instanceof ClientError ? "client error" : "unexpected error";
  console.error(`${prefix}: ${error.message}`);
  if (error.body) console.error(JSON.stringify(error.body, null, 2));
  process.exitCode = 1;
});
