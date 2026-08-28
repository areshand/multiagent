#!/usr/bin/env node
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const repositoryNamePattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export function githubRepositoryFromUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname !== "github.com" || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("GitHub App clone URL must be credential-free github.com HTTPS");
  }
  const match = parsed.pathname.match(/^\/([^/]+)\/([^/]+?)(?:\.git)?$/);
  if (!match) throw new Error("GitHub App clone URL must identify one owner/repository");
  return { owner: match[1], repository: match[2] };
}

function validateCloneUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("clone URL must be credential-free HTTPS");
  }
}

export function issueAppJwt({ appId, privateKey, nowSeconds = Math.floor(Date.now() / 1000) }) {
  const header = Buffer.from(JSON.stringify({ alg: "RS256", typ: "JWT" })).toString("base64url");
  const payload = Buffer.from(JSON.stringify({ iat: nowSeconds - 60, exp: nowSeconds + 540, iss: String(appId) })).toString("base64url");
  const unsigned = `${header}.${payload}`;
  const signature = crypto.sign("RSA-SHA256", Buffer.from(unsigned), privateKey).toString("base64url");
  return `${unsigned}.${signature}`;
}

async function githubJson(fetchImpl, url, options) {
  const response = await fetchImpl(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(`GitHub API ${response.status}: ${body.message || "request failed"}`);
  return body;
}

export async function issueInstallationToken({ cloneUrl, appId, privateKey, fetchImpl = fetch }) {
  const { owner, repository } = githubRepositoryFromUrl(cloneUrl);
  const jwt = issueAppJwt({ appId, privateKey });
  const headers = {
    accept: "application/vnd.github+json",
    authorization: `Bearer ${jwt}`,
    "user-agent": "multiagent-repository-bootstrap",
    "x-github-api-version": "2022-11-28",
  };
  const installation = await githubJson(fetchImpl, `https://api.github.com/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repository)}/installation`, { headers });
  if (!Number.isSafeInteger(Number(installation.id))) throw new Error("GitHub App installation lookup returned no installation ID");
  const token = await githubJson(fetchImpl, `https://api.github.com/app/installations/${installation.id}/access_tokens`, {
    method: "POST",
    headers: { ...headers, "content-type": "application/json" },
    body: JSON.stringify({ repositories: [repository], permissions: { contents: "read" } }),
  });
  if (typeof token.token !== "string" || !token.token) throw new Error("GitHub App token response contained no token");
  return token.token;
}

function readRequiredFile(value, label) {
  if (!value) throw new Error(`${label} file is required`);
  return fs.readFileSync(value, "utf8").trim();
}

export async function prepareRepository({
  name,
  cloneUrl,
  authentication = "anonymous",
  repositoryRoot,
  appIdFile,
  privateKeyFile,
  fetchImpl = fetch,
  spawnImpl = spawnSync,
}) {
  if (!repositoryNamePattern.test(name || "")) throw new Error("invalid repository name");
  validateCloneUrl(cloneUrl);
  const root = path.resolve(repositoryRoot);
  const destination = path.resolve(root, name);
  if (!destination.startsWith(`${root}${path.sep}`)) throw new Error("repository destination escapes repository root");
  if (fs.existsSync(path.join(destination, ".git"))) return { destination, cloned: false };
  if (fs.existsSync(destination)) throw new Error(`repository bootstrap target exists but is not a git repository: ${destination}`);
  fs.mkdirSync(root, { recursive: true });

  const temporary = path.join(root, `.${name}.clone-${process.pid}`);
  if (fs.existsSync(temporary)) fs.rmSync(temporary, { recursive: true, force: true });
  const environment = { ...process.env, GIT_TERMINAL_PROMPT: "0" };
  let askpassFile = "";
  if (authentication === "github-app") {
    const token = await issueInstallationToken({
      cloneUrl,
      appId: readRequiredFile(appIdFile, "GitHub App ID"),
      privateKey: readRequiredFile(privateKeyFile, "GitHub App private key"),
      fetchImpl,
    });
    askpassFile = path.join(root, `.${name}.askpass-${process.pid}.sh`);
    fs.writeFileSync(askpassFile, "#!/bin/sh\ncase \"$1\" in\n  *Username*) printf '%s\\n' \"$GITHUB_APP_USERNAME\" ;;\n  *Password*) printf '%s\\n' \"$GITHUB_APP_TOKEN\" ;;\nesac\n", { mode: 0o700 });
    Object.assign(environment, {
      GIT_ASKPASS: askpassFile,
      GITHUB_APP_USERNAME: "x-access-token",
      GITHUB_APP_TOKEN: token,
    });
  } else if (authentication !== "anonymous") {
    throw new Error(`unsupported repository authentication: ${authentication}`);
  }

  try {
    const result = spawnImpl("git", ["clone", "--", cloneUrl, temporary], { env: environment, encoding: "utf8" });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`git clone failed: ${String(result.stderr || "").trim() || `exit ${result.status}`}`);
    fs.renameSync(temporary, destination);
    return { destination, cloned: true };
  } finally {
    if (askpassFile) fs.rmSync(askpassFile, { force: true });
    if (fs.existsSync(temporary)) fs.rmSync(temporary, { recursive: true, force: true });
  }
}

async function main() {
  const result = await prepareRepository({
    name: process.env.MULTIAGENT_BOOTSTRAP_REPOSITORY_NAME,
    cloneUrl: process.env.MULTIAGENT_BOOTSTRAP_REPOSITORY_URL,
    authentication: process.env.MULTIAGENT_BOOTSTRAP_REPOSITORY_AUTHENTICATION || "anonymous",
    repositoryRoot: process.env.MULTIAGENT_REPOSITORY_ROOT || "/var/lib/multiagent/repositories",
    appIdFile: process.env.MULTIAGENT_GITHUB_APP_ID_FILE,
    privateKeyFile: process.env.MULTIAGENT_GITHUB_APP_PRIVATE_KEY_FILE,
  });
  console.log(result.cloned ? `repository prepared: ${process.env.MULTIAGENT_BOOTSTRAP_REPOSITORY_NAME}` : `repository already prepared: ${process.env.MULTIAGENT_BOOTSTRAP_REPOSITORY_NAME}`);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
