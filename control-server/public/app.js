const $ = (selector) => document.querySelector(selector);

let active = null;
let socket = null;
let threads = [];
let sessions = [];
let events = [];
let threadSessions = [];

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "content-type": "application/json", ...(options.headers || {}) }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showLogin() { $("#login").hidden = false; $("#console").hidden = true; }
function showConsole(username) { $("#username").textContent = username; $("#login").hidden = true; $("#console").hidden = false; }
function selection(kind, id) { return id ? `${kind}:${id}` : null; }
function activeKind() { return active?.split(":", 1)[0] || null; }
function activeId() { return active?.slice(active.indexOf(":") + 1) || null; }

function taskState(thread) {
  if (["starting", "running"].includes(thread.state)) return thread.state;
  if (thread.queuedSessionId) return "queued";
  return thread.state || "idle";
}

function legacySessions() {
  const durableThreadIds = new Set(threads.map((thread) => thread.id));
  return sessions.filter((session) => !durableThreadIds.has(session.threadId));
}

function appendTaskButton(target, { kind, id, title, detail }) {
  const button = document.createElement("button");
  button.className = `session${active === selection(kind, id) ? " active" : ""}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const small = document.createElement("small");
  small.textContent = detail;
  button.append(strong, small);
  button.onclick = () => kind === "thread" ? selectThread(id) : selectLegacySession(id);
  target.append(button);
}

function renderTaskList() {
  const target = $("#sessions");
  target.replaceChildren();
  for (const thread of threads) {
    appendTaskButton(target, {
      kind: "thread",
      id: thread.id,
      title: thread.title || thread.id,
      detail: `${taskState(thread)} · ${thread.repository}`,
    });
  }
  for (const session of legacySessions()) {
    appendTaskButton(target, {
      kind: "session",
      id: session.id,
      title: session.id,
      detail: `legacy ${session.live ? "live" : session.status} · ${session.repository}`,
    });
  }
  if (!target.childElementCount) {
    const empty = document.createElement("p");
    empty.className = "empty-list";
    empty.textContent = "No tasks yet.";
    target.append(empty);
  }
}

function updateThreadHeader(thread) {
  $("#active-name").textContent = thread?.title || thread?.id || "Select a task";
  $("#active-repo").textContent = thread ? `${thread.repository} · ${thread.id}` : "";
  $("#live-dot").classList.toggle("live", Boolean(thread && ["starting", "running"].includes(thread.state)));
}

function showLegacyActions(show) {
  $("#legacy-actions").hidden = !show;
}

function configureLegacyActions(session) {
  const running = Boolean(session?.live);
  $("#resume").disabled = !session || running;
  $("#restart").disabled = !running;
  $("#checkpoint").disabled = !running;
  $("#pause").disabled = !running;
  $("#complete").disabled = !running;
  $("#archive").disabled = !session || running || session.status === "archived";
}

async function refreshTasks() {
  const [threadResponse, sessionResponse] = await Promise.all([api("/api/threads"), api("/api/sessions")]);
  threads = threadResponse.threads || [];
  sessions = sessionResponse.sessions || [];
  renderTaskList();

  if (activeKind() === "thread") {
    const thread = threads.find((candidate) => candidate.id === activeId());
    if (!thread) return clearSelection();
    updateThreadHeader(thread);
  } else if (activeKind() === "session") {
    const session = legacySessions().find((candidate) => candidate.id === activeId());
    if (!session) return clearSelection();
    updateLegacyHeader(session);
    configureLegacyActions(session);
  }
}

function closeSocket() {
  if (socket) socket.close();
  socket = null;
}

function clearSelection() {
  closeSocket();
  active = null;
  events = [];
  threadSessions = [];
  updateThreadHeader(null);
  showLegacyActions(false);
  $("#report").textContent = "";
  $("#terminal").textContent = "No task selected.";
  $("#message").disabled = true;
  $("#send").disabled = true;
  renderTaskList();
}

function eventText(event) {
  return String(event?.payload?.text || event?.payload?.report || "").trim();
}

function renderConversation() {
  const lines = [];
  for (const event of events) {
    const text = eventText(event);
    if (!text) continue;
    const label = event.type === "user_message"
      ? "You"
      : event.type === "assistant_message"
        ? "Assistant"
        : event.type.replaceAll("_", " ");
    lines.push(`[${label}]`, text, "");
  }
  if (!lines.length) lines.push("This task has no messages yet.");
  if (threadSessions.length) {
    const latest = threadSessions.at(-1);
    lines.push(`Execution: ${latest.status} (${latest.id})`);
  }
  const terminal = $("#terminal");
  const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
  terminal.textContent = lines.join("\n");
  if (atBottom) terminal.scrollTop = terminal.scrollHeight;
}

function connectThreadStream(threadId) {
  closeSocket();
  const cursor = events.reduce((maximum, event) => Math.max(maximum, Number(event.sequence) || 0), 0);
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/api/threads/${threadId}/stream?after_sequence=${cursor}`);
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "event" && !events.some((event) => event.sequence === message.event.sequence)) {
      events.push(message.event);
      events.sort((left, right) => left.sequence - right.sequence);
      renderConversation();
    }
    if (message.type === "error") $("#terminal").textContent += `\n[control error] ${message.error}`;
  };
}

async function loadThread(threadId) {
  const [eventResponse, sessionResponse] = await Promise.all([
    api(`/api/threads/${threadId}/events?after_sequence=0&limit=500`),
    api(`/api/threads/${threadId}/sessions`),
  ]);
  if (active !== selection("thread", threadId)) return;
  events = eventResponse.events || [];
  threadSessions = sessionResponse.sessions || [];
  renderConversation();
  connectThreadStream(threadId);
}

async function selectThread(id) {
  closeSocket();
  active = selection("thread", id);
  const thread = threads.find((candidate) => candidate.id === id);
  if (!thread) return clearSelection();
  events = [];
  threadSessions = [];
  renderTaskList();
  updateThreadHeader(thread);
  showLegacyActions(false);
  $("#report").textContent = "";
  $("#terminal").textContent = "Loading task history…";
  $("#message").disabled = false;
  $("#send").disabled = false;
  try { await loadThread(id); }
  catch (error) { if (active === selection("thread", id)) $("#terminal").textContent = `[control error] ${error.message}`; }
}

function updateLegacyHeader(session) {
  $("#active-name").textContent = session?.id || "Select a task";
  $("#active-repo").textContent = session ? `${session.repository} · legacy session` : "";
  $("#live-dot").classList.toggle("live", Boolean(session?.live));
}

async function selectLegacySession(id) {
  closeSocket();
  active = selection("session", id);
  const session = legacySessions().find((candidate) => candidate.id === id);
  if (!session) return clearSelection();
  renderTaskList();
  updateLegacyHeader(session);
  showLegacyActions(true);
  configureLegacyActions(session);
  $("#message").disabled = !session.live;
  $("#send").disabled = !session.live;
  $("#report").textContent = "";
  api(`/api/sessions/${id}/report`).then(({ report, transcript }) => {
    $("#report").textContent = report || (transcript ? `Trace references\n${(transcript.traceReferences || []).join("\n")}` : "");
  }).catch((error) => { $("#report").textContent = `[report unavailable] ${error.message}`; });
  if (!session.live) {
    $("#terminal").textContent = "This legacy execution has stopped. Its retained report is shown above.";
    return;
  }
  $("#terminal").textContent = "Connecting to orchestrator…";
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/api/sessions/${id}/terminal`);
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "output") {
      const terminal = $("#terminal");
      const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
      terminal.textContent = message.output || "No retained terminal output.";
      $("#live-dot").classList.toggle("live", message.live);
      if (atBottom) terminal.scrollTop = terminal.scrollHeight;
    }
    if (message.type === "error") $("#terminal").textContent += `\n[control error] ${message.error}`;
  };
  socket.onclose = () => $("#live-dot").classList.remove("live");
}

function messageId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `message-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

async function submitThreadMessage(threadId, text) {
  const result = await api(`/api/threads/${threadId}/messages`, {
    method: "POST",
    headers: { "idempotency-key": messageId() },
    body: JSON.stringify({ text }),
  });
  threadSessions = threadSessions.filter((session) => session.id !== result.session.id).concat(result.session);
  await loadThread(threadId);
}

$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try { const me = await api("/api/login", { method: "POST", body: JSON.stringify(data) }); showConsole(me.username); await refreshTasks(); }
  catch (error) { $("#login-error").textContent = error.message; }
};

$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); closeSocket(); clearSelection(); showLogin(); };

$("#message-form").onsubmit = async (event) => {
  event.preventDefault();
  const text = $("#message").value;
  if (!text.trim()) return;
  if (activeKind() === "thread") {
    const threadId = activeId();
    $("#send").disabled = true;
    try {
      await submitThreadMessage(threadId, text);
      $("#message").value = "";
      await refreshTasks();
    } catch (error) {
      $("#terminal").textContent += `\n[control error] ${error.message}`;
    } finally {
      if (active === selection("thread", threadId)) $("#send").disabled = false;
    }
    return;
  }
  if (activeKind() === "session" && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "input", text }));
    $("#message").value = "";
  }
};

for (const action of ["restart", "resume", "checkpoint", "pause", "complete", "archive"]) $("#" + action).onclick = async () => {
  if (activeKind() !== "session") return;
  const id = activeId();
  await api(`/api/sessions/${id}/${action}`, { method: "POST" });
  await refreshTasks();
  if (["restart", "resume"].includes(action)) await selectLegacySession(id);
};

$("#new-session").onclick = async () => {
  $("#create-error").textContent = "";
  const repositories = (await api("/api/repositories")).repositories;
  const select = $("#create-form select");
  select.replaceChildren(...repositories.map((name) => new Option(name, name)));
  $("#create-dialog").showModal();
};

$("#cancel-create").onclick = () => $("#create-dialog").close();

$("#create-form").onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = Object.fromEntries(new FormData(form));
  $("#create-error").textContent = "";
  try {
    const { thread } = await api("/api/threads", {
      method: "POST",
      body: JSON.stringify({ id: body.id, repository: body.repository, title: body.id }),
    });
    $("#create-dialog").close();
    form.reset();
    await refreshTasks();
    await selectThread(thread.id);
    await submitThreadMessage(thread.id, body.task);
    await refreshTasks();
  } catch (error) {
    if ($("#create-dialog").open) $("#create-error").textContent = error.message;
    else $("#terminal").textContent += `\n[control error] ${error.message}`;
  }
};

try { const me = await api("/api/me"); showConsole(me.username); await refreshTasks(); } catch { showLogin(); }
setInterval(() => $("#console").hidden || refreshTasks().catch(() => {}), 5000);
