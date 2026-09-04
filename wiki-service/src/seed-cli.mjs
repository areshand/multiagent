import path from "node:path";

import { loadConfig } from "./config.mjs";
import { readSeedManifest, seedCatalog } from "./seed.mjs";

function usage() {
  return "usage: wiki-seed --manifest PATH\n";
}

export function parseSeedArguments(arguments_) {
  let manifestPath = null;
  for (let index = 0; index < arguments_.length; index += 1) {
    if (arguments_[index] === "--manifest" && arguments_[index + 1] !== undefined) {
      manifestPath = path.resolve(arguments_[index + 1]);
      index += 1;
    } else if (arguments_[index] === "--help" || arguments_[index] === "-h") {
      return Object.freeze({ help: true });
    } else {
      throw new Error(`unknown or incomplete argument: ${arguments_[index]}\n${usage()}`);
    }
  }
  if (!manifestPath) throw new Error(`--manifest is required\n${usage()}`);
  return Object.freeze({ manifestPath });
}

export async function runWikiSeed({ arguments_, environment = process.env }) {
  const parsed = parseSeedArguments(arguments_);
  if (parsed.help) return { help: usage() };
  const config = loadConfig(environment);
  const manifest = await readSeedManifest(parsed.manifestPath);
  return seedCatalog(config.root, manifest);
}

export { usage };
