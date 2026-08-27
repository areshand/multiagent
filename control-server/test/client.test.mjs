import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { ControlClient, main } from "../src/client.mjs";

function writer() {
  return { output: "", write(value) { this.output += String(value); } };
}

function jsonResponse(value, init = {}) {
  return new Response(JSON.stringify(value), {
    status: init.status || 200,
    headers: { "content-type": "application/json", ...(init.headers || {}) },
  });
}

async function sessionFixture() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "multiagent-client-"));
  const file = path.join(directory, "session.json");
  await writeFile(file, JSON.stringify({
    server: "https://control.example/",
    cookie: "multiagent_session=signed-cookie",
    username: "operator",
  }), { mode: 0o600 });
  return file;
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

test("users can list durable threads through the client CLI", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  let cookie = "";
  await main([
    "--server", "https://control.example", "--session-file", sessionFile, "threads", "list",
  ], {
    stdout: output,
    fetchImpl: async (url, options) => {
      assert.equal(String(url), "https://control.example/api/threads");
      cookie = options.headers.cookie;
      return jsonResponse({ threads: [{ id: "thread-1", state: "idle", repository: "multiagent" }] });
    },
  });
  assert.equal(cookie, "multiagent_session=signed-cookie");
  assert.deepEqual(JSON.parse(output.output), [{ id: "thread-1", state: "idle", repository: "multiagent" }]);
});

test("thread creation lets the server generate the execution session ID", async () => {
  const sessionFile = await sessionFixture();
  const output = writer();
  const requests = [];
  await main([
    "--server", "https://control.example", "--session-file", sessionFile,
    "threads", "create", "thread-1", "--repository", "multiagent", "--message", "Investigate the incident",
  ], {
    stdout: output,
    fetchImpl: async (url, options) => {
      requests.push({ url: String(url), options });
      if (String(url).endsWith("/api/threads")) return jsonResponse({ thread: { id: "thread-1", repository: "multiagent" } }, { status: 201 });
      return jsonResponse({ createdSession: true, session: { id: "thread-1-generated-session", status: "running" } }, { status: 202 });
    },
  });
  assert.equal(requests.length, 2);
  assert.deepEqual(JSON.parse(requests[0].options.body), { id: "thread-1", repository: "multiagent", title: "thread-1" });
  assert.equal(requests[1].url, "https://control.example/api/threads/thread-1/messages");
  assert.deepEqual(JSON.parse(requests[1].options.body), { text: "Investigate the incident" });
  assert.ok(requests[1].options.headers["idempotency-key"]);
  assert.equal(JSON.parse(output.output).route.session.id, "thread-1-generated-session");
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

test("landing page remains a minimal pointer to the client CLI", async () => {
  const page = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(page, /Thread Gateway/);
  assert.match(page, /npm run client -- threads list/);
  assert.doesNotMatch(page, /<form|<button|<script/i);
});
