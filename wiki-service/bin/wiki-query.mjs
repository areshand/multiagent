#!/usr/bin/env node
import process from "node:process";

import { runWikiQuery } from "../src/query-cli.mjs";

try {
  const output = await runWikiQuery({ arguments_: process.argv.slice(2) });
  if (output.help) process.stdout.write(output.help);
  else process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
