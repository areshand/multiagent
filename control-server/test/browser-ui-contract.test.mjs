import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");

test("browser task creation uses the durable thread API", () => {
  assert.match(app, /api\("\/api\/threads", \{/);
  assert.match(app, /api\(`\/api\/threads\/\$\{threadId\}\/messages`/);
  assert.match(app, /await selectThread\(thread\.id\)/);
  assert.doesNotMatch(app, /selectSession\(body\.id\)/);
});

test("browser keeps legacy sessions addressable without treating them as threads", () => {
  assert.match(app, /function legacySessions\(\)/);
  assert.match(app, /selectLegacySession\(id\)/);
  assert.match(app, /api\(`\/api\/sessions\/\$\{id\}\/report`\)/);
  assert.doesNotMatch(app, /api\/sessions\/\$\{id\}\/terminal/);
});
