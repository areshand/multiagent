import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { WebSocket, WebSocketServer } from "ws";
import { jobPhase, KubernetesSessionClient } from "./kubernetes-session.mjs";
import { createThreadStore } from "./thread-store.mjs";
import {
  completionExitDelayMs,
  controlMode,
  findActiveSession,
  normalizeWorkerReport,
  scopedThreadTranscript,
  selectFinalMessage,
  sessionControlInvocation,
  sessionLaunchInvocation,
  validResourceId,
} from "./session-runtime.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(here, "../public");
const launcherRoot = path.resolve(process.env.MULTIAGENT_LAUNCHER_ROOT || path.join(here, "../.."));
const stateRoot = path.resolve(process.env.MULTIAGENT_STATE_DIR || "/var/lib/multiagent/state");
const repositoryRoot = path.resolve(process.env.MULTIAGENT_REPOSITORY_ROOT || "/var/lib/multiagent/repositories");
const usersFile = path.resolve(process.env.MULTIAGENT_USERS_FILE || "/run/secrets/multiagent/users.json");
const traceExportStatusFile = process.env.MULTIAGENT_TRACE_EXPORT_STATUS_FILE
  ? path.resolve(process.env.MULTIAGENT_TRACE_EXPORT_STATUS_FILE)
  : null;
const traceExportMaxAgeSeconds = Math.max(Number(process.env.MULTIAGENT_TRACE_EXPORT_MAX_AGE_SECONDS || "120"), 30);
const port = Number(process.env.PORT || "8080");
const host = process.env.HOST || "0.0.0.0";
const cookieSecure = process.env.MULTIAGENT_COOKIE_SECURE !== "false";
const sessionTtlSeconds = Number(process.env.MULTIAGENT_LOGIN_TTL_SECONDS || "43200");
const captureLines = Math.min(Number(process.env.MULTIAGENT_CAPTURE_LINES || "1200"), 5000);
const snapshotIntervalMs = Math.max(Number(process.env.MULTIAGENT_SNAPSHOT_INTERVAL_SECONDS || "60"), 15) * 1000;
const idleTimeoutMs = Math.max(Number(process.env.MULTIAGENT_IDLE_TIMEOUT_SECONDS || "86400"), 300) * 1000;
const completionGraceMs = completionExitDelayMs();
const uidSandbox = process.env.MULTIAGENT_UID_SANDBOX === "1";
const mode = controlMode();
const gatewayMode = mode === "gateway";
const workerMode = mode === "session-worker";
const registryFile = path.join(stateRoot, "control-server", "sessions.json");
const sessionJobTemplateFile = process.env.MULTIAGENT_SESSION_JOB_TEMPLATE_FILE || "/etc/multiagent-session/job-template.json";
const repositoryCatalog = gatewayMode ? JSON.parse(process.env.MULTIAGENT_REPOSITORIES_JSON || "{}") : {};
const kubernetes = gatewayMode ? new KubernetesSessionClient() : null;
const sessionJobTemplate = gatewayMode ? JSON.parse(fs.readFileSync(sessionJobTemplateFile, "utf8")) : null;
const threadStore = await createThreadStore({
  backend: process.env.MULTIAGENT_THREAD_STORE_BACKEND || (gatewayMode ? "file" : "memory"),
  filePath: process.env.MULTIAGENT_THREAD_STORE_FILE || path.join(stateRoot, "control-server", "thread-manifest-v1.json"),
});

fs.mkdirSync(path.dirname(registryFile), { recursive: true });
fs.mkdirSync(repositoryRoot, { recursive: true });

function loadUsers() {
  const parsed = JSON.parse(fs.readFileSync(usersFile, "utf8"));
  if (!Array.isArray(parsed.users) || parsed.users.length === 0 || typeof parsed.sessionSecret !== "string" || parsed.sessionSecret.length < 32) {
    throw new Error("users file requires at least one named user and a sessionSecret of at least 32 characters");
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

function issueWorkerToken(sessionId) {
  const payload = base64url(JSON.stringify({
    audience: "multiagent-session-worker",
    sessionId,
    expiresAt: Date.now() + 5 * 60_000,
    nonce: crypto.randomBytes(12).toString("hex"),
  }));
  const signature = crypto.createHmac("sha256", authConfig.sessionSecret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function verifyWorkerToken(request, sessionId) {
  if (!workerMode) return false;
  const authorization = String(request.headers.authorization || "");
  if (!authorization.startsWith("Bearer ")) return false;
  const token = authorization.slice(7);
  const [payload, signature] = token.split(".", 2);
  if (!payload || !signature) return false;
  const expected = crypto.createHmac("sha256", authConfig.sessionSecret).update(payload).digest();
  let supplied;
  try { supplied = Buffer.from(signature, "base64url"); } catch { return false; }
  if (supplied.length !== expected.length || !crypto.timingSafeEqual(supplied, expected)) return false;
  try {
    const value = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
    return value.audience === "multiagent-session-worker" && value.sessionId === sessionId && value.expiresAt > Date.now();
  } catch { return false; }
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
  return run("tmux", args, options);
}

function runSessionControl(id, action, args = [], options = {}) {
  const invocation = sessionControlInvocation(id, action, args);
  return run(invocation.command, invocation.args, { ...invocation.options, ...options });
}

function tmuxAlive(id) {
  try {
    if (uidSandbox) runSessionControl(id, "status");
    else runTmux(id, ["has-session", "-t", id]);
    return true;
  } catch { return false; }
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
  if (!validResourceId(name)) throw new Error("invalid repository name");
  const candidate = path.resolve(repositoryRoot, name);
  if (!candidate.startsWith(`${repositoryRoot}${path.sep}`) || !fs.existsSync(path.join(candidate, ".git"))) {
    throw new Error(`repository is not bootstrapped: ${name}`);
  }
  return candidate;
}

function configuredRepository(name) {
  if (!validResourceId(name) || typeof repositoryCatalog[name] !== "string" || !repositoryCatalog[name]) {
    throw new Error(`repository is not configured: ${name}`);
  }
  return repositoryCatalog[name];
}

function sessionStateDir(id) {
  return path.join(stateRoot, "sessions", id);
}

function traceRoot(id) {
  return path.join(sessionStateDir(id), "logs");
}

function gatewayReportFile(id) {
  return path.join(stateRoot, "control-server", "reports", `${id}.json`);
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
  let result = "";
  let fallback = "";
  try { result = conciseTail(fs.readFileSync(path.join(sessionStateDir(id), "orchestrator-result.md"), "utf8"), 80, 6000); } catch {}
  try { fallback = conciseTail(fs.readFileSync(path.join(sessionStateDir(id), "orchestrator-last-message.txt"), "utf8"), 40, 6000); } catch {}
  const finalMessage = selectFinalMessage(result, fallback);
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

function launchSession(id, repository, resume, actor, originalTask = "", metadata = {}) {
  if (!validResourceId(id)) throw new Error("invalid session id");
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
  const controlState = path.join(persistent, "control-server");
  fs.mkdirSync(controlState, { recursive: true, mode: 0o750 });
  const originalTaskFile = path.join(controlState, "original-task.md");
  if (!resume) {
    const task = String(originalTask || "").trim();
    if (!task || task.length > 32768) throw new Error("task must contain 1 to 32768 characters");
    fs.writeFileSync(originalTaskFile, task + "\n", { mode: 0o640 });
  } else if (!fs.existsSync(originalTaskFile)) {
    throw new Error("cannot resume a task without its bound original task");
  }
  const now = new Date().toISOString();
  const authorityActor = actor === "system" ? (existing?.authorityActor || existing?.createdBy) : actor;
  const authorityApprovedAt = actor === "system" ? (existing?.authorityApprovedAt || existing?.createdAt) : now;
  const invocation = sessionLaunchInvocation(launcherRoot, id, root, resume);
  const env = {
    ...process.env,
    MULTIAGENT_SESSION: id,
    MULTIAGENT_THREAD_ID: metadata.threadId || existing?.threadId || id,
    MULTIAGENT_LEASE_GENERATION: String(metadata.leaseGeneration || existing?.leaseGeneration || 1),
    MULTIAGENT_AUTHORIZING_EVENT_ID: metadata.authorizingEventId || existing?.authorizingEventId || id,
    MULTIAGENT_ROOT: root,
    MULTIAGENT_STATE_DIR: persistent,
    MULTIAGENT_WRITE_POLICY: path.join(persistent, "write-policy.paths"),
    MULTIAGENT_PROMPT: path.join(launcherRoot, "orchestrator_prompt.md"),
    MULTIAGENT_ORIGINAL_TASK_FILE: originalTaskFile,
    MULTIAGENT_USER_MESSAGE_FILE: fs.existsSync(path.join(controlState, "pending-user-message.md"))
      ? path.join(controlState, "pending-user-message.md")
      : "",
    MULTIAGENT_CALLER_SUBJECT: `caller-${crypto.createHash("sha256").update(authorityActor).digest("hex").slice(0, 32)}`,
    MULTIAGENT_CALLER_APPROVED_AT: authorityApprovedAt,
  };
  run(invocation.command, invocation.args, { cwd: launcherRoot, env });
  registry.sessions[id] = {
    ...existing, id, repository, status: "running", autoResume: true,
    threadId: metadata.threadId || existing?.threadId || id,
    leaseGeneration: metadata.leaseGeneration || existing?.leaseGeneration || 1,
    authorizingEventId: metadata.authorizingEventId || existing?.authorizingEventId || id,
    createdBy: existing?.createdBy || actor, createdAt: existing?.createdAt || now,
    authorityActor, authorityApprovedAt,
    resumedBy: resume ? actor : undefined, resumedAt: resume ? now : undefined,
    updatedAt: now, lastActivityAt: now,
  };
  saveRegistry();
  return sessionView(id);
}

async function launchGatewaySession(id, repository, resume, actor, originalTask = "", metadata = {}) {
  if (!validResourceId(id)) throw new Error("invalid session id");
  if (registry.sessions[id]) throw new Error("task id already exists");
  const task = String(originalTask || "").trim();
  if (!task || task.length > 32768) throw new Error("task must contain 1 to 32768 characters");
  const repositoryUrl = configuredRepository(repository);
  const callerSubject = `caller-${crypto.createHash("sha256").update(actor).digest("hex").slice(0, 32)}`;
  fs.rmSync(gatewayReportFile(id), { force: true });
  await kubernetes.createSession({
    id,
    threadId: metadata.threadId || id,
    leaseGeneration: metadata.leaseGeneration || 1,
    authorizingEventId: metadata.authorizingEventId || id,
    task,
    actor: callerSubject,
    repositoryName: repository,
    repositoryUrl,
    resume,
    template: sessionJobTemplate,
  });
  const now = new Date().toISOString();
  registry.sessions[id] = {
    id,
    threadId: metadata.threadId || id,
    leaseGeneration: metadata.leaseGeneration || 1,
    authorizingEventId: metadata.authorizingEventId || id,
    repository,
    status: "pending",
    live: false,
    autoResume: true,
    createdBy: actor,
    createdAt: now,
    updatedAt: now,
    lastActivityAt: now,
  };
  await saveRegistry();
  return registry.sessions[id];
}

function readGatewayReport(id) {
  try { return normalizeWorkerReport(JSON.parse(fs.readFileSync(gatewayReportFile(id), "utf8"))); }
  catch { return null; }
}

async function writeGatewayReport(id, report) {
  const file = gatewayReportFile(id);
  await fs.promises.mkdir(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.${crypto.randomUUID()}.tmp`;
  await fs.promises.writeFile(temporary, JSON.stringify(report) + "\n", { mode: 0o600 });
  await fs.promises.rename(temporary, file);
}

function fetchWorkerReport(id, podIP) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: podIP,
      port: 8080,
      method: "GET",
      path: `/api/sessions/${id}/report`,
      headers: { accept: "application/json", authorization: `Bearer ${issueWorkerToken(id)}` },
      timeout: 3000,
    }, (response) => {
      const chunks = [];
      let size = 0;
      response.on("data", (chunk) => {
        size += chunk.length;
        if (size > 128 * 1024) response.destroy(new Error("session report exceeds cache limit"));
        else chunks.push(chunk);
      });
      response.on("end", () => {
        if (response.statusCode < 200 || response.statusCode >= 300) return reject(new Error(`session worker report returned ${response.statusCode}`));
        try { resolve(JSON.parse(Buffer.concat(chunks).toString("utf8"))); }
        catch { reject(new Error("session worker returned invalid report JSON")); }
      });
    });
    request.on("timeout", () => request.destroy(new Error("session worker report timed out")));
    request.on("error", reject);
    request.end();
  });
}

async function reconcileGatewaySession(id) {
  const record = registry.sessions[id];
  if (!record) return null;
  const [job, pod] = await Promise.all([kubernetes.getJob(id), kubernetes.getPod(id)]);
  const status = jobPhase(job);
  const podIP = pod?.status?.podIP || null;
  const live = status === "running" && Boolean(podIP);
  if (live && !readGatewayReport(id)) {
    try {
      const report = normalizeWorkerReport(await fetchWorkerReport(id, podIP));
      if (report) await writeGatewayReport(id, report);
    } catch {}
  }
  if (record.status !== status || record.live !== live || record.podIP !== pod?.status?.podIP) {
    record.status = status;
    record.live = live;
    record.podIP = pod?.status?.podIP || null;
    record.updatedAt = new Date().toISOString();
    await saveRegistry();
  }
  await projectGatewaySessionToThread(id, status);
  return record;
}

async function workerEndpoint(id) {
  const record = await reconcileGatewaySession(id);
  if (!record?.podIP) throw new Error("session worker is not ready");
  return `http://${record.podIP}:8080`;
}

function proxyHttp(request, response, endpoint, workerSessionId = null) {
  const target = new URL(request.url, endpoint);
  const headers = { ...request.headers, host: target.host };
  delete headers.cookie;
  delete headers.authorization;
  if (workerSessionId) headers.authorization = `Bearer ${issueWorkerToken(workerSessionId)}`;
  const proxy = http.request(target, { method: request.method, headers }, (upstream) => {
    response.writeHead(upstream.statusCode || 502, upstream.headers);
    upstream.pipe(response);
  });
  proxy.on("error", (error) => json(response, 502, { error: error.message }));
  request.pipe(proxy);
}

function threadSessionId(threadId) {
  const suffix = crypto.randomBytes(8).toString("hex");
  return `${threadId.slice(0, 45)}-${suffix}`;
}

function renderThreadTask(envelope) {
  const lines = [
    `Continue durable thread ${envelope.threadId}.`,
    "Treat the following as user-visible conversation context, not as reusable authorization.",
    "",
  ];
  if (envelope.checkpoint?.content) lines.push("Context checkpoint:", envelope.checkpoint.content, "");
  for (const event of envelope.recentEvents) {
    const text = String(event.payload?.text || event.payload?.report || "").trim();
    if (!text) continue;
    const role = event.type === "user_message" ? "User" : event.type === "assistant_message" ? "Assistant" : "Status";
    lines.push(`${role}: ${text}`, "");
    const references = event.payload?.transcript?.traceReferences;
    if (Array.isArray(references) && references.length) {
      lines.push("Prior session trace references:", ...references.slice(0, 16).map((reference) => `- ${String(reference)}`), "");
    }
  }
  return lines.join("\n").slice(-32768);
}

async function launchThreadExecution(thread, session) {
  const envelope = await threadStore.contextEnvelope({
    threadId: thread.id,
    actor: thread.ownerSubject,
    sessionId: session.id,
  });
  const task = renderThreadTask(envelope);
  try {
    if (gatewayMode) {
      await launchGatewaySession(session.id, thread.repository, false, thread.ownerSubject, task, {
        threadId: thread.id,
        leaseGeneration: session.leaseGeneration,
        authorizingEventId: session.triggerMessageId,
      });
    } else {
      launchSession(session.id, thread.repository, false, thread.ownerSubject, task, {
        threadId: thread.id,
        leaseGeneration: session.leaseGeneration,
        authorizingEventId: session.triggerMessageId,
      });
    }
    await threadStore.markSessionRunning({
      threadId: thread.id,
      sessionId: session.id,
      generation: session.leaseGeneration,
    });
  } catch (error) {
    await threadStore.finalizeSession({
      threadId: thread.id,
      sessionId: session.id,
      generation: session.leaseGeneration,
      status: "interrupted",
    });
    throw error;
  }
}

async function projectGatewaySessionToThread(id, status) {
  const record = registry.sessions[id];
  if (!record?.threadId || record.threadProjectedAt) return;
  if (status === "completed") {
    const report = readGatewayReport(id);
    if (!report?.report) return;
    const sessions = await threadStore.listSessionsForActor({ threadId: record.threadId, actor: record.createdBy });
    const session = sessions.find((candidate) => candidate.id === id);
    if (!session) return;
    await threadStore.appendFencedSessionEvent({
      threadId: record.threadId,
      sessionId: id,
      generation: record.leaseGeneration,
      eventId: `final-${id}`,
      type: "assistant_message",
      payload: { text: report.report, transcript: scopedThreadTranscript(id, report.transcript) },
    });
    await threadStore.acknowledgeInbox({
      threadId: record.threadId,
      sessionId: id,
      generation: record.leaseGeneration,
      throughSequence: session.inboxHeadSequence,
    });
    await threadStore.markSessionFinishing({ threadId: record.threadId, sessionId: id, generation: record.leaseGeneration });
    const finalized = await threadStore.finalizeSession({ threadId: record.threadId, sessionId: id, generation: record.leaseGeneration });
    record.threadProjectedAt = new Date().toISOString();
    await saveRegistry();
    if (finalized.activatedSession) {
      const thread = await threadStore.getThreadForActor(record.threadId, record.createdBy);
      await launchThreadExecution(thread, finalized.activatedSession);
    }
  } else if (status === "failed") {
    await threadStore.appendFencedSessionEvent({
      threadId: record.threadId,
      sessionId: id,
      generation: record.leaseGeneration,
      eventId: `interrupted-${id}`,
      type: "session_interrupted",
      payload: { text: "Execution session failed" },
    });
    await threadStore.finalizeSession({ threadId: record.threadId, sessionId: id, generation: record.leaseGeneration, status: "interrupted" });
    record.threadProjectedAt = new Date().toISOString();
    await saveRegistry();
  }
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
  if (uidSandbox) return runSessionControl(id, "capture", [String(captureLines)], { maxBuffer: 8 * 1024 * 1024 });
  return runTmux(id, ["capture-pane", "-p", "-J", "-S", `-${captureLines}`, "-t", `${id}:orchestrator`], { maxBuffer: 8 * 1024 * 1024 });
}

function sendInput(id, text) {
  if (!tmuxAlive(id)) throw new Error("session is not running");
  if (typeof text !== "string" || !text.trim() || text.length > 32768) throw new Error("message must contain 1 to 32768 characters");
  if (uidSandbox) {
    runSessionControl(id, "submit", [], { input: text, timeout: 5000 });
    registry.sessions[id].updatedAt = new Date().toISOString();
    registry.sessions[id].lastActivityAt = registry.sessions[id].updatedAt;
    saveRegistry();
    return;
  }
  execFileSync("tmux", ["load-buffer", "-"], { input: text, timeout: 5000 });
  runTmux(id, ["paste-buffer", "-d", "-t", `${id}:orchestrator`]);
  runTmux(id, ["send-keys", "-t", `${id}:orchestrator`, "Enter"]);
  registry.sessions[id].updatedAt = new Date().toISOString();
  registry.sessions[id].lastActivityAt = registry.sessions[id].updatedAt;
  saveRegistry();
}

function restartWithUserMessage(id, text, actor) {
  if (typeof text !== "string" || !text.trim() || text.length > 32768) throw new Error("message must contain 1 to 32768 characters");
  const controlState = path.join(sessionStateDir(id), "control-server");
  const history = path.join(controlState, "user-messages");
  fs.mkdirSync(history, { recursive: true, mode: 0o750 });
  const submittedAt = new Date().toISOString();
  const record = { actor, submittedAt, text, sha256: crypto.createHash("sha256").update(text).digest("hex") };
  const name = `${submittedAt.replace(/[^0-9]/g, "")}-${crypto.randomBytes(6).toString("hex")}.json`;
  fs.writeFileSync(path.join(history, name), JSON.stringify(record, null, 2) + "\n", { mode: 0o640 });
  fs.writeFileSync(path.join(controlState, "pending-user-message.md"), text.trim() + "\n", { mode: 0o640 });
  if (tmuxAlive(id)) {
    if (uidSandbox) runSessionControl(id, "stop");
    else runTmux(id, ["kill-session", "-t", id]);
  }
  return launchSession(id, registry.sessions[id].repository, true, actor);
}

function checkpoint(id) {
  const destination = path.join(sessionStateDir(id), "control-server");
  const traces = traceRoot(id);
  fs.mkdirSync(destination, { recursive: true });
  fs.mkdirSync(traces, { recursive: true });
  let terminalTail = "";
  if (tmuxAlive(id)) {
    try {
      terminalTail = conciseTail(capture(id));
      fs.writeFileSync(path.join(traces, "terminal-tail.log"), terminalTail, { mode: 0o600 });
      const digest = crypto.createHash("sha256").update(terminalTail).digest("hex");
      if (registry.sessions[id]?.lastOutputSha256 !== digest) {
        registry.sessions[id].lastOutputSha256 = digest;
        registry.sessions[id].lastActivityAt = new Date().toISOString();
        saveRegistry();
      }
    } catch (error) {
      console.error(`checkpoint capture skipped for ${id}`, error);
    }
  }
  const references = traceReferences(id);
  fs.writeFileSync(path.join(traces, "transcript-index.json"), JSON.stringify({ taskId: id, capturedAt: new Date().toISOString(), terminalTail: "terminal-tail.log", traceReferences: references }, null, 2) + "\n", { mode: 0o600 });
  fs.writeFileSync(path.join(destination, "checkpoint.json"), JSON.stringify({ capturedAt: new Date().toISOString(), live: tmuxAlive(id), transcriptIndex: "../logs/transcript-index.json" }, null, 2) + "\n", { mode: 0o600 });
}

async function checkpointAll() {
  for (const id of Object.keys(registry.sessions)) {
    try { checkpoint(id); } catch (error) { console.error(`checkpoint failed for ${id}`, error); }
  }
}

async function retireSession(id, status, actor) {
  const record = registry.sessions[id];
  if (!record) throw new Error("unknown task");
  checkpoint(id);
  if (tmuxAlive(id)) {
    if (uidSandbox) runSessionControl(id, "stop");
    else runTmux(id, ["kill-session", "-t", id]);
  }
  const now = new Date().toISOString();
  record.status = status;
  record.autoResume = false;
  record.updatedAt = now;
  record[`${status}At`] = now;
  record[`${status}By`] = actor;
  await saveRegistry();
  writeTraceSummary(id, status);
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

function traceExportStatus() {
  if (!traceExportStatusFile) return { configured: false, ready: true };
  try {
    const value = JSON.parse(fs.readFileSync(traceExportStatusFile, "utf8"));
    const ageSeconds = Math.max(0, (Date.now() - Date.parse(value.lastAttemptAt)) / 1000);
    return {
      configured: true,
      ready: value.ok === true && Number.isFinite(ageSeconds) && ageSeconds <= traceExportMaxAgeSeconds,
      ok: value.ok === true,
      lastAttemptAt: value.lastAttemptAt,
      lastSuccessAt: value.lastSuccessAt || null,
      fileCount: Number(value.fileCount || 0),
      ageSeconds: Number.isFinite(ageSeconds) ? Math.floor(ageSeconds) : null,
    };
  } catch {
    return { configured: true, ready: false, ok: false, lastAttemptAt: null, lastSuccessAt: null, fileCount: 0, ageSeconds: null };
  }
}

const server = http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  try {
    if (request.method === "GET" && url.pathname === "/healthz") return json(response, 200, { ok: true });
    if (request.method === "GET" && url.pathname === "/readyz") {
      const traceExport = traceExportStatus();
      const ready = fs.existsSync(usersFile) && traceExport.ready;
      return json(response, ready ? 200 : 503, { ready, traceExport });
    }
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
    const workerPathMatch = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)(?:\/|$)/);
    const workerSessionId = workerPathMatch && verifyWorkerToken(request, workerPathMatch[1]) ? workerPathMatch[1] : null;
    if (!username && !workerSessionId) return json(response, 401, { error: "authentication required" });
    if (request.method === "POST" && url.pathname === "/api/logout") {
      return json(response, 200, { ok: true }, { "set-cookie": "multiagent_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" });
    }
    if (request.method === "GET" && url.pathname === "/api/me") return json(response, 200, { username });
    if (request.method === "GET" && url.pathname === "/api/trace-export/status") return json(response, 200, traceExportStatus());
    if (request.method === "GET" && url.pathname === "/api/repositories") {
      const repositories = gatewayMode
        ? Object.keys(repositoryCatalog).sort()
        : fs.readdirSync(repositoryRoot, { withFileTypes: true }).filter((entry) => entry.isDirectory() && fs.existsSync(path.join(repositoryRoot, entry.name, ".git"))).map((entry) => entry.name).sort();
      return json(response, 200, { repositories });
    }
    if (request.method === "GET" && url.pathname === "/api/threads") {
      return json(response, 200, { threads: await threadStore.listThreadsForActor(username) });
    }
    if (request.method === "POST" && url.pathname === "/api/threads") {
      if (workerMode) throw new Error("session workers cannot create threads");
      const body = await readBody(request);
      if (gatewayMode) configuredRepository(String(body.repository || ""));
      else repositoryPath(String(body.repository || ""));
      const thread = await threadStore.createThread({
        id: String(body.id || ""),
        ownerSubject: username,
        repository: String(body.repository || ""),
        title: String(body.title || ""),
      });
      return json(response, 201, { thread });
    }
    const threadMatch = url.pathname.match(/^\/api\/threads\/([a-z0-9-]+)$/);
    if (request.method === "GET" && threadMatch) {
      return json(response, 200, { thread: await threadStore.getThreadForActor(threadMatch[1], username) });
    }
    const threadEventsMatch = url.pathname.match(/^\/api\/threads\/([a-z0-9-]+)\/events$/);
    if (request.method === "GET" && threadEventsMatch) {
      return json(response, 200, { events: await threadStore.readEventsAfter({
        threadId: threadEventsMatch[1],
        actor: username,
        afterSequence: Number(url.searchParams.get("after_sequence") || 0),
        limit: Number(url.searchParams.get("limit") || 200),
      }) });
    }
    const threadSessionsMatch = url.pathname.match(/^\/api\/threads\/([a-z0-9-]+)\/sessions$/);
    if (request.method === "GET" && threadSessionsMatch) {
      const threadId = threadSessionsMatch[1];
      await threadStore.getThreadForActor(threadId, username);
      if (gatewayMode) {
        await Promise.all(Object.values(registry.sessions).filter((record) => record.threadId === threadId).map((record) => reconcileGatewaySession(record.id)));
      }
      return json(response, 200, { sessions: await threadStore.listSessionsForActor({ threadId, actor: username }) });
    }
    const threadMessagesMatch = url.pathname.match(/^\/api\/threads\/([a-z0-9-]+)\/messages$/);
    if (request.method === "POST" && threadMessagesMatch) {
      if (workerMode) throw new Error("session workers cannot append user messages");
      const messageId = String(request.headers["idempotency-key"] || "");
      const body = await readBody(request);
      const thread = await threadStore.getThreadForActor(threadMessagesMatch[1], username);
      const routed = await threadStore.appendUserMessageAndRoute({
        threadId: thread.id,
        actor: username,
        messageId,
        text: String(body.text || ""),
        newSessionId: threadSessionId(thread.id),
      });
      if (routed.createdSession && routed.session.leaseGeneration !== null) await launchThreadExecution(thread, routed.session);
      return json(response, 202, routed);
    }
    if (request.method === "GET" && url.pathname === "/api/sessions") {
      if (gatewayMode) await Promise.all(Object.keys(registry.sessions).map(reconcileGatewaySession));
      return json(response, 200, { sessions: Object.keys(registry.sessions).sort().filter((id) => registry.sessions[id].createdBy === username).map((id) => gatewayMode ? registry.sessions[id] : sessionView(id)) });
    }
    const reportMatch = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/report$/);
    if (request.method === "GET" && reportMatch) {
      const id = reportMatch[1];
      if (!registry.sessions[id] || (workerSessionId !== id && registry.sessions[id].createdBy !== username)) {
        return json(response, 404, { error: "unknown session" });
      }
      if (gatewayMode) {
        const cached = readGatewayReport(id);
        if (cached) return json(response, 200, cached);
        const record = await reconcileGatewaySession(id);
        const refreshed = readGatewayReport(id);
        if (refreshed) return json(response, 200, refreshed);
        if (!record?.podIP) return json(response, 200, { report: "", transcript: null });
        return proxyHttp(request, response, `http://${record.podIP}:8080`, id);
      }
      try {
        const report = fs.readFileSync(path.join(traceRoot(id), "final-report.md"), "utf8");
        const transcript = JSON.parse(fs.readFileSync(path.join(traceRoot(id), "transcript-index.json"), "utf8"));
        return json(response, 200, { report, transcript });
      } catch { return json(response, 200, { report: "", transcript: null }); }
    }
    if (request.method === "POST" && url.pathname === "/api/sessions") {
      const body = await readBody(request);
      if (workerMode) throw new Error("session workers cannot create additional sessions");
      const session = gatewayMode
        ? await launchGatewaySession(String(body.id || ""), String(body.repository || ""), Boolean(body.resume), username, String(body.task || ""))
        : launchSession(String(body.id || ""), String(body.repository || ""), Boolean(body.resume), username, String(body.task || ""));
      return json(response, 201, session);
    }
    const match = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/(restart|resume|pause|complete|archive|checkpoint)$/);
    if (request.method === "POST" && match) {
      const [, id, action] = match;
      if (!registry.sessions[id] || (workerSessionId !== id && registry.sessions[id].createdBy !== username)) {
        return json(response, 404, { error: "unknown session" });
      }
      if (gatewayMode) return proxyHttp(request, response, await workerEndpoint(id), id);
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
        if (tmuxAlive(id)) {
          if (uidSandbox) runSessionControl(id, "stop");
          else runTmux(id, ["kill-session", "-t", id]);
        }
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
const activeSubmissions = new Set();
const submissionWindows = new Map();

function admitSubmission(username, id) {
  const key = `${username}:${id}`;
  const now = Date.now();
  const recent = (submissionWindows.get(key) || []).filter((timestamp) => now - timestamp < 60_000);
  if (recent.length >= 10) throw new Error("session submission rate limit exceeded");
  if (activeSubmissions.has(id)) throw new Error("a session submission is already being processed");
  recent.push(now);
  submissionWindows.set(key, recent);
  activeSubmissions.add(id);
}
server.on("upgrade", async (request, socket, head) => {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  const match = url.pathname.match(/^\/api\/sessions\/([a-z0-9-]+)\/terminal$/);
  const threadMatch = url.pathname.match(/^\/api\/threads\/([a-z0-9-]+)\/stream$/);
  const username = currentUser(request);
  const workerAuthorized = match ? verifyWorkerToken(request, match[1]) : false;
  let authorized = false;
  if (threadMatch && username) {
    try { await threadStore.getThreadForActor(threadMatch[1], username); authorized = true; } catch {}
  }
  if (match && registry.sessions[match[1]] && ((username && registry.sessions[match[1]].createdBy === username) || workerAuthorized)) authorized = true;
  if ((!match && !threadMatch) || !validOrigin(request) || !authorized) {
    socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
    return socket.destroy();
  }
  request.sessionId = match?.[1] || null;
  request.threadId = threadMatch?.[1] || null;
  request.username = username;
  sockets.handleUpgrade(request, socket, head, (websocket) => sockets.emit("connection", websocket, request));
});

sockets.on("connection", (socket, request) => {
  const id = request.sessionId;
  if (request.threadId) {
    let cursor = Number(new URL(request.url, "http://localhost").searchParams.get("after_sequence") || 0);
    let publishing = false;
    const publish = async () => {
      if (publishing) return;
      publishing = true;
      try {
        const events = await threadStore.readEventsAfter({ threadId: request.threadId, actor: request.username, afterSequence: cursor, limit: 200 });
        for (const event of events) {
          cursor = event.sequence;
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "event", event }));
        }
      } catch (error) {
        if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "error", error: error.message }));
      } finally { publishing = false; }
    };
    publish();
    const interval = setInterval(() => void publish(), 750);
    socket.on("message", () => socket.send(JSON.stringify({ type: "error", error: "thread stream is read-only" })));
    socket.on("close", () => clearInterval(interval));
    return;
  }
  if (gatewayMode) {
    workerEndpoint(id).then((endpoint) => {
      const target = new URL(request.url, endpoint.replace(/^http/, "ws"));
      const upstream = new WebSocket(target, { headers: { authorization: `Bearer ${issueWorkerToken(id)}` } });
      upstream.on("open", () => socket.on("message", (message) => upstream.send(message)));
      upstream.on("message", (message) => { if (socket.readyState === WebSocket.OPEN) socket.send(message); });
      upstream.on("close", () => socket.close());
      upstream.on("error", (error) => { if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "error", error: error.message })); });
      socket.on("close", () => upstream.close());
    }).catch((error) => {
      socket.send(JSON.stringify({ type: "error", error: error.message }));
      socket.close();
    });
    return;
  }
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
      admitSubmission(request.username, id);
      try {
        const session = restartWithUserMessage(id, payload.text, request.username);
        socket.send(JSON.stringify({ type: "accepted", mode: "supervisor-resume", session }));
      } finally {
        activeSubmissions.delete(id);
      }
      publish();
    } catch (error) { socket.send(JSON.stringify({ type: "error", error: error.message })); }
  });
  socket.on("close", () => clearInterval(interval));
});

if (workerMode) {
  const id = String(process.env.MULTIAGENT_SESSION_ID || "");
  const repository = String(process.env.MULTIAGENT_SESSION_REPOSITORY || "");
  const taskFile = String(process.env.MULTIAGENT_SESSION_TASK_FILE || "");
  const actor = String(process.env.MULTIAGENT_SESSION_CALLER || "deployment-gateway");
  const resume = process.env.MULTIAGENT_SESSION_RESUME === "1";
  if (!validResourceId(id) || !validResourceId(repository) || !taskFile) throw new Error("session-worker mode requires a valid session, repository, and task file");
  const threadId = String(process.env.MULTIAGENT_THREAD_ID || id);
  const leaseGeneration = Number(process.env.MULTIAGENT_LEASE_GENERATION || "1");
  const authorizingEventId = String(process.env.MULTIAGENT_AUTHORIZING_EVENT_ID || id);
  if (!registry.sessions[id]) launchSession(id, repository, resume, actor, fs.readFileSync(taskFile, "utf8"), { threadId, leaseGeneration, authorizingEventId });
}

for (const record of gatewayMode ? [] : Object.values(registry.sessions)) {
  if (record.status === "running" && workflowPhase(record.id) === "complete") {
    const now = new Date().toISOString();
    record.status = "completed";
    record.autoResume = false;
    record.completedAt = now;
    record.completedBy = "workflow-supervisor";
    record.updatedAt = now;
    writeTraceSummary(record.id, "completed");
    saveRegistry();
    if (workerMode) setTimeout(() => process.exit(0), completionGraceMs);
  } else if (record.status === "running" && record.autoResume && !tmuxAlive(record.id)) {
    try { launchSession(record.id, record.repository, true, "system"); } catch (error) { console.error(`restore failed for ${record.id}`, error); }
  }
}

const snapshotTimer = gatewayMode
  ? setInterval(() => Promise.all(Object.keys(registry.sessions).map(reconcileGatewaySession)).catch((error) => console.error("session reconciliation failed", error)), 5000)
  : setInterval(() => checkpointAll().catch((error) => console.error("checkpoint cycle failed", error)), snapshotIntervalMs);
const retirementTimer = setInterval(() => {
  if (gatewayMode) return;
  const now = Date.now();
  for (const record of Object.values(registry.sessions)) {
    if (record.status === "running" && workflowPhase(record.id) === "complete") {
      retireSession(record.id, "completed", "workflow-supervisor").then(() => {
        if (workerMode) setTimeout(() => process.exit(0), completionGraceMs);
      }).catch((error) => console.error(`completion retirement failed for ${record.id}`, error));
      continue;
    }
    if (record.status === "running" && !tmuxAlive(record.id)) {
      retireSession(record.id, "failed", "process-exit").then(() => {
        if (workerMode) setTimeout(() => process.exit(1), 1000);
      }).catch((error) => console.error(`failed retirement failed for ${record.id}`, error));
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
