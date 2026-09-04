import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

import { atomicReplace } from "./markdown-store.mjs";
import {
  RequestError,
  boundedString,
  commitSha,
  repositoryName,
  requireObject,
  validateSources,
} from "./validation.mjs";

export const MAX_MANIFEST_BYTES = 2 * 1024 * 1024;
export const MAX_REPOSITORIES = 1_000;

const CATALOG_ID_PATTERN = /^[a-z0-9](?:[a-z0-9_-]{0,126}[a-z0-9])?$/;
const VISIBILITIES = new Set(["public", "private", "internal"]);
const SOURCE_STATUSES = new Set(["verified", "empty"]);

function normalizeText(value, name, maximum) {
  return boundedString(value, name, { maximum }).replace(/\r\n?/g, "\n");
}

function catalogId(value, index) {
  const id = boundedString(value, `repositories[${index}].catalogId`, { maximum: 128 });
  if (!CATALOG_ID_PATTERN.test(id)) {
    throw new RequestError(`repositories[${index}].catalogId must be a lowercase filesystem-safe identifier`);
  }
  return id;
}

function repositoryUrl(value, index) {
  const raw = boundedString(value, `repositories[${index}].url`, { maximum: 2048 });
  let parsed;
  try { parsed = new URL(raw); } catch { throw new RequestError(`repositories[${index}].url must be a valid HTTPS URL`); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash || !parsed.hostname) {
    throw new RequestError(`repositories[${index}].url must be a credential-free HTTPS URL without query or fragment`);
  }
  return parsed.toString();
}

function visibility(value, index) {
  const normalized = boundedString(value, `repositories[${index}].visibility`, { maximum: 20 }).toLowerCase();
  if (!VISIBILITIES.has(normalized)) {
    throw new RequestError(`repositories[${index}].visibility must be public, private, or internal`);
  }
  return normalized;
}

function defaultBranch(value, index) {
  const branch = boundedString(value, `repositories[${index}].defaultBranch`, { maximum: 255 });
  if (!/^[A-Za-z0-9._/-]+$/.test(branch)
      || branch.startsWith("/") || branch.endsWith("/")
      || branch.includes("//") || branch.includes("..") || branch.includes("@{")
      || branch.endsWith(".lock")) {
    throw new RequestError(`repositories[${index}].defaultBranch is not a safe Git branch name`);
  }
  return branch;
}

function conciseSummary(value, index) {
  const summary = normalizeText(value, `repositories[${index}].summary`, 500);
  if (summary.includes("\n")) throw new RequestError(`repositories[${index}].summary must be a single paragraph`);
  return summary;
}

function sourceStatus(value, index) {
  const status = boundedString(value, `repositories[${index}].sourceStatus`, { maximum: 20 }).toLowerCase();
  if (!SOURCE_STATUSES.has(status)) {
    throw new RequestError(`repositories[${index}].sourceStatus must be verified or empty`);
  }
  return status;
}

function normalizeRepository(value, index) {
  requireObject(value, `repositories[${index}]`);
  const status = sourceStatus(value.sourceStatus, index);
  let sources;
  let resolvedCommitSha;
  if (status === "verified") {
    resolvedCommitSha = commitSha(value.resolvedCommitSha);
    if (resolvedCommitSha.length !== 40) {
      throw new RequestError(`repositories[${index}].resolvedCommitSha must be a 40-character Git commit SHA for verified sources`);
    }
    sources = validateSources(value.sources)
      .map((source) => ({ path: source.path, sha256: source.sha256 }))
      .sort((left, right) => left.path.localeCompare(right.path, "en"));
  } else {
    if (value.resolvedCommitSha !== null) {
      throw new RequestError(`repositories[${index}].resolvedCommitSha must be null when sourceStatus is empty`);
    }
    if (!Array.isArray(value.sources) || value.sources.length !== 0) {
      throw new RequestError(`repositories[${index}].sources must be empty when sourceStatus is empty`);
    }
    resolvedCommitSha = null;
    sources = [];
  }
  const summary = conciseSummary(value.summary, index);
  const description = normalizeText(value.description, `repositories[${index}].description`, 20_000);
  return Object.freeze({
    catalogId: catalogId(value.catalogId, index),
    repository: repositoryName(value.repository),
    url: repositoryUrl(value.url, index),
    visibility: visibility(value.visibility, index),
    defaultBranch: defaultBranch(value.defaultBranch, index),
    sourceStatus: status,
    resolvedCommitSha,
    summary,
    description,
    sources: Object.freeze(sources),
  });
}

export function normalizeSeedManifest(value) {
  requireObject(value, "manifest");
  if (value.schema !== "wiki-catalog-seed/v1") {
    throw new RequestError("manifest.schema must be wiki-catalog-seed/v1");
  }
  if (!Array.isArray(value.repositories) || value.repositories.length < 1 || value.repositories.length > MAX_REPOSITORIES) {
    throw new RequestError(`manifest.repositories must contain 1-${MAX_REPOSITORIES} records`);
  }
  const repositories = value.repositories.map(normalizeRepository)
    .sort((left, right) => left.catalogId.localeCompare(right.catalogId, "en"));
  const catalogIds = new Set();
  const repositoryNames = new Set();
  for (const record of repositories) {
    if (catalogIds.has(record.catalogId)) throw new RequestError(`duplicate catalogId: ${record.catalogId}`);
    if (repositoryNames.has(record.repository.toLowerCase())) throw new RequestError(`duplicate repository: ${record.repository}`);
    catalogIds.add(record.catalogId);
    repositoryNames.add(record.repository.toLowerCase());
  }
  return Object.freeze({ schema: value.schema, repositories: Object.freeze(repositories) });
}

function yamlString(value) {
  return JSON.stringify(value);
}

function escapeInline(value) {
  return value.replaceAll("\\", "\\\\").replaceAll("`", "\\`").replaceAll("[", "\\[").replaceAll("]", "\\]");
}

function repositoryMarkdown(record, catalogDigest) {
  return [
    "---",
    "schema: wiki-repository/v1",
    `catalog_id: ${yamlString(record.catalogId)}`,
    `repository: ${yamlString(record.repository)}`,
    `url: ${yamlString(record.url)}`,
    `visibility: ${yamlString(record.visibility)}`,
    `default_branch: ${yamlString(record.defaultBranch)}`,
    `source_status: ${yamlString(record.sourceStatus)}`,
    `resolved_commit_sha: ${record.resolvedCommitSha === null ? "null" : yamlString(record.resolvedCommitSha)}`,
    `catalog_digest: ${yamlString(`sha256:${catalogDigest}`)}`,
    "---",
    "",
    `# ${record.repository}`,
    "",
    "## Summary",
    "",
    record.summary,
    "",
    "## Description",
    "",
    record.description,
    "",
    "## Sources",
    "",
    ...(record.sourceStatus === "empty"
      ? ["No repository source exists at this catalog snapshot."]
      : record.sources.map((source) => `- \`${source.path.replaceAll("`", "\\`")}\` — \`sha256:${source.sha256}\``)),
    "",
  ].join("\n");
}

function indexMarkdown(repositories, catalogDigest) {
  return [
    "---",
    "schema: wiki-repository-catalog/v1",
    `catalog_digest: ${yamlString(`sha256:${catalogDigest}`)}`,
    `repository_count: ${repositories.length}`,
    "---",
    "",
    "# Organization repository catalog",
    "",
    ...repositories.map((record) => `- [${escapeInline(record.repository)}](repos/${record.catalogId}.md) — ${record.summary}`),
    "",
  ].join("\n");
}

function catalogDigest(manifest) {
  return crypto.createHash("sha256").update(JSON.stringify(manifest)).digest("hex");
}

export async function seedCatalog(root, input) {
  const absoluteRoot = path.resolve(root);
  const manifest = normalizeSeedManifest(input);
  const digest = catalogDigest(manifest);
  const repositoriesDirectory = path.join(absoluteRoot, "repos");
  await fs.mkdir(repositoriesDirectory, { recursive: true });

  // Each page is replaced atomically. index.md is published last and therefore
  // acts as the commit marker: queries only discover the new set after all pages
  // it references are durable.
  for (const record of manifest.repositories) {
    await atomicReplace(
      path.join(repositoriesDirectory, `${record.catalogId}.md`),
      repositoryMarkdown(record, digest),
    );
  }
  await atomicReplace(path.join(absoluteRoot, "index.md"), indexMarkdown(manifest.repositories, digest));
  return Object.freeze({
    status: "seeded",
    repositories: manifest.repositories.length,
    catalogDigest: `sha256:${digest}`,
    indexPath: "index.md",
  });
}

export async function readSeedManifest(manifestPath, maximumBytes = MAX_MANIFEST_BYTES) {
  const stat = await fs.stat(manifestPath);
  if (!stat.isFile()) throw new RequestError("manifest path must identify a regular file");
  if (stat.size > maximumBytes) throw new RequestError(`manifest exceeds ${maximumBytes} bytes`);
  const bytes = await fs.readFile(manifestPath);
  if (bytes.length > maximumBytes) throw new RequestError(`manifest exceeds ${maximumBytes} bytes`);
  try {
    return JSON.parse(bytes.toString("utf8"));
  } catch {
    throw new RequestError("manifest must contain valid JSON");
  }
}
