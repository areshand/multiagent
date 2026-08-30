import assert from "node:assert/strict";
import test from "node:test";
import { visibleLegacySessionIds } from "../src/session-visibility.mjs";

test("legacy session listing cannot reveal thread-backed executions", async () => {
  const records = {
    "legacy-1": { id: "legacy-1", createdBy: "operator", threadId: "legacy-1" },
    "session-thread-1": { id: "session-thread-1", createdBy: "operator", threadId: "thread-1" },
    "other-user": { id: "other-user", createdBy: "someone-else", threadId: "legacy-2" },
  };
  const visible = await visibleLegacySessionIds({
    records,
    username: "operator",
    hasThread: async (threadId) => threadId === "thread-1",
  });
  assert.deepEqual(visible, ["legacy-1"]);
});
