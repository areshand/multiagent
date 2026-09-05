import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

const EXCLUDED_DIRECTORIES = new Set([".git", "node_modules"]);

export function sha256Text(text) {
  return crypto.createHash("sha256").update(text).digest("hex");
}

export function containedPath(root, relativePath) {
  if (typeof relativePath !== "string" || path.isAbsolute(relativePath) || relativePath.includes("\0")) return null;
  const normalized = relativePath.replaceAll("\\", "/").replace(/^\.\//, "").split("#", 1)[0];
  if (!normalized || normalized.split("/").some((part) => !part || part === "." || part === "..")) return null;
  const candidate = path.resolve(root, normalized);
  return candidate.startsWith(`${root}${path.sep}`) ? candidate : null;
}

async function markdownDescriptor(root, relative) {
  const absolute = containedPath(root, relative);
  if (!absolute) return null;
  let stat;
  try {
    stat = await fs.lstat(absolute);
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
  if (!stat.isFile() || stat.isSymbolicLink()) return null;
  const real = await fs.realpath(absolute);
  if (!real.startsWith(`${root}${path.sep}`)) return null;
  return { absolute: real, path: relative, size: stat.size };
}

async function walkFallbackMarkdown(root, limits, excludedPaths) {
  const files = [];
  let totalBytes = 0;
  const pending = [{ absolute: root, relative: "" }];
  while (pending.length) {
    const directory = pending.pop();
    let entries;
    try {
      entries = await fs.readdir(directory.absolute, { withFileTypes: true });
    } catch (error) {
      if (error.code === "ENOENT") return files;
      throw error;
    }
    entries.sort((left, right) => left.name.localeCompare(right.name, "en"));
    for (let index = entries.length - 1; index >= 0; index -= 1) {
      const entry = entries[index];
      if (entry.isSymbolicLink()) continue;
      // `index.md` is the only authority for repository membership. Never walk
      // the repos directory: an old object left by a previous S3 seed must not
      // consume corpus limits or become fallback evidence.
      if (directory.relative === "" && entry.isDirectory()
          && (entry.name === "system" || entry.name === "repos")) continue;
      const relative = directory.relative ? `${directory.relative}/${entry.name}` : entry.name;
      const absolute = path.join(directory.absolute, entry.name);
      if (entry.isDirectory() && !EXCLUDED_DIRECTORIES.has(entry.name)) {
        pending.push({ absolute, relative });
      } else if (entry.isFile() && entry.name.toLowerCase().endsWith(".md")
          && relative !== "index.md" && !excludedPaths.has(relative)) {
        const stat = await fs.stat(absolute);
        if (files.length >= limits.maxFiles || totalBytes + stat.size > limits.maxBytes) {
          throw new Error(`Markdown corpus exceeds configured bounds (${limits.maxFiles} files, ${limits.maxBytes} bytes)`);
        }
        files.push({ absolute, path: relative, size: stat.size });
        totalBytes += stat.size;
      }
    }
  }
  return files.sort((left, right) => left.path.localeCompare(right.path, "en"));
}

function titleOf(markdown, fallback) {
  const match = /^#\s+(.+)$/m.exec(markdown);
  return match ? match[1].trim().slice(0, 300) : fallback.replace(/\.md$/i, "");
}

function catalogDigestOf(markdown) {
  const frontmatter = /^---\n([\s\S]*?)\n---(?:\n|$)/.exec(markdown)?.[1];
  if (!frontmatter) return null;
  return /^catalog_digest:\s*["']?(sha256:[a-f0-9]{64})["']?\s*$/m.exec(frontmatter)?.[1] || null;
}

function frontmatterField(markdown, field) {
  const frontmatter = /^---\n([\s\S]*?)\n---(?:\n|$)/.exec(markdown)?.[1];
  if (!frontmatter) return null;
  const match = new RegExp(`^${field}:\\s*["']?([^"'\\n]+)["']?\\s*$`, "m").exec(frontmatter);
  return match?.[1]?.trim() || null;
}

function repositoryCountOf(markdown) {
  const frontmatter = /^---\n([\s\S]*?)\n---(?:\n|$)/.exec(markdown)?.[1];
  const value = frontmatter && /^repository_count:\s*(\d+)\s*$/m.exec(frontmatter)?.[1];
  return value === undefined ? null : Number(value);
}

function indexLinks(markdown, root) {
  const links = [];
  const seen = new Set();
  const targets = [];
  for (const match of markdown.matchAll(/\[[^\]]*\]\(([^)]+\.md(?:#[^)]*)?)\)/gi)) {
    targets.push(match[1]);
  }
  for (const match of markdown.matchAll(/\[\[([^\]]+)\]\]/g)) {
    let target = match[1].split("|", 1)[0].split("#", 1)[0].trim();
    if (target && !target.toLowerCase().endsWith(".md")) target = `${target}.md`;
    targets.push(target);
  }
  for (const rawTarget of targets) {
    let target;
    try { target = decodeURIComponent(rawTarget.trim()); } catch { continue; }
    const absolute = containedPath(root, target);
    if (!absolute) continue;
    const relative = path.relative(root, absolute).split(path.sep).join("/");
    if (relative === "index.md" || relative.startsWith("system/") || seen.has(relative)) continue;
    seen.add(relative);
    links.push(relative);
  }
  return links;
}

async function loadDocument(file) {
  const text = await fs.readFile(file.absolute, "utf8");
  return Object.freeze({
    path: file.path,
    title: titleOf(text, file.path),
    text,
    bytes: Buffer.byteLength(text),
    sha256: sha256Text(text),
  });
}

export async function loadCorpus(root, limits) {
  await fs.mkdir(root, { recursive: true });
  let selectedRoot = root;
  try {
    const directIndex = path.join(root, "index.md");
    await fs.access(directIndex);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    try {
      await fs.access(path.join(root, "LLM Wiki", "index.md"));
      selectedRoot = path.join(root, "LLM Wiki");
    } catch (nestedError) {
      if (nestedError.code !== "ENOENT") throw nestedError;
    }
  }
  const canonicalRoot = await fs.realpath(selectedRoot);
  const documents = [];
  let totalBytes = 0;
  const include = async (descriptor) => {
    if (!descriptor) return null;
    if (documents.length >= limits.maxFiles || totalBytes + descriptor.size > limits.maxBytes) {
      throw new Error(`Markdown corpus exceeds configured bounds (${limits.maxFiles} files, ${limits.maxBytes} bytes)`);
    }
    const document = await loadDocument(descriptor);
    documents.push(document);
    totalBytes += document.bytes;
    return document;
  };

  const index = await include(await markdownDescriptor(canonicalRoot, "index.md"));
  const linkedPaths = index ? indexLinks(index.text, canonicalRoot) : [];
  const generationDigest = index ? catalogDigestOf(index.text) : null;
  const indexSchema = index ? frontmatterField(index.text, "schema") : null;
  if (index && limits.profile === "organization" && indexSchema !== "wiki-repository-catalog/v1") {
    throw new Error("organization Wiki requires schema wiki-repository-catalog/v1");
  }
  if (index && (limits.profile === "organization" || indexSchema === "wiki-repository-catalog/v1") && !generationDigest) {
    throw new Error("organization Wiki index has no valid catalog_digest");
  }
  const expectedCount = index ? repositoryCountOf(index.text) : null;
  const repositoryLinks = linkedPaths.filter((linkedPath) => linkedPath.startsWith("repos/"));
  if (expectedCount !== null && expectedCount !== repositoryLinks.length) {
    throw new Error("Wiki index repository_count does not match its unique repository links");
  }
  const indexed = [];
  for (const linkedPath of linkedPaths) {
    const descriptor = await markdownDescriptor(canonicalRoot, linkedPath);
    if (!descriptor) throw new Error(`indexed Markdown page is missing or unsafe: ${linkedPath}`);
    const document = await include(descriptor);
    if (generationDigest && catalogDigestOf(document.text) !== generationDigest) {
      throw new Error(`indexed Markdown page has a different catalog_digest: ${linkedPath}`);
    }
    indexed.push(document);
  }

  const indexedSet = new Set(indexed.map((document) => document.path));
  const remaining = {
    maxFiles: Math.max(0, limits.maxFiles - documents.length),
    maxBytes: Math.max(0, limits.maxBytes - totalBytes),
  };
  const fallbackFiles = await walkFallbackMarkdown(canonicalRoot, remaining, indexedSet);
  const fallback = [];
  for (const file of fallbackFiles) {
    const document = await include(file);
    if (document) fallback.push(document);
  }
  return Object.freeze({
    documents: Object.freeze(documents),
    indexed: Object.freeze(indexed),
    fallback: Object.freeze(fallback),
    indexDigest: index?.sha256 || null,
    catalogDigest: generationDigest,
    loadedAt: new Date().toISOString(),
  });
}

export async function atomicReplace(destination, contents, mode = 0o644) {
  const directory = path.dirname(destination);
  await fs.mkdir(directory, { recursive: true });
  const temporary = path.join(directory, `.${path.basename(destination)}.${process.pid}.${crypto.randomUUID()}.tmp`);
  await fs.writeFile(temporary, contents, { encoding: "utf8", mode, flag: "wx" });
  try {
    await fs.rename(temporary, destination);
  } catch (error) {
    await fs.rm(temporary, { force: true });
    throw error;
  }
}
