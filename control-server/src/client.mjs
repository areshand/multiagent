import crypto from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { createInterface } from "node:readline/promises";

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
  threads create THREAD_ID --repository NAME [--title TITLE] (--message TEXT | --message-file PATH)
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
  /new THREAD_ID REPO      Create and open a thread
  /sessions                List execution sessions for the open thread
  /refresh                 Replay new events
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
    return runInteractive({ client, stdin, stdout, sleep, createInterfaceImpl, initialThreadId });
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

export async function runInteractive({ client, stdin, stdout, sleep, createInterfaceImpl = createInterface, initialThreadId = "" }) {
  if (!stdin?.isTTY) throw new ClientError("interactive mode requires a terminal; use a JSON command for non-interactive calls");
  const terminal = createInterfaceImpl({ input: stdin, output: stdout, terminal: true });
  let threads = [];
  let current = null;
  let cursor = 0;

  const listThreads = async () => {
    threads = (await client.request("/api/threads")).value.threads || [];
    if (!threads.length) {
      stdout.write("\nNo threads. Create one with /new THREAD_ID REPOSITORY.\n");
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
      cursor = Math.max(cursor, Number(event.sequence) || 0);
      renderInteractiveEvent(stdout, event);
    }
    return events;
  };

  const openThread = async (selector) => {
    const selected = selectThread(threads, selector);
    const response = await client.request(`/api/threads/${encodeURIComponent(selected)}`);
    current = response.value.thread;
    cursor = 0;
    stdout.write(`\nOpened ${current.id} [${current.state}] — ${current.repository}\n`);
    await replay({ all: true });
  };

  const waitForReply = async () => {
    while (true) {
      const events = await replay();
      if (events.some((event) => new Set(["assistant_message", "question", "session_interrupted"]).has(event.type))) return;
      await sleep(1000);
    }
  };

  stdout.write("Multiagent terminal\n");
  stdout.write("Threads are durable conversations; execution session IDs are managed by the server.\n");
  stdout.write("Type /help for commands.\n");
  try {
    await listThreads();
    if (initialThreadId) await openThread(initialThreadId);
    while (true) {
      const line = String(await terminal.question(current ? `${current.id}> ` : "multiagent> ")).trim();
      if (!line) continue;
      try {
        if (line === "/quit" || line === "/exit") return 0;
        if (line === "/help") { stdout.write(`\n${interactiveHelp}\n`); continue; }
        if (line === "/threads") { await listThreads(); continue; }
        if (line === "/refresh") { await replay(); continue; }
        if (line === "/sessions") {
          if (!current) { stdout.write("Open a thread first.\n"); continue; }
          const sessions = (await client.request(`/api/threads/${encodeURIComponent(current.id)}/sessions`)).value.sessions || [];
          if (!sessions.length) stdout.write("No execution sessions yet.\n");
          else sessions.forEach((session) => stdout.write(`  ${session.ordinal}. ${session.id}  [${session.status}]\n`));
          continue;
        }
        if (line.startsWith("/open ")) { await openThread(line.slice(6).trim()); continue; }
        if (line.startsWith("/new ")) {
          const [threadId, repository, ...titleParts] = line.slice(5).trim().split(/\s+/);
          if (!threadId || !repository) { stdout.write("Usage: /new THREAD_ID REPOSITORY [TITLE]\n"); continue; }
          const created = await client.request("/api/threads", {
            method: "POST",
            body: { id: threadId, repository, title: titleParts.join(" ") || threadId },
          });
          current = created.value.thread;
          cursor = 0;
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
          cursor = Math.max(cursor, Number(routed.event.sequence) || 0);
          renderInteractiveEvent(stdout, routed.event);
        }
        const session = routed.session;
        if (session) stdout.write(`[execution ${session.status}] ${session.id}\n`);
        await waitForReply();
      } catch (error) {
        if (!(error instanceof ClientError)) throw error;
        stdout.write(`[error] ${error.message}\n`);
      }
    }
  } finally {
    terminal.close();
  }
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
    const threadId = requiredArgument(args.shift(), "thread ID");
    const options = parseOptions(args, new Set(["--repository", "--title", "--message", "--message-file"]));
    const repository = requiredOption(options, "--repository");
    const message = await readMessage(options, stdin);
    const created = await client.request("/api/threads", {
      method: "POST",
      body: { id: threadId, repository, title: options.get("--title") || threadId },
    });
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
