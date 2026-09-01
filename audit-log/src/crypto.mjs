import {
  createHash,
  createPrivateKey,
  createPublicKey,
  sign as cryptoSign,
  verify as cryptoVerify,
} from "node:crypto";
import fs from "node:fs";
import { canonicalJson } from "./canonical-json.mjs";

export function sha256(value) {
  return `sha256:${createHash("sha256").update(value).digest("hex")}`;
}

export class Ed25519Signer {
  constructor({ privateKeyFile, keyId, loggerId }) {
    if (!privateKeyFile) throw new Error("AUDIT_LOG_SIGNING_KEY_FILE is required");
    if (!keyId) throw new Error("AUDIT_LOG_SIGNING_KEY_ID is required");
    if (!loggerId) throw new Error("AUDIT_LOG_LOGGER_ID is required");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(keyId)) throw new Error("audit logger signing key ID has an invalid format");
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(loggerId)) throw new Error("audit logger identity has an invalid format");
    this.privateKey = createPrivateKey(fs.readFileSync(privateKeyFile));
    if (this.privateKey.asymmetricKeyType !== "ed25519") {
      throw new Error("audit logger signing key must be Ed25519");
    }
    this.publicKey = createPublicKey(this.privateKey);
    this.keyId = keyId;
    this.loggerId = loggerId;
  }

  sign(body) {
    return {
      algorithm: "Ed25519",
      keyId: this.keyId,
      signature: Buffer.from(cryptoSign(null, Buffer.from(canonicalJson(body)), this.privateKey)).toString("base64url"),
    };
  }

  verify(body, signature) {
    if (
      !signature ||
      signature.algorithm !== "Ed25519" ||
      signature.keyId !== this.keyId ||
      typeof signature.signature !== "string"
    ) return false;
    try {
      return cryptoVerify(
        null,
        Buffer.from(canonicalJson(body)),
        this.publicKey,
        Buffer.from(signature.signature, "base64url"),
      );
    } catch {
      return false;
    }
  }

  publicDescriptor() {
    return {
      algorithm: "Ed25519",
      keyId: this.keyId,
      loggerIdentity: this.loggerId,
      publicKeyPem: this.publicKey.export({ type: "spki", format: "pem" }).toString(),
    };
  }
}
