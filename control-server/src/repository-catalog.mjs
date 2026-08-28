const repositoryNamePattern = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const supportedAuthentication = new Set(["anonymous", "github-app"]);

function normalizedEntry(name, value) {
  if (!repositoryNamePattern.test(name)) throw new Error(`invalid repository name in catalog: ${name}`);
  const entry = typeof value === "string" ? { url: value, authentication: "anonymous" } : value;
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    throw new Error(`repository catalog entry must be a URL or object: ${name}`);
  }
  const url = String(entry.url || "");
  let parsed;
  try { parsed = new URL(url); } catch { throw new Error(`repository URL is invalid: ${name}`); }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error(`repository URL must be credential-free HTTPS: ${name}`);
  }
  const authentication = String(entry.authentication || "anonymous");
  if (!supportedAuthentication.has(authentication)) {
    throw new Error(`unsupported repository authentication: ${name}`);
  }
  if (authentication === "github-app" && parsed.hostname !== "github.com") {
    throw new Error(`github-app repositories must use github.com: ${name}`);
  }
  return Object.freeze({ url, authentication });
}

export function parseRepositoryCatalog(serialized) {
  let raw;
  try { raw = JSON.parse(serialized || "{}"); } catch { throw new Error("MULTIAGENT_REPOSITORIES_JSON must be valid JSON"); }
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error("MULTIAGENT_REPOSITORIES_JSON must be an object");
  }
  return Object.freeze(Object.fromEntries(Object.entries(raw).map(([name, value]) => [name, normalizedEntry(name, value)])));
}

export function configuredRepository(catalog, name) {
  if (!repositoryNamePattern.test(name) || !catalog[name]) throw new Error(`repository is not configured: ${name}`);
  return catalog[name];
}

