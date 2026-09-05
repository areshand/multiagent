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
  reviews list [--status pending|approved|rejected|all]
  reviews decide REVIEW_ID yes|no
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
  /reviews                 Refresh pending repair reviews
  /list                    List threads created by this local client
  /open THREAD_ID          Open a thread
  /new REPO [TITLE]        Create and open a server-assigned thread
  /sessions                List Sessions for the open thread
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
  const setIntervalImpl = dependencies.setInterval || setInterval;
  const clearIntervalImpl = dependencies.clearInterval || clearInterval;
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
  const threadIndex = {
    file: `${sessionFile}.threads.json`,
    server: normalizedServer,
    username: stored?.server === normalizedServer ? String(stored.username || "") : "",
  };

  if (!command || command === "connect") {
    if (!client.cookie) throw new ClientError(`not logged in to ${normalizedServer}; run the login command first`);
    const initialThreadId = command === "connect" ? parsed.args.shift() || "" : "";
    rejectExtraArguments(parsed.args);
    return runInteractive({ client, stdin, stdout, sleep, createInterfaceImpl, createWebSocketImpl, setIntervalImpl, clearIntervalImpl, initialThreadId, threadIndex });
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

  if (command === "reviews") {
    return runReviews({ client, args: parsed.args, stdout, threadIndex });
  }

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
    return runThreads({ client, args: parsed.args, stdout, stdin, sleep, threadIndex });
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
  setIntervalImpl = setInterval,
  clearIntervalImpl = clearInterval,
  initialThreadId = "",
  threadIndex,
}) {
  if (!stdin?.isTTY) throw new ClientError("interactive mode requires a terminal; use a JSON command for non-interactive calls");
  const terminal = createInterfaceImpl({ input: stdin, output: stdout, terminal: true });
  let threads = (await loadLocalThreadIds(threadIndex)).map((id) => ({ id }));
  let current = null;
  let cursor = 0;
  let monitor = null;
  let threadConnection = null;
  let reviewTimer = null;
  let promptActive = false;
  let promptLabel = "› ";
  let reviews = [];
  const announcedReviewIds = new Set();
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
  const applyPaneEvent = (event) => {
    const outcome = paneOutcomeForEvent(event);
    if (outcome) agentPane.setOutcome(outcome.status, outcome.summary);
  };

  const listThreads = async () => {
    threads = await fetchLocalThreads(client, threadIndex);
    if (!threads.length) {
      stdout.write("\nNo threads created by this local client. Use /new REPOSITORY [TITLE].\n");
      return;
    }
    stdout.write("\nThreads\n");
    threads.forEach((thread, index) => {
      const title = thread.title && thread.title !== thread.id ? ` — ${thread.title}` : "";
      stdout.write(`  ${index + 1}. ${thread.id}${title}  [${thread.state}]  ${thread.repository}\n`);
    });
  };


  const showReviews = () => {
    if (!reviews.length) {
      interactiveOutput.write("\nNo pending repair reviews.\n");
      return;
    }
    for (const review of reviews) {
      if (announcedReviewIds.has(review.id)) continue;
      announcedReviewIds.add(review.id);
      interactiveOutput.write([
        "",
        "=== REPAIR REVIEW REQUIRED ===",
        `Review: ${review.id}`,
        `Thread: ${review.threadId}`,
        `Requested: ${review.requestedAt}`,
        "",
        review.question,
        "",
        "Enter yes to approve and start a fresh session, or no to reject and close this thread.",
        "",
      ].join("\n"));
    }
  };

  const refreshReviews = async ({ announce = false, showEmpty = false } = {}) => {
    reviews = (await client.request("/api/reviews?status=pending")).value.reviews || [];
    if (announce && (reviews.length || showEmpty)) showReviews();
    promptLabel = reviews[0] ? `review ${reviews[0].id} [yes/no]> ` : current ? "› " : "multiagent> ";
    refreshPrompt();
    return reviews;
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
      applyPaneEvent(event);
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
        applyPaneEvent(event);
        renderInteractiveEvent(interactiveOutput, event);
        if (event.type === "question" && stdout.isTTY) {
          void refreshReviews({ announce: true }).catch((error) => interactiveOutput.write(`[review refresh error] ${error.message}\n`));
        }
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
    agentPane.setOutcome("", "");
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
    if (stdout.isTTY) {
      await refreshReviews({ announce: true, showEmpty: true });
      reviewTimer = setIntervalImpl(() => {
        void refreshReviews({ announce: true }).catch((error) => interactiveOutput.write(`[review refresh error] ${error.message}\n`));
      }, 2_000);
      reviewTimer?.unref?.();
    }
    if (initialThreadId) await openThread(initialThreadId);
    while (true) {
      promptLabel = reviews[0] ? `review ${reviews[0].id} [yes/no]> ` : current ? "› " : "multiagent> ";
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
        if (line === "/reviews") { announcedReviewIds.clear(); await refreshReviews({ announce: true, showEmpty: true }); continue; }
        if (line === "/list" || line === "/threads") { await listThreads(); continue; }
        if (line === "/refresh") { await replay(); continue; }
        if (line === "/wait") {
          if (!monitor) stdout.write("No execution is currently running.\n");
          else await monitor.promise;
          continue;
        }
        if (line === "/sessions") {
          if (!current) { stdout.write("Open a thread first.\n"); continue; }
          const sessions = (await client.request(`/api/threads/${encodeURIComponent(current.id)}/sessions`)).value.sessions || [];
          if (!sessions.length) stdout.write("No Sessions yet.\n");
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
          await rememberLocalThread(threadIndex, created.value.thread.id);
          await stopMonitor();
          await stopThreadConnection();
          current = created.value.thread;
          agentPane.setOutcome("", "");
          agentPane.setThread(current);
          cursor = 0;
          threads = [...threads.filter((thread) => thread.id !== current.id), current];
          startThreadConnection(current.id);
          stdout.write(`\nOpened ${current.id}. Enter its first message.\n`);
          continue;
        }
        if (reviews[0]) {
          const review = reviews[0];
          const answer = line.toLowerCase();
          if (!new Set(["yes", "y", "no", "n"]).has(answer)) {
            stdout.write(`Review ${review.id} is pending. Enter yes or no, or use /help.\n`);
            continue;
          }
          const approve = answer === "yes" || answer === "y";
          const routed = (await client.request(`/api/reviews/${encodeURIComponent(review.id)}/decision`, {
            method: "POST",
            headers: { "idempotency-key": crypto.randomUUID() },
            body: { decision: approve ? "approve" : "reject" },
          })).value;
          reviews = reviews.filter((candidate) => candidate.id !== review.id);
          if (approve) {
            await rememberLocalThread(threadIndex, routed.thread.id);
            await stopMonitor();
            await stopThreadConnection();
            current = routed.thread;
            cursor = 0;
            threads = [...threads.filter((thread) => thread.id !== current.id), current];
            agentPane.setOutcome("", "");
            agentPane.setThread(current, routed.session?.status || "starting");
            stdout.write(`\nApproved ${review.id}. Started fresh Session ${routed.session.id} for ${current.id}.\n`);
            await replay({ all: true });
            startThreadConnection(current.id);
            await startMonitor(routed.session.id);
          } else {
            if (current?.id === routed.thread.id) {
              current = routed.thread;
              agentPane.setThread(current);
              agentPane.setOutcome("blocked", "Repair review rejected; this thread is closed.");
            }
            stdout.write(`\nRejected ${review.id}. Thread ${routed.thread.id} cannot continue.\n`);
          }
          await refreshReviews({ announce: true });
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
            applyPaneEvent(routed.event);
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
    if (reviewTimer) clearIntervalImpl(reviewTimer);
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

const inactiveAgentStatuses = new Set(["complete", "done", "completed", "closed", "cancelled", "canceled", "failed", "released", "skipped", "finalized", "killed", "missing"]);

export function paneOutcomeForEvent(event) {
  const type = String(event?.type || "");
  if (type === "user_message") return { status: "", summary: "" };
  if (type === "session_started") return { status: "running", summary: "" };
  if (type === "assistant_message") {
    const summary = compactOutcomeSummary(event);
    const status = /^(?:blocker\s*:|.*\bblocked\b)/i.test(summary) ? "blocked" : "complete";
    return { status, summary };
  }
  if (type === "question") return { status: "waiting", summary: compactOutcomeSummary(event) };
  if (type === "session_interrupted") return { status: "interrupted", summary: compactOutcomeSummary(event) };
  if (type === "progress") return { status: "working", summary: compactOutcomeSummary(event) };
  if (type === "session_completed") return { status: "complete", summary: undefined };
  return null;
}

export function finalAgentMessageText(value) {
  const sources = String(value || "").split(/\r?\n/);
  const finalMessageHeading = sources.findIndex((source) => /^\s*#{1,6}\s+final agent message\s*$/i.test(source));
  if (finalMessageHeading < 0) return String(value || "").trim();
  const finalSection = sources.slice(finalMessageHeading + 1);
  const traceHeading = finalSection.findIndex((source) => /^\s*#{1,6}\s+trace references\s*$/i.test(source));
  return (traceHeading >= 0 ? finalSection.slice(0, traceHeading) : finalSection).join("\n").trim();
}

export function compactOutcomeSummary(event) {
  const sources = finalAgentMessageText(event?.payload?.text || event?.payload?.report || "").split(/\r?\n/);
  const entries = sources
    .map((source) => {
      const tableRow = /^\s*\|/.test(source) && /\|\s*$/.test(source);
      let text = source
        .replace(/\[([^\]]+)\]\([^\)]+\)/g, "$1")
        .replace(/^\s*#{1,6}\s*/, "")
        .replace(/[`*_>]+/g, "")
        .replace(/^\s*[-•]\s*/, "");
      if (tableRow) {
        text = text.replace(/^\s*\|\s*/, "").replace(/\s*\|\s*$/, "").replace(/\s*\|\s*/g, " — ");
      }
      return { text: text.replace(/\s+/g, " ").trim(), tableRow };
    })
    .filter(({ text }) => text);
  const meaningful = entries.filter(({ text }) =>
    !/^(?:result|summary|outcome|answer|final answer)$/i.test(text)
    && !/^(?:status|workflow|session|task)\s*:/i.test(text)
    && !/^trace references$/i.test(text)
    && !/^(?:-+)(?:\s+—\s+-+)*$/.test(text));
  const latestIndex = meaningful.findIndex(({ text }) => /\b(?:most recently|latest)\b/i.test(text));
  if (latestIndex >= 0) {
    const label = meaningful[latestIndex].text;
    if (/\bpr\b/i.test(label) && !/#\d+\b/.test(label)) {
      const detail = meaningful.slice(latestIndex + 1).find(({ text }) => /#\d+\b/.test(text));
      if (detail) return `${label.replace(/[:：]\s*$/, "")}: ${detail.text}`.slice(0, 240);
    }
  }
  const lines = meaningful.map(({ text }) => text);
  const preferred = lines.find((line) => /\b(?:most recently|latest)\b/i.test(line))
    || lines.find((line) => /^(?:result|answer|outcome|blocker|finding|conclusion)\s*:/i.test(line))
    || lines.find((line) => /\b(?:found|fixed|created|updated|merged|deployed|completed)\b/i.test(line))
    || lines[0]
    || entries[0]?.text
    || "";
  return String(preferred?.text || preferred).slice(0, 240);
}

export function renderAgentPane(agents, {
  columns = 80,
  maxRows = 6,
  connectionState = "connected",
  thread = null,
  executionStatus = "",
  agentSnapshot = null,
  outcomeStatus = "",
  taskSummary = "",
} = {}) {
  const values = Array.isArray(agents) ? agents : [];
  const executionState = outcomeStatus || executionStatus || thread?.state || "idle";
  const planning = !values.length
    && !agentSnapshot?.error
    && new Set(["running", "working", "in-progress"]).has(String(executionState).toLowerCase());
  const orchestratorStatus = planning ? "planning" : executionState;
  const connection = connectionState === "connected" ? "" : ` · ${connectionState}`;
  const rows = [`${agentStatusGlyph(orchestratorStatus)} orchestrator · ${orchestratorStatus}${connection}`];
  if (taskSummary && rows.length < maxRows) {
    rows.push(...wrapTerminalText(taskSummary, {
      columns,
      firstPrefix: "   ↳ ",
      continuationPrefix: "     ",
      maxLines: Math.min(3, maxRows - rows.length),
    }));
  }
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
    if (agentSnapshot?.error) rows.push("└─ ◌ subagent status unavailable");
    else if (planning) rows.push("└─ ○ no delegated agents yet");
    else rows.push("└─ ○ no active agents");
  }
  return rows.slice(0, maxRows).map((line) => truncateTerminalLine(line, columns));
}

function agentStatusGlyph(status) {
  const value = String(status || "").toLowerCase();
  if (new Set(["blocked", "failed", "killed", "cancelled", "canceled", "delivery-blocked", "interrupted"]).has(value)) return "×";
  if (inactiveAgentStatuses.has(value)) return "✓";
  if (new Set(["starting", "queued", "connecting", "restoring", "waiting"]).has(value)) return "◌";
  if (new Set(["running", "working", "in-progress", "planning"]).has(value)) return "●";
  return "○";
}

function truncateTerminalLine(value, columns) {
  const width = Math.max(8, Number(columns) || 80);
  const line = String(value || "").replace(/[\r\n\t]+/g, " ");
  if (line.length <= width) return line;
  return width <= 3 ? line.slice(0, width) : `${line.slice(0, width - 3)}...`;
}

function wrapTerminalText(value, {
  columns,
  firstPrefix = "",
  continuationPrefix = firstPrefix,
  maxLines = 3,
} = {}) {
  const width = Math.max(8, Number(columns) || 80);
  let remaining = String(value || "").replace(/[\r\n\t]+/g, " ").replace(/\s+/g, " ").trim();
  const lines = [];
  while (remaining && lines.length < maxLines) {
    const prefix = lines.length === 0 ? firstPrefix : continuationPrefix;
    const available = Math.max(1, width - prefix.length);
    if (remaining.length <= available) {
      lines.push(prefix + remaining);
      remaining = "";
      break;
    }
    let split = remaining.lastIndexOf(" ", available);
    if (split <= 0) split = available;
    lines.push(prefix + remaining.slice(0, split).trimEnd());
    remaining = remaining.slice(split).trimStart();
  }
  if (remaining && lines.length) {
    const last = lines.length - 1;
    lines[last] = truncateTerminalLine(`${lines[last]}…`, width);
  }
  return lines;
}

function createAgentPane(stdout, { onDraw = () => {} } = {}) {
  const enabled = Boolean(stdout?.isTTY && Number(stdout.rows) >= 8);
  let agents = [];
  let connectionState = "disconnected";
  let thread = null;
  let executionStatus = "";
  let agentSnapshot = null;
  let outcomeStatus = "";
  let taskSummary = "";
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
      outcomeStatus,
      taskSummary,
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
    setOutcome(nextStatus, nextSummary) {
      if (nextStatus !== undefined) outcomeStatus = nextStatus || "";
      if (nextSummary !== undefined) taskSummary = nextSummary || "";
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
  const rawText = String(event.payload?.text || event.payload?.report || "").trim();
  const text = new Set(["assistant_message", "question", "session_interrupted"]).has(event.type)
    ? finalAgentMessageText(rawText)
    : rawText;
  if (event.type === "user_message") stdout.write(`\nyou> ${text}\n`);
  else if (event.type === "system_message") stdout.write(`\nslack> ${text}\n`);
  else if (event.type === "assistant_message") stdout.write(`\nassistant> ${text}\n`);
  else if (event.type === "question") stdout.write(`\nassistant? ${text}\n`);
  else if (event.type === "review_resolved") stdout.write(`\n[review ${event.payload?.decision}] ${event.payload?.reviewId || ""}\n`);
  else if (event.type === "review_requested") stdout.write(`\n[review required] ${text || event.payload?.reviewId || ""}\n`);
  else if (event.type === "artifact_available") stdout.write(`\n[artifact] ${text || JSON.stringify(event.payload)}\n`);
  else stdout.write(`\n[${event.type.replaceAll("_", " ")}] ${text || JSON.stringify(event.payload)}\n`);
}

async function runThreads({ client, args, stdout, stdin, sleep, threadIndex }) {
  const action = requiredArgument(args.shift(), "threads action");
  if (action === "list") {
    rejectExtraArguments(args);
    writeJson(stdout, await fetchLocalThreads(client, threadIndex));
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
    await rememberLocalThread(threadIndex, threadId);
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

async function runReviews({ client, args, stdout, threadIndex }) {
  const action = requiredArgument(args.shift(), "reviews action");
  if (action === "list") {
    const options = parseOptions(args, new Set(["--status"]));
    const status = options.get("--status") || "pending";
    if (!new Set(["pending", "approved", "rejected", "all"]).has(status)) {
      throw new ClientError("--status must be pending, approved, rejected, or all");
    }
    writeJson(stdout, (await client.request(`/api/reviews?status=${encodeURIComponent(status)}`)).value.reviews || []);
    return 0;
  }
  if (action === "decide") {
    const reviewId = requiredArgument(args.shift(), "review ID");
    const answer = requiredArgument(args.shift(), "yes or no").toLowerCase();
    rejectExtraArguments(args);
    if (!new Set(["yes", "no", "y", "n"]).has(answer)) throw new ClientError("review decision must be yes or no");
    const result = (await client.request(`/api/reviews/${encodeURIComponent(reviewId)}/decision`, {
      method: "POST",
      headers: { "idempotency-key": crypto.randomUUID() },
      body: { decision: new Set(["yes", "y"]).has(answer) ? "approve" : "reject" },
    })).value;
    if (result.thread?.id) await rememberLocalThread(threadIndex, result.thread.id);
    writeJson(stdout, result);
    return 0;
  }
  throw new ClientError(`unknown reviews action: ${action}`);
}

async function runLegacy({ client, args, stdout }) {
  const action = requiredArgument(args.shift(), "legacy action");
  if (action === "list") {
    rejectExtraArguments(args);
    writeJson(stdout, (await client.request("/api/sessions")).value.sessions || []);
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

async function loadThreadIndex(file) {
  try {
    const value = JSON.parse(await fs.readFile(file, "utf8"));
    if (value.schemaVersion !== 1 || !Array.isArray(value.profiles)) throw new Error("invalid fields");
    return value;
  } catch (error) {
    if (error.code === "ENOENT") return { schemaVersion: 1, profiles: [] };
    throw new ClientError(`cannot read local thread index: ${error.message}`);
  }
}

async function loadLocalThreadIds(threadIndex) {
  if (!threadIndex?.file || !threadIndex.server || !threadIndex.username) return [];
  const value = await loadThreadIndex(threadIndex.file);
  const profile = value.profiles.find((candidate) => candidate?.server === threadIndex.server && candidate?.username === threadIndex.username);
  if (!profile) return [];
  if (!Array.isArray(profile.threadIds)) throw new ClientError("cannot read local thread index: invalid thread IDs");
  return [...new Set(profile.threadIds.filter((id) => typeof id === "string" && /^[a-z0-9-]+$/.test(id)))];
}

async function rememberLocalThread(threadIndex, threadId) {
  if (!threadIndex?.file || !threadIndex.server || !threadIndex.username) {
    throw new ClientError("cannot record the local thread without an authenticated client profile");
  }
  const value = await loadThreadIndex(threadIndex.file);
  let profile = value.profiles.find((candidate) => candidate?.server === threadIndex.server && candidate?.username === threadIndex.username);
  if (!profile) {
    profile = { server: threadIndex.server, username: threadIndex.username, threadIds: [] };
    value.profiles.push(profile);
  }
  profile.threadIds = [...new Set([...(Array.isArray(profile.threadIds) ? profile.threadIds : []), threadId])];
  await saveSession(threadIndex.file, value);
}

async function fetchLocalThreads(client, threadIndex) {
  const ids = await loadLocalThreadIds(threadIndex);
  return Promise.all(ids.map(async (id) => {
    try {
      return (await client.request(`/api/threads/${encodeURIComponent(id)}`)).value.thread;
    } catch (error) {
      if (error instanceof ClientError && error.statusCode === 404) return { id, state: "unavailable", repository: "-" };
      throw error;
    }
  }));
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
