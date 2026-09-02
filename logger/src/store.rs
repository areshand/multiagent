use crate::{
    canonical,
    model::{
        validate_event, validate_identifier, AcceptedEvent, Checkpoint, CheckpointBody, Entry,
        EntryBody, Event, LogHead, Verification,
    },
    signer::Ed25519Signer,
};
use chrono::{DateTime, SecondsFormat, Utc};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub const GENESIS_HASH: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";
const MAX_LEDGER_RECORD_BYTES: usize = 2 * 1024 * 1024;

pub struct Store {
    ledger_path: PathBuf,
    ledger: File,
    signer: Ed25519Signer,
    checkpoint_interval: u64,
    projection_enabled: bool,
    projection_dirty: BTreeSet<String>,
    state: LedgerState,
    writable: bool,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
struct LedgerState {
    logs: BTreeMap<String, LogHead>,
    events: HashMap<String, StoredEvent>,
    entries: BTreeMap<String, Vec<Entry>>,
    checkpoints: HashMap<String, Checkpoint>,
    checkpoints_by_log: BTreeMap<String, Vec<Checkpoint>>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct StoredEvent {
    request_digest: String,
    accepted_event: AcceptedEvent,
    entry: Entry,
}

/// One newline-delimited record is one authoritative transaction. Keeping an
/// optional checkpoint in the same record prevents a crash from committing an
/// entry without its scheduled checkpoint.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct LedgerRecord {
    api_version: String,
    kind: String,
    request_digest: String,
    accepted_event: AcceptedEvent,
    entry: Entry,
    #[serde(skip_serializing_if = "Option::is_none")]
    checkpoint: Option<Checkpoint>,
}

#[derive(Debug, Eq, PartialEq)]
pub enum AppendResult {
    Appended,
    Duplicate,
}

#[derive(Debug)]
pub enum StoreError {
    Conflict(String),
    Internal(String),
}

impl From<String> for StoreError {
    fn from(value: String) -> Self {
        Self::Internal(value)
    }
}

impl Store {
    pub fn open(
        ledger_path: &Path,
        signer: Ed25519Signer,
        checkpoint_interval: u64,
        projection_enabled: bool,
    ) -> Result<Self, String> {
        if let Some(parent) = ledger_path
            .parent()
            .filter(|path| !path.as_os_str().is_empty())
        {
            fs::create_dir_all(parent)
                .map_err(|error| format!("create logger data directory: {error}"))?;
        }
        let ledger = secure_append_file(ledger_path)?;
        lock_single_writer(&ledger)?;
        let state = replay(ledger_path, &signer)?;
        let projection_dirty = if projection_enabled {
            state.logs.keys().cloned().collect()
        } else {
            BTreeSet::new()
        };
        Ok(Self {
            ledger_path: ledger_path.to_path_buf(),
            ledger,
            signer,
            checkpoint_interval,
            projection_enabled,
            projection_dirty,
            state,
            writable: true,
        })
    }

    pub fn append(
        &mut self,
        event: Event,
        producer_id: String,
    ) -> Result<AppendResult, StoreError> {
        if !self.writable {
            return Err(StoreError::Internal(
                "logger ledger is unavailable after a durability failure".into(),
            ));
        }
        let accepted_event = AcceptedEvent {
            event: event.clone(),
            producer_id: producer_id.clone(),
        };
        let request_digest = canonical::sha256(&accepted_event)?;
        if let Some(existing) = self.state.events.get(&event.event_id) {
            if existing.request_digest == request_digest {
                return Ok(AppendResult::Duplicate);
            }
            return Err(StoreError::Conflict(
                "eventId already commits different content".into(),
            ));
        }

        let head = self.state.logs.get(&event.session_id);
        let sequence = head.map_or(1, |value| value.sequence + 1);
        let previous_hash = head
            .map(|value| value.entry_hash.clone())
            .unwrap_or_else(|| GENESIS_HASH.into());
        let committed_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let body = EntryBody {
            log_id: event.session_id.clone(),
            sequence,
            previous_hash,
            committed_at: committed_at.clone(),
            producer_id,
            event_id: event.event_id.clone(),
            event_type: event.event_type.clone(),
            payload_digest: event.payload_digest.clone(),
            artifact_references: event.artifact_references.clone(),
        };
        let entry = Entry {
            entry_hash: canonical::sha256(&body)?,
            body,
        };
        let checkpoint = if sequence % self.checkpoint_interval == 0 {
            Some(create_checkpoint(&self.signer, &entry)?)
        } else {
            None
        };
        let record = LedgerRecord {
            api_version: "logger.multiagent.dev/v1".into(),
            kind: "LoggerLedgerRecord".into(),
            request_digest,
            accepted_event,
            entry,
            checkpoint,
        };
        validate_next(&self.state, &self.signer, &record)?;
        let mut encoded = canonical::bytes(&record)?;
        if encoded.len() > MAX_LEDGER_RECORD_BYTES {
            return Err(StoreError::Internal(
                "encoded logger record exceeds the ledger limit".into(),
            ));
        }
        encoded.push(b'\n');
        if let Err(error) = self
            .ledger
            .write_all(&encoded)
            .and_then(|()| self.ledger.sync_data())
        {
            self.writable = false;
            return Err(StoreError::Internal(format!(
                "durably append logger ledger: {error}"
            )));
        }
        let log_id = record.entry.body.log_id.clone();
        insert_record(&mut self.state, record);
        if self.projection_enabled {
            self.projection_dirty.insert(log_id);
        }
        Ok(AppendResult::Appended)
    }

    pub fn head(&self, log_id: &str) -> Result<Option<LogHead>, String> {
        Ok(self.state.logs.get(log_id).cloned())
    }

    pub fn entries(&self, log_id: &str, after: u64, limit: usize) -> Result<Vec<Entry>, String> {
        Ok(self
            .state
            .entries
            .get(log_id)
            .into_iter()
            .flatten()
            .filter(|entry| entry.body.sequence > after)
            .take(limit)
            .cloned()
            .collect())
    }

    pub fn checkpoints(
        &self,
        log_id: &str,
        after: u64,
        limit: usize,
    ) -> Result<Vec<Checkpoint>, String> {
        Ok(self
            .state
            .checkpoints_by_log
            .get(log_id)
            .into_iter()
            .flatten()
            .filter(|checkpoint| checkpoint.body.sequence > after)
            .take(limit)
            .cloned()
            .collect())
    }

    pub fn checkpoint(&self, id: &str) -> Result<Option<Checkpoint>, String> {
        Ok(self.state.checkpoints.get(id).cloned())
    }

    pub fn checkpoint_session(&self, id: &str) -> Result<Option<String>, String> {
        Ok(self
            .state
            .checkpoints
            .get(id)
            .map(|checkpoint| checkpoint.body.log_id.clone()))
    }

    pub fn verify(&self, log_id: Option<&str>) -> Result<Verification, String> {
        let replayed = match replay(&self.ledger_path, &self.signer) {
            Ok(state) => state,
            Err(error) => {
                return Ok(Verification {
                    ok: false,
                    checked_logs: 0,
                    checked_entries: 0,
                    checked_checkpoints: 0,
                    errors: vec![error],
                });
            }
        };
        let mut errors = Vec::new();
        if !self.writable {
            errors.push("ledger writes are disabled after a durability failure".into());
        }
        if replayed != self.state {
            errors.push("on-disk ledger differs from the active in-memory index".into());
        }
        let logs = replayed
            .logs
            .keys()
            .filter(|candidate| log_id.is_none_or(|selected| candidate.as_str() == selected))
            .collect::<BTreeSet<_>>();
        let checked_entries = logs
            .iter()
            .map(|id| replayed.entries.get(id.as_str()).map_or(0, Vec::len))
            .sum();
        let checked_checkpoints = logs
            .iter()
            .map(|id| {
                replayed
                    .checkpoints_by_log
                    .get(id.as_str())
                    .map_or(0, Vec::len)
            })
            .sum();
        Ok(Verification {
            ok: errors.is_empty(),
            checked_logs: logs.len(),
            checked_entries,
            checked_checkpoints,
            errors,
        })
    }

    pub fn projection_counts(&self) -> Result<BTreeMap<String, u64>, String> {
        Ok(BTreeMap::from([(
            "pending".into(),
            self.projection_dirty.len() as u64,
        )]))
    }

    /// Projections are rebuilt atomically from the authoritative ledger index.
    /// This avoids a second state store and makes retries naturally idempotent.
    pub fn flush_projections(&mut self, directory: &Path) -> Result<(u64, u64), String> {
        fs::create_dir_all(directory).map_err(|error| error.to_string())?;
        let pending = self.projection_dirty.iter().cloned().collect::<Vec<_>>();
        let (mut success, mut failed) = (0, 0);
        for log_id in pending {
            match write_projection(
                directory,
                &log_id,
                self.state.entries.get(&log_id).map_or(&[], Vec::as_slice),
            ) {
                Ok(()) => {
                    self.projection_dirty.remove(&log_id);
                    success += 1;
                }
                Err(error) => {
                    eprintln!("logger projection for {log_id} failed: {error}");
                    failed += 1;
                }
            }
        }
        Ok((success, failed))
    }
}

fn replay(path: &Path, signer: &Ed25519Signer) -> Result<LedgerState, String> {
    let file =
        File::open(path).map_err(|error| format!("open logger ledger for replay: {error}"))?;
    let mut reader = BufReader::new(file);
    let mut state = LedgerState::default();
    let mut line = Vec::new();
    let mut record_number = 0usize;
    loop {
        line.clear();
        let count = reader
            .by_ref()
            .take((MAX_LEDGER_RECORD_BYTES + 2) as u64)
            .read_until(b'\n', &mut line)
            .map_err(|error| format!("read logger ledger: {error}"))?;
        if count == 0 {
            break;
        }
        record_number += 1;
        if line.len() > MAX_LEDGER_RECORD_BYTES + 1 {
            return Err(format!(
                "logger ledger record {record_number} exceeds the size limit"
            ));
        }
        if line.last() != Some(&b'\n') {
            return Err(format!(
                "logger ledger record {record_number} is truncated; manual recovery is required"
            ));
        }
        line.pop();
        if line.is_empty() {
            return Err(format!("logger ledger record {record_number} is empty"));
        }
        let record: LedgerRecord = serde_json::from_slice(&line)
            .map_err(|error| format!("decode logger ledger record {record_number}: {error}"))?;
        if canonical::bytes(&record)? != line {
            return Err(format!(
                "logger ledger record {record_number} is not canonical JSON"
            ));
        }
        validate_next(&state, signer, &record)
            .map_err(|error| format!("verify logger ledger record {record_number}: {error}"))?;
        insert_record(&mut state, record);
    }
    Ok(state)
}

fn validate_next(
    state: &LedgerState,
    signer: &Ed25519Signer,
    record: &LedgerRecord,
) -> Result<(), String> {
    if record.api_version != "logger.multiagent.dev/v1" || record.kind != "LoggerLedgerRecord" {
        return Err("ledger record has an unsupported contract".into());
    }
    validate_event(&record.accepted_event.event)?;
    validate_identifier(&record.accepted_event.producer_id, "producer ID")?;
    if canonical::sha256(&record.accepted_event)? != record.request_digest {
        return Err("accepted event digest mismatch".into());
    }
    if state
        .events
        .contains_key(&record.accepted_event.event.event_id)
    {
        return Err("ledger contains a duplicate event ID".into());
    }
    let event = &record.accepted_event.event;
    let entry = &record.entry;
    if entry.body.log_id != event.session_id
        || entry.body.event_id != event.event_id
        || entry.body.producer_id != record.accepted_event.producer_id
        || entry.body.event_type != event.event_type
        || entry.body.payload_digest != event.payload_digest
        || entry.body.artifact_references != event.artifact_references
    {
        return Err("accepted event does not match the chain entry".into());
    }
    DateTime::parse_from_rfc3339(&entry.body.committed_at)
        .map_err(|_| "entry committedAt is not RFC3339")?;
    let head = state.logs.get(&event.session_id);
    let expected_sequence = head.map_or(1, |value| value.sequence + 1);
    let expected_previous = head
        .map(|value| value.entry_hash.as_str())
        .unwrap_or(GENESIS_HASH);
    if entry.body.sequence != expected_sequence || entry.body.previous_hash != expected_previous {
        return Err("entry does not extend the current log head".into());
    }
    if canonical::sha256(&entry.body)? != entry.entry_hash {
        return Err("entry hash mismatch".into());
    }
    if let Some(checkpoint) = &record.checkpoint {
        validate_checkpoint(state, signer, checkpoint, entry)?;
    }
    Ok(())
}

fn validate_checkpoint(
    state: &LedgerState,
    signer: &Ed25519Signer,
    checkpoint: &Checkpoint,
    entry: &Entry,
) -> Result<(), String> {
    let body = &checkpoint.body;
    if body.api_version != "logger.multiagent.dev/v1"
        || body.kind != "LoggerCheckpoint"
        || body.logger_identity != signer.logger_id
        || body.log_id != entry.body.log_id
        || body.sequence != entry.body.sequence
        || body.entry_hash != entry.entry_hash
        || body.created_at != entry.body.committed_at
    {
        return Err("checkpoint does not match its ledger entry".into());
    }
    if state.checkpoints.contains_key(&body.checkpoint_id)
        || state
            .checkpoints_by_log
            .get(&body.log_id)
            .is_some_and(|values| {
                values
                    .iter()
                    .any(|value| value.body.sequence == body.sequence)
            })
    {
        return Err("ledger contains a duplicate checkpoint".into());
    }
    if !signer.verify(body, &checkpoint.logger_signature) {
        return Err("checkpoint signature is invalid".into());
    }
    Ok(())
}

fn insert_record(state: &mut LedgerState, record: LedgerRecord) {
    let log_id = record.entry.body.log_id.clone();
    let event_id = record.accepted_event.event.event_id.clone();
    state.logs.insert(
        log_id.clone(),
        LogHead {
            log_id: log_id.clone(),
            sequence: record.entry.body.sequence,
            entry_hash: record.entry.entry_hash.clone(),
            updated_at: record.entry.body.committed_at.clone(),
        },
    );
    state
        .entries
        .entry(log_id.clone())
        .or_default()
        .push(record.entry.clone());
    state.events.insert(
        event_id,
        StoredEvent {
            request_digest: record.request_digest,
            accepted_event: record.accepted_event,
            entry: record.entry,
        },
    );
    if let Some(checkpoint) = record.checkpoint {
        state
            .checkpoints_by_log
            .entry(log_id)
            .or_default()
            .push(checkpoint.clone());
        state
            .checkpoints
            .insert(checkpoint.body.checkpoint_id.clone(), checkpoint);
    }
}

fn create_checkpoint(signer: &Ed25519Signer, entry: &Entry) -> Result<Checkpoint, String> {
    let digest = format!(
        "{:x}",
        Sha256::digest(format!(
            "{}:{}:{}",
            entry.body.log_id, entry.body.sequence, entry.entry_hash
        ))
    );
    let body = CheckpointBody {
        api_version: "logger.multiagent.dev/v1".into(),
        kind: "LoggerCheckpoint".into(),
        checkpoint_id: format!("checkpoint-{}", &digest[..48]),
        log_id: entry.body.log_id.clone(),
        sequence: entry.body.sequence,
        entry_hash: entry.entry_hash.clone(),
        created_at: entry.body.committed_at.clone(),
        logger_identity: signer.logger_id.clone(),
    };
    Ok(Checkpoint {
        logger_signature: signer.sign(&body)?,
        body,
    })
}

fn write_projection(directory: &Path, log_id: &str, entries: &[Entry]) -> Result<(), String> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let temporary = directory.join(format!(".{log_id}.{}.{unique}.tmp", std::process::id()));
    let destination = directory.join(format!("{log_id}.jsonl"));
    let result = (|| {
        let mut file = secure_create_new(&temporary)?;
        for entry in entries {
            file.write_all(&canonical::bytes(entry)?)
                .and_then(|()| file.write_all(b"\n"))
                .map_err(|error| format!("write logger projection: {error}"))?;
        }
        file.sync_all()
            .map_err(|error| format!("sync logger projection: {error}"))?;
        fs::rename(&temporary, &destination)
            .map_err(|error| format!("publish logger projection: {error}"))?;
        protect(&destination, 0o600)?;
        sync_directory(directory)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn secure_append_file(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.create(true).read(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    }
    let file = options
        .open(path)
        .map_err(|error| format!("open logger ledger: {error}"))?;
    if !file
        .metadata()
        .map_err(|error| format!("inspect logger ledger: {error}"))?
        .is_file()
    {
        return Err("logger ledger must be a regular file".into());
    }
    protect_file(&file, 0o600)?;
    Ok(file)
}

fn secure_create_new(path: &Path) -> Result<File, String> {
    let mut options = OpenOptions::new();
    options.create_new(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|error| format!("create logger projection: {error}"))
}

#[cfg(unix)]
fn protect(path: &Path, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .map_err(|error| format!("protect {}: {error}", path.display()))
}

#[cfg(unix)]
fn protect_file(file: &File, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    file.set_permissions(fs::Permissions::from_mode(mode))
        .map_err(|error| format!("protect logger ledger: {error}"))
}

#[cfg(not(unix))]
fn protect(_path: &Path, _mode: u32) -> Result<(), String> {
    Ok(())
}

#[cfg(not(unix))]
fn protect_file(_file: &File, _mode: u32) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
fn lock_single_writer(file: &File) -> Result<(), String> {
    use std::os::fd::AsRawFd;
    let result = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if result == 0 {
        Ok(())
    } else {
        Err("another logger process already owns this ledger".into())
    }
}

#[cfg(not(unix))]
fn lock_single_writer(_file: &File) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
fn sync_directory(path: &Path) -> Result<(), String> {
    File::open(path)
        .and_then(|file| file.sync_all())
        .map_err(|error| format!("sync logger projection directory: {error}"))
}

#[cfg(not(unix))]
fn sync_directory(_path: &Path) -> Result<(), String> {
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{model::ArtifactReference, signer::Ed25519Signer};

    fn event(id: &str, digest_byte: char) -> Event {
        Event {
            event_id: id.into(),
            session_id: "session-1".into(),
            event_type: "reviewer.verdict".into(),
            payload_digest: format!("sha256:{}", digest_byte.to_string().repeat(64)),
            artifact_references: vec![ArtifactReference {
                uri: "s3://audit/session-1.jsonl".into(),
                digest: None,
                size: Some(42),
                media_type: Some("application/jsonl".into()),
            }],
        }
    }

    #[test]
    fn file_ledger_is_idempotent_checkpointed_replayable_and_fail_closed() {
        let directory = tempfile::tempdir().unwrap();
        let ledger = directory.path().join("ledger.jsonl");
        {
            let signer = Ed25519Signer::from_seed([7; 32]);
            let mut store = Store::open(&ledger, signer, 1, false).unwrap();
            assert_eq!(
                store
                    .append(event("event-1", '1'), "producer-1".into())
                    .unwrap(),
                AppendResult::Appended
            );
            assert_eq!(
                store
                    .append(event("event-1", '1'), "producer-1".into())
                    .unwrap(),
                AppendResult::Duplicate
            );
            assert!(matches!(
                store.append(event("event-1", '2'), "producer-1".into()),
                Err(StoreError::Conflict(_))
            ));
            assert!(store.verify(None).unwrap().ok);
        }
        {
            let signer = Ed25519Signer::from_seed([7; 32]);
            let store = Store::open(&ledger, signer, 1, false).unwrap();
            assert_eq!(store.head("session-1").unwrap().unwrap().sequence, 1);
            assert_eq!(store.checkpoints("session-1", 0, 10).unwrap().len(), 1);
        }
        OpenOptions::new()
            .append(true)
            .open(&ledger)
            .unwrap()
            .write_all(b"{\"truncated\":true}")
            .unwrap();
        let signer = Ed25519Signer::from_seed([7; 32]);
        assert!(Store::open(&ledger, signer, 1, false).is_err());
    }
}
