const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const EVENT_TYPE = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,15}$/;
const DIGEST = /^sha256:[a-f0-9]{64}$/;

export class ValidationError extends Error {
  constructor(message, code = "invalid_request") {
    super(message);
    this.code = code;
  }
}

function requiredString(value, name, max = 128) {
  if (typeof value !== "string" || value.length === 0 || value.length > max) {
    throw new ValidationError(`${name} must be a non-empty string of at most ${max} characters`);
  }
  return value;
}

export function validateEvent(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ValidationError("event must be a JSON object");
  }
  const allowed = new Set(["eventId", "sessionId", "eventType", "payloadDigest", "artifactReferences"]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new ValidationError(`event contains unsupported property ${key}`);
  }
  const eventId = requiredString(value.eventId, "eventId");
  const sessionId = requiredString(value.sessionId, "sessionId");
  const eventType = requiredString(value.eventType, "eventType");
  const payloadDigest = requiredString(value.payloadDigest, "payloadDigest", 71);
  if (!IDENTIFIER.test(eventId)) throw new ValidationError("eventId has an invalid format");
  if (!IDENTIFIER.test(sessionId)) throw new ValidationError("sessionId has an invalid format");
  if (!EVENT_TYPE.test(eventType)) throw new ValidationError("eventType has an invalid format");
  if (!DIGEST.test(payloadDigest)) throw new ValidationError("payloadDigest must be a lowercase sha256 digest");
  const references = value.artifactReferences;
  if (!Array.isArray(references) || references.length > 64) {
    throw new ValidationError("artifactReferences must be an array of at most 64 entries");
  }
  const artifactReferences = references.map((reference, index) => validateArtifactReference(reference, index));
  return { eventId, sessionId, eventType, payloadDigest, artifactReferences };
}

function validateArtifactReference(value, index) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ValidationError(`artifactReferences[${index}] must be an object`);
  }
  const allowed = new Set(["uri", "digest", "size", "mediaType"]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) throw new ValidationError(`artifactReferences[${index}] contains unsupported property ${key}`);
  }
  const uri = requiredString(value.uri, `artifactReferences[${index}].uri`, 2048);
  if (/[\r\n]/.test(uri) || uri.includes("?") || uri.includes("#")) {
    throw new ValidationError(`artifactReferences[${index}].uri must be a stable reference without query or fragment data`);
  }
  const result = { uri };
  if (value.digest !== undefined) {
    if (typeof value.digest !== "string" || !DIGEST.test(value.digest)) {
      throw new ValidationError(`artifactReferences[${index}].digest must be a lowercase sha256 digest`);
    }
    result.digest = value.digest;
  }
  if (value.size !== undefined) {
    if (!Number.isSafeInteger(value.size) || value.size < 0) {
      throw new ValidationError(`artifactReferences[${index}].size must be a non-negative safe integer`);
    }
    result.size = value.size;
  }
  if (value.mediaType !== undefined) {
    result.mediaType = requiredString(value.mediaType, `artifactReferences[${index}].mediaType`, 255);
  }
  return result;
}

export function validateIdentifier(value, name) {
  if (typeof value !== "string" || !IDENTIFIER.test(value)) {
    throw new ValidationError(`${name} has an invalid format`);
  }
  return value;
}
