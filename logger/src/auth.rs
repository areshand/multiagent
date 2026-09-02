use crate::model::validate_identifier;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::{collections::HashSet, fs, path::Path};
use subtle::ConstantTimeEq;

#[derive(Clone, Debug)]
pub struct Client {
    pub id: String,
    token_digest: [u8; 32],
    permissions: HashSet<String>,
    event_types: Vec<String>,
    sessions: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct Authorizer {
    clients: Vec<Client>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ClientsFile {
    clients: Vec<ClientSpec>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ClientSpec {
    id: String,
    token_sha256: String,
    permissions: Vec<String>,
    event_types: Vec<String>,
    sessions: Vec<String>,
}

impl Authorizer {
    pub fn load(path: &Path) -> Result<Self, String> {
        let raw = fs::read(path).map_err(|error| format!("read logger clients file: {error}"))?;
        let decoded: ClientsFile = serde_json::from_slice(&raw)
            .map_err(|error| format!("decode logger clients file: {error}"))?;
        if decoded.clients.is_empty() {
            return Err("logger clients file must contain clients".into());
        }
        let mut clients = Vec::with_capacity(decoded.clients.len());
        let mut ids = HashSet::new();
        let mut digests = HashSet::new();
        for spec in decoded.clients {
            validate_identifier(&spec.id, "logger client ID")?;
            if !ids.insert(spec.id.clone()) {
                return Err("logger client IDs must be unique".into());
            }
            let digest = decode_digest(&spec.token_sha256)?;
            if !digests.insert(digest) {
                return Err("logger client token digests must be unique".into());
            }
            let permissions = validate_permissions(spec.permissions, &spec.id)?;
            let event_types = validate_patterns(
                spec.event_types,
                &format!("logger client {} eventTypes", spec.id),
            )?;
            let sessions = validate_patterns(
                spec.sessions,
                &format!("logger client {} sessions", spec.id),
            )?;
            clients.push(Client {
                id: spec.id,
                token_digest: digest,
                permissions,
                event_types,
                sessions,
            });
        }
        Ok(Self { clients })
    }

    pub fn authenticate(&self, header: Option<&str>) -> Result<Client, AuthError> {
        let token = header
            .and_then(|value| value.strip_prefix("Bearer "))
            .filter(|value| (20..=512).contains(&value.len()))
            .filter(|value| {
                value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"._~+/=-".contains(&byte))
            })
            .ok_or_else(|| AuthError::unauthorized("bearer authentication is required"))?;
        let digest: [u8; 32] = Sha256::digest(token.as_bytes()).into();
        let selected = self.clients.iter().fold(None, |selected, client| {
            if bool::from(digest.ct_eq(&client.token_digest)) {
                Some(client.clone())
            } else {
                selected
            }
        });
        selected.ok_or_else(|| AuthError::unauthorized("invalid bearer token"))
    }
}

impl Client {
    pub fn require(&self, permission: &str) -> Result<(), AuthError> {
        if self.permissions.contains(permission) {
            Ok(())
        } else {
            Err(AuthError::forbidden(
                "forbidden",
                format!("client {} lacks {permission} permission", self.id),
            ))
        }
    }
    pub fn authorize_session(&self, value: &str) -> Result<(), AuthError> {
        if matches_any(&self.sessions, value) {
            Ok(())
        } else {
            Err(AuthError::forbidden(
                "session_forbidden",
                format!("client {} is not authorized for this session", self.id),
            ))
        }
    }
    pub fn authorize_event(&self, value: &str) -> Result<(), AuthError> {
        if matches_any(&self.event_types, value) {
            Ok(())
        } else {
            Err(AuthError::forbidden(
                "event_type_forbidden",
                format!("client {} is not authorized for this event type", self.id),
            ))
        }
    }
}

#[derive(Debug)]
pub struct AuthError {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
}
impl AuthError {
    fn unauthorized(message: &str) -> Self {
        Self {
            status: 401,
            code: "unauthorized",
            message: message.into(),
        }
    }
    fn forbidden(code: &'static str, message: String) -> Self {
        Self {
            status: 403,
            code,
            message,
        }
    }
}

fn decode_digest(value: &str) -> Result<[u8; 32], String> {
    let hex = value
        .strip_prefix("sha256:")
        .ok_or("tokenSha256 must be a lowercase sha256 digest")?;
    if hex.len() != 64
        || !hex
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    {
        return Err("tokenSha256 must be a lowercase sha256 digest".into());
    }
    let mut output = [0; 32];
    for (index, pair) in hex.as_bytes().chunks_exact(2).enumerate() {
        output[index] = (nibble(pair[0])? << 4) | nibble(pair[1])?;
    }
    Ok(output)
}
fn nibble(value: u8) -> Result<u8, String> {
    match value {
        b'0'..=b'9' => Ok(value - b'0'),
        b'a'..=b'f' => Ok(value - b'a' + 10),
        _ => Err("invalid digest".into()),
    }
}
fn validate_permissions(values: Vec<String>, id: &str) -> Result<HashSet<String>, String> {
    if values.is_empty() {
        return Err(format!("logger client {id} must have permissions"));
    }
    let values = values.into_iter().collect::<HashSet<_>>();
    if values
        .iter()
        .any(|value| !matches!(value.as_str(), "append" | "read" | "verify"))
    {
        return Err(format!("logger client {id} has unsupported permission"));
    }
    Ok(values)
}
fn validate_patterns(values: Vec<String>, name: &str) -> Result<Vec<String>, String> {
    if values.is_empty() || values.len() > 128 {
        return Err(format!(
            "{name} must be a non-empty array of at most 128 patterns"
        ));
    }
    for pattern in &values {
        let stars = pattern.matches('*').count();
        if pattern.is_empty()
            || pattern.len() > 128
            || stars > 1
            || (stars == 1 && !pattern.ends_with('*'))
        {
            return Err(format!("{name} contains an invalid pattern"));
        }
    }
    Ok(values)
}
fn matches_any(patterns: &[String], value: &str) -> bool {
    patterns.iter().any(|pattern| {
        pattern == "*"
            || pattern == value
            || pattern
                .strip_suffix('*')
                .is_some_and(|prefix| value.starts_with(prefix))
    })
}
