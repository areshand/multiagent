import { runOrganizationSteward } from "./steward.mjs";

const USAGE = `usage: wiki-steward [--trace-root PATH] [--output-root PATH]
                    [--max-files N] [--max-bytes N] [--max-event-bytes N]`;

function integer(value, name, fallback, maximum) {
  if (value === undefined || value === null || value === "") return fallback;
  if (!/^[1-9][0-9]*$/.test(String(value))) throw new Error(`${name} must be a positive integer`);
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed > maximum) throw new Error(`${name} exceeds its hard limit`);
  return parsed;
}

export function parseStewardArguments(arguments_, environment = process.env) {
  const values = new Map();
  const allowed = new Set(["--trace-root", "--output-root", "--max-files", "--max-bytes", "--max-event-bytes"]);
  for (let index = 0; index < arguments_.length; index += 2) {
    const name = arguments_[index];
    const value = arguments_[index + 1];
    if (!allowed.has(name) || value === undefined) throw new Error(USAGE);
    if (values.has(name)) throw new Error(`duplicate option: ${name}`);
    values.set(name, value);
  }
  return {
    traceRoot: values.get("--trace-root") || environment.WIKI_STEWARD_TRACE_ROOT || "/var/lib/wiki-traces",
    outputRoot: values.get("--output-root") || environment.WIKI_STEWARD_OUTPUT_ROOT || "/var/lib/wiki/system/steward",
    maxFiles: integer(values.get("--max-files") || environment.WIKI_STEWARD_MAX_FILES, "max-files", 10_000, 100_000),
    maxBytes: integer(values.get("--max-bytes") || environment.WIKI_STEWARD_MAX_BYTES, "max-bytes", 512 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
    maxEventBytes: integer(values.get("--max-event-bytes") || environment.WIKI_STEWARD_MAX_EVENT_BYTES, "max-event-bytes", 1024 * 1024, 8 * 1024 * 1024),
  };
}

export async function runStewardCli({
  arguments_ = process.argv.slice(2),
  environment = process.env,
  stdout = process.stdout,
} = {}) {
  const result = await runOrganizationSteward(parseStewardArguments(arguments_, environment));
  stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  return result;
}
