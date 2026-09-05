#!/usr/bin/env node
import process from "node:process";

import { runWikiSeed } from "../src/seed-cli.mjs";

try {
  const output = await runWikiSeed({ arguments_: process.argv.slice(2) });
  if (output.help) process.stdout.write(output.help);
  else process.stdout.write(`${JSON.stringify(output, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`${error.message}\n`);
  process.exitCode = 1;
}
