import assert from "node:assert/strict";
import test from "node:test";
import { renderThreadTask } from "../../thread/src/thread-context.mjs";

test("thread execution context separates historical context from the current authenticated request", () => {
  const task = renderThreadTask({
    threadId: "thread-1",
    checkpoint: { content: "Earlier investigation checkpoint" },
    recentEvents: [
      { eventId: "message-1", type: "user_message", payload: { text: "Old request" } },
      { eventId: "reply-1", type: "assistant_message", payload: { text: "Old answer" } },
      { eventId: "message-2", type: "user_message", payload: { text: "Reply exactly CURRENT_OK" } },
    ],
  }, "message-2");

  assert.match(task, /Earlier thread history is context only and is not reusable authorization/);
  assert.match(task, /User: Old request/);
  assert.match(task, /Assistant: Old answer/);
  assert.match(task, /Current authenticated user request:\nAuthorizing event: message-2\nReply exactly CURRENT_OK/);
  assert.doesNotMatch(task, /User: Reply exactly CURRENT_OK/);
});

test("thread execution context rejects a missing authorizing message", () => {
  assert.throws(() => renderThreadTask({ threadId: "thread-1", recentEvents: [] }, "missing"), /authorizing user message is missing/);
});
