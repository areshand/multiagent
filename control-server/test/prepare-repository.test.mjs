import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { githubRepositoryFromUrl, issueAppJwt, issueInstallationToken, prepareRepository } from "../../bin/prepare-repository.mjs";

test("GitHub repository URLs are parsed without accepting credentials or extra paths", () => {
  assert.deepEqual(githubRepositoryFromUrl("https://github.com/MoveIndustries/sdk.git"), { owner: "MoveIndustries", repository: "sdk" });
  assert.throws(() => githubRepositoryFromUrl("https://token@github.com/MoveIndustries/sdk.git"), /credential-free/);
  assert.throws(() => githubRepositoryFromUrl("https://github.com/MoveIndustries/sdk/tree/main"), /one owner\/repository/);
});

test("GitHub App JWT is signed and bounded to ten minutes", () => {
  const { privateKey, publicKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const jwt = issueAppJwt({ appId: "123", privateKey, nowSeconds: 1_000_000 });
  const [header, payload, signature] = jwt.split(".");
  assert.equal(JSON.parse(Buffer.from(payload, "base64url")).iss, "123");
  assert.equal(JSON.parse(Buffer.from(payload, "base64url")).exp, 1_000_540);
  assert.equal(crypto.verify("RSA-SHA256", Buffer.from(`${header}.${payload}`), publicKey, Buffer.from(signature, "base64url")), true);
});

test("installation token is scoped to the selected repository and read-only contents", async () => {
  const { privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const requests = [];
  const token = await issueInstallationToken({
    cloneUrl: "https://github.com/MoveIndustries/sdk.git",
    appId: "123",
    privateKey,
    fetchImpl: async (url, options) => {
      requests.push({ url, options });
      return { ok: true, status: 200, json: async () => requests.length === 1 ? { id: 456 } : { token: "short-lived" } };
    },
  });
  assert.equal(token, "short-lived");
  assert.equal(requests[0].url, "https://api.github.com/repos/MoveIndustries/sdk/installation");
  assert.deepEqual(JSON.parse(requests[1].options.body), { repositories: ["sdk"], permissions: { contents: "read" } });
});

test("clone receives the short-lived token only through init-process environment", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "multiagent-repo-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const { privateKey } = crypto.generateKeyPairSync("rsa", { modulusLength: 2048 });
  const appIdFile = path.join(root, "app-id");
  const privateKeyFile = path.join(root, "private-key");
  fs.writeFileSync(appIdFile, "123\n");
  fs.writeFileSync(privateKeyFile, privateKey.export({ type: "pkcs8", format: "pem" }));
  let invocation;
  await prepareRepository({
    name: "sdk",
    cloneUrl: "https://github.com/MoveIndustries/sdk.git",
    authentication: "github-app",
    repositoryRoot: path.join(root, "repositories"),
    appIdFile,
    privateKeyFile,
    fetchImpl: async (url) => ({ ok: true, status: 200, json: async () => url.endsWith("/installation") ? { id: 456 } : { token: "secret-token" } }),
    spawnImpl: (command, args, options) => {
      invocation = { command, args, options };
      fs.mkdirSync(args.at(-1));
      fs.mkdirSync(path.join(args.at(-1), ".git"));
      return { status: 0, stderr: "" };
    },
  });
  assert.deepEqual(invocation.args, ["clone", "--", "https://github.com/MoveIndustries/sdk.git", invocation.args.at(-1)]);
  assert.equal(invocation.args.join(" ").includes("secret-token"), false);
  assert.equal(invocation.options.env.GITHUB_APP_TOKEN, "secret-token");
  assert.equal(fs.existsSync(path.join(root, "repositories", "sdk", ".git")), true);
  assert.equal(fs.readdirSync(path.join(root, "repositories")).some((name) => name.includes("askpass")), false);
});

test("anonymous clone rejects URLs containing credentials before invoking git", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "multiagent-repo-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let invoked = false;
  await assert.rejects(
    prepareRepository({
      name: "unsafe",
      cloneUrl: "https://secret@github.com/example/repository.git",
      authentication: "anonymous",
      repositoryRoot: root,
      spawnImpl: () => { invoked = true; return { status: 0 }; },
    }),
    /credential-free HTTPS/,
  );
  assert.equal(invoked, false);
});
