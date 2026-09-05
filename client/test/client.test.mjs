import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  compactOutcomeSummary,
  ControlClient,
  finalAgentMessageText,
  main,
  paneOutcomeForEvent,
  renderAgentPane,
  terminalDelta,
  terminalProgressView,
} from "../src/client.mjs";

function writer() {
  return { output: "", write(value) { this.output += String(value); } };
}

function ttyWriter({ rows = 24, columns = 100 } = {}) {
  return { ...writer(), isTTY: true, rows, columns };
}

function jsonResponse(value, init = {}) {
  return new Response(JSON.stringify(value), {
    status: init.status || 200,
    headers: { "content-type": "application/json", ...(init.headers || {}) },
  });
}

async function sessionFixture({ threadIds = ["thread-1"] } = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "multiagent-client-"));
  const file = path.join(directory, "session.json");
  await writeFile(file, JSON.stringify({
    server: "https://control.example/",
    cookie: "multiagent_session=signed-cookie",
    username: "operator",
  }), { mode: 0o600 });
  await writeFile(`${file}.threads.json`, JSON.stringify({
    schemaVersion: 1,
    profiles: [{ server: "https://control.example/", username: "operator", threadIds }],
  }), { mode: 0o600 });
  return file;
}

function terminalSocketFactory(outputs = []) {
  return (url) => {
    const socket = new EventEmitter();
    let closed = false;
    socket.close = () => {
      if (closed) return;
      closed = true;
      queueMicrotask(() => socket.emit("close"));
    };
    queueMicrotask(() => {
      if (String(url).includes("/stream")) {
        socket.emit("message", Buffer.from(JSON.stringify({ type: "heartbeat" })));
      } else {
        for (const output of outputs) socket.emit("message", Buffer.from(JSON.stringify({ type: "output", output, live: true })));
      }
    });
    return socket;
  };
}

function retryingTerminalSocketFactory() {
  const state = { attempts: 0 };
  state.factory = (url) => {
    const isThread = String(url).includes("/stream");
    if (!isThread) state.attempts += 1;
    const attempt = state.attempts;
    const socket = new EventEmitter();
    let closed = false;
    socket.close = () => {
      if (closed) return;
      closed = true;
      queueMicrotask(() => socket.emit("close"));
    };
    queueMicrotask(() => {
      if (isThread) {
        socket.emit("message", Buffer.from(JSON.stringify({ type: "heartbeat" })));
      } else if (attempt === 1) {
        socket.emit("message", Buffer.from(JSON.stringify({ type: "error", error: "session worker is not ready" })));
      } else {
        socket.emit("message", Buffer.from(JSON.stringify({ type: "output", output: "Worker connected\nInvestigating", live: true })));
      }
    });
    return socket;
  };
  return state;
}

test("client login stores only the scoped session cookie with mode 0600", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "multiagent-client-login-"));
  const sessionFile = path.join(directory, "nested", "session.json");
  const output = writer();
  let request = null;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "login", "operator",
  ], {
    stdout: output,
    readPassword: async () => "secret-password",
    fetchImpl: async (url, options) => {
      request = { url: String(url), options };
      return jsonResponse({ username: "operator" }, { headers: { "set-cookie": "multiagent_session=signed-cookie; HttpOnly; Secure" } });
    },
  });
  assert.equal(request.url, "https://control.example/api/login");
  assert.deepEqual(JSON.parse(request.options.body), { username: "operator", password: "secret-password" });
  assert.equal((await stat(sessionFile)).mode & 0o777, 0o600);
  assert.deepEqual(JSON.parse(await readFile(sessionFile, "utf8")), {
    server: "https://control.example/",
    cookie: "multiagent_session=signed-cookie",
    username: "operator",
  });
});

test("users list only locally created threads through individually authorized lookups", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  let cookie = "";
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "threads", "list",
  ], {
    stdout: output,
    fetchImpl: async (url, options) => {
      assert.equal(String(url), "https://control.example/api/threads/thread-1");
      cookie = options.headers.cookie;
      return jsonResponse({ thread: { id: "thread-1", state: "idle", repository: "multiagent" } });
    },
  });
  assert.equal(cookie, "multiagent_session=signed-cookie");
  assert.deepEqual(JSON.parse(output.output), [{ id: "thread-1", state: "idle", repository: "multiagent" }]);
});

test("thread creation lets the server generate both the Thread and Session IDs", async () => {
  const sessionFile = await sessionFixture({ threadIds: [] });
  const output = writer();
  const requests = [];
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
    "threads", "create", "--repository", "multiagent", "--message", "Investigate the incident",
  ], {
    stdout: output,
    fetchImpl: async (url, options) => {
      requests.push({ url: String(url), options });
      if (String(url).endsWith("/api/threads")) return jsonResponse({ thread: { id: "thread-1", repository: "multiagent" } }, { status: 201 });
      return jsonResponse({ createdSession: true, session: { id: "thread-1-generated-session", status: "running" } }, { status: 202 });
    },
  });
  assert.equal(requests.length, 2);
  assert.deepEqual(JSON.parse(requests[0].options.body), { repository: "multiagent", title: "" });
  assert.equal(requests[1].url, "https://control.example/api/threads/thread-1/messages");
  assert.deepEqual(JSON.parse(requests[1].options.body), { text: "Investigate the incident" });
  assert.ok(requests[1].options.headers["idempotency-key"]);
  assert.equal(JSON.parse(output.output).route.session.id, "thread-1-generated-session");
  const index = JSON.parse(await readFile(`${sessionFile}.threads.json`, "utf8"));
  assert.deepEqual(index.profiles[0].threadIds, ["thread-1"]);
  assert.equal((await stat(`${sessionFile}.threads.json`)).mode & 0o777, 0o600);
});

test("thread show and one-shot watch expose history and execution state as JSON", async () => {
  const sessionFile = await sessionFixture();
  const showOutput = writer();
  const fetchImpl = async (url) => {
    const value = String(url);
    if (value.endsWith("/api/threads/thread-1")) return jsonResponse({ thread: { id: "thread-1", state: "running" } });
    if (value.includes("/events")) return jsonResponse({ events: [{ sequence: 3, type: "progress", payload: { text: "working" } }] });
    if (value.endsWith("/sessions")) return jsonResponse({ sessions: [{ id: "session-1", status: "running" }] });
    throw new Error(`unexpected request: ${value}`);
  };
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "threads", "show", "thread-1",
  ], { stdout: showOutput, fetchImpl });
  assert.equal(JSON.parse(showOutput.output).sessions[0].id, "session-1");

  const watchOutput = writer();
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "threads", "watch", "thread-1", "--once",
  ], { stdout: watchOutput, fetchImpl });
  assert.deepEqual(JSON.parse(watchOutput.output), { sequence: 3, type: "progress", payload: { text: "working" } });
});

test("client refuses to send authentication over non-local plaintext HTTP", () => {
  assert.throws(() => new ControlClient({ server: "http://control.example" }), /must use HTTPS/);
  assert.doesNotThrow(() => new ControlClient({ server: "http://127.0.0.1:8080" }));
});

test("interactive terminal lists, opens, and continues durable threads", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  const answers = ["/list", "/open missing", "/open 1", "Continue the investigation", "/wait", "/quit"];
  const requests = [];
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => {},
    createInterface: () => ({ question: async () => answers.shift(), close() {} }),
    createWebSocket: terminalSocketFactory(["Planning", "Planning\nDelegating"]),
    fetchImpl: async (url, options) => {
      const value = String(url);
      requests.push({ url: value, options });
      if (value.endsWith("/api/threads")) {
        if (options.method === "POST") throw new Error("unexpected thread creation");
        return jsonResponse({ threads: [{ id: "thread-1", title: "Incident", state: "idle", repository: "multiagent" }] });
      }
      if (value.endsWith("/api/threads/thread-1")) {
        return jsonResponse({ thread: { id: "thread-1", title: "Incident", state: "idle", repository: "multiagent" } });
      }
      if (value.endsWith("/api/threads/missing")) return jsonResponse({ error: "thread not found" }, { status: 404 });
      if (value.includes("after_sequence=0")) return jsonResponse({ events: [] });
      if (value.endsWith("/api/threads/thread-1/messages")) {
        assert.deepEqual(JSON.parse(options.body), { text: "Continue the investigation" });
        return jsonResponse({
          event: { sequence: 1, type: "user_message", payload: { text: "Continue the investigation" } },
          session: { id: "thread-1-generated-session", status: "running" },
        }, { status: 202 });
      }
      if (value.includes("after_sequence=1")) {
        return jsonResponse({ events: [{ sequence: 2, type: "assistant_message", payload: { text: "Investigation complete" } }] });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.match(output.output, /Threads/);
  assert.match(output.output, /1\. thread-1 — Incident/);
  assert.match(output.output, /\[error\] thread not found/);
  assert.match(output.output, /Opened thread-1/);
  assert.match(output.output, /\[orchestrator thread-1-generated-session\]/);
  assert.match(output.output, /Planning\nDelegating/);
  assert.match(output.output, /assistant> Investigation complete/);
  assert.ok(requests.some((request) => request.url.endsWith("/api/threads/thread-1/messages")));
  assert.deepEqual(JSON.parse(await readFile(`${sessionFile}.threads.json`, "utf8")).profiles[0].threadIds, ["thread-1"]);
});

test("interactive new asks only for a repository and streams its first execution", async () => {
  const sessionFile = await sessionFixture({ threadIds: [] });
  const output = writer();
  const answers = ["/new multiagent Incident triage", "Investigate now", "/wait", "/quit"];
  let created = null;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => {},
    createInterface: () => ({ question: async () => answers.shift(), close() {} }),
    createWebSocket: terminalSocketFactory(["Starting orchestrator", "Starting orchestrator\nReader assigned"]),
    fetchImpl: async (url, options) => {
      const value = String(url);
      if (value.endsWith("/api/threads") && options.method === "POST") {
        assert.deepEqual(JSON.parse(options.body), { repository: "multiagent", title: "Incident triage" });
        created = { id: "thread-generated", title: "Incident triage", state: "idle", repository: "multiagent" };
        return jsonResponse({ thread: created }, { status: 201 });
      }
      if (value.endsWith("/api/threads")) return jsonResponse({ threads: created ? [created] : [] });
      if (value.endsWith("/api/threads/thread-generated/messages")) {
        return jsonResponse({
          event: { sequence: 1, type: "user_message", payload: { text: "Investigate now" } },
          session: { id: "session-generated", status: "running" },
        }, { status: 202 });
      }
      if (value.includes("after_sequence=1")) {
        return jsonResponse({ events: [{ sequence: 2, type: "assistant_message", payload: { text: "Done" } }] });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.doesNotMatch(output.output, /No threads|\nThreads\n/);
  assert.match(output.output, /Opened thread-generated\. Enter its first message/);
  assert.match(output.output, /Starting orchestrator\nReader assigned/);
  assert.match(output.output, /assistant> Done/);
  assert.deepEqual(JSON.parse(await readFile(`${sessionFile}.threads.json`, "utf8")).profiles[0].threadIds, ["thread-generated"]);
});

test("interactive streaming retries while the session worker starts", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  const answers = ["/open 1", "Investigate", "/wait", "/quit"];
  const sockets = retryingTerminalSocketFactory();
  let eventPolls = 0;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => {},
    createInterface: () => ({ question: async () => answers.shift(), close() {} }),
    createWebSocket: sockets.factory,
    fetchImpl: async (url) => {
      const value = String(url);
      if (value.endsWith("/api/threads")) {
        return jsonResponse({ threads: [{ id: "thread-1", state: "idle", repository: "multiagent" }] });
      }
      if (value.endsWith("/api/threads/thread-1")) {
        return jsonResponse({ thread: { id: "thread-1", state: "idle", repository: "multiagent" } });
      }
      if (value.includes("after_sequence=0")) return jsonResponse({ events: [] });
      if (value.endsWith("/api/threads/thread-1/messages")) {
        return jsonResponse({
          event: { sequence: 1, type: "user_message", payload: { text: "Investigate" } },
          session: { id: "session-starting", status: "running" },
        }, { status: 202 });
      }
      if (value.includes("after_sequence=1")) {
        eventPolls += 1;
        return jsonResponse({ events: eventPolls < 3 ? [] : [
          { sequence: 2, type: "assistant_message", payload: { text: "Finished" } },
        ] });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.ok(sockets.attempts >= 2);
  assert.match(output.output, /waiting for session worker: session worker is not ready/);
  assert.match(output.output, /Worker connected\nInvestigating/);
  assert.match(output.output, /assistant> Finished/);
});

test("interactive terminal accepts another request while the open thread is streaming", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  const answers = ["/open 1", "First request", "Second request", "/wait", "/quit"];
  const messages = [];
  let eventPolls = 0;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => new Promise((resolve) => setImmediate(resolve)),
    createInterface: () => ({ question: async () => answers.shift(), close() {} }),
    createWebSocket: terminalSocketFactory(["Working", "Working\nApplying follow-up"]),
    fetchImpl: async (url, options) => {
      const value = String(url);
      if (value.endsWith("/api/threads")) {
        return jsonResponse({ threads: [{ id: "thread-1", state: "idle", repository: "multiagent" }] });
      }
      if (value.endsWith("/api/threads/thread-1")) {
        return jsonResponse({ thread: { id: "thread-1", state: "idle", repository: "multiagent" } });
      }
      if (value.includes("after_sequence=0")) return jsonResponse({ events: [] });
      if (value.endsWith("/api/threads/thread-1/messages")) {
        const text = JSON.parse(options.body).text;
        messages.push(text);
        const sequence = messages.length;
        return jsonResponse({
          event: { sequence, type: "user_message", payload: { text } },
          session: { id: "session-active", status: "running" },
          delivery: { mode: sequence === 1 ? "initial-context" : "supervisor-resume" },
        }, { status: 202 });
      }
      if (value.includes("/events?after_sequence=")) {
        eventPolls += 1;
        return jsonResponse({ events: messages.length < 2 || eventPolls < 2 ? [] : [
          { sequence: 3, type: "assistant_message", payload: { text: "Both requests handled" } },
        ] });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.deepEqual(messages, ["First request", "Second request"]);
  assert.match(output.output, /Applying follow-up/);
  assert.match(output.output, /assistant> Both requests handled/);
});

test("terminal snapshots render only newly appended or rolled output", () => {
  assert.equal(terminalDelta("Planning", "Planning\nDelegating"), "Delegating");
  assert.equal(terminalDelta("old\nPlanning\nDelegating", "Planning\nDelegating\nDone"), "Done");
  assert.equal(terminalDelta("same", "same"), "");
});

test("Codex terminal projection keeps progress and drops prompt chrome and spinners", () => {
  const snapshot = [
    "╭────────────────╮",
    "│ >_ OpenAI Codex",
    "╰────────────────╯",
    "› ----- BEGIN ORCHESTRATOR ROLE -----",
    "  # Multi-Agent Orchestrator",
    "  - internal prompt detail",
    "• Working (12s • esc to interrupt)",
    "• The orchestrator is checking the workflow.",
    "  It will keep this explanation concise.",
    "• Ran multiagent workflow context run-1",
    "  │ ignored command wrapping",
    "  └ phase=pre-implementation",
    "• READY_FOR_FOLLOWUP",
    "  gpt-5.6-sol high · /tmp/session",
  ].join("\n");
  assert.equal(terminalProgressView(snapshot), [
    "• The orchestrator is checking the workflow.",
    "  It will keep this explanation concise.",
    "• Ran multiagent workflow context run-1",
    "  └ phase=pre-implementation",
    "• READY_FOR_FOLLOWUP",
  ].join("\n"));
  assert.equal(terminalProgressView("Planning\nDelegating"), "Planning\nDelegating");
});

test("Claude stream-json projection keeps concise progress without exposing provider envelopes", () => {
  const snapshot = [
    "Multiagent launch mode: MULTIAGENT_RESUME=0 (fresh)",
    JSON.stringify({ type: "system", subtype: "init", tools: ["Task", "Bash"], session_id: "provider-session" }),
    JSON.stringify({ type: "assistant", message: { content: [
      { type: "text", text: "Checking the repository state." },
      { type: "tool_use", name: "Task", input: { description: "Inspect HEAD", prompt: "Long internal prompt that should only appear as a compact summary" } },
    ] } }),
    JSON.stringify({ type: "user", message: { content: [{ type: "tool_result", content: "thousands of raw tool output" }] } }),
    JSON.stringify({ type: "assistant", message: { content: [{ type: "text", text: "DEPLOYED_CREATE_OK 9157586" }] } }),
    JSON.stringify({ type: "result", result: "DEPLOYED_CREATE_OK 9157586", usage: { input_tokens: 50000 } }),
  ].join("\n");

  const projection = terminalProgressView(snapshot);
  assert.equal(projection, [
    "Checking the repository state.",
    "• Task: Inspect HEAD",
    "DEPLOYED_CREATE_OK 9157586",
  ].join("\n"));
  assert.doesNotMatch(projection, /session_id|input_tokens|raw tool output|Long internal prompt/);
});

test("interactive client keeps a scoped thread WebSocket open until exit", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  const answers = ["/open 1", "/quit"];
  const connections = [];
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => {},
    createInterface: () => ({ question: async () => answers.shift(), close() {} }),
    createWebSocket: (url, options) => {
      const socket = new EventEmitter();
      socket.closed = false;
      socket.close = () => {
        if (socket.closed) return;
        socket.closed = true;
        queueMicrotask(() => socket.emit("close"));
      };
      connections.push({ url: String(url), options, socket });
      queueMicrotask(() => socket.emit("message", Buffer.from(JSON.stringify({
        type: "agents",
        sessionId: null,
        agents: [],
      }))));
      return socket;
    },
    fetchImpl: async (url) => {
      const value = String(url);
      if (value.endsWith("/api/threads")) return jsonResponse({ threads: [{ id: "thread-1", state: "idle", repository: "multiagent" }] });
      if (value.endsWith("/api/threads/thread-1")) return jsonResponse({ thread: { id: "thread-1", state: "idle", repository: "multiagent" } });
      if (value.includes("/events?after_sequence=0")) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.equal(connections.length, 1);
  assert.equal(connections[0].url, "wss://control.example/api/threads/thread-1/stream?after_sequence=0");
  assert.deepEqual(connections[0].options.headers, {
    cookie: "multiagent_session=signed-cookie",
    origin: "https://control.example",
  });
  assert.equal(connections[0].socket.closed, true);
});

test("subagent pane includes status, role, and current work within its width", () => {
  const lines = renderAgentPane([
    { name: "reader", status: "working", role: "investigator", workingOn: "Tracing the session lifecycle" },
    { name: "tester", status: "done", role: "verification", workingOn: "Ran the client tests" },
  ], { columns: 72, maxRows: 6, connectionState: "connected" });
  assert.equal(lines[0], "○ orchestrator · idle");
  assert.match(lines[1], /├─ ● reader · investigator · working/);
  assert.match(lines[2], /↳ Tracing the session lifecycle/);
  assert.match(lines[3], /└─ ✓ tester · verification · done/);
  assert.match(lines[4], /↳ Ran the client tests/);
  assert.ok(lines.every((line) => line.length <= 72));
});

test("subagent pane keeps the open thread and orchestrator status visible", () => {
  const lines = renderAgentPane([
    { name: "reader", status: "running", role: "investigator", workingOn: "Inspecting the runtime" },
  ], {
    columns: 100,
    maxRows: 5,
    connectionState: "connected",
    thread: { id: "thread-123", state: "running" },
  });
  assert.equal(lines[0], "● orchestrator · running");
  assert.equal(lines[1], "└─ ● reader · investigator · running");
  assert.equal(lines[2], "     ↳ Inspecting the runtime");
});

test("subagent pane distinguishes idle, orchestrator planning, and unavailable snapshots", () => {
  assert.equal(renderAgentPane([], {
    columns: 80,
    maxRows: 4,
    thread: { id: "thread-idle", state: "idle" },
  })[1], "└─ ○ no active agents");
  const planning = renderAgentPane([], {
    columns: 80,
    maxRows: 4,
    thread: { id: "thread-running", state: "running" },
  });
  assert.equal(planning[0], "● orchestrator · planning");
  assert.equal(planning[1], "└─ ○ no delegated agents yet");
  assert.equal(renderAgentPane([], {
    columns: 80,
    maxRows: 4,
    thread: { id: "thread-running", state: "running" },
    agentSnapshot: { error: "subagent status temporarily unavailable" },
  })[1], "└─ ◌ subagent status unavailable");
});

test("completed orchestrator pane retains a concise outcome summary", () => {
  const lines = renderAgentPane([], {
    columns: 90,
    maxRows: 5,
    thread: { id: "thread-complete", state: "idle" },
    outcomeStatus: "complete",
    taskSummary: "Found open PR #421 and returned its review status.",
  });
  assert.equal(lines[0], "✓ orchestrator · complete");
  assert.equal(lines[1], "   ↳ Found open PR #421 and returned its review status.");
  assert.equal(lines[2], "└─ ○ no active agents");
});

test("completed outcome summary wraps within the terminal width", () => {
  const lines = renderAgentPane([], {
    columns: 52,
    maxRows: 6,
    thread: { id: "thread-complete", state: "idle" },
    outcomeStatus: "complete",
    taskSummary: "Latest open PR: #421 — fix: remove global waypoint signature-verification bypass from the live consensus path",
  });
  assert.deepEqual(lines.slice(0, 4), [
    "✓ orchestrator · complete",
    "   ↳ Latest open PR: #421 — fix: remove global",
    "     waypoint signature-verification bypass from the",
    "     live consensus path",
  ]);
  assert.ok(lines.every((line) => line.length <= 52));
});

test("interrupted orchestrator pane shows the bounded blocker instead of a generic failure", () => {
  const lines = renderAgentPane([], {
    columns: 64,
    maxRows: 6,
    thread: { id: "thread-blocked", state: "interrupted" },
    outcomeStatus: "interrupted",
    taskSummary: "Grafana read blocked: runbook requests 1.0.0 but prod-mcp certifies 1.1.0.",
  });
  assert.deepEqual(lines.slice(0, 3), [
    "× orchestrator · interrupted",
    "   ↳ Grafana read blocked: runbook requests 1.0.0 but prod-mcp",
    "     certifies 1.1.0.",
  ]);
});

test("structured PR review reports expose the blocker and hide the runtime envelope", () => {
  const report = "# session-pr-review\n\nStatus: completed\nWorkflow: run-pr-review\n\n## Final agent message\n# PR #68 Review — Blocked\n\n**Blocker:** GitHub read access does not expose the PR diff, changed files, or CI checks.\n\n## Trace references\n- agents/ops-01/events.jsonl";
  assert.equal(finalAgentMessageText(report), "# PR #68 Review — Blocked\n\n**Blocker:** GitHub read access does not expose the PR diff, changed files, or CI checks.");
  assert.equal(compactOutcomeSummary({ payload: { text: report } }), "Blocker: GitHub read access does not expose the PR diff, changed files, or CI checks.");
  assert.deepEqual(paneOutcomeForEvent({ type: "assistant_message", payload: { text: report } }), {
    status: "blocked",
    summary: "Blocker: GitHub read access does not expose the PR diff, changed files, or CI checks.",
  });
  assert.equal(renderAgentPane([], {
    outcomeStatus: "blocked",
    taskSummary: "Blocker: GitHub read access does not expose the PR diff.",
  })[0], "× orchestrator · blocked");
});

test("latest-open-PR interaction ends with an informative summary, completed agent graph, and active prompt", async () => {
  const sessionFile = await sessionFixture();
  const output = ttyWriter();
  let questionCount = 0;
  let threadSocket = null;
  const prompts = [];
  const terminal = {
    async question() {
      questionCount += 1;
      if (questionCount === 1) return "/open 1";
      return new Promise((resolve) => {
        setImmediate(() => {
          threadSocket.emit("message", Buffer.from(JSON.stringify({
            type: "agents",
            agents: [{ name: "reader", status: "running", role: "investigator", workingOn: "Checking status" }],
          })));
          threadSocket.emit("message", Buffer.from(JSON.stringify({
            type: "event",
            event: {
              sequence: 1,
              type: "assistant_message",
              payload: {
                text: "# thread-latest-open-pr\n\nStatus: completed\nWorkflow: run-latest-open-pr\n\n## Final agent message\nLatest open PR: **#421** — fix: remove global waypoint signature-verification bypass\n- Author: contributor\n- URL: https://github.com/movement-network/aptos-core/pull/421\n\n## Trace references\n- agents/ops-01/attempt-0001/events.jsonl",
              },
            },
          })));
          threadSocket.emit("message", Buffer.from(JSON.stringify({
            type: "agents",
            agents: [{
              name: "ops-01",
              status: "done",
              role: "ops",
              workingOn: "Found latest open PR #421",
            }],
            available: true,
          })));
          setImmediate(() => resolve("/quit"));
        });
      });
    },
    setPrompt(value) { this.label = value; },
    prompt(preserveCursor) { prompts.push({ label: this.label, preserveCursor }); },
    close() {},
  };

  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    createInterface: () => terminal,
    sleep: async () => {},
    createWebSocket: (url) => {
      const socket = new EventEmitter();
      socket.close = () => queueMicrotask(() => socket.emit("close"));
      if (String(url).includes("/stream")) threadSocket = socket;
      return socket;
    },
    fetchImpl: async (url) => {
      const value = String(url);
      if (value.endsWith("/api/reviews?status=pending")) return jsonResponse({ reviews: [] });
      if (value.endsWith("/api/threads")) return jsonResponse({ threads: [{ id: "thread-1", state: "running", repository: "multiagent" }] });
      if (value.endsWith("/api/threads/thread-1")) return jsonResponse({ thread: { id: "thread-1", state: "running", repository: "multiagent" } });
      if (value.includes("/events?after_sequence=0")) return jsonResponse({ events: [] });
      throw new Error(`unexpected request: ${value}`);
    },
  });

  assert.match(output.output, /● orchestrator · running/);
  assert.match(output.output, /assistant> Latest open PR: \*\*#421\*\*/);
  assert.match(output.output, /✓ orchestrator · complete/);
  assert.match(output.output, /↳ Latest open PR: #421 — fix: remove global waypoint/);
  assert.match(output.output, /✓ ops-01 · ops · done/);
  assert.match(output.output, /↳ Found latest open PR #421/);
  assert.doesNotMatch(output.output, /↳ Status: completed/);
  assert.doesNotMatch(output.output, /assistant> # thread-latest-open-pr|Status: completed|Workflow: run-latest-open-pr|Trace references/);
  assert.ok(prompts.some((prompt) => prompt.label === "› " && prompt.preserveCursor === true));
});

test("scriptable review commands list and decide with yes or no", async () => {
  const sessionFile = await sessionFixture({ threadIds: [] });
  const listed = writer();
  const review = { id: "review-1", threadId: "thread-slack", status: "pending", question: "Approve restart?" };
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "reviews", "list",
  ], {
    stdout: listed,
    fetchImpl: async (url) => {
      assert.equal(String(url), "https://control.example/api/reviews?status=pending");
      return jsonResponse({ reviews: [review] });
    },
  });
  assert.deepEqual(JSON.parse(listed.output), [review]);

  const decided = writer();
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "reviews", "decide", "review-1", "yes",
  ], {
    stdout: decided,
    fetchImpl: async (url, options) => {
      assert.equal(String(url), "https://control.example/api/reviews/review-1/decision");
      assert.deepEqual(JSON.parse(options.body), { decision: "approve" });
      assert.ok(options.headers["idempotency-key"]);
      return jsonResponse({
        review: { ...review, status: "approved" },
        thread: { id: "thread-slack", state: "starting" },
        session: { id: "session-repair", status: "running" },
      }, { status: 202 });
    },
  });
  assert.equal(JSON.parse(decided.output).session.id, "session-repair");
  assert.deepEqual(JSON.parse(await readFile(`${sessionFile}.threads.json`, "utf8")).profiles[0].threadIds, ["thread-slack"]);
});

test("open TTY announces a repair review that arrives after startup", async () => {
  const sessionFile = await sessionFixture({ threadIds: [] });
  const output = ttyWriter();
  let tick = null;
  let cleared = false;
  let questionCount = 0;
  let reviewFetches = 0;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    createInterface: () => ({
      async question() {
        if (questionCount++ === 0) {
          tick();
          await new Promise((resolve) => setImmediate(resolve));
          return "no";
        }
        return "/quit";
      },
      setPrompt() {},
      prompt() {},
      close() {},
    }),
    setInterval(callback, milliseconds) {
      assert.equal(milliseconds, 2_000);
      tick = callback;
      return { unref() {} };
    },
    clearInterval() { cleared = true; },
    fetchImpl: async (url, options = {}) => {
      const value = String(url);
      if (value.endsWith("/api/reviews?status=pending")) {
        reviewFetches += 1;
        return jsonResponse({ reviews: reviewFetches === 2 ? [{
          id: "review-background",
          threadId: "thread-background",
          requestedAt: "2026-09-04T00:00:00.000Z",
          question: "Approve the bounded background repair?",
        }] : [] });
      }
      if (value.endsWith("/api/reviews/review-background/decision")) {
        assert.deepEqual(JSON.parse(options.body), { decision: "reject" });
        return jsonResponse({
          review: { id: "review-background", status: "rejected" },
          thread: { id: "thread-background", state: "review_rejected" },
          session: null,
        });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.match(output.output, /REPAIR REVIEW REQUIRED/);
  assert.match(output.output, /Approve the bounded background repair\?/);
  assert.match(output.output, /Rejected review-background\. Thread thread-background cannot continue\./);
  assert.equal(cleared, true);
});

test("TTY startup shows a pending repair and yes starts its fresh session", async () => {
  const sessionFile = await sessionFixture({ threadIds: [] });
  const output = ttyWriter();
  const answers = ["yes", "/quit"];
  let reviewFetches = 0;
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
  ], {
    stdin: { isTTY: true },
    stdout: output,
    sleep: async () => {},
    createInterface: () => ({ question: async () => answers.shift(), setPrompt() {}, prompt() {}, close() {} }),
    createWebSocket: terminalSocketFactory([]),
    fetchImpl: async (url, options = {}) => {
      const value = String(url);
      if (value.endsWith("/api/reviews?status=pending")) {
        reviewFetches += 1;
        return jsonResponse({ reviews: reviewFetches === 1 ? [{
          id: "review-session-diagnose",
          threadId: "thread-slack",
          requestedAt: "2026-09-04T00:00:00.000Z",
          question: "Approve restarting api in testnet?",
        }] : [] });
      }
      if (value.endsWith("/api/reviews/review-session-diagnose/decision")) {
        assert.deepEqual(JSON.parse(options.body), { decision: "approve" });
        return jsonResponse({
          review: { id: "review-session-diagnose", status: "approved" },
          thread: { id: "thread-slack", state: "starting", activeSessionId: "session-repair", repository: "multiagent" },
          session: { id: "session-repair", status: "running" },
        }, { status: 202 });
      }
      if (value.includes("/api/threads/thread-slack/events?after_sequence=0")) {
        return jsonResponse({ events: [
          { sequence: 1, type: "system_message", payload: { text: "Slack alert" } },
          { sequence: 2, type: "question", payload: { text: "Approve restarting api in testnet?" } },
          { sequence: 3, type: "user_message", payload: { text: "I approve repair review" } },
        ] });
      }
      throw new Error(`unexpected request: ${value}`);
    },
  });
  assert.match(output.output, /REPAIR REVIEW REQUIRED/);
  assert.match(output.output, /Approve restarting api in testnet\?/);
  assert.match(output.output, /Approved review-session-diagnose\. Started fresh Session session-repair/);
  assert.match(output.output, /slack> Slack alert/);
});
