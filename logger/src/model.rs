use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Event {
    pub event_id: String,
    pub session_id: String,
    pub event_type: String,
    pub payload_digest: String,
    pub artifact_references: Vec<ArtifactReference>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct ArtifactReference {
    pub uri: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub digest: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub media_type: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AcceptedEvent {
    #[serde(flatten)]
    pub event: Event,
    pub producer_id: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EntryBody {
    pub log_id: String,
    pub sequence: u64,
    pub previous_hash: String,
    pub committed_at: String,
    pub producer_id: String,
    pub event_id: String,
    pub event_type: String,
    pub payload_digest: String,
    pub artifact_references: Vec<ArtifactReference>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Entry {
    #[serde(flatten)]
    pub body: EntryBody,
    pub entry_hash: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Signature {
    pub algorithm: String,
    pub key_id: String,
    pub signature: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct CheckpointBody {
    pub api_version: String,
    pub kind: String,
    pub checkpoint_id: String,
    pub log_id: String,
    pub sequence: u64,
    pub entry_hash: String,
    pub created_at: String,
    pub logger_identity: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Checkpoint {
    #[serde(flatten)]
    pub body: CheckpointBody,
    pub logger_signature: Signature,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogHead {
    pub log_id: String,
    pub sequence: u64,
    pub entry_hash: String,
    pub updated_at: String,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Verification {
    pub ok: bool,
    pub checked_logs: usize,
    pub checked_entries: usize,
    pub checked_checkpoints: usize,
    pub errors: Vec<String>,
}

pub fn validate_event(event: &Event) -> Result<(), String> {
    validate_identifier(&event.event_id, "eventId")?;
    validate_identifier(&event.session_id, "sessionId")?;
    if !valid_event_type(&event.event_type) {
        return Err("eventType has an invalid format".into());
    }
    validate_digest(&event.payload_digest, "payloadDigest")?;
    if event.artifact_references.len() > 64 {
        return Err("artifactReferences must contain at most 64 entries".into());
    }
    for (index, reference) in event.artifact_references.iter().enumerate() {
        if reference.uri.is_empty()
            || reference.uri.len() > 2048
            || reference.uri.contains(['\r', '\n', '?', '#'])
        {
            return Err(format!("artifactReferences[{index}].uri is invalid"));
        }
        if let Some(digest) = &reference.digest {
            validate_digest(digest, &format!("artifactReferences[{index}].digest"))?;
        }
        if reference
            .media_type
            .as_ref()
            .is_some_and(|value| value.is_empty() || value.len() > 255)
        {
            return Err(format!("artifactReferences[{index}].mediaType is invalid"));
        }
    }
    Ok(())
}

pub fn validate_identifier(value: &str, label: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || !value.chars().enumerate().all(|(index, ch)| {
            ch.is_ascii_alphanumeric() || index > 0 && matches!(ch, '.' | '_' | ':' | '-')
        })
    {
        return Err(format!("{label} has an invalid format"));
    }
    Ok(())
}

pub fn validate_digest(value: &str, label: &str) -> Result<(), String> {
    let Some(hex) = value.strip_prefix("sha256:") else {
        return Err(format!("{label} must be a lowercase sha256 digest"));
    };
    if hex.len() != 64
        || !hex
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
    {
        return Err(format!("{label} must be a lowercase sha256 digest"));
    }
    Ok(())
}

fn valid_event_type(value: &str) -> bool {
    if value.is_empty()
        || value.len() > 128
        || !value.starts_with(|ch: char| ch.is_ascii_lowercase())
    {
        return false;
    }
    let mut segments = 1;
    let mut previous_separator = false;
    for ch in value.chars() {
        if matches!(ch, '.' | '_' | '-') {
            if previous_separator {
                return false;
            }
            previous_separator = true;
            segments += 1;
        } else if ch.is_ascii_lowercase() || ch.is_ascii_digit() {
            previous_separator = false;
        } else {
            return false;
        }
    }
    !previous_separator && segments <= 16
}
