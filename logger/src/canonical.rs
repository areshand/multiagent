use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

pub fn bytes<T: Serialize>(value: &T) -> Result<Vec<u8>, String> {
    let value =
        serde_json::to_value(value).map_err(|error| format!("encode canonical value: {error}"))?;
    serde_json::to_vec(&canonical_value(value))
        .map_err(|error| format!("encode canonical JSON: {error}"))
}

pub fn string<T: Serialize>(value: &T) -> Result<String, String> {
    String::from_utf8(bytes(value)?)
        .map_err(|error| format!("canonical JSON is not UTF-8: {error}"))
}

pub fn sha256<T: Serialize>(value: &T) -> Result<String, String> {
    Ok(format!("sha256:{:x}", Sha256::digest(bytes(value)?)))
}

pub fn sha256_bytes(value: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(value))
}

fn canonical_value(value: Value) -> Value {
    match value {
        Value::Array(values) => Value::Array(values.into_iter().map(canonical_value).collect()),
        Value::Object(values) => {
            let mut pairs = values.into_iter().collect::<Vec<_>>();
            pairs.sort_by(|left, right| left.0.cmp(&right.0));
            Value::Object(
                pairs
                    .into_iter()
                    .map(|(key, value)| (key, canonical_value(value)))
                    .collect(),
            )
        }
        other => other,
    }
}
