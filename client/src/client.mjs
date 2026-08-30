import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";
import { WebSocket } from "ws";

export const usage = `Usage: multiagent-client [global options] [connect [THREAD_ID] | <command>]

Global options:
  --server URL          Control-server URL (or MULTIAGENT_SERVER)
  --session-file PATH   Login session file (or MULTIAGENT_CLIENT_SESSION_FILE)

Commands:
  connect [THREAD_ID]   Open the interactive terminal client (default)
  login USERNAME
  logout
  whoami
  repositories list
  threads list
  threads show THREAD_ID
  threads create --repository NAME [--title TITLE] (--message TEXT | --message-file PATH)
  threads send THREAD_ID (--message TEXT | --message-file PATH)
  threads watch THREAD_ID [--after SEQUENCE] [--once]
  sessions list THREAD_ID
  legacy list
  legacy report SESSION_ID

Passwords are read from a hidden terminal prompt or stdin. Command output is JSON;
thread watch emits one JSON event per line. Run without a command for the
interactive terminal client.`;

const interactiveHelp = `Commands:
  /threads                 List threads
  /open THREAD_ID          Open a thread
  /new REPO [TITLE]        Create and open a server-assigned thread
  /sessions                List execution sessions for the open thread
  /refresh                 Replay new events
  /wait                    Wait for the current execution to reply
  /help                    Show this help
  /quit                    Exit

After opening a thread, enter a message normally to send it.`;

export class ClientError extends Error {
  constructor(message, { statusCode = null, body = null } = {}) {
    super(message);
    this.statusCode = statusCode;
    this.body = body;
  }
}

export class ControlClient {
  constructor({ server, cookie = "", fetchImpl = globalThis.fetch }) {
    this.server = normalizeServer(server);
    this.cookie = cookie;
    this.fetch = fetchImpl;
  }

  async request(apiPath, { method = "GET", body, headers = {} } = {}) {
    const requestHeaders = { accept: "application/json", ...headers };
    if (this.cookie) requestHeaders.cookie = this.cookie;
    if (body !== undefined) requestHeaders["content-type"] = "application/json";
    let response;
    try {
      response = await this.fetch(new URL(apiPath, this.server), {
        method,
        headers: requestHeaders,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (error) {
      throw new ClientError(`cannot reach control server: ${error.message}`);
    }
    const value = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new ClientError(value.error || `request failed (${response.status})`, { statusCode: response.status, body: value });
    }
    return { value, response };
  }
}

export async function main(argv = process.argv.slice(2), dependencies = {}) {
  const stdout = dependencies.stdout || process.stdout;
  const stderr = dependencies.stderr || process.stderr;
  const stdin = dependencies.stdin || process.stdin;
  const environment = dependencies.env || process.env;
  const fetchImpl = dependencies.fetchImpl || globalThis.fetch;
  const sleep = dependencies.sleep || ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
  const readPassword = dependencies.readPassword || (() => readSecret(stdin, stderr));
  const createInterfaceImpl = dependencies.createInterface || createInterface;
  const createWebSocketImpl = dependencies.createWebSocket || ((url, options) => new WebSocket(url, options));
  const parsed = parseGlobalOptions(argv);
  const sessionFile = path.resolve(parsed.sessionFile || environment.MULTIAGENT_CLIENT_SESSION_FILE || defaultSessionFile());
  const stored = await loadSession(sessionFile);
  const server = parsed.server || environment.MULTIAGENT_SERVER || stored?.server;
  const command = parsed.args.shift();
  if (command === "help" || command === "--help" || command === "-h") {
    stdout.write(usage + "\n");
    return 0;
  }
  if (!server) throw new ClientError("control-server URL is required; pass --server or set MULTIAGENT_SERVER");
  const normalizedServer = normalizeServer(server).href;
  const cookie = stored?.server === normalizedServer ? stored.cookie : "";
  const client = new ControlClient({ server: normalizedServer, cookie, fetchImpl });

  if (!command || command === "connect") {
    if (!client.cookie) throw new ClientError(`not logged in to ${normalizedServer}; run the login command first`);
    const initialThreadId = command === "connect" ? parsed.args.shift() || "" : "";
    rejectExtraArguments(parsed.args);
    return runInteractive({ client, stdin, stdout, sleep, createInterfaceImpl, createWebSocketImpl, initialThreadId });
  }

  if (command === "login") {
    const username = requiredArgument(parsed.args.shift(), "username");
    rejectExtraArguments(parsed.args);
    const password = await readPassword();
    if (!String(password || "").length) throw new ClientError("password is required on stdin or at the terminal prompt");
    const { value, response } = await client.request("/api/login", { method: "POST", body: { username, password } });
    const setCookie = response.headers.get("set-cookie") || "";
    const sessionCookie = setCookie.split(";", 1)[0];
    if (!sessionCookie.startsWith("multiagent_session=")) throw new ClientError("control server did not return a login session cookie");
    await saveSession(sessionFile, { server: normalizedServer, cookie: sessionCookie, username: value.username });
    writeJson(stdout, { authenticated: true, server: normalizedServer, username: value.username, sessionFile });
    return 0;
  }

  if (!client.cookie) throw new ClientError(`not logged in to ${normalizedServer}; run the login command first`);

  if (command === "logout") {
    rejectExtraArguments(parsed.args);
    await client.request("/api/logout", { method: "POST" });
    await fs.rm(sessionFile, { force: true });
    writeJson(stdout, { authenticated: false, server: normalizedServer });
    return 0;
  }
  if (command === "whoami") {
    rejectExtraArguments(parsed.args);
    writeJson(stdout, (await client.request("/api/me")).value);
    return 0;
  }
  if (command === "repositories") {
    requireAction(parsed.args.shift(), "list", "repositories");
    rejectExtraArguments(parsed.args);
    writeJson(stdout, (await client.request("/api/repositories")).value.repositories || []);
    return 0;
  }
  if (command === "threads") {
    return runThreads({ client, args: parsed.args, stdout, stdin, sleep });
  }
  if (command === "sessions") {
    requireAction(parsed.args.shift(), "list", "sessions");
    const threadId = requiredArgument(parsed.args.shift(), "thread ID");
    rejectExtraArguments(parsed.args);
    writeJson(stdout, (await client.request(`/api/threads/${encodeURIComponent(threadId)}/sessions`)).value.sessions || []);
    return 0;
  }
  if (command === "legacy") {
    return runLegacy({ client, args: parsed.args, stdout });
  }
  throw new ClientError(`unknown command: ${command}\n\n${usage}`);
}

export async function runInteractive({
  client,
  stdin,
  stdout,
  sleep,
  createInterfaceImpl = createInterface,
  createWebSocketImpl = (url, options) => new WebSocket(url, options),
  initialThreadId = "",
}) {
  if (!stdin?.isTTY) throw new ClientError("interactive mode requires a terminal; use a JSON command for non-interactive calls");
  const terminal = createInterfaceImpl({ input: stdin, output: stdout, terminal: true });
  let threads = [];
  let current = null;
  let cursor = 0;
  let monitor = null;
  let threadConnection = null;
  let promptActive = false;
  let promptLabel = "› ";
  const refreshPrompt = () => {
    if (!promptActive) return;
    if (typeof terminal.setPrompt !== "function" || typeof terminal.prompt !== "function") return;
    terminal.setPrompt(promptLabel);
    terminal.prompt(true);
  };
  const interactiveOutput = {
    write(value) {
      if (promptActive && stdout.isTTY) stdout.write("\r\u001b[2K");
      stdout.write(value);
      refreshPrompt();
    },
  };
  const agentPane = createAgentPane(stdout, { onDraw: refreshPrompt });

  const listThreads = async () => {
    threads = (await client.request("/api/threads")).value.threads || [];
    if (!threads.length) {
      stdout.write("\nNo threads. Create one with /new REPOSITORY [TITLE].\n");
      return;
    }
    stdout.write("\nThreads\n");
    threads.forEach((thread, index) => {
      const title = thread.title && thread.title !== thread.id ? ` — ${thread.title}` : "";
      stdout.write(`  ${index + 1}. ${thread.id}${title}  [${thread.state}]  ${thread.repository}\n`);
    });
  };

  const replay = async ({ all = false } = {}) => {
    if (!current) return [];
    const after = all ? 0 : cursor;
    const response = await client.request(`/api/threads/${encodeURIComponent(current.id)}/events?after_sequence=${after}&limit=500`);
    const events = response.value.events || [];
    if (all) cursor = 0;
    for (const event of events) {
      const sequence = Number(event.sequence) || 0;
      if (sequence <= cursor) continue;
      cursor = sequence;
      renderInteractiveEvent(interactiveOutput, event);
    }
    return events;
  };

  const stopMonitor = async () => {
    const active = monitor;
    if (!active) return;
    active.controller.abort();
    await active.promise;
    if (monitor === active) monitor = null;
  };

  const stopThreadConnection = async () => {
    const active = threadConnection;
    if (!active) return;
    active.controller.abort();
    await active.promise;
    if (threadConnection === active) threadConnection = null;
    agentPane.render([], "disconnected", null);
  };

  const startThreadConnection = (threadId) => {
    if (!threadId || threadConnection?.threadId === threadId) return;
    const controller = new AbortController();
    const active = { threadId, controller, promise: null };
    threadConnection = active;
    active.promise = streamThread({
      client,
      threadId,
      getCursor: () => cursor,
      sleep,
      signal: controller.signal,
      createWebSocketImpl,
      onState: (state) => agentPane.setConnectionState(state),
      onThread: (thread) => {
        if (current?.id === threadId) {
          current = thread;
          agentPane.setThread(thread);
        }
      },
      onAgents: (agents, snapshot) => agentPane.render(agents, undefined, snapshot),
      onEvent: (event) => {
        if (current?.id !== threadId) return;
        const sequence = Number(event.sequence) || 0;
        if (sequence <= cursor) return;
        cursor = sequence;
        renderInteractiveEvent(interactiveOutput, event);
        if (new Set(["assistant_message", "question", "session_interrupted"]).has(event.type)) {
          monitor?.controller.abort();
        }
      },
    }).finally(() => {
      if (threadConnection === active) threadConnection = null;
    });
  };

  const startMonitor = async (sessionId) => {
    if (!sessionId || monitor?.sessionId === sessionId) return;
    await stopMonitor();
    const controller = new AbortController();
    const active = { sessionId, controller, promise: null };
    monitor = active;
    active.promise = (async () => {
      const stream = streamSessionTerminal({ client, sessionId, stdout: interactiveOutput, sleep, signal: controller.signal, createWebSocketImpl });
      try {
        while (!controller.signal.aborted) {
          const events = await replay();
          if (events.some((event) => new Set(["assistant_message", "question", "session_interrupted"]).has(event.type))) return;
          await waitUntilRetry(sleep, controller.signal, 1000);
        }
      } catch (error) {
        if (!controller.signal.aborted) stdout.write(`[monitor error] ${error.message}\n`);
      } finally {
        controller.abort();
        await stream;
        if (monitor === active) monitor = null;
      }
    })();
  };

  const openThread = async (selector) => {
    const selected = selectThread(threads, selector);
    const response = await client.request(`/api/threads/${encodeURIComponent(selected)}`);
    await stopMonitor();
    await stopThreadConnection();
    current = response.value.thread;
    agentPane.setThread(current);
    cursor = 0;
    stdout.write(`\nOpened ${current.id} [${current.state}] — ${current.repository}\n`);
    await replay({ all: true });
    startThreadConnection(current.id);
    if (current.activeSessionId && new Set(["starting", "running"]).has(current.state)) {
      await startMonitor(current.activeSessionId);
    }
  };

  stdout.write("Multiagent — /help\n");
  try {
    await listThreads();
    if (initialThreadId) await openThread(initialThreadId);
    while (true) {
      promptLabel = current ? "› " : "multiagent> ";
      promptActive = true;
      let answer;
      try {
        answer = await terminal.question(promptLabel);
      } finally {
        promptActive = false;
      }
      const line = String(answer).trim();
      if (!line) continue;
      try {
        if (line === "/quit" || line === "/exit") return 0;
        if (line === "/help") { stdout.write(`\n${interactiveHelp}\n`); continue; }
        if (line === "/threads") { await listThreads(); continue; }
        if (line === "/refresh") { await replay(); continue; }
        if (line === "/wait") {
          if (!monitor) stdout.write("No execution is currently running.\n");
          else await monitor.promise;
          continue;
        }
        if (line === "/sessions") {
          if (!current) { stdout.write("Open a thread first.\n"); continue; }
          const sessions = (await client.request(`/api/threads/${encodeURIComponent(current.id)}/sessions`)).value.sessions || [];
          if (!sessions.length) stdout.write("No execution sessions yet.\n");
          else sessions.forEach((session) => stdout.write(`  ${session.ordinal}. ${session.id}  [${session.status}]\n`));
          continue;
        }
        if (line.startsWith("/open ")) { await openThread(line.slice(6).trim()); continue; }
        if (line === "/new" || line.startsWith("/new ")) {
          const [repository, ...titleParts] = line.slice(4).trim().split(/\s+/);
          if (!repository) { stdout.write("Usage: /new REPOSITORY [TITLE]\n"); continue; }
          const created = await client.request("/api/threads", {
            method: "POST",
            body: { repository, title: titleParts.join(" ") },
          });
          await stopMonitor();
          await stopThreadConnection();
          current = created.value.thread;
          agentPane.setThread(current);
          cursor = 0;
          startThreadConnection(current.id);
          await listThreads();
          stdout.write(`\nOpened ${current.id}. Enter its first message.\n`);
          continue;
        }
        if (line.startsWith("/")) { stdout.write(`Unknown command: ${line.split(/\s+/, 1)[0]}. Type /help.\n`); continue; }
        if (!current) {
          await openThread(line);
          continue;
        }
        if (line.length > 32768) { stdout.write("Message exceeds 32768 characters.\n"); continue; }
        const routed = await sendThreadMessage(client, current.id, line);
        if (routed.event) {
          const sequence = Number(routed.event.sequence) || 0;
          if (sequence > cursor) {
            cursor = sequence;
            renderInteractiveEvent(interactiveOutput, routed.event);
          }
        }
        const session = routed.session;
        if (session) {
          agentPane.setThread(current, session.status);
          stdout.write(`[execution ${session.status}] ${session.id}\n`);
        }
        await startMonitor(session?.id || "");
      } catch (error) {
        if (!(error instanceof ClientError)) throw error;
        stdout.write(`[error] ${error.message}\n`);
      }
    }
  } finally {
    await stopMonitor();
    await stopThreadConnection();
    agentPane.close();
    terminal.close();
  }
}

async function streamThread({ client, threadId, getCursor, sleep, signal, createWebSocketImpl, onState, onThread, onAgents, onEvent }) {
  while (!signal.aborted) {
    onState("connecting");
    await streamThreadConnection({
      client,
      threadId,
      afterSequence: getCursor(),
      signal,
      createWebSocketImpl,
      onMessage(payload) {
        onState("connected");
        if (payload.type === "event" && payload.event) onEvent(payload.event);
        else if (payload.type === "thread" && payload.thread) onThread(payload.thread);
        else if (payload.type === "agents") onAgents(Array.isArray(payload.agents) ? payload.agents : [], payload);
      },
    });
    if (signal.aborted) return;
    onState("reconnecting");
    await waitUntilRetry(sleep, signal, 1000);
  }
}

function streamThreadConnection({ client, threadId, afterSequence, signal, createWebSocketImpl, onMessage }) {
  return new Promise((resolve) => {
    const url = new URL(`/api/threads/${encodeURIComponent(threadId)}/stream`, client.server);
    url.searchParams.set("after_sequence", String(Math.max(0, Number(afterSequence) || 0)));
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    let error = "";
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      resolve({ error });
    };
    const abort = () => {
      try { socket?.close(); } catch {}
      finish();
    };
    try {
      socket = createWebSocketImpl(url, {
        headers: { cookie: client.cookie, origin: client.server.origin },
      });
      signal.addEventListener("abort", abort, { once: true });
      socket.on("message", (data) => {
        try {
          const payload = JSON.parse(data.toString());
          if (payload.type === "error") {
            error = String(payload.error || "thread stream failed");
            try { socket.close(); } catch {}
            return;
          }
          onMessage(payload);
        } catch {
          error = "thread stream returned invalid JSON";
          try { socket.close(); } catch {}
        }
      });
      socket.on("error", (cause) => {
        error = cause?.message || "thread stream failed";
        try { socket.close(); } catch {}
        finish();
      });
      socket.on("close", finish);
    } catch (cause) {
      error = cause?.message || "thread stream failed";
      finish();
    }
  });
}

async function streamSessionTerminal({ client, sessionId, stdout, sleep, signal, createWebSocketImpl }) {
  let previousProgress = "";
  let announced = false;
  let waitingAnnounced = false;
  stdout.write(`[orchestrator] connecting to execution ${sessionId}...\n`);
  while (!signal.aborted) {
    const result = await streamTerminalConnection({
      client,
      sessionId,
      signal,
      createWebSocketImpl,
      onOutput(output) {
        const nextProgress = terminalProgressView(output);
        const delta = previousProgress
          ? terminalDelta(previousProgress, nextProgress)
          : nextProgress.split("\n").slice(-20).join("\n");
        previousProgress = nextProgress;
        if (!delta.trim()) return;
        if (!announced) {
          stdout.write(`\n[orchestrator ${sessionId}]\n`);
          announced = true;
        }
        stdout.write(delta + (delta.endsWith("\n") ? "" : "\n"));
      },
    });
    if (signal.aborted) return;
    if (!waitingAnnounced && result.error) {
      stdout.write(`[orchestrator] waiting for session worker: ${result.error}\n`);
      waitingAnnounced = true;
    }
    await waitUntilRetry(sleep, signal, 1000);
  }
}

function streamTerminalConnection({ client, sessionId, signal, createWebSocketImpl, onOutput }) {
  return new Promise((resolve) => {
    const url = new URL(`/api/sessions/${encodeURIComponent(sessionId)}/terminal`, client.server);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    let socket;
    let error = "";
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", abort);
      resolve({ error });
    };
    const abort = () => {
      try { socket?.close(); } catch {}
      finish();
    };
    try {
      socket = createWebSocketImpl(url, {
        headers: { cookie: client.cookie, origin: client.server.origin },
      });
      signal.addEventListener("abort", abort, { once: true });
      socket.on("message", (data) => {
        try {
          const payload = JSON.parse(data.toString());
          if (payload.type === "output") onOutput(payload.output);
          else if (payload.type === "error") {
            error = String(payload.error || "terminal stream failed");
            try { socket.close(); } catch {}
          }
        } catch {
          error = "terminal stream returned invalid JSON";
        }
      });
      socket.on("error", (cause) => {
        error = cause?.message || "terminal stream failed";
        try { socket.close(); } catch {}
        finish();
      });
      socket.on("close", finish);
    } catch (cause) {
      error = cause?.message || "terminal stream failed";
      finish();
    }
  });
}

function waitUntilRetry(sleep, signal, milliseconds) {
  if (signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      signal.removeEventListener("abort", finish);
      resolve();
    };
    signal.addEventListener("abort", finish, { once: true });
    Promise.resolve(sleep(milliseconds)).then(finish, finish);
  });
}

function normalizeTerminalOutput(value) {
  return String(value || "").replaceAll("\r\n", "\n").replace(/[ \t\n]+$/, "");
}

export function terminalDelta(previous, next) {
  const before = normalizeTerminalOutput(previous);
  const after = normalizeTerminalOutput(next);
  if (!after || before === after) return "";
  if (!before) return after;
  if (after.startsWith(before)) return after.slice(before.length).replace(/^\n/, "");
  const beforeLines = before.split("\n");
  const afterLines = after.split("\n");
  let commonPrefix = 0;
  while (commonPrefix < beforeLines.length
    && commonPrefix < afterLines.length
    && beforeLines[commonPrefix] === afterLines[commonPrefix]) {
    commonPrefix += 1;
  }
  if (commonPrefix > 0) return afterLines.slice(commonPrefix).join("\n");
  for (let overlap = Math.min(beforeLines.length, afterLines.length); overlap > 0; overlap -= 1) {
    if (beforeLines.slice(-overlap).every((line, index) => line === afterLines[index])) {
      return afterLines.slice(overlap).join("\n");
    }
  }
  return after;
}

export function terminalProgressView(value) {
  const normalized = normalizeTerminalOutput(value);
  const lines = normalized.split("\n");
  const claude = claudeStreamProgress(lines);
  if (claude.detected) return claude.progress;
  const looksLikeCodexTui = lines.some((line) => /^╭|^│ >_ OpenAI Codex|^› |^\s+gpt-[^ ]+|^[•◦] (?:Working|Starting MCP servers)/.test(line));
  if (!looksLikeCodexTui) return normalized;

  const progress = [];
  let continuationBudget = 0;
  const append = (line) => {
    const value = line.trimEnd();
    if (value && progress.at(-1) !== value) progress.push(value);
  };
  for (const line of lines) {
    if (/^⚠ /.test(line)) {
      append(line);
      continuationBudget = 1;
      continue;
    }
    if (/^• /.test(line)) {
      if (/^• (?:Working|Starting MCP servers|You have \d+ usage limit reset)/.test(line)) {
        continuationBudget = 0;
        continue;
      }
      append(line);
      continuationBudget = 4;
      continue;
    }
    if (continuationBudget > 0 && /^  \S/.test(line) && !/^  (?:gpt-|│)/.test(line)) {
      append(line);
      continuationBudget -= 1;
      continue;
    }
    if (!line.trim()) continuationBudget = 0;
  }
  return progress.join("\n");
}

function claudeStreamProgress(lines) {
  const progress = [];
  let detected = false;
  const append = (value) => {
    const text = String(value || "").replace(/\s+/g, " ").trim().slice(0, 500);
    if (text && progress.at(-1) !== text && !progress.includes(text)) progress.push(text);
  };
  for (const line of lines) {
    let value;
    try { value = JSON.parse(line.trim()); } catch { continue; }
    if (!value || typeof value !== "object" || !new Set(["system", "assistant", "user", "result"]).has(value.type)) continue;
    detected = true;
    if (value.type === "result") {
      append(value.result);
      continue;
    }
    const content = Array.isArray(value.message?.content) ? value.message.content : [];
    for (const item of content) {
      if (item?.type === "text") append(item.text);
      if (item?.type === "tool_use") {
        const input = item.input && typeof item.input === "object" ? item.input : {};
        const summary = input.description || input.summary || input.name || "";
        append(`• ${String(item.name || "tool").slice(0, 60)}${summary ? `: ${String(summary).replace(/\s+/g, " ").trim().slice(0, 240)}` : ""}`);
      }
      if (item?.type === "tool_result" && item.is_error) append(`⚠ ${typeof item.content === "string" ? item.content : "Tool failed"}`);
    }
  }
  return { detected, progress: progress.join("\n") };
}

const inactiveAgentStatuses = new Set(["done", "completed", "closed", "cancelled", "canceled", "failed", "released", "skipped", "finalized", "killed", "missing"]);

export function renderAgentPane(agents, {
  columns = 80,
  maxRows = 6,
  connectionState = "connected",
  thread = null,
  executionStatus = "",
  agentSnapshot = null,
} = {}) {
  const values = Array.isArray(agents) ? agents : [];
  const orchestratorStatus = executionStatus || thread?.state || "idle";
  const connection = connectionState === "connected" ? "" : ` · ${connectionState}`;
  const rows = [`${agentStatusGlyph(orchestratorStatus)} orchestrator · ${orchestratorStatus}${connection}`];
  const agentCapacity = Math.max(0, Math.floor((maxRows - rows.length) / 2));
  const visible = values.slice(0, agentCapacity);
  visible.forEach((agent, index) => {
    const status = String(agent.status || "unknown");
    const role = agent.role ? ` · ${agent.role}` : "";
    const work = String(agent.workingOn || agent.assignment || "waiting");
    const last = index === visible.length - 1 && values.length === visible.length;
    rows.push(`${last ? "└─" : "├─"} ${agentStatusGlyph(status)} ${agent.name || "agent"}${role} · ${status}`);
    rows.push(`${last ? "   " : "│  "}  ↳ ${work}`);
  });
  if (values.length > visible.length && rows.length < maxRows) rows.push(`└─ … ${values.length - visible.length} more`);
  if (!values.length && maxRows > 1) {
    const active = new Set(["queued", "starting", "running", "working", "in-progress"]).has(String(orchestratorStatus).toLowerCase());
    if (agentSnapshot?.error) rows.push("└─ ◌ subagent status unavailable");
    else if (active) rows.push("└─ ◌ discovering subagents");
    else rows.push("└─ ○ no active agents");
  }
  return rows.slice(0, maxRows).map((line) => truncateTerminalLine(line, columns));
}

function agentStatusGlyph(status) {
  const value = String(status || "").toLowerCase();
  if (new Set(["failed", "killed", "cancelled", "canceled", "delivery-blocked", "interrupted"]).has(value)) return "×";
  if (inactiveAgentStatuses.has(value)) return "✓";
  if (new Set(["starting", "queued", "connecting", "restoring", "waiting"]).has(value)) return "◌";
  if (new Set(["running", "working", "in-progress"]).has(value)) return "●";
  return "○";
}

function truncateTerminalLine(value, columns) {
  const width = Math.max(8, Number(columns) || 80);
  const line = String(value || "").replace(/[\r\n\t]+/g, " ");
  if (line.length <= width) return line;
  return width <= 3 ? line.slice(0, width) : `${line.slice(0, width - 3)}...`;
}

function createAgentPane(stdout, { onDraw = () => {} } = {}) {
  const enabled = Boolean(stdout?.isTTY && Number(stdout.rows) >= 8);
  let agents = [];
  let connectionState = "disconnected";
  let thread = null;
  let executionStatus = "";
  let agentSnapshot = null;
  let panel = null;
  let lastFrame = "";

  const clear = () => {
    if (!enabled || !panel) return;
    let output = "\u001b7\u001b[r";
    for (let row = panel.start; row <= panel.rows; row += 1) output += `\u001b[${row};1H\u001b[2K`;
    output += "\u001b8";
    stdout.write(output);
    panel = null;
    lastFrame = "";
  };

  const draw = () => {
    if (!enabled) return;
    const rows = Math.max(8, Number(stdout.rows) || 24);
    const columns = Math.max(20, Number(stdout.columns) || 80);
    const height = Math.min(9, Math.max(4, Math.floor(rows / 3)));
    const start = rows - height + 1;
    const mainBottom = start - 1;
    if (panel && (panel.rows !== rows || panel.start !== start)) clear();
    panel = { rows, start };
    const lines = renderAgentPane(agents, {
      columns,
      maxRows: height,
      connectionState,
      thread,
      executionStatus,
      agentSnapshot,
    });
    const frame = JSON.stringify({ rows, columns, height, lines });
    if (frame === lastFrame) return;
    lastFrame = frame;
    let output = `\u001b7\u001b[1;${mainBottom}r`;
    for (let index = 0; index < height; index += 1) {
      output += `\u001b[${start + index};1H\u001b[2K${lines[index] || ""}`;
    }
    output += "\u001b8";
    stdout.write(output);
    onDraw();
  };

  return {
    render(nextAgents, nextConnectionState, nextAgentSnapshot) {
      agents = Array.isArray(nextAgents) ? nextAgents : [];
      if (nextConnectionState) connectionState = nextConnectionState;
      if (nextAgentSnapshot !== undefined) agentSnapshot = nextAgentSnapshot;
      draw();
    },
    setConnectionState(next) {
      connectionState = next;
      draw();
    },
    setThread(nextThread, nextExecutionStatus = "") {
      thread = nextThread || null;
      executionStatus = nextExecutionStatus || "";
      draw();
    },
    close: clear,
  };
}

function selectThread(threads, selector) {
  const value = String(selector || "").trim();
  if (/^[1-9][0-9]*$/.test(value)) {
    const selected = threads[Number(value) - 1];
    if (!selected) throw new ClientError(`thread number does not exist: ${value}`);
    return selected.id;
  }
  if (!value) throw new ClientError("thread ID is required");
  return value;
}

function renderInteractiveEvent(stdout, event) {
  const text = String(event.payload?.text || event.payload?.report || "").trim();
  if (event.type === "user_message") stdout.write(`\nyou> ${text}\n`);
  else if (event.type === "assistant_message") stdout.write(`\nassistant> ${text}\n`);
  else if (event.type === "question") stdout.write(`\nassistant? ${text}\n`);
  else if (event.type === "artifact_available") stdout.write(`\n[artifact] ${text || JSON.stringify(event.payload)}\n`);
  else stdout.write(`\n[${event.type.replaceAll("_", " ")}] ${text || JSON.stringify(event.payload)}\n`);
}

async function runThreads({ client, args, stdout, stdin, sleep }) {
  const action = requiredArgument(args.shift(), "threads action");
  if (action === "list") {
    rejectExtraArguments(args);
    writeJson(stdout, (await client.request("/api/threads")).value.threads || []);
    return 0;
  }
  if (action === "show") {
    const threadId = requiredArgument(args.shift(), "thread ID");
    rejectExtraArguments(args);
    const encoded = encodeURIComponent(threadId);
    const [thread, events, sessions] = await Promise.all([
      client.request(`/api/threads/${encoded}`),
      client.request(`/api/threads/${encoded}/events?after_sequence=0&limit=500`),
      client.request(`/api/threads/${encoded}/sessions`),
    ]);
    writeJson(stdout, { thread: thread.value.thread, events: events.value.events || [], sessions: sessions.value.sessions || [] });
    return 0;
  }
  if (action === "create") {
    const options = parseOptions(args, new Set(["--repository", "--title", "--message", "--message-file"]));
    const repository = requiredOption(options, "--repository");
    const message = await readMessage(options, stdin);
    const created = await client.request("/api/threads", {
      method: "POST",
      body: { repository, title: options.get("--title") || "" },
    });
    const threadId = created.value.thread.id;
    try {
      const routed = await sendThreadMessage(client, threadId, message);
      writeJson(stdout, { thread: created.value.thread, route: routed });
    } catch (error) {
      throw new ClientError(`thread ${threadId} was created, but its initial message failed: ${error.message}`, {
        statusCode: error.statusCode,
        body: { thread: created.value.thread, cause: error.body },
      });
    }
    return 0;
  }
  if (action === "send") {
    const threadId = requiredArgument(args.shift(), "thread ID");
    const options = parseOptions(args, new Set(["--message", "--message-file"]));
    writeJson(stdout, await sendThreadMessage(client, threadId, await readMessage(options, stdin)));
    return 0;
  }
  if (action === "watch") {
    const threadId = requiredArgument(args.shift(), "thread ID");
    const options = parseOptions(args, new Set(["--after", "--once"]), new Set(["--once"]));
    let cursor = parseSequence(options.get("--after") || "0");
    do {
      const response = await client.request(`/api/threads/${encodeURIComponent(threadId)}/events?after_sequence=${cursor}&limit=200`);
      for (const event of response.value.events || []) {
        cursor = Math.max(cursor, Number(event.sequence) || 0);
        stdout.write(JSON.stringify(event) + "\n");
      }
      if (options.has("--once")) break;
      await sleep(1000);
    } while (true);
    return 0;
  }
  throw new ClientError(`unknown threads action: ${action}`);
}

async function runLegacy({ client, args, stdout }) {
  const action = requiredArgument(args.shift(), "legacy action");
  if (action === "list") {
    rejectExtraArguments(args);
    const [threads, sessions] = await Promise.all([client.request("/api/threads"), client.request("/api/sessions")]);
    const threadIds = new Set((threads.value.threads || []).map((thread) => thread.id));
    writeJson(stdout, (sessions.value.sessions || []).filter((session) => !threadIds.has(session.threadId)));
    return 0;
  }
  if (action === "report") {
    const sessionId = requiredArgument(args.shift(), "session ID");
    rejectExtraArguments(args);
    writeJson(stdout, (await client.request(`/api/sessions/${encodeURIComponent(sessionId)}/report`)).value);
    return 0;
  }
  throw new ClientError(`unknown legacy action: ${action}`);
}

async function sendThreadMessage(client, threadId, text) {
  return (await client.request(`/api/threads/${encodeURIComponent(threadId)}/messages`, {
    method: "POST",
    headers: { "idempotency-key": crypto.randomUUID() },
    body: { text },
  })).value;
}

function parseGlobalOptions(argv) {
  const args = [...argv];
  let server = "";
  let sessionFile = "";
  for (let index = 0; index < args.length;) {
    const name = args[index];
    if (name !== "--server" && name !== "--session-file") { index += 1; continue; }
    const value = args[index + 1];
    if (!value || value.startsWith("--")) throw new ClientError(`${name} requires a value`);
    if (name === "--server") server = value;
    else sessionFile = value;
    args.splice(index, 2);
  }
  return { server, sessionFile, args };
}

function parseOptions(args, allowed, flags = new Set()) {
  const options = new Map();
  while (args.length) {
    const name = args.shift();
    if (!allowed.has(name)) throw new ClientError(`unknown option: ${name}`);
    if (options.has(name)) throw new ClientError(`duplicate option: ${name}`);
    if (flags.has(name)) options.set(name, true);
    else options.set(name, requiredArgument(args.shift(), `${name} value`));
  }
  return options;
}

async function readMessage(options, stdin) {
  const inline = options.get("--message");
  const file = options.get("--message-file");
  if (inline !== undefined && file !== undefined) throw new ClientError("use only one of --message or --message-file");
  let message = inline;
  if (file !== undefined) message = file === "-" ? await readAll(stdin) : await fs.readFile(path.resolve(file), "utf8");
  if (message === undefined && !stdin.isTTY) message = await readAll(stdin);
  message = String(message || "").trim();
  if (!message) throw new ClientError("a message is required via --message, --message-file, or stdin");
  if (message.length > 32768) throw new ClientError("message exceeds 32768 characters");
  return message;
}

async function readSecret(stdin, stderr) {
  if (!stdin.isTTY) return (await readAll(stdin)).replace(/[\r\n]+$/, "");
  if (typeof stdin.setRawMode !== "function") throw new ClientError("cannot securely read a password from this terminal; pipe it on stdin");
  stderr.write("Password: ");
  stdin.setRawMode(true);
  stdin.setEncoding("utf8");
  stdin.resume();
  return new Promise((resolve, reject) => {
    let value = "";
    let finished = false;
    const cleanup = () => {
      if (finished) return;
      finished = true;
      stdin.off("data", onData);
      stdin.setRawMode(false);
      stdin.pause();
      stderr.write("\n");
    };
    const onData = (chunk) => {
      for (const character of chunk) {
        if (character === "\u0003") { cleanup(); reject(new ClientError("login cancelled")); return; }
        if (character === "\r" || character === "\n") { cleanup(); resolve(value); return; }
        if (character === "\u007f" || character === "\b") value = value.slice(0, -1);
        else value += character;
      }
    };
    stdin.on("data", onData);
  });
}

async function readAll(stream) {
  const chunks = [];
  for await (const chunk of stream) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks).toString("utf8");
}

function normalizeServer(value) {
  let server;
  try { server = new URL(String(value || "")); }
  catch { throw new ClientError("control-server URL is invalid"); }
  const local = new Set(["localhost", "127.0.0.1", "::1"]).has(server.hostname);
  if (server.protocol !== "https:" && !(server.protocol === "http:" && local)) {
    throw new ClientError("control-server URL must use HTTPS except on localhost");
  }
  server.pathname = "/";
  server.search = "";
  server.hash = "";
  return server;
}

function defaultSessionFile() {
  return path.join(os.homedir(), ".config", "multiagent", "client-session.json");
}

async function loadSession(file) {
  try {
    const value = JSON.parse(await fs.readFile(file, "utf8"));
    if (typeof value.server !== "string" || typeof value.cookie !== "string") throw new Error("invalid fields");
    return value;
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw new ClientError(`cannot read client session file: ${error.message}`);
  }
}

async function saveSession(file, value) {
  await fs.mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await fs.writeFile(temporary, JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
  await fs.rename(temporary, file);
  await fs.chmod(file, 0o600);
}

function writeJson(stream, value) {
  stream.write(JSON.stringify(value, null, 2) + "\n");
}

function requiredArgument(value, name) {
  if (!value || String(value).startsWith("--")) throw new ClientError(`${name} is required`);
  return String(value);
}

function requiredOption(options, name) {
  const value = options.get(name);
  if (!value) throw new ClientError(`${name} is required`);
  return value;
}

function rejectExtraArguments(args) {
  if (args.length) throw new ClientError(`unexpected argument: ${args[0]}`);
}

function requireAction(actual, expected, command) {
  if (actual !== expected) throw new ClientError(`${command} supports only: ${expected}`);
}

function parseSequence(value) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) throw new ClientError("--after must be a non-negative integer");
  return number;
}
