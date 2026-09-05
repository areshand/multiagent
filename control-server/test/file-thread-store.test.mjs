import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createThreadStore } from "../../thread/src/thread-model.mjs";

test("file thread manifests survive gateway restart without duplicating messages", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "multiagent-thread-store-"));
  const filePath = path.join(directory, "thread-manifest-v1.json");
  const first = await createThreadStore({ backend: "file", filePath });
  await first.createThread({ id: "thread-1", ownerSubject: "user-1", repository: "multiagent" });
  const routed = await first.appendUserMessageAndRoute({
    threadId: "thread-1",
    actor: "user-1",
    messageId: "message-1",
    text: "Find the latest pull request",
    newSessionId: "session-1",
  });

  const restored = await createThreadStore({ backend: "file", filePath });
  assert.equal((await restored.getThreadForActor("thread-1", "user-1")).activeSessionId, "session-1");
  assert.deepEqual(await restored.appendUserMessageAndRoute({
    threadId: "thread-1",
    actor: "user-1",
    messageId: "message-1",
    text: "Find the latest pull request",
    newSessionId: "unused-session",
  }), routed);
  assert.equal((await stat(filePath)).mode & 0o777, 0o600);
});

test("file thread manifests fail closed when their schema is corrupt", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "multiagent-thread-store-corrupt-"));
  const filePath = path.join(directory, "thread-manifest-v1.json");
  await writeFile(filePath, JSON.stringify({ schemaVersion: 99 }), "utf8");
  await assert.rejects(createThreadStore({ backend: "file", filePath }), /unsupported thread manifest schema/);
  assert.match(await readFile(filePath, "utf8"), /99/);
});
