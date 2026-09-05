#!/usr/bin/env node

import { runStewardCli } from "../src/steward-cli.mjs";

runStewardCli().catch((error) => {
  console.error(`wiki-steward: ${error.message}`);
  process.exitCode = 1;
});
