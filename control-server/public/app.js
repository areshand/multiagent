const $ = (selector) => document.querySelector(selector);
let active = null;
let socket = null;
let sessions = [];

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "content-type": "application/json", ...(options.headers || {}) }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
  return body;
}

function showLogin() { $("#login").hidden = false; $("#console").hidden = true; }
function showConsole(username) { $("#username").textContent = username; $("#login").hidden = true; $("#console").hidden = false; }

async function refreshSessions() {
  sessions = (await api("/api/sessions")).sessions;
  const target = $("#sessions");
  target.replaceChildren(...sessions.map((session) => {
    const button = document.createElement("button");
    button.className = `session${active === session.id ? " active" : ""}`;
    button.innerHTML = `<strong>${session.id}</strong><small>${session.live ? "live" : session.status} · ${session.repository}</small>`;
    button.onclick = () => selectSession(session.id);
    return button;
  }));
  if (active && !sessions.some((session) => session.id === active)) selectSession(null);
}

function selectSession(id) {
  if (socket) socket.close();
  active = id;
  const session = sessions.find((candidate) => candidate.id === id);
  $("#active-name").textContent = session?.id || "Select a session";
  $("#active-repo").textContent = session?.repository || "";
  $("#live-dot").classList.toggle("live", Boolean(session?.live));
  const running = Boolean(session?.live);
  $("#resume").disabled = !session || running;
  $("#restart").disabled = !running;
  $("#checkpoint").disabled = !running;
  $("#pause").disabled = !running;
  $("#complete").disabled = !running;
  $("#archive").disabled = !session || running || session.status === "archived";
  $("#message").disabled = !running;
  $("#send").disabled = !running;
  $("#report").textContent = "";
  if (!session) { $("#terminal").textContent = "No orchestrator selected."; return refreshSessions(); }
  api(`/api/sessions/${id}/report`).then(({ report, transcript }) => {
    $("#report").textContent = report || (transcript ? `Trace references\n${transcript.traceReferences.join("\n")}` : "");
  }).catch(() => {});
  $("#terminal").textContent = "Connecting to orchestrator…";
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(`${protocol}://${location.host}/api/sessions/${id}/terminal`);
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.type === "output") {
      const terminal = $("#terminal");
      const atBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
      terminal.textContent = message.output;
      $("#live-dot").classList.toggle("live", message.live);
      if (atBottom) terminal.scrollTop = terminal.scrollHeight;
    }
    if (message.type === "error") $("#terminal").textContent += `\n[control error] ${message.error}`;
  };
  socket.onclose = () => $("#live-dot").classList.remove("live");
  refreshSessions();
}

$("#login-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.currentTarget));
  try { const me = await api("/api/login", { method: "POST", body: JSON.stringify(data) }); showConsole(me.username); await refreshSessions(); }
  catch (error) { $("#login-error").textContent = error.message; }
};
$("#logout").onclick = async () => { await api("/api/logout", { method: "POST" }); if (socket) socket.close(); showLogin(); };
$("#message-form").onsubmit = (event) => {
  event.preventDefault();
  const text = $("#message").value;
  if (!text.trim() || socket?.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "input", text }));
  $("#message").value = "";
};
for (const action of ["restart", "resume", "checkpoint", "pause", "complete", "archive"]) $("#" + action).onclick = async () => {
  if (!active) return;
  await api(`/api/sessions/${active}/${action}`, { method: "POST" });
  await refreshSessions();
  if (action === "restart" || action === "resume") selectSession(active);
};
$("#new-session").onclick = async () => {
  const repositories = (await api("/api/repositories")).repositories;
  const select = $("#create-form select");
  select.replaceChildren(...repositories.map((name) => new Option(name, name)));
  $("#create-dialog").showModal();
};
$("#cancel-create").onclick = () => $("#create-dialog").close();
$("#create-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const body = Object.fromEntries(new FormData(event.currentTarget));
    await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
    $("#create-dialog").close();
    await refreshSessions();
    selectSession(body.id);
  } catch (error) { $("#create-error").textContent = error.message; }
};

try { const me = await api("/api/me"); showConsole(me.username); await refreshSessions(); } catch { showLogin(); }
setInterval(() => $("#console").hidden || refreshSessions().catch(() => {}), 5000);
