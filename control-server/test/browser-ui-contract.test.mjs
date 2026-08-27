import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
const page = await readFile(new URL("../public/index.html", import.meta.url), "utf8");

test("browser thread creation uses the durable thread API", () => {
  assert.match(app, /api\("\/api\/threads", \{/);
  assert.match(app, /api\(`\/api\/threads\/\$\{threadId\}\/messages`/);
  assert.match(app, /await selectThread\(thread\.id\)/);
  assert.doesNotMatch(app, /selectSession\(body\.id\)/);
});

test("browser labels the durable conversation as a thread", () => {
  for (const label of ["Thread Control", "Threads", "New thread", "Thread ID", "Initial message", "Select a thread"]) {
    assert.match(page, new RegExp(label));
  }
  for (const staleLabel of ["Task Control", ">Tasks<", "New task", "Task ID", "Original task", "Select a task"]) {
    assert.doesNotMatch(page, new RegExp(staleLabel));
  }
});

test("browser keeps legacy sessions addressable without treating them as threads", () => {
  assert.match(app, /function legacySessions\(\)/);
  assert.match(app, /selectLegacySession\(id\)/);
  assert.match(app, /api\(`\/api\/sessions\/\$\{id\}\/report`\)/);
  assert.doesNotMatch(app, /api\/sessions\/\$\{id\}\/terminal/);
});
