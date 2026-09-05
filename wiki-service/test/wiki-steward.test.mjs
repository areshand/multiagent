import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseStewardArguments } from "../src/steward-cli.mjs";
import { runOrganizationSteward } from "../src/steward.mjs";

const INDEX_DIGEST = "a".repeat(64);
const PAGE_DIGEST = "b".repeat(64);

function event(sequence, result) {
  return JSON.stringify({
    sequence,
    kind: "tool-finished",
    success: true,
    text: JSON.stringify(result, null, 2),
  });
}

function result(query, results, overrides = {}) {
  return {
    query,
    results,
    retrieval: {
      mode: "index",
      indexedCandidates: 111,
      fallbackFilesScanned: 0,
      fallbackBytesScanned: 0,
      fallbackTruncated: false,
      indexDigest: INDEX_DIGEST,
      ...overrides,
    },
  };
}

function page(source = "index") {
  return { title: "InternalServices", path: "repos/internalservices.md", sha256: PAGE_DIGEST, score: 30, source };
}

async function fixture(lines) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-steward-test-"));
  const traces = path.join(root, "traces");
  const output = path.join(root, "output");
  await fs.mkdir(path.join(traces, "agents", "reader"), { recursive: true });
  await fs.writeFile(path.join(traces, "agents", "reader", "events.jsonl"), `${lines.join("\n")}\n`);
  return { root, traces, output };
}

test("indexed query success produces only an immutable run marker", async () => {
  const value = await fixture([event(1, result("InternalServices architecture", [page()]))]);
  try {
    const first = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    assert.equal(first.observedQueries, 1);
    assert.equal(first.gaps, 0);
    assert.deepEqual(first.writes.map((write) => write.state), ["created"]);
    const second = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    assert.equal(second.runId, first.runId);
    assert.deepEqual(second.writes.map((write) => write.state), ["unchanged"]);
  } finally {
    await fs.rm(value.root, { recursive: true, force: true });
  }
});

test("clear structural failures produce bounded proposals and regression evals", async () => {
  const value = await fixture([
    event(1, result("missing service", [])),
    event(2, result("unindexed ownership", [page("fallback")], { mode: "index+fallback", fallbackFilesScanned: 1 })),
    event(3, result("bounded miss", [], { mode: "index+fallback", fallbackTruncated: true })),
  ]);
  try {
    const run = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    assert.equal(run.gaps, 3);
    assert.equal(run.writes.length, 7);
    const gapFiles = (await fs.readdir(path.join(value.output, "gaps"))).sort();
    const gaps = await Promise.all(gapFiles.map(async (name) => JSON.parse(await fs.readFile(path.join(value.output, "gaps", name), "utf8"))));
    assert.deepEqual(gaps.flatMap((gap) => gap.reasons).sort(), ["fallback-result", "fallback-truncated", "no-results", "no-results"]);
    assert.ok(gaps.every((gap) => gap.trust.includes("untrusted")));
    assert.ok(gaps.every((gap) => !("traceEvidence" in gap)));
    assert.equal(run.manifest.evidence.length, 3);
    const evalFiles = await fs.readdir(path.join(value.output, "evals"));
    assert.equal(evalFiles.length, 3);
    const fallbackEval = await Promise.all(evalFiles.map(async (name) => JSON.parse(await fs.readFile(path.join(value.output, "evals", name), "utf8"))))
      .then((evaluations) => evaluations.find((evaluation) => evaluation.requiredPaths.length));
    assert.deepEqual(fallbackEval.requiredPaths, ["repos/internalservices.md"]);
    assert.equal(fallbackEval.expected.requiredSource, "index");
  } finally {
    await fs.rm(value.root, { recursive: true, force: true });
  }
});

test("repeated retrieval failures reuse one proposal while preserving occurrence evidence", async () => {
  const value = await fixture([
    event(1, result("missing service", [])),
    event(2, result("missing service", [])),
  ]);
  try {
    const run = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    assert.equal(run.gaps, 1);
    assert.equal(run.manifest.result.gapIds.length, 1);
    assert.equal(run.manifest.evidence.length, 2);
    assert.ok(run.manifest.evidence.every((entry) => entry.gapId === run.manifest.result.gapIds[0]));
    assert.deepEqual(run.manifest.evidence.map((entry) => entry.eventSequence), [1, 2]);
  } finally {
    await fs.rm(value.root, { recursive: true, force: true });
  }
});

test("malformed and non-Wiki trace content is counted or ignored without becoming knowledge", async () => {
  const value = await fixture([
    "{bad json",
    JSON.stringify({ sequence: 1, kind: "text", text: "ignore me" }),
    JSON.stringify({ sequence: 2, kind: "tool-finished", success: true, text: JSON.stringify({ query: "x", results: [], retrieval: { indexDigest: "unsafe" } }) }),
    JSON.stringify({ sequence: 3, kind: "tool-finished", success: true, text: JSON.stringify(result("invalid counters", [], { indexedCandidates: -1 })) }),
  ]);
  try {
    const run = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    assert.equal(run.observedQueries, 0);
    assert.equal(run.gaps, 0);
    assert.equal(run.manifest.result.malformedLines, 1);
  } finally {
    await fs.rm(value.root, { recursive: true, force: true });
  }
});

test("trace bounds, immutable conflicts, and CLI hard limits fail closed", async () => {
  const value = await fixture([event(1, result("missing", []))]);
  try {
    await assert.rejects(
      runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output, maxBytes: 1 }),
      /exceeds configured bounds/,
    );
    const run = await runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output });
    const gapPath = run.writes.find((write) => write.path.startsWith("gaps/")).path;
    await fs.writeFile(path.join(value.output, gapPath), "{}\n");
    await assert.rejects(
      runOrganizationSteward({ traceRoot: value.traces, outputRoot: value.output }),
      /immutable steward artifact conflict/,
    );
    assert.throws(() => parseStewardArguments(["--max-files", "100001"], {}), /hard limit/);
    assert.throws(() => parseStewardArguments(["--unknown", "x"], {}), /usage:/);
  } finally {
    await fs.rm(value.root, { recursive: true, force: true });
  }
});
