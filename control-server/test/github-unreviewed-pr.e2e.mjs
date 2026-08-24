import assert from "node:assert/strict";

const baseUrl = new URL(process.env.MULTIAGENT_E2E_URL || "http://127.0.0.1:18080");
const repository = process.env.MULTIAGENT_E2E_REPOSITORY || "multiagent";
const githubRepository = process.env.MULTIAGENT_E2E_GITHUB_REPOSITORY || "aptos-labs/aptos-core";
const timeoutMs = Number(process.env.MULTIAGENT_E2E_TIMEOUT_MS || 30 * 60_000);
const sessionId = process.env.MULTIAGENT_E2E_SESSION_ID || `github-pr-e2e-${Date.now().toString(36)}`;

const expected = await latestOpenPullRequestWithoutReviews(githubRepository);
console.log(`oracle: ${expected.html_url} (${expected.title})`);

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

const repositories = await request("/api/repositories", { cookie });
assert.ok(repositories.body.repositories.includes(repository), `repository is not configured: ${repository}`);

const task = [
  `Read the latest open pull request in ${githubRepository} that has no submitted pull-request reviews.`,
  "Use the GitHub Markdown runbook and access GitHub only through prod-mcp.",
  "Return its PR number, title, author, URL, creation timestamp, and explicit evidence that its submitted review list is empty.",
  "Do not clone or modify the repository.",
].join(" ");
await request("/api/sessions", {
  method: "POST",
  cookie,
  body: { id: sessionId, repository, task },
  expectedStatus: 201,
});
console.log(`session: ${sessionId}`);

const deadline = Date.now() + timeoutMs;
let lastStatus = "pending";
let lastReport = "";
while (Date.now() < deadline) {
  const sessions = await request("/api/sessions", { cookie });
  const session = sessions.body.sessions.find((candidate) => candidate.id === sessionId);
  assert.ok(session, `session disappeared: ${sessionId}`);
  if (session.status !== lastStatus) {
    lastStatus = session.status;
    console.log(`status: ${lastStatus}`);
  }
  try {
    const report = await request(`/api/sessions/${sessionId}/report`, { cookie });
    lastReport = String(report.body.report || "");
    if (reportMatches(lastReport, expected)) {
      console.log(lastReport);
      console.log("GitHub unreviewed PR E2E passed");
      process.exit(0);
    }
  } catch {}
  if (["failed", "archived"].includes(session.status)) {
    throw new Error(`session ended with status ${session.status}\n${lastReport}`);
  }
  await sleep(10_000);
}
throw new Error(`timed out waiting for ${sessionId}; last status: ${lastStatus}\n${lastReport}`);

function reportMatches(report, pullRequest) {
  const number = String(pullRequest.number);
  return report.includes(pullRequest.html_url)
    && (report.includes(`#${number}`) || report.includes(`PR ${number}`) || report.includes(`pull request ${number}`))
    && /(?:zero|no|empty).{0,80}(?:submitted )?reviews?/is.test(report);
}

async function latestOpenPullRequestWithoutReviews(repositoryName) {
  const headers = {
    accept: "application/vnd.github+json",
    "user-agent": "multiagent-live-e2e",
    "x-github-api-version": "2022-11-28",
  };
  if (process.env.GITHUB_TOKEN) headers.authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
  const pulls = await githubJson(`https://api.github.com/repos/${repositoryName}/pulls?state=open&sort=created&direction=desc&per_page=30`, headers);
  for (const pull of pulls) {
    const reviews = await githubJson(
      `https://api.github.com/repos/${repositoryName}/pulls/${pull.number}/reviews?per_page=1`,
      headers,
    );
    if (reviews.length === 0) return pull;
  }
  throw new Error(`no unreviewed open pull request found in the latest ${pulls.length} PRs`);
}

async function githubJson(url, headers) {
  const response = await fetch(url, { headers });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`GitHub oracle failed with HTTP ${response.status}: ${JSON.stringify(body)}`);
  return body;
}

async function request(path, options = {}) {
  const headers = { accept: "application/json", origin: baseUrl.origin };
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
