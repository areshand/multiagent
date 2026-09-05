import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createWikiApp } from "../src/app.mjs";
import { WikiClient } from "../src/client.mjs";
import { loadConfig } from "../src/config.mjs";
import { parseQueryArguments, runWikiQuery } from "../src/query-cli.mjs";

const TEST_CATALOG_DIGEST = `sha256:${"b".repeat(64)}`;

test("deployment profile defaults to strict organization mode", () => {
  assert.equal(loadConfig({ WIKI_ROOT: "/tmp/wiki" }).profile, "organization");
  assert.equal(loadConfig({ WIKI_ROOT: "/tmp/wiki", WIKI_PROFILE: "personal" }).profile, "personal");
  assert.throws(
    () => loadConfig({ WIKI_ROOT: "/tmp/wiki", WIKI_PROFILE: "automatic" }),
    /must be organization or personal/,
  );
});

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-service-test-"));
  await fs.mkdir(path.join(root, "repos"), { recursive: true });
  await fs.mkdir(path.join(root, "topics"), { recursive: true });
  await fs.writeFile(path.join(root, "index.md"), [
    "---",
    "schema: wiki-repository-catalog/v1",
    `catalog_digest: "${TEST_CATALOG_DIGEST}"`,
    "repository_count: 2",
    "---",
    "",
    "# Organization repository catalog",
    "",
    "- [Internal Services](repos/internal-services.md): infrastructure, Kubernetes, IAM, storage, and deployment architecture.",
    "- [Multiagent](repos/multiagent.md): supervised multi-agent runtime.",
    "",
  ].join("\n"));
  await fs.writeFile(path.join(root, "repos", "internal-services.md"), [
    "---",
    "repository: MoveIndustries/InternalServices",
    "source_commit: 0123456789012345678901234567890123456789",
    `catalog_digest: "${TEST_CATALOG_DIGEST}"`,
    "---",
    "# InternalServices architecture",
    "",
    "InternalServices owns Kubernetes clusters, IAM, KMS, ingress, persistent storage, and deployment configuration.",
    "The multiagent service consumes its provider-specific infrastructure through deployment-owned contracts.",
    "",
    "## Sources",
    "",
    "- `docs/architecture.md` — `sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`",
    "",
  ].join("\n"));
  await fs.writeFile(path.join(root, "repos", "multiagent.md"), [
    "---",
    "schema: wiki-repository/v1",
    `catalog_digest: "${TEST_CATALOG_DIGEST}"`,
    "---",
    "",
    "# Multiagent",
    "",
    "A supervised runtime for confined role agents.",
    "",
  ].join("\n"));
  await fs.writeFile(path.join(root, "orphan.md"), "# Billing fallback\n\nThe billing repository owns invoice reconciliation.\n");
  return root;
}

async function personalFixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-personal-test-"));
  await fs.cp(
    new URL("../engine/tests/fixtures/demo-vault/", import.meta.url),
    root,
    { recursive: true },
  );
  return root;
}

function config(root, overrides = {}) {
  return {
    root,
    profile: "organization",
    maxRequestBytes: 32 * 1024,
    maxCorpusFiles: 100,
    maxCorpusBytes: 1024 * 1024,
    maxFallbackFiles: 10,
    maxFallbackBytes: 1024 * 1024,
    ...overrides,
  };
}

async function withServer(root, callback, overrides = {}) {
  const app = await createWikiApp(config(root, overrides));
  await new Promise((resolve) => app.server.listen(0, "127.0.0.1", resolve));
  const address = app.server.address();
  try {
    await callback(`http://127.0.0.1:${address.port}`, app);
  } finally {
    await new Promise((resolve, reject) => app.server.close((error) => error ? reject(error) : resolve()));
    await fs.rm(root, { recursive: true, force: true });
  }
}

async function post(baseUrl, route, body) {
  return fetch(`${baseUrl}${route}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

test("index-first query locates InternalServices architecture with citation", async () => {
  await withServer(await fixture(), async (baseUrl) => {
    const response = await post(baseUrl, "/v1/query", { query: "internal services architecture", limit: 1 });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.results[0].path, "repos/internal-services.md");
    assert.equal(body.results[0].title, "InternalServices architecture");
    assert.equal(body.results[0].source, "index");
    assert.match(body.results[0].excerpt, /owns Kubernetes clusters/);
    assert.match(body.results[0].sha256, /^[a-f0-9]{64}$/);
    assert.equal(body.retrieval.mode, "index");
    assert.equal(body.retrieval.fallbackFilesScanned, 0);
  });
});

test("existing personal vault root and Obsidian index links use the same query adapter", async () => {
  await withServer(await personalFixture(), async (baseUrl, app) => {
    assert.equal(app.snapshot().catalogDigest, null);
    const response = await post(baseUrl, "/v1/query", {
      query: "synthetic memory platform concept",
      limit: 1,
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.results[0].path, "concepts/example-memory-platform.md");
    assert.equal(body.results[0].source, "index");
    assert.match(body.results[0].excerpt, /Synthetic concept used by tests/);
  }, { profile: "personal" });
});

test("organization profile rejects a personal index without an explicit local profile", async () => {
  const root = await personalFixture();
  await assert.rejects(createWikiApp(config(root)), /organization Wiki requires schema/);
  await fs.rm(root, { recursive: true, force: true });
});

test("organization catalog schema still requires its generation digest", async () => {
  const root = await fixture();
  const indexPath = path.join(root, "index.md");
  const index = (await fs.readFile(indexPath, "utf8"))
    .replace(`catalog_digest: "${TEST_CATALOG_DIGEST}"\n`, "")
    .replace("---\n", "---\nschema: wiki-repository-catalog/v1\n");
  await fs.writeFile(indexPath, index);
  await assert.rejects(createWikiApp(config(root)), /no valid catalog_digest/);
  await fs.rm(root, { recursive: true, force: true });
});

test("bounded fallback finds an unindexed Markdown page", async () => {
  await withServer(await fixture(), async (baseUrl) => {
    const response = await post(baseUrl, "/v1/query", { query: "invoice reconciliation", limit: 3 });
    const body = await response.json();
    assert.equal(body.results[0].path, "orphan.md");
    assert.equal(body.results[0].source, "fallback");
    assert.equal(body.retrieval.mode, "index+fallback");
    assert.equal(body.retrieval.fallbackFilesScanned, 1);
  });
});

test("refresh makes a newly written canonical Markdown page searchable", async () => {
  const root = await fixture();
  await withServer(root, async (baseUrl) => {
    await fs.writeFile(path.join(root, "topics", "compiler-tools.md"), "# Compiler tools\n\nOwns deterministic bytecode builds.\n");
    let response = await post(baseUrl, "/v1/query", { query: "bytecode", limit: 2 });
    assert.equal((await response.json()).results.length, 0);
    response = await post(baseUrl, "/v1/refresh", {});
    assert.equal(response.status, 200);
    response = await post(baseUrl, "/v1/query", { query: "bytecode", limit: 2 });
    assert.equal((await response.json()).results[0].path, "topics/compiler-tools.md");
  });
});

test("the read-only MVP does not expose feedback or source-submission APIs", async () => {
  await withServer(await fixture(), async (baseUrl) => {
    assert.equal((await post(baseUrl, "/v1/feedback", { query: "x", helpful: false })).status, 404);
    assert.equal((await post(baseUrl, "/v1/source-submissions", {})).status, 404);
  });
});

test("an empty bucket is unhealthy for retrieval until a reviewed index is loaded", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-empty-corpus-"));
  await withServer(root, async (baseUrl) => {
    assert.equal((await fetch(`${baseUrl}/healthz`)).status, 200);
    assert.equal((await fetch(`${baseUrl}/readyz`)).status, 503);
    const response = await post(baseUrl, "/v1/query", { query: "InternalServices" });
    assert.equal(response.status, 503);
    assert.match((await response.json()).error, /index is not ready/);
  });
});

test("health, readiness, methods, and request size bounds are enforced", async () => {
  await withServer(await fixture(), async (baseUrl) => {
    assert.equal((await fetch(`${baseUrl}/healthz`)).status, 200);
    assert.equal((await fetch(`${baseUrl}/readyz`)).status, 200);
    assert.equal((await fetch(`${baseUrl}/v1/query`)).status, 405);
    assert.equal((await post(baseUrl, "/v1/query", { query: "x".repeat(501) })).status, 400);
    assert.equal((await post(baseUrl, "/v1/query", { query: "... !!! ???" })).status, 400);
    const oversized = await fetch(`${baseUrl}/v1/query`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query: "x", padding: "x".repeat(3000) }),
    });
    assert.equal(oversized.status, 413);
  }, { maxRequestBytes: 2048 });
});

test("wiki-query exposes a stable provider-neutral agent command", async () => {
  assert.deepEqual(parseQueryArguments(["--query", "internal services", "--max-results", "2"]), {
    query: "internal services",
    limit: 2,
    maxExcerptChars: 1200,
  });
  await withServer(await fixture(), async (baseUrl) => {
    const output = await runWikiQuery({
      arguments_: ["--query", "InternalServices architecture", "--max-results", "1"],
      environment: { MULTIAGENT_WIKI_URL: baseUrl },
    });
    assert.equal(output.results[0].path, "repos/internal-services.md");
  });
});

test("repository_count allows indexed topic pages from the same catalog generation", async () => {
  const root = await fixture();
  await fs.writeFile(path.join(root, "topics", "ownership.md"), [
    "---",
    `catalog_digest: "${TEST_CATALOG_DIGEST}"`,
    "---",
    "",
    "# Ownership",
    "",
    "The Infrastructure team owns shared platform architecture.",
  ].join("\n"));
  const indexPath = path.join(root, "index.md");
  const index = await fs.readFile(indexPath, "utf8");
  await fs.writeFile(indexPath, `${index}- [Ownership](topics/ownership.md) — ownership map.\n`);
  await withServer(root, async (baseUrl) => {
    assert.equal((await fetch(`${baseUrl}/readyz`)).status, 200);
    const response = await post(baseUrl, "/v1/query", { query: "Infrastructure team ownership", limit: 1 });
    assert.equal((await response.json()).results[0].path, "topics/ownership.md");
  });
});

test("partial and mixed catalog generations fail closed", async () => {
  const partialRoot = await fixture();
  await fs.rm(path.join(partialRoot, "repos", "multiagent.md"));
  await assert.rejects(createWikiApp(config(partialRoot)), /indexed Markdown page is missing/);
  await fs.rm(partialRoot, { recursive: true, force: true });

  const mixedRoot = await fixture();
  const mixedPath = path.join(mixedRoot, "repos", "multiagent.md");
  const mixed = (await fs.readFile(mixedPath, "utf8")).replace(TEST_CATALOG_DIGEST, `sha256:${"c".repeat(64)}`);
  await fs.writeFile(mixedPath, mixed);
  await assert.rejects(createWikiApp(config(mixedRoot)), /different catalog_digest/);
  await fs.rm(mixedRoot, { recursive: true, force: true });
});

test("bootstrap documentation stages locally and commits the catalog index last", async () => {
  const readme = await fs.readFile(new URL("../README.md", import.meta.url), "utf8");
  assert.match(readme, /WIKI_STAGING_ROOT=\/tmp\/wiki-seed/);
  assert.match(readme, /upload `index\.md` last/);
  assert.doesNotMatch(readme, /WIKI_ROOT=\/var\/lib\/wiki wiki-seed/);
});

test("stale unindexed repository pages do not consume corpus bounds", async () => {
  const root = await fixture();
  await fs.writeFile(path.join(root, "repos", "stale.md"), `# stale\n\n${"x".repeat(20_000)}\n`);
  await withServer(root, async (baseUrl) => {
    assert.equal((await fetch(`${baseUrl}/readyz`)).status, 200);
    const response = await post(baseUrl, "/v1/query", { query: "internal services architecture", limit: 1 });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).results[0].path, "repos/internal-services.md");
  }, { maxCorpusBytes: 4096 });
});

test("wiki client aborts a stalled request at its deadline", async () => {
  const fetchImpl = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
  });
  const client = new WikiClient({ baseUrl: "http://wiki.invalid", fetchImpl, timeoutMs: 20 });
  await assert.rejects(client.query({ query: "architecture" }), /timed out after 20 ms/);
});

test("wiki client deadline includes a stalled response body", async () => {
  const fetchImpl = (_url, { signal }) => Promise.resolve({
    ok: true,
    status: 200,
    body: {
      getReader: () => ({
        read: () => new Promise((_resolve, reject) => {
          signal.addEventListener("abort", () => reject(new Error("body aborted")), { once: true });
        }),
        cancel: async () => {},
        releaseLock: () => {},
      }),
    },
  });
  const client = new WikiClient({ baseUrl: "http://wiki.invalid", fetchImpl, timeoutMs: 20 });
  await assert.rejects(client.query({ query: "architecture" }), /timed out after 20 ms/);
});

test("wiki client stops streaming response bytes at the configured bound", async () => {
  let reads = 0;
  let canceled = false;
  const reader = {
    read: async () => {
      reads += 1;
      if (reads > 2) throw new Error("client read beyond its response bound");
      return { done: false, value: new Uint8Array(600) };
    },
    cancel: async () => { canceled = true; },
    releaseLock: () => {},
  };
  const client = new WikiClient({
    baseUrl: "http://wiki.invalid",
    maxResponseBytes: 1024,
    fetchImpl: async () => ({ ok: true, status: 200, body: { getReader: () => reader } }),
  });
  await assert.rejects(client.query({ query: "architecture" }), /response exceeds 1024 bytes/);
  assert.equal(reads, 2);
  assert.equal(canceled, true);
});

test("corpus loader ignores symlinked Markdown outside the wiki root", async (t) => {
  const root = await fixture();
  const outside = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-outside-"));
  await fs.writeFile(path.join(outside, "secret.md"), "# Secret\n\noutside marker\n");
  try {
    await fs.symlink(path.join(outside, "secret.md"), path.join(root, "leak.md"));
  } catch (error) {
    if (error.code === "EPERM") {
      t.skip("symlinks are not supported in this environment");
      return;
    }
    throw error;
  }
  try {
    await withServer(root, async (baseUrl) => {
      const response = await post(baseUrl, "/v1/query", { query: "outside marker", limit: 5 });
      assert.equal((await response.json()).results.length, 0);
    });
  } finally {
    await fs.rm(outside, { recursive: true, force: true });
  }
});
