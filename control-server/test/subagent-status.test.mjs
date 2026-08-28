import assert from "node:assert/strict";
import { mkdir, mkdtemp, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { readSubagentSnapshot } from "../src/subagent-status.mjs";

test("subagent snapshots expose bounded structured progress with active agents first", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "multiagent-subagents-"));
  await mkdir(path.join(root, "subagents", "reader"), { recursive: true });
  await mkdir(path.join(root, "subagents", "reviewer"), { recursive: true });
  await mkdir(path.join(root, "assignments", "reader"), { recursive: true });
  await writeFile(path.join(root, "subagents", "reader", "status"), "working\n");
  await writeFile(path.join(root, "subagents", "reader", "meta.env"), "role=investigator\n");
  await writeFile(path.join(root, "subagents", "reader", "current.txt"), "Working...\n\u001b[32mTracing thread delivery\u001b[0m\n");
  await writeFile(path.join(root, "subagents", "reader", "instruction.txt"), "# Inspect delivery semantics\nDetails\n");
  await writeFile(path.join(root, "assignments", "reader", "assignment.env"), "role=fallback-role\n");
  await writeFile(path.join(root, "subagents", "reviewer", "status"), "done\n");
  await writeFile(path.join(root, "subagents", "reviewer", "instruction.txt"), "# Verify tests\n");

  const agents = readSubagentSnapshot(root);
  assert.equal(agents.length, 2);
  assert.deepEqual(agents[0], {
    name: "reader",
    status: "working",
    role: "investigator",
    workingOn: "Tracing thread delivery",
    assignment: "Inspect delivery semantics",
    updatedAt: agents[0].updatedAt,
  });
  assert.match(agents[0].updatedAt, /^\d{4}-\d{2}-\d{2}T/);
  assert.equal(agents[1].name, "reviewer");
  assert.equal(agents[1].status, "done");
  assert.equal(agents[1].workingOn, "Verify tests");
});

test("missing subagent state produces an empty snapshot", () => {
  assert.deepEqual(readSubagentSnapshot("/path/that/does/not/exist"), []);
});

test("provider JSON and terminal markers fall back to the explicit task assignment", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "multiagent-subagent-provider-status-"));
  await mkdir(path.join(root, "subagents", "acceptance-reader"), { recursive: true });
  await writeFile(path.join(root, "subagents", "acceptance-reader", "status"), "done\n");
  await writeFile(path.join(root, "subagents", "acceptance-reader", "instruction.txt"), [
    "# Contract Scout Role Prompt",
    "",
    "## Task Assignment",
    "",
    "Inspect the repository HEAD without modifying files.",
    "",
    "## Mandatory Final Artifact Contract",
    "",
    "Internal details",
  ].join("\n"));
  await writeFile(path.join(root, "subagents", "acceptance-reader", "current.txt"), [
    JSON.stringify({ type: "result", result: "long provider result", uuid: "provider-id" }),
    "final status: coding agent exited rc=0",
  ].join("\n"));

  const [agent] = readSubagentSnapshot(root);
  assert.equal(agent.assignment, "Inspect the repository HEAD without modifying files.");
  assert.equal(agent.workingOn, "Inspect the repository HEAD without modifying files.");
  assert.doesNotMatch(agent.workingOn, /provider|uuid|final status/);
});
