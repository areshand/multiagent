import assert from "node:assert/strict";
import test from "node:test";
import { configuredRepository, parseRepositoryCatalog } from "../src/repository-catalog.mjs";

test("repository catalog remains compatible with string URLs", () => {
  const catalog = parseRepositoryCatalog(JSON.stringify({ multiagent: "https://github.com/areshand/multiagent.git" }));
  assert.deepEqual(catalog.multiagent, { url: "https://github.com/areshand/multiagent.git", authentication: "anonymous" });
});

test("repository catalog accepts GitHub App authentication without embedded credentials", () => {
  const catalog = parseRepositoryCatalog(JSON.stringify({ sdk: { url: "https://github.com/MoveIndustries/sdk.git", authentication: "github-app" } }));
  assert.deepEqual(configuredRepository(catalog, "sdk"), { url: "https://github.com/MoveIndustries/sdk.git", authentication: "github-app" });
});

test("repository catalog rejects unsafe or unsupported entries", () => {
  assert.throws(() => parseRepositoryCatalog("[]"), /must be an object/);
  assert.throws(() => parseRepositoryCatalog(JSON.stringify({ repo: "http://github.com/org/repo.git" })), /credential-free HTTPS/);
  assert.throws(() => parseRepositoryCatalog(JSON.stringify({ repo: "https://token@github.com/org/repo.git" })), /credential-free HTTPS/);
  assert.throws(() => parseRepositoryCatalog(JSON.stringify({ repo: { url: "https://git.example.com/org/repo.git", authentication: "github-app" } })), /must use github.com/);
  assert.throws(() => configuredRepository({}, "missing"), /not configured/);
});

