const SHA_PATTERN = /^(?:sha256:)?([a-f0-9]{64})$/i;
const COMMIT_PATTERN = /^[a-f0-9]{40,64}$/i;
const REPOSITORY_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

export class RequestError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.name = "RequestError";
    this.status = status;
  }
}

export function requireObject(value, name = "request") {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new RequestError(`${name} must be a JSON object`);
  }
  return value;
}

export function boundedString(value, name, { minimum = 1, maximum }) {
  if (typeof value !== "string") throw new RequestError(`${name} must be a string`);
  const normalized = value.trim();
  if (normalized.length < minimum || normalized.length > maximum) {
    throw new RequestError(`${name} must contain ${minimum}-${maximum} characters`);
  }
  return normalized;
}

export function boundedOptionalString(value, name, { maximum }) {
  if (value === undefined || value === null) return null;
  return boundedString(value, name, { minimum: 1, maximum });
}

export function boundedInteger(value, name, { fallback, minimum, maximum }) {
  if (value === undefined) return fallback;
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new RequestError(`${name} must be an integer from ${minimum} through ${maximum}`);
  }
  return value;
}

export function repositoryName(value) {
  const repository = boundedString(value, "repository", { maximum: 200 });
  if (!REPOSITORY_PATTERN.test(repository) || repository.includes("..")) {
    throw new RequestError("repository must be an owner/name catalog identifier");
  }
  return repository;
}

export function commitSha(value) {
  const commit = boundedString(value, "resolvedCommitSha", { maximum: 64 }).toLowerCase();
  if (!COMMIT_PATTERN.test(commit)) {
    throw new RequestError("resolvedCommitSha must be a 40-64 character hexadecimal commit SHA");
  }
  return commit;
}

export function sourcePath(value, index) {
  const name = `sources[${index}].path`;
  const candidate = boundedString(value, name, { maximum: 1024 }).replaceAll("\\", "/");
  if (candidate.startsWith("/") || candidate.includes("\0")) {
    throw new RequestError(`${name} must be a relative repository path`);
  }
  const segments = candidate.split("/");
  if (segments.some((part) => !part || part === "." || part === "..")) {
    throw new RequestError(`${name} must not contain empty, dot, or parent segments`);
  }
  return segments.join("/");
}

export function sha256(value, index) {
  const digest = boundedString(value, `sources[${index}].sha256`, { maximum: 71 });
  const match = SHA_PATTERN.exec(digest);
  if (!match) throw new RequestError(`sources[${index}].sha256 must be a SHA-256 digest`);
  return match[1].toLowerCase();
}

export function validateSources(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 100) {
    throw new RequestError("sources must contain 1-100 source records");
  }
  const seen = new Set();
  return value.map((entry, index) => {
    requireObject(entry, `sources[${index}]`);
    const item = { path: sourcePath(entry.path, index), sha256: sha256(entry.sha256, index) };
    if (seen.has(item.path)) throw new RequestError(`sources contains duplicate path: ${item.path}`);
    seen.add(item.path);
    return Object.freeze(item);
  });
}
