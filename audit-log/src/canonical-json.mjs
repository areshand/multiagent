export function canonicalJson(value) {
  return JSON.stringify(canonicalValue(value));
}

function canonicalValue(value) {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("canonical JSON forbids non-finite numbers");
    return Object.is(value, -0) ? 0 : value;
  }
  if (Array.isArray(value)) return value.map(canonicalValue);
  if (typeof value === "object") {
    const result = {};
    for (const key of Object.keys(value).sort()) {
      const child = value[key];
      if (child === undefined || typeof child === "function" || typeof child === "symbol") {
        throw new TypeError(`canonical JSON cannot encode property ${key}`);
      }
      result[key] = canonicalValue(child);
    }
    return result;
  }
  throw new TypeError(`canonical JSON cannot encode ${typeof value}`);
}
