import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { createWikiApp } from "../src/app.mjs";
import { normalizeSeedManifest, seedCatalog } from "../src/seed.mjs";

const repositories = [
  {
    catalogId: "multiagent",
    repository: "MoveIndustries/multiagent",
    url: "https://github.com/MoveIndustries/multiagent.git",
    visibility: "private",
    defaultBranch: "main",
    sourceStatus: "verified",
    resolvedCommitSha: "b".repeat(40),
    summary: "Runs supervised and confined role agents.",
    description: "The runtime routes tasks to role agents and enforces authority boundaries.",
    sources: [{ path: "docs/architecture/system-architecture.md", sha256: "2".repeat(64) }],
  },
  {
    catalogId: "internal-services",
    repository: "MoveIndustries/InternalServices",
    url: "https://github.com/MoveIndustries/InternalServices.git",
    visibility: "internal",
    defaultBranch: "main",
    sourceStatus: "verified",
    resolvedCommitSha: "a".repeat(40),
    summary: "Owns organization infrastructure and deployment configuration.",
    description: "InternalServices defines Kubernetes clusters, IAM, KMS, ingress, storage, and service deployment architecture.",
    sources: [
      { path: "terraform/storage/main.tf", sha256: "4".repeat(64) },
      { path: "docs/architecture.md", sha256: "3".repeat(64) },
    ],
  },
];

function manifest(records = repositories) {
  return { schema: "wiki-catalog-seed/v1", repositories: records };
}

function config(root, overrides = {}) {
  return {
    root,
    maxRequestBytes: 32 * 1024,
    maxCorpusFiles: 100,
    maxCorpusBytes: 1024 * 1024,
    maxFallbackFiles: 10,
    maxFallbackBytes: 1024 * 1024,
    ...overrides,
  };
}

async function post(baseUrl, route, body) {
  return fetch(`${baseUrl}${route}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

test("wiki-seed deterministically writes a two-repository Markdown catalog", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-seed-test-"));
  try {
    const first = await seedCatalog(root, manifest(repositories));
    const firstIndex = await fs.readFile(path.join(root, "index.md"), "utf8");
    const firstInternal = await fs.readFile(path.join(root, "repos", "internal-services.md"), "utf8");
    const firstMultiagent = await fs.readFile(path.join(root, "repos", "multiagent.md"), "utf8");

    const second = await seedCatalog(root, manifest([...repositories].reverse()));
    assert.deepEqual(second, first);
    assert.equal(await fs.readFile(path.join(root, "index.md"), "utf8"), firstIndex);
    assert.equal(await fs.readFile(path.join(root, "repos", "internal-services.md"), "utf8"), firstInternal);
    assert.equal(await fs.readFile(path.join(root, "repos", "multiagent.md"), "utf8"), firstMultiagent);
    assert.ok(firstIndex.indexOf("InternalServices") < firstIndex.indexOf("multiagent"));
    assert.match(firstInternal, /visibility: "internal"/);
    assert.match(firstInternal, /default_branch: "main"/);
    assert.match(firstInternal, /source_status: "verified"/);
    assert.match(firstInternal, /resolved_commit_sha: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"/);
    assert.ok(firstInternal.indexOf("docs/architecture.md") < firstInternal.indexOf("terraform/storage/main.tf"));
    assert.match(firstInternal, /sha256:3333333333333333333333333333333333333333333333333333333333333333/);
    assert.equal((await fs.readdir(path.join(root, "repos"))).some((name) => name.endsWith(".tmp")), false);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("wiki-seed records an empty repository without invented commit or sources", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-seed-empty-"));
  const empty = {
    catalogId: "empty-repository",
    repository: "MoveIndustries/empty-repository",
    url: "https://github.com/MoveIndustries/empty-repository.git",
    visibility: "private",
    defaultBranch: "main",
    sourceStatus: "empty",
    resolvedCommitSha: null,
    summary: "Empty repository; no implementation is present.",
    description: "No source-grounded detail is available because GitHub reports that the repository has no commits.",
    sources: [],
  };
  try {
    await seedCatalog(root, manifest([empty]));
    const markdown = await fs.readFile(path.join(root, "repos", "empty-repository.md"), "utf8");
    assert.match(markdown, /source_status: "empty"/);
    assert.match(markdown, /resolved_commit_sha: null/);
    assert.match(markdown, /No repository source exists at this catalog snapshot\./);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("seeded catalog answers an InternalServices architecture query", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-seed-query-"));
  let app;
  try {
    await seedCatalog(root, manifest());
    app = await createWikiApp(config(root));
    await new Promise((resolve) => app.server.listen(0, "127.0.0.1", resolve));
    const address = app.server.address();
    const response = await post(`http://127.0.0.1:${address.port}`, "/v1/query", {
      query: "Which repository owns internal services architecture?",
      limit: 1,
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.results[0].path, "repos/internal-services.md");
    assert.equal(body.results[0].source, "index");
    assert.match(body.results[0].excerpt, /InternalServices defines Kubernetes clusters/);
  } finally {
    if (app?.server.listening) await new Promise((resolve) => app.server.close(resolve));
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("a repository removed from the seeded index cannot leak through fallback", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-seed-stale-"));
  let app;
  try {
    await seedCatalog(root, manifest());
    await seedCatalog(root, manifest([repositories[1]]));
    await fs.access(path.join(root, "repos", "multiagent.md"));
    app = await createWikiApp(config(root));
    await new Promise((resolve) => app.server.listen(0, "127.0.0.1", resolve));
    const address = app.server.address();
    const response = await post(`http://127.0.0.1:${address.port}`, "/v1/query", {
      query: "supervised confined role agents",
      limit: 10,
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.results.some((result) => result.path === "repos/multiagent.md"), false);
    assert.equal(body.retrieval.mode, "index+fallback");
  } finally {
    if (app?.server.listening) await new Promise((resolve) => app.server.close(resolve));
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("checked-in catalog covers the deployed 111 repositories and routes InternalServices", async () => {
  const input = JSON.parse(await fs.readFile(
    new URL("../catalog/repository-catalog.json", import.meta.url),
    "utf8",
  ));
  const normalized = normalizeSeedManifest(input);
  assert.equal(normalized.repositories.length, 111);
  assert.equal(normalized.repositories.filter((record) => record.sourceStatus === "verified").length, 109);
  assert.equal(normalized.repositories.filter((record) => record.sourceStatus === "empty").length, 2);
  const internalServices = normalized.repositories.find(
    (record) => record.repository === "MoveIndustries/InternalServices",
  );
  assert.equal(internalServices.catalogId, "internalservices");
  assert.match(internalServices.resolvedCommitSha, /^[a-f0-9]{40}$/);
  assert.ok(internalServices.sources.some((source) => source.path === "README.md"));

  const root = await fs.mkdtemp(path.join(os.tmpdir(), "wiki-deployed-catalog-"));
  let app;
  try {
    const seeded = await seedCatalog(root, input);
    assert.equal(seeded.repositories, 111);
    assert.equal((await fs.readdir(path.join(root, "repos"))).length, 111);
    app = await createWikiApp(config(root, { maxCorpusFiles: 200 }));
    await new Promise((resolve) => app.server.listen(0, "127.0.0.1", resolve));
    const address = app.server.address();
    const response = await post(`http://127.0.0.1:${address.port}`, "/v1/query", {
      query: "Explain the InternalServices architecture and identify the canonical repository",
      limit: 1,
      maxExcerptChars: 1800,
    });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.results[0].path, "repos/internalservices.md");
    assert.equal(body.results[0].source, "index");
    assert.match(body.results[0].excerpt, /MoveIndustries\/InternalServices/);
    assert.match(body.results[0].excerpt, /README\.md/);
  } finally {
    if (app?.server.listening) await new Promise((resolve) => app.server.close(resolve));
    await fs.rm(root, { recursive: true, force: true });
  }
});

test("seed manifest validation rejects unsafe catalog data", () => {
  assert.throws(() => normalizeSeedManifest(manifest([{ ...repositories[0], catalogId: "../escape" }])), /filesystem-safe/);
  assert.throws(() => normalizeSeedManifest(manifest([{ ...repositories[0], url: "https://token@example.com/repo.git" }])), /credential-free/);
  assert.throws(() => normalizeSeedManifest(manifest([{ ...repositories[0], defaultBranch: "../../main" }])), /safe Git branch/);
  assert.throws(() => normalizeSeedManifest(manifest([repositories[0], { ...repositories[0], catalogId: "copy" }])), /duplicate repository/);
  assert.throws(() => normalizeSeedManifest(manifest([{ ...repositories[0], sourceStatus: "empty", resolvedCommitSha: null }])), /sources must be empty/);
  assert.throws(() => normalizeSeedManifest(manifest([{ ...repositories[0], resolvedCommitSha: "a".repeat(64) }])), /40-character/);
});
