use crate::{
    canonical,
    model::validate_identifier,
    model::{CheckpointBody, Signature},
};
use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine};
use ed25519_dalek::{
    pkcs8::{spki::der::pem::LineEnding, DecodePrivateKey, EncodePublicKey},
    Signature as DalekSignature, Signer as _, SigningKey, Verifier as _,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::{fs, path::Path};

#[derive(Clone)]
pub struct Ed25519Signer {
    key: SigningKey,
    pub key_id: String,
    pub logger_id: String,
}

impl Ed25519Signer {
    pub fn load(path: &Path, key_id: String, logger_id: String) -> Result<Self, String> {
        validate_identifier(&key_id, "logger signing key ID")?;
        validate_identifier(&logger_id, "logger identity")?;
        let pem = fs::read_to_string(path)
            .map_err(|error| format!("read logger signing key: {error}"))?;
        let key = SigningKey::from_pkcs8_pem(&pem)
            .map_err(|error| format!("decode Ed25519 PKCS#8 signing key: {error}"))?;
        Ok(Self {
            key,
            key_id,
            logger_id,
        })
    }
    pub fn sign<T: Serialize>(&self, body: &T) -> Result<Signature, String> {
        let bytes = canonical::bytes(body)?;
        Ok(Signature {
            algorithm: "Ed25519".into(),
            key_id: self.key_id.clone(),
            signature: URL_SAFE_NO_PAD.encode(self.key.sign(&bytes).to_bytes()),
        })
    }
    pub fn verify(&self, body: &CheckpointBody, signature: &Signature) -> bool {
        if signature.algorithm != "Ed25519" || signature.key_id != self.key_id {
            return false;
        }
        let Ok(bytes) = canonical::bytes(body) else {
            return false;
        };
        let Ok(raw) = URL_SAFE_NO_PAD.decode(&signature.signature) else {
            return false;
        };
        let Ok(signature) = DalekSignature::from_slice(&raw) else {
            return false;
        };
        self.key.verifying_key().verify(&bytes, &signature).is_ok()
    }
    pub fn public_descriptor(&self) -> Result<Value, String> {
        let pem = self
            .key
            .verifying_key()
            .to_public_key_pem(LineEnding::LF)
            .map_err(|error| format!("encode logger public key: {error}"))?;
        Ok(
            json!({"algorithm":"Ed25519","keyId":self.key_id,"loggerIdentity":self.logger_id,"publicKeyPem":pem}),
        )
    }
}

#[cfg(test)]
impl Ed25519Signer {
    pub(crate) fn from_seed(seed: [u8; 32]) -> Self {
        Self {
            key: SigningKey::from_bytes(&seed),
            key_id: "test-key".into(),
            logger_id: "test-logger".into(),
        }
    }
}
