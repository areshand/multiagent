import assert from "node:assert/strict";

const baseUrl = new URL(process.env.MULTIAGENT_E2E_URL || "http://127.0.0.1:18080");
const repository = process.env.MULTIAGENT_E2E_REPOSITORY || "multiagent";
const githubRepository = process.env.MULTIAGENT_E2E_GITHUB_REPOSITORY || "aptos-labs/aptos-core";
const timeoutMs = Number(process.env.MULTIAGENT_E2E_TIMEOUT_MS || 45 * 60_000);
const threadId = process.env.MULTIAGENT_E2E_THREAD_ID || `github-thread-e2e-${Date.now().toString(36)}`;

const [unreviewed, merged] = await Promise.all([
  latestOpenPullRequestWithoutReviews(githubRepository),
  mostRecentlyMergedPullRequest(githubRepository),
]);
console.log(`unreviewed oracle: ${unreviewed.html_url} (${unreviewed.title})`);
console.log(`merged oracle: ${merged.html_url} (${merged.title})`);

let cookie = process.env.MULTIAGENT_E2E_COOKIE || "";
if (!cookie) {
  const login = await request("/api/login", {
    method: "POST",
    body: {
      username: required("MULTIAGENT_E2E_USERNAME"),
      password: required("MULTIAGENT_E2E_PASSWORD"),
    },
  });
  cookie = login.headers.get("set-cookie")?.split(";", 1)[0] || "";
  assert.ok(cookie, "control server did not issue an authentication cookie");
}

await request("/api/threads", {
  method: "POST",
  cookie,
  body: { id: threadId, repository, title: "Aptos core pull-request investigation" },
  expectedStatus: 201,
});

const firstPrompt = [
  `Find the latest open pull request in ${githubRepository} that has no submitted pull-request reviews.`,
  "Use the GitHub Markdown runbook and access GitHub only through prod-mcp.",
  "Return its PR number, title, author, URL, creation timestamp, and explicit evidence that its submitted review list is empty.",
  "Do not clone or modify the repository.",
].join(" ");
const first = await submitMessage("message-a", firstPrompt);
assert.equal(first.createdSession, true);
const sessionA = first.session.id;
console.log(`thread: ${threadId}; session A: ${sessionA}`);

const firstResult = await waitForResult({
  afterSequence: 0,
  matches: (text) => unreviewedReportMatches(text, unreviewed),
  description: "latest unreviewed pull request",
});
await waitForSessionEnd(sessionA);

const second = await submitMessage(
  "message-b",
  "Now find the most recent pull request that landed in that repository. Return its PR number, title, author, URL, and merge timestamp. Use the same read-only GitHub runbook path.",
);
assert.equal(second.createdSession, true);
const sessionB = second.session.id;
assert.notEqual(sessionB, sessionA, "thread follow-up reused the previous execution session");
console.log(`session B: ${sessionB}`);

const secondResult = await waitForResult({
  afterSequence: firstResult.sequence,
  matches: (text) => mergedReportMatches(text, merged),
  description: "most recently merged pull request",
});
await waitForSessionEnd(sessionB);

const history = await request(`/api/threads/${threadId}/events?after_sequence=0&limit=500`, { cookie });
const events = history.body.events || [];
assert.ok(events.some((event) => event.eventId === "message-a" && event.type === "user_message"));
assert.ok(events.some((event) => event.eventId === "message-b" && event.type === "user_message"));
assert.ok(events.some((event) => event.sessionId === sessionA && unreviewedReportMatches(eventText(event), unreviewed)));
assert.ok(events.some((event) => event.sessionId === sessionB && mergedReportMatches(eventText(event), merged)));
assert.ok(secondResult.sequence > firstResult.sequence);

console.log("GitHub durable-thread resume E2E passed");

async function submitMessage(messageId, text) {
  const result = await request(`/api/threads/${threadId}/messages`, {
    method: "POST",
    cookie,
    headers: { "idempotency-key": messageId },
    body: { text },
    expectedStatus: 202,
  });
  return result.body;
}

async function waitForResult({ afterSequence, matches, description }) {
  const deadline = Date.now() + timeoutMs;
  let cursor = afterSequence;
  while (Date.now() < deadline) {
    const response = await request(`/api/threads/${threadId}/events?after_sequence=${cursor}&limit=200`, { cookie });
    for (const event of response.body.events || []) {
      cursor = Math.max(cursor, event.sequence);
      const text = eventText(event);
      if (event.type === "assistant_message" && matches(text)) {
        console.log(text);
        return event;
      }
      if (event.type === "session_interrupted") throw new Error(`${description} session was interrupted: ${text}`);
    }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for ${description}`);
}

async function waitForSessionEnd(sessionId) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await request(`/api/threads/${threadId}/sessions`, { cookie });
    const session = (response.body.sessions || []).find((candidate) => candidate.id === sessionId);
    assert.ok(session, `session disappeared: ${sessionId}`);
    if (session.status === "completed") return;
    if (["failed", "interrupted", "cancelled"].includes(session.status)) {
      throw new Error(`session ${sessionId} ended with status ${session.status}`);
    }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for session completion: ${sessionId}`);
}

function eventText(event) {
  return String(event?.payload?.text || event?.payload?.report || "");
}

function unreviewedReportMatches(report, pullRequest) {
  const number = String(pullRequest.number);
  return report.includes(pullRequest.html_url)
    && (report.includes(`#${number}`) || report.includes(`PR ${number}`) || report.includes(`pull request ${number}`))
    && /(?:zero|no|empty).{0,80}(?:submitted )?reviews?/is.test(report);
}

function mergedReportMatches(report, pullRequest) {
  const number = String(pullRequest.number);
  return report.includes(pullRequest.html_url)
    && (report.includes(`#${number}`) || report.includes(`PR ${number}`) || report.includes(`pull request ${number}`))
    && report.includes(pullRequest.merged_at);
}

async function latestOpenPullRequestWithoutReviews(repositoryName) {
  const headers = githubHeaders();
  const pulls = await githubJson(`https://api.github.com/repos/${repositoryName}/pulls?state=open&sort=created&direction=desc&per_page=30`, headers);
  for (const pull of pulls) {
    const reviews = await githubJson(`https://api.github.com/repos/${repositoryName}/pulls/${pull.number}/reviews?per_page=1`, headers);
    if (reviews.length === 0) return pull;
  }
  throw new Error(`no unreviewed open pull request found in the latest ${pulls.length} PRs`);
}

async function mostRecentlyMergedPullRequest(repositoryName) {
  const pulls = await githubJson(
    `https://api.github.com/repos/${repositoryName}/pulls?state=closed&sort=updated&direction=desc&per_page=100`,
    githubHeaders(),
  );
  const merged = pulls.filter((pull) => pull.merged_at).sort((left, right) => right.merged_at.localeCompare(left.merged_at));
  if (!merged.length) throw new Error("GitHub oracle found no recently merged pull request");
  return merged[0];
}

function githubHeaders() {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "multiagent-thread-e2e",
    "x-github-api-version": "2022-11-28",
  };
  if (process.env.GITHUB_TOKEN) headers.authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  return headers;
}

async function githubJson(url, headers) {
  const response = await fetch(url, { headers });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`GitHub oracle failed with HTTP ${response.status}: ${JSON.stringify(body)}`);
  return body;
}

async function request(path, options = {}) {
  const headers = { accept: "application/json", origin: baseUrl.origin, ...(options.headers || {}) };
  if (options.body) headers["content-type"] = "application/json";
  if (options.cookie) headers.cookie = options.cookie;
  const response = await fetch(new URL(path, baseUrl), {
    method: options.method || "GET",
    headers,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const body = await response.json().catch(() => ({}));
  const expectedStatus = options.expectedStatus || 200;
  if (response.status !== expectedStatus) {
    throw new Error(`${options.method || "GET"} ${path} returned ${response.status}: ${JSON.stringify(body)}`);
  }
  return { response, headers: response.headers, body };
}

function required(name) {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
