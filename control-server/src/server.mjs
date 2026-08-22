import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { execFile, execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { WebSocket, WebSocketServer } from "ws";
import { findActiveSession, tmuxInvocation } from "./session-runtime.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(here, "../public");
const launcherRoot = path.resolve(process.env.MULTIAGENT_LAUNCHER_ROOT || path.join(here, "../.."));
const stateRoot = path.resolve(process.env.MULTIAGENT_STATE_DIR || "/var/lib/multiagent/state");
const repositoryRoot = path.resolve(process.env.MULTIAGENT_REPOSITORY_ROOT || "/var/lib/multiagent/repositories");
const usersFile = path.resolve(process.env.MULTIAGENT_USERS_FILE || "/run/secrets/multiagent/users.json");
const port = Number(process.env.PORT || "8080");
const host = process.env.HOST || "0.0.0.0";
const cookieSecure = process.env.MULTIAGENT_COOKIE_SECURE !== "false";
const sessionTtlSeconds = Number(process.env.MULTIAGENT_LOGIN_TTL_SECONDS || "43200");
const captureLines = Math.min(Number(process.env.MULTIAGENT_CAPTURE_LINES || "1200"), 5000);
const s3StateUri = (process.env.MULTIAGENT_STATE_S3_URI || "").replace(/\/$/, "");
const snapshotIntervalMs = Math.max(Number(process.env.MULTIAGENT_SNAPSHOT_INTERVAL_SECONDS || "60"), 15) * 1000;
const idleTimeoutMs = Math.max(Number(process.env.MULTIAGENT_IDLE_TIMEOUT_SECONDS || "86400"), 300) * 1000;
const uidSandbox = process.env.MULTIAGENT_UID_SANDBOX === "1";
const idPattern = /^[a-z0-9][a-z0-9-]{0,62}$/;
const registryFile = path.join(stateRoot, "control-server", "sessions.json");

fs.mkdirSync(path.dirname(registryFile), { recursive: true });
fs.mkdirSync(repositoryRoot, { recursive: true });

function loadUsers() {
  const parsed = JSON.parse(fs.readFileSync(usersFile, "utf8"));
  if (!Array.isArray(parsed.users) || typeof parsed.sessionSecret !== "string" || parsed.sessionSecret.length < 32) {
    throw new Error("users file requires users[] and a sessionSecret of at least 32 characters");
  }
  return parsed;
}

let authConfig = loadUsers();
fs.watchFile(usersFile, { interval: 5000 }, () => {
  try { authConfig = loadUsers(); } catch (error) { console.error("users reload failed", error); }
});

function json(response, status, value, headers = {}) {
  const body = JSON.stringify(value);
  response.writeHead(status, { "content-type": "application/json", "content-length": Buffer.byteLength(body), ...headers });
  response.end(body);
}

function parseCookies(request) {
  return Object.fromEntries((request.headers.cookie || "").split(";").map((item) => item.trim()).filter(Boolean).map((item) => {
    const split = item.indexOf("=");
    return split < 0 ? [item, ""] : [item.slice(0, split), decodeURIComponent(item.slice(split + 1))];
  }));
}

function base64url(value) {
  return Buffer.from(value).toString("base64url");
}

function issueSession(username) {
  const payload = base64url(JSON.stringify({ username, expiresAt: Date.now() + sessionTtlSeconds * 1000, nonce: crypto.randomBytes(12).toString("hex") }));
  const signature = crypto.createHmac("sha256", authConfig.sessionSecret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function verifySession(token) {
  if (!token || !token.includes(".")) return null;
  const [payload, signature] = token.split(".", 2);
  const expected = crypto.createHmac("sha256", authConfig.sessionSecret).update(payload).digest();
  let supplied;
  try { supplied = Buffer.from(signature, "base64url"); } catch { return null; }
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) return null;
  try {
    const session = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return session.expiresAt > Date.now() ? session : null;
  } catch { return null; }
}

function currentUser(request) {
  return verifySession(parseCookies(request).multiagent_session)?.username || null;
}

function verifyPassword(password, encoded) {
  const [scheme, n, r, p, salt, expected] = String(encoded).split("$");
  if (scheme !== "scrypt") return false;
  try {
    const actual = crypto.scryptSync(password, Buffer.from(salt, "base64url"), Buffer.from(expected, "base64url").length, {
      N: Number(n), r: Number(r), p: Number(p), maxmem: 64 * 1024 * 1024,
    });
    return crypto.timingSafeEqual(actual, Buffer.from(expected, "base64url"));
  } catch { return false; }
}

function validOrigin(request) {
  const origin = request.headers.origin;
  if (!origin) return true;
  const forwardedProto = String(request.headers["x-forwarded-proto"] || (request.socket.encrypted ? "https" : "http")).split(",")[0].trim();
  const expected = `${forwardedProto}://${request.headers.host}`;
  return origin === expected || origin === process.env.MULTIAGENT_PUBLIC_URL;
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 1024 * 1024) throw new Error("request body too large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function run(command, args, options = {}) {
  return execFileSync(command, args, { encoding: "utf8", timeout: 30000, ...options }).trim();
}

function runTmux(id, args, options = {}) {
  const invocation = tmuxInvocation(stateRoot, id, args, uidSandbox);
  return run("tmux", invocation.args, { ...invocation.options, ...options });
}

function tmuxAlive(id) {
  try { runTmux(id, ["has-session", "-t", id]); return true; } catch { return false; }
}

function loadRegistry() {
  try { return JSON.parse(fs.readFileSync(registryFile, "utf8")); } catch { return { sessions: {} }; }
}

let registry = loadRegistry();
let registryWrite = Promise.resolve();

function saveRegistry() {
  const serialized = JSON.stringify(registry, null, 2) + "\n";
  registryWrite = registryWrite.then(async () => {
    const temporary = `${registryFile}.${process.pid}.tmp`;
    await fs.promises.writeFile(temporary, serialized, { mode: 0o600 });
    await fs.promises.rename(temporary, registryFile);
  });
  return registryWrite;
}

function repositoryPath(name) {
  if (!idPattern.test(name)) throw new Error("invalid repository name");
  const candidate = path.resolve(repositoryRoot, name);
  if (!candidate.startsWith(`${repositoryRoot}${path.sep}`) || !fs.existsSync(path.join(candidate, ".git"))) {
    throw new Error(`repository is not bootstrapped: ${name}`);
  }
  return candidate;
}

function sessionStateDir(id) {
  return path.join(stateRoot, "sessions", id);
}

function traceRoot(id) {
  return path.join(sessionStateDir(id), "logs");
}

function conciseTail(value, lines = 80, characters = 12000) {
  return String(value || "").split("\n").slice(-lines).join("\n").slice(-characters);
}

function activeWorkflow(id) {
  try { return fs.readFileSync(path.join(sessionStateDir(id), "runtime_state", "active-workflow-id"), "utf8").trim(); } catch { return ""; }
}

function workflowPhase(id) {
  const workflow = activeWorkflow(id);
  if (!workflow) return "";
  try {
    const lifecycle = fs.readFileSync(path.join(sessionStateDir(id), "workflows", workflow, "lifecycle", "lifecycle.env"), "utf8");
    return lifecycle.split("\n").find((line) => line.startsWith("phase="))?.slice(6).trim() || "";
  } catch { return ""; }
}

function traceReferences(id) {
  const root = traceRoot(id);
  const references = [];
  const workflow = activeWorkflow(id);
  if (workflow) references.push(`../workflows/${workflow}/lifecycle/events.log`);
  const agents = path.join(root, "agents");
  try {
    for (const entry of fs.readdirSync(agents, { withFileTypes: true }).filter((item) => item.isDirectory())) {
      const base = path.join(agents, entry.name);
      let attempt = "";
      try { attempt = fs.readFileSync(path.join(base, "latest"), "utf8").trim(); } catch {}
      const events = path.join("agents", entry.name, attempt, "events.jsonl");
      if (attempt && fs.existsSync(path.join(root, events))) references.push(events);
    }
  } catch {}
  return references;
}

function writeTraceSummary(id, status) {
  const root = traceRoot(id);
  fs.mkdirSync(root, { recursive: true });
  let finalMessage = "";
  try { finalMessage = conciseTail(fs.readFileSync(path.join(sessionStateDir(id), "orchestrator-last-message.txt"), "utf8"), 40, 6000); } catch {}
  const references = traceReferences(id);
  const report = {
    taskId: id,
    workflowId: activeWorkflow(id) || null,
    status,
    completedAt: registry.sessions[id]?.completedAt || null,
    finalMessage,
    traceReferences: references,
  };
  const markdown = [
    `# ${id}`,
    "",
    `Status: ${status}`,
    report.workflowId ? `Workflow: ${report.workflowId}` : null,
    "",
    finalMessage ? "## Final agent message" : null,
    finalMessage || null,
    "",
    "## Trace references",
    ...references.map((reference) => `- ${reference}`),
    "",
  ].filter((line) => line !== null).join("\n");
  fs.writeFileSync(path.join(root, "final-report.json"), JSON.stringify(report, null, 2) + "\n", { mode: 0o600 });
  fs.writeFileSync(path.join(root, "final-report.md"), markdown, { mode: 0o600 });
}

function launchSession(id, repository, resume, actor) {
  if (!idPattern.test(id)) throw new Error("invalid session id");
  if (tmuxAlive(id)) throw new Error("session already running");
  if (uidSandbox) {
    const active = findActiveSession(Object.keys(registry.sessions), id, tmuxAlive);
    if (active) throw new Error(`UID-isolated pods support one active session; pause ${active} first`);
  }
  const existing = registry.sessions[id];
  if (resume && !existing) throw new Error("cannot resume an unknown task");
  if (!resume && existing) throw new Error("task id already exists");
  const root = repositoryPath(repository);
  const persistent = sessionStateDir(id);
  fs.mkdirSync(persistent, { recursive: true });
  const now = new Date().toISOString();
  const args = [path.join(launcherRoot, "launch.sh"), "--session", id, "--root", root, "--no-attach"];
  if (resume) args.push("--resume");
  const env = {
    ...process.env,
    MULTIAGENT_SESSION: id,
    MULTIAGENT_ROOT: root,
    MULTIAGENT_STATE_DIR: persistent,
    MULTIAGENT_WRITE_POLICY: path.join(persistent, "write-policy.paths"),
    MULTIAGENT_PROMPT: path.join(launcherRoot, "orchestrator_prompt.md"),
    MULTIAGENT_CALLER_SUBJECT: `caller-${crypto.createHash("sha256").update(existing?.createdBy || actor).digest("hex").slice(0, 32)}`,
    MULTIAGENT_CALLER_APPROVED_AT: existing?.createdAt || now,
  };
  run("bash", args, { cwd: launcherRoot, env });
  registry.sessions[id] = {
    ...existing, id, repository, status: "running", autoResume: true,
    createdBy: existing?.createdBy || actor, createdAt: existing?.createdAt || now,
    resumedBy: resume ? actor : undefined, resumedAt: resume ? now : undefined,
    updatedAt: now, lastActivityAt: now,
  };
  saveRegistry();
  return sessionView(id);
}

function sessionView(id) {
  const record = registry.sessions[id];
  if (!record) return null;
  return { ...record, live: tmuxAlive(id) };
}

function capture(id) {
  if (!tmuxAlive(id)) {
    try { return fs.readFileSync(path.join(traceRoot(id), "terminal-tail.log"), "utf8"); } catch { return ""; }
  }
  return runTmux(id, ["capture-pane", "-p", "-J", "-S", `-${captureLines}`, "-t", `${id}:orchestrator`], { maxBuffer: 8 * 1024 * 1024 });
}

function sendInput(id, text) {
  if (!tmuxAlive(id)) throw new Error("session is not running");
  if (typeof text !== "string" || !text.trim() || text.length > 32768) throw new Error("message must contain 1 to 32768 characters");
  const load = tmuxInvocation(stateRoot, id, ["load-buffer", "-"], uidSandbox);
  execFileSync("tmux", load.args, { ...load.options, input: text, timeout: 5000 });
  runTmux(id, ["paste-buffer", "-d", "-t", `${id}:orchestrator`]);
  runTmux(id, ["send-keys", "-t", `${id}:orchestrator`, "Enter"]);
  registry.sessions[id].updatedAt = new Date().toISOString();
  registry.sessions[id].lastActivityAt = registry.sessions[id].updatedAt;
  saveRegistry();
}

function checkpoint(id) {
  const destination = path.join(sessionStateDir(id), "control-server");
  const traces = traceRoot(id);
  fs.mkdirSync(destination, { recursive: true });
  fs.mkdirSync(traces, { recursive: true });
  let terminalTail = "";
  if (tmuxAlive(id)) {
    terminalTail = conciseTail(capture(id));
    fs.writeFileSync(path.join(traces, "terminal-tail.log"), terminalTail, { mode: 0o600 });
    const digest = crypto.createHash("sha256").update(terminalTail).digest("hex");
    if (registry.sessions[id]?.lastOutputSha256 !== digest) {
      registry.sessions[id].lastOutputSha256 = digest;
      registry.sessions[id].lastActivityAt = new Date().toISOString();
      saveRegistry();
    }
  }
  const references = traceReferences(id);
  fs.writeFileSync(path.join(traces, "transcript-index.json"), JSON.stringify({ taskId: id, capturedAt: new Date().toISOString(), terminalTail: "terminal-tail.log", traceReferences: references }, null, 2) + "\n", { mode: 0o600 });
  fs.writeFileSync(path.join(destination, "checkpoint.json"), JSON.stringify({ capturedAt: new Date().toISOString(), live: tmuxAlive(id), transcriptIndex: "../logs/transcript-index.json" }, null, 2) + "\n", { mode: 0o600 });
}

let s3Sync = Promise.resolve();
function syncS3() {
  if (!s3StateUri) return Promise.resolve();
  s3Sync = s3Sync.then(() => new Promise((resolve) => {
    execFile("aws", ["s3", "sync", stateRoot, s3StateUri, "--only-show-errors", "--exclude", "worktrees/*/.git/objects/*"], { timeout: 20000, killSignal: "SIGKILL" }, (error) => {
      if (error) console.error("S3 state sync failed", error.message);
      resolve();
    });
  }));
  return s3Sync;
}

async function checkpointAll() {
  for (const id of Object.keys(registry.sessions)) {
    try { checkpoint(id); } catch (error) { console.error(`checkpoint failed for ${id}`, error); }
  }
  await syncS3();
}

async function retireSession(id, status, actor) {
  const record = registry.sessions[id];
  if (!record) throw new Error("unknown task");
  checkpoint(id);
  if (tmuxAlive(id)) runTmux(id, ["kill-session", "-t", id]);
  const now = new Date().toISOString();
  record.status = status;
  record.autoResume = false;
  record.updatedAt = now;
  record[`${status}At`] = now;
  record[`${status}By`] = actor;
  await saveRegistry();
  writeTraceSummary(id, status);
  await syncS3();
  return sessionView(id);
}

const loginAttempts = new Map();
function loginAllowed(address) {
  const entry = loginAttempts.get(address);
  return !entry || entry.blockedUntil < Date.now();
}

function recordLoginFailure(address) {
  const previous = loginAttempts.get(address) || { failures: 0, blockedUntil: 0 };
  previous.failures += 1;
  if (previous.failures >= 5) previous.blockedUntil = Date.now() + 15 * 60 * 1000;
  loginAttempts.set(address, previous);
}

function staticFile(response, file, type) {
  const body = fs.readFileSync(path.join(publicDir, file));
  response.writeHead(200, { "content-type": type, "content-length": body.length, "cache-control": "no-store" });
  response.end(body);
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  try {
    if (request.method === "GET" && url.pathname === "/healthz") return json(response, 200, { ok: true });
    if (request.method === "GET" && url.pathname === "/readyz") return json(response, 200, { ready: fs.existsSync(usersFile) });
    if (request.method === "GET" && url.pathname === "/") return staticFile(response, "index.html", "text/html; charset=utf-8");
    if (request.method === "GET" && url.pathname === "/app.js") return staticFile(response, "app.js", "text/javascript; charset=utf-8");
    if (request.method === "GET" && url.pathname === "/styles.css") return staticFile(response, "styles.css", "text/css; charset=utf-8");

    if (!validOrigin(request)) return json(response, 403, { error: "origin rejected" });
    if (request.method === "POST" && url.pathname === "/api/login") {
      const address = request.socket.remoteAddress || "unknown";
      if (!loginAllowed(address)) return json(response, 429, { error: "too many login attempts" });
      const body = await readBody(request);
      const user = authConfig.users.find((candidate) => candidate.username === body.username && candidate.disabled !== true);
      if (!user || !verifyPassword(String(body.password || ""), user.passwordHash)) {
        recordLoginFailure(address);
        return json(response, 401, { error: "invalid username or password" });
      }
      loginAttempts.delete(address);
      const cookie = `multiagent_session=${encodeURIComponent(issueSession(user.username))}; HttpOnly; SameSite=Strict; Path=/; Max-Age=${sessionTtlSeconds}${cookieSecure ? "; Secure" : ""}`;
      return json(response, 200, { username: user.username }, { "set-cookie": cookie });
    }

    const username = currentUser(request);
    if (!username) return json(response, 401, { error: "authentication required" });
    if (request.method === "POST" && url.pathname === "/api/logout") {
      return json(response, 200, { ok: true }, { "set-cookie": "multiagent_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" });
    }
    if (request.method === "GET" && url.pathname === "/api/me") return json(response, 200, { username });
    if (request.method === "GET" && url.pathname === "/api/repositories") {
      const repositories = fs.readdirSync(repositoryRoot, { withFileTypes: true }).filter((entry) => entry.isDirectory() && fs.existsSync(path.join(repositoryRoot, entry.name, ".git"))).map((entry) => entry.name).sort();
      return json(response, 200, { repositories });
    }
    if (request.method === "GET" && url.pathname === "/api/sessions") {
      return json(response, 200, { sessions: Object.keys(registry.sessions).sort().map(sessionView) });
    }
    const reportMatch = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/report$/);
    if (request.method === "GET" && reportMatch) {
      const id = reportMatch[1];
      if (!registry.sessions[id]) return json(response, 404, { error: "unknown session" });
      try {
        const report = fs.readFileSync(path.join(traceRoot(id), "final-report.md"), "utf8");
        const transcript = JSON.parse(fs.readFileSync(path.join(traceRoot(id), "transcript-index.json"), "utf8"));
        return json(response, 200, { report, transcript });
      } catch { return json(response, 200, { report: "", transcript: null }); }
    }
    if (request.method === "POST" && url.pathname === "/api/sessions") {
      const body = await readBody(request);
      return json(response, 201, launchSession(String(body.id || ""), String(body.repository || ""), Boolean(body.resume), username));
    }
    const match = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/(restart|resume|pause|complete|archive|checkpoint)$/);
    if (request.method === "POST" && match) {
      const [, id, action] = match;
      if (!registry.sessions[id]) return json(response, 404, { error: "unknown session" });
      if (action === "checkpoint") checkpoint(id);
      if (action === "pause") return json(response, 200, await retireSession(id, "paused", username));
      if (action === "complete") return json(response, 200, await retireSession(id, "completed", username));
      if (action === "archive") {
        if (tmuxAlive(id)) throw new Error("pause or complete the task before archiving");
        return json(response, 200, await retireSession(id, "archived", username));
      }
      if (action === "resume") {
        if (tmuxAlive(id)) throw new Error("task is already running");
        return json(response, 200, launchSession(id, registry.sessions[id].repository, true, username));
      }
      if (action === "restart") {
        checkpoint(id);
        if (tmuxAlive(id)) runTmux(id, ["kill-session", "-t", id]);
        return json(response, 200, launchSession(id, registry.sessions[id].repository, true, username));
      }
      return json(response, 200, sessionView(id));
    }
    return json(response, 404, { error: "not found" });
  } catch (error) {
    console.error(error);
    return json(response, 400, { error: error.message || "request failed" });
  }
});

const sockets = new WebSocketServer({ noServer: true, maxPayload: 64 * 1024 });
server.on("upgrade", (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  const match = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/terminal$/);
  if (!match || !validOrigin(request) || !currentUser(request) || !registry.sessions[match[1]]) {
    socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
    return socket.destroy();
  }
  request.sessionId = match[1];
  sockets.handleUpgrade(request, socket, head, (websocket) => sockets.emit("connection", websocket, request));
});

sockets.on("connection", (socket, request) => {
  const id = request.sessionId;
  let previous = "";
  const publish = () => {
    try {
      const output = capture(id);
      if (output !== previous && socket.readyState === WebSocket.OPEN) {
        previous = output;
        socket.send(JSON.stringify({ type: "output", output, live: tmuxAlive(id), capturedAt: new Date().toISOString() }));
      }
    } catch (error) {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "error", error: error.message }));
    }
  };
  publish();
  const interval = setInterval(publish, 750);
  socket.on("message", (message) => {
    try {
      const payload = JSON.parse(message.toString());
      if (payload.type !== "input") throw new Error("unsupported WebSocket message");
      sendInput(id, payload.text);
      publish();
    } catch (error) { socket.send(JSON.stringify({ type: "error", error: error.message })); }
  });
  socket.on("close", () => clearInterval(interval));
});

for (const record of Object.values(registry.sessions)) {
  if (record.status === "running" && workflowPhase(record.id) === "complete") {
    const now = new Date().toISOString();
    record.status = "completed";
    record.autoResume = false;
    record.completedAt = now;
    record.completedBy = "workflow-supervisor";
    record.updatedAt = now;
    writeTraceSummary(record.id, "completed");
    saveRegistry();
  } else if (record.status === "running" && record.autoResume && !tmuxAlive(record.id)) {
    try { launchSession(record.id, record.repository, true, "system"); } catch (error) { console.error(`restore failed for ${record.id}`, error); }
  }
}

const snapshotTimer = setInterval(() => checkpointAll().catch((error) => console.error("checkpoint cycle failed", error)), snapshotIntervalMs);
const retirementTimer = setInterval(() => {
  const now = Date.now();
  for (const record of Object.values(registry.sessions)) {
    if (record.status === "running" && workflowPhase(record.id) === "complete") {
      retireSession(record.id, "completed", "workflow-supervisor").catch((error) => console.error(`completion retirement failed for ${record.id}`, error));
      continue;
    }
    if (record.status === "running" && !tmuxAlive(record.id)) {
      retireSession(record.id, "failed", "process-exit").catch((error) => console.error(`failed retirement failed for ${record.id}`, error));
      continue;
    }
    const lastActivity = Date.parse(record.lastActivityAt || record.updatedAt || record.createdAt);
    if (record.status === "running" && tmuxAlive(record.id) && Number.isFinite(lastActivity) && now - lastActivity >= idleTimeoutMs) {
      retireSession(record.id, "paused", "idle-timeout").catch((error) => console.error(`idle retirement failed for ${record.id}`, error));
    }
  }
}, 5000);
server.listen(port, host, () => console.log(`multiagent control server listening on ${host}:${port}`));

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`received ${signal}; checkpointing sessions`);
  clearInterval(snapshotTimer);
  clearInterval(retirementTimer);
  setTimeout(() => process.exit(1), 25000).unref();
  await checkpointAll();
  setTimeout(() => server.close(() => process.exit(0)), 1000).unref();
}
process.on("SIGTERM", () => void shutdown("SIGTERM"));
process.on("SIGINT", () => void shutdown("SIGINT"));
