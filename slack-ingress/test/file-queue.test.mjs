import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { SlackEventFileQueue } from "../src/file-queue.mjs";

test("file queue deduplicates events and retains failures for retry", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "slack-queue-"));
  const queue = new SlackEventFileQueue(root);
  const event = { eventId: "Ev123", text: "alert" };
  assert.deepEqual(await queue.enqueue(event), { queued: true, duplicate: false });
  assert.deepEqual(await queue.enqueue(event), { queued: false, duplicate: true });
  assert.equal(await queue.size(), 1);

  const failed = await queue.drainOne(async () => { throw new Error("offline"); });
  assert.equal(failed.delivered, false);
  assert.match(failed.error.message, /offline/);
  assert.equal(await queue.size(), 1);

  const delivered = [];
  assert.equal((await queue.drainOne(async (value) => delivered.push(value))).delivered, true);
  assert.deepEqual(delivered, [event]);
  assert.equal(await queue.size(), 0);
});
