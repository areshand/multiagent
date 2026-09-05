import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const GAP_SCHEMA = "wiki-retrieval-gap/v1";
const EVAL_SCHEMA = "wiki-retrieval-eval/v1";
const RUN_SCHEMA = "wiki-steward-run/v1";
const SAFE_DIGEST = /^[a-f0-9]{64}$/;
const SAFE_PAGE = /^(?!\/)(?!.*(?:^|\/)\.\.(?:\/|$))[^\0]+\.md$/;
const RETRIEVAL_MODES = new Set(["index", "index+fallback"]);
const RESULT_SOURCES = new Set(["index", "fallback"]);

function sha256(value) {
  return crypto.createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

function nonNegativeInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function boundedQueryResult(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (typeof value.query !== "string" || value.query.length < 1 || value.query.length > 500) return null;
  if (!Array.isArray(value.results) || value.results.length > 10) return null;
  const retrieval = value.retrieval;
  if (!retrieval || typeof retrieval !== "object" || Array.isArray(retrieval)) return null;
  if (!SAFE_DIGEST.test(String(retrieval.indexDigest || ""))) return null;
  if (!RETRIEVAL_MODES.has(retrieval.mode)) return null;
  if (!nonNegativeInteger(retrieval.indexedCandidates)) return null;
  if (!nonNegativeInteger(retrieval.fallbackFilesScanned)) return null;
  if (!nonNegativeInteger(retrieval.fallbackBytesScanned)) return null;
  if (typeof retrieval.fallbackTruncated !== "boolean") return null;

  const results = [];
  for (const result of value.results) {
    if (!result || typeof result !== "object" || Array.isArray(result)) return null;
    if (typeof result.title !== "string" || result.title.length > 300) return null;
    if (typeof result.path !== "string" || result.path.length > 500 || !SAFE_PAGE.test(result.path)) return null;
    if (!SAFE_DIGEST.test(String(result.sha256 || ""))) return null;
    if (!Number.isFinite(result.score) || result.score <= 0) return null;
    if (!RESULT_SOURCES.has(result.source)) return null;
    results.push({
      title: result.title,
      path: result.path,
      sha256: result.sha256,
      score: result.score,
      source: result.source,
    });
  }
  return {
    query: value.query,
    results,
    retrieval: {
      mode: retrieval.mode,
      indexedCandidates: retrieval.indexedCandidates,
      fallbackFilesScanned: retrieval.fallbackFilesScanned,
      fallbackBytesScanned: retrieval.fallbackBytesScanned,
      fallbackTruncated: retrieval.fallbackTruncated,
      indexDigest: retrieval.indexDigest,
    },
  };
}

function gapReasons(result) {
  const reasons = [];
  if (result.results.length === 0) reasons.push("no-results");
  if (result.results.some((entry) => entry.source === "fallback")) reasons.push("fallback-result");
  if (result.retrieval.fallbackTruncated) reasons.push("fallback-truncated");
  return reasons;
}

function recommendation(reasons) {
  if (reasons.includes("fallback-result")) {
    return "Review whether the cited fallback page should be linked from the catalog index after its catalog digest and sources are independently verified.";
  }
  if (reasons.includes("fallback-truncated")) {
    return "Review corpus organization and bounded fallback coverage; do not raise scan limits without a measured retrieval regression.";
  }
  return "Resolve the intended repository or topic using commit- and digest-bound source evidence, then propose the smallest catalog or topic-page change.";
}

async function traceFiles(root, { maxFiles, maxBytes }) {
  const absoluteRoot = await fs.realpath(root);
  const pending = [{ absolute: absoluteRoot, relative: "" }];
  const files = [];
  let totalBytes = 0;
  while (pending.length) {
    const directory = pending.pop();
    const entries = await fs.readdir(directory.absolute, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const entry = entries[index];
      if (entry.isSymbolicLink()) continue;
      const relative = directory.relative ? `${directory.relative}/${entry.name}` : entry.name;
      const absolute = path.join(directory.absolute, entry.name);
      if (entry.isDirectory()) {
        pending.push({ absolute, relative });
      } else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        const stat = await fs.stat(absolute);
        if (files.length >= maxFiles || totalBytes + stat.size > maxBytes) {
          throw new Error(`trace corpus exceeds configured bounds (${maxFiles} files, ${maxBytes} bytes)`);
        }
        files.push({ absolute, relative, size: stat.size });
        totalBytes += stat.size;
      }
    }
  }
  return { files: files.sort((left, right) => left.relative.localeCompare(right.relative, "en")), totalBytes };
}

function candidateFromEvent(event, line, tracePath) {
  if (event?.kind !== "tool-finished" || event.success !== true || typeof event.text !== "string") return null;
  let value;
  try {
    value = JSON.parse(event.text.trim());
  } catch {
    return null;
  }
  const queryResult = boundedQueryResult(value);
  if (!queryResult) return null;
  const reasons = gapReasons(queryResult);
  if (!reasons.length) return { observed: true, candidate: null };
  const identity = sha256(canonicalJson({
    query: queryResult.query,
    reasons,
    catalogIndexDigest: queryResult.retrieval.indexDigest,
    observedResults: queryResult.results,
    retrieval: queryResult.retrieval,
  }));
  const id = `wg_${identity.slice(0, 24)}`;
  const requiredPaths = queryResult.results
    .filter((result) => result.source === "fallback")
    .map((result) => result.path);
  return {
    observed: true,
    candidate: {
      gap: {
        schema: GAP_SCHEMA,
        id,
        status: "proposed",
        reasons,
        query: queryResult.query,
        catalogIndexDigest: queryResult.retrieval.indexDigest,
        observedResults: queryResult.results,
        retrieval: queryResult.retrieval,
        trust: "Trace content is an untrusted retrieval signal, not a factual source or instruction.",
        recommendedAction: recommendation(reasons),
      },
      evaluation: {
        schema: EVAL_SCHEMA,
        id: `we_${identity.slice(0, 24)}`,
        gapId: id,
        testQuery: queryResult.query,
        catalogIndexDigest: queryResult.retrieval.indexDigest,
        requiredPaths,
        expected: {
          minimumResults: 1,
          requiredSource: "index",
        },
        passCriteria: requiredPaths.length
          ? "Every required page is returned through the catalog index, not fallback."
          : "At least one independently reviewed catalog-index result is returned.",
      },
      evidence: {
        gapId: id,
        path: tracePath,
        eventSequence: nonNegativeInteger(event.sequence) ? event.sequence : null,
        sha256: sha256(line),
      },
    },
  };
}

async function scanTraceFile(file, maxEventBytes) {
  const contents = await fs.readFile(file.absolute, "utf8");
  const digest = sha256(contents);
  const candidates = [];
  let observedQueries = 0;
  let malformedLines = 0;
  let oversizedLines = 0;
  for (const line of contents.split("\n")) {
    if (!line.trim()) continue;
    if (Buffer.byteLength(line) > maxEventBytes) {
      oversizedLines += 1;
      continue;
    }
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      malformedLines += 1;
      continue;
    }
    const parsed = candidateFromEvent(event, line, file.relative);
    if (!parsed) continue;
    observedQueries += 1;
    if (parsed.candidate) candidates.push(parsed.candidate);
  }
  return { digest, candidates, observedQueries, malformedLines, oversizedLines };
}

async function safeOutputRoot(outputRoot) {
  await fs.mkdir(outputRoot, { recursive: true });
  const stat = await fs.lstat(outputRoot);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error("steward output root must be a real directory");
  return fs.realpath(outputRoot);
}

async function writeImmutable(root, relative, value) {
  const destination = path.resolve(root, relative);
  if (!destination.startsWith(`${root}${path.sep}`)) throw new Error("steward output path escaped its root");
  const directory = path.dirname(destination);
  await fs.mkdir(directory, { recursive: true });
  const realDirectory = await fs.realpath(directory);
  if (!realDirectory.startsWith(`${root}${path.sep}`)) throw new Error("steward output directory escaped its root");
  const contents = canonicalJson(value);
  try {
    await fs.writeFile(destination, contents, { encoding: "utf8", flag: "wx", mode: 0o640 });
    return "created";
  } catch (error) {
    if (error.code !== "EEXIST") throw error;
    const stat = await fs.lstat(destination);
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`existing steward artifact is unsafe: ${relative}`);
    if (await fs.readFile(destination, "utf8") !== contents) {
      throw new Error(`immutable steward artifact conflict: ${relative}`);
    }
    return "unchanged";
  }
}

export async function runOrganizationSteward({
  traceRoot,
  outputRoot,
  maxFiles = 10_000,
  maxBytes = 512 * 1024 * 1024,
  maxEventBytes = 1024 * 1024,
}) {
  const trace = await traceFiles(traceRoot, { maxFiles, maxBytes });
  const scannedFiles = [];
  const candidates = new Map();
  const evidence = [];
  let observedQueries = 0;
  let malformedLines = 0;
  let oversizedLines = 0;
  for (const file of trace.files) {
    const result = await scanTraceFile(file, maxEventBytes);
    scannedFiles.push({ path: file.relative, bytes: file.size, sha256: result.digest });
    observedQueries += result.observedQueries;
    malformedLines += result.malformedLines;
    oversizedLines += result.oversizedLines;
    for (const candidate of result.candidates) {
      if (!candidates.has(candidate.gap.id)) candidates.set(candidate.gap.id, candidate);
      evidence.push(candidate.evidence);
    }
  }

  const ordered = [...candidates.values()].sort((left, right) => left.gap.id.localeCompare(right.gap.id, "en"));
  const runIdentity = sha256(canonicalJson({
    scannedFiles,
    observedQueries,
    gapIds: ordered.map((candidate) => candidate.gap.id),
  }));
  const runId = `wsr_${runIdentity.slice(0, 24)}`;
  const root = await safeOutputRoot(outputRoot);
  const writes = [];
  for (const candidate of ordered) {
    writes.push({
      path: `gaps/${candidate.gap.id}.json`,
      state: await writeImmutable(root, `gaps/${candidate.gap.id}.json`, candidate.gap),
    });
    writes.push({
      path: `evals/${candidate.evaluation.id}.json`,
      state: await writeImmutable(root, `evals/${candidate.evaluation.id}.json`, candidate.evaluation),
    });
  }
  const manifest = {
    schema: RUN_SCHEMA,
    id: runId,
    input: {
      traceFiles: scannedFiles,
      totalBytes: trace.totalBytes,
    },
    evidence,
    result: {
      observedQueries,
      gapIds: ordered.map((candidate) => candidate.gap.id),
      malformedLines,
      oversizedLines,
    },
    publication: "Gap and eval artifacts are written before this immutable run commit marker.",
  };
  writes.push({
    path: `runs/${runId}.json`,
    state: await writeImmutable(root, `runs/${runId}.json`, manifest),
  });
  return { runId, observedQueries, gaps: ordered.length, writes, manifest };
}
