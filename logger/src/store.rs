use crate::{
    canonical,
    model::{
        AcceptedEvent, Checkpoint, CheckpointBody, Entry, EntryBody, Event, LogHead, Verification,
    },
    signer::Ed25519Signer,
};
use chrono::{SecondsFormat, Utc};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use sha2::{Digest, Sha256};
use std::{collections::BTreeMap, fs, path::Path};

pub const GENESIS_HASH: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";

pub struct Store {
    connection: Connection,
    signer: Ed25519Signer,
    checkpoint_interval: u64,
    projection_enabled: bool,
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
impl From<rusqlite::Error> for StoreError {
    fn from(value: rusqlite::Error) -> Self {
        Self::Internal(value.to_string())
    }
}
impl From<String> for StoreError {
    fn from(value: String) -> Self {
        Self::Internal(value)
    }
}

impl Store {
    pub fn open(
        database: &Path,
        signer: Ed25519Signer,
        checkpoint_interval: u64,
        projection_enabled: bool,
    ) -> Result<Self, String> {
        if let Some(parent) = database.parent() {
            fs::create_dir_all(parent)
                .map_err(|error| format!("create logger data directory: {error}"))?;
        }
        let connection =
            Connection::open(database).map_err(|error| format!("open logger database: {error}"))?;
        connection.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA locking_mode=EXCLUSIVE; PRAGMA foreign_keys=ON; PRAGMA trusted_schema=OFF;
          CREATE TABLE IF NOT EXISTS logs(log_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, head_hash TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
          CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL, log_id TEXT NOT NULL, sequence INTEGER NOT NULL, previous_hash TEXT NOT NULL, entry_hash TEXT NOT NULL UNIQUE, committed_at TEXT NOT NULL, producer_id TEXT NOT NULL, event_type TEXT NOT NULL, payload_digest TEXT NOT NULL, event_json TEXT NOT NULL, entry_json TEXT NOT NULL, UNIQUE(log_id,sequence), FOREIGN KEY(log_id) REFERENCES logs(log_id)) STRICT;
          CREATE TABLE IF NOT EXISTS checkpoints(checkpoint_id TEXT PRIMARY KEY, log_id TEXT NOT NULL, sequence INTEGER NOT NULL, entry_hash TEXT NOT NULL, created_at TEXT NOT NULL, checkpoint_json TEXT NOT NULL, UNIQUE(log_id,sequence), FOREIGN KEY(log_id) REFERENCES logs(log_id)) STRICT;
          CREATE TABLE IF NOT EXISTS projection_queue(event_id TEXT PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, last_error TEXT, FOREIGN KEY(event_id) REFERENCES events(event_id)) STRICT;
          CREATE INDEX IF NOT EXISTS events_log_sequence ON events(log_id,sequence); CREATE INDEX IF NOT EXISTS projections_pending ON projection_queue(status,next_attempt_at);")
            .map_err(|error| format!("initialize logger database: {error}"))?;
        let journal_mode: String = connection
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .map_err(|error| format!("read logger journal mode: {error}"))?;
        if !journal_mode.eq_ignore_ascii_case("wal") {
            return Err("logger ledger requires SQLite WAL mode".into());
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            for suffix in ["", "-wal", "-shm"] {
                let file = format!("{}{}", database.display(), suffix);
                if Path::new(&file).exists() {
                    fs::set_permissions(&file, fs::Permissions::from_mode(0o600))
                        .map_err(|error| format!("protect logger database: {error}"))?;
                }
            }
        }
        let store = Self {
            connection,
            signer,
            checkpoint_interval,
            projection_enabled,
        };
        let result = store.verify(None)?;
        if !result.ok {
            return Err(format!(
                "logger integrity check failed: {}",
                result.errors.join("; ")
            ));
        }
        Ok(store)
    }

    pub fn append(
        &mut self,
        event: Event,
        producer_id: String,
    ) -> Result<AppendResult, StoreError> {
        let accepted = AcceptedEvent {
            event: event.clone(),
            producer_id: producer_id.clone(),
        };
        let request_digest = canonical::sha256(&accepted)?;
        let signer = self.signer.clone();
        let checkpoint_interval = self.checkpoint_interval;
        let projection_enabled = self.projection_enabled;
        let transaction = self
            .connection
            .transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing: Option<String> = transaction
            .query_row(
                "SELECT request_digest FROM events WHERE event_id=?1",
                [&event.event_id],
                |row| row.get(0),
            )
            .optional()?;
        if let Some(existing) = existing {
            if existing == request_digest {
                transaction.commit()?;
                return Ok(AppendResult::Duplicate);
            }
            return Err(StoreError::Conflict(
                "eventId already commits different content".into(),
            ));
        }
        let head: Option<(u64, String)> = transaction
            .query_row(
                "SELECT sequence,head_hash FROM logs WHERE log_id=?1",
                [&event.session_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        let sequence = head.as_ref().map_or(1, |(sequence, _)| sequence + 1);
        let previous_hash = head.map_or_else(|| GENESIS_HASH.into(), |(_, hash)| hash);
        let committed_at = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
        let body = EntryBody {
            log_id: event.session_id.clone(),
            sequence,
            previous_hash: previous_hash.clone(),
            committed_at: committed_at.clone(),
            producer_id,
            event_id: event.event_id.clone(),
            event_type: event.event_type.clone(),
            payload_digest: event.payload_digest.clone(),
            artifact_references: event.artifact_references.clone(),
        };
        let entry_hash = canonical::sha256(&body)?;
        let entry = Entry {
            body,
            entry_hash: entry_hash.clone(),
        };
        transaction.execute("INSERT INTO logs(log_id,sequence,head_hash,updated_at) VALUES(?1,?2,?3,?4) ON CONFLICT(log_id) DO UPDATE SET sequence=excluded.sequence,head_hash=excluded.head_hash,updated_at=excluded.updated_at", params![event.session_id,sequence,entry_hash,committed_at])?;
        transaction.execute("INSERT INTO events(event_id,request_digest,log_id,sequence,previous_hash,entry_hash,committed_at,producer_id,event_type,payload_digest,event_json,entry_json) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)", params![event.event_id,request_digest,event.session_id,sequence,previous_hash,entry_hash,committed_at,accepted.producer_id,event.event_type,event.payload_digest,canonical::string(&accepted)?,canonical::string(&entry)?])?;
        if sequence % checkpoint_interval == 0 {
            create_checkpoint(
                &transaction,
                &signer,
                &event.session_id,
                sequence,
                &entry_hash,
                &committed_at,
            )?;
        }
        if projection_enabled {
            transaction.execute("INSERT INTO projection_queue(event_id,status,next_attempt_at) VALUES(?1,'pending',?2)", params![event.event_id,committed_at])?;
        }
        transaction.commit()?;
        Ok(AppendResult::Appended)
    }

    pub fn head(&self, log_id: &str) -> Result<Option<LogHead>, String> {
        self.connection
            .query_row(
                "SELECT log_id,sequence,head_hash,updated_at FROM logs WHERE log_id=?1",
                [log_id],
                |row| {
                    Ok(LogHead {
                        log_id: row.get(0)?,
                        sequence: row.get(1)?,
                        entry_hash: row.get(2)?,
                        updated_at: row.get(3)?,
                    })
                },
            )
            .optional()
            .map_err(|e| e.to_string())
    }
    pub fn entries(&self, log_id: &str, after: u64, limit: usize) -> Result<Vec<Entry>, String> {
        json_rows(&self.connection, "SELECT entry_json FROM events WHERE log_id=?1 AND sequence>?2 ORDER BY sequence LIMIT ?3", params![log_id,after,limit])
    }
    pub fn checkpoints(
        &self,
        log_id: &str,
        after: u64,
        limit: usize,
    ) -> Result<Vec<Checkpoint>, String> {
        json_rows(&self.connection, "SELECT checkpoint_json FROM checkpoints WHERE log_id=?1 AND sequence>?2 ORDER BY sequence LIMIT ?3", params![log_id,after,limit])
    }
    pub fn checkpoint(&self, id: &str) -> Result<Option<Checkpoint>, String> {
        self.connection
            .query_row(
                "SELECT checkpoint_json FROM checkpoints WHERE checkpoint_id=?1",
                [id],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|e| e.to_string())?
            .map(|raw| serde_json::from_str(&raw).map_err(|e| e.to_string()))
            .transpose()
    }
    pub fn checkpoint_session(&self, id: &str) -> Result<Option<String>, String> {
        self.connection
            .query_row(
                "SELECT log_id FROM checkpoints WHERE checkpoint_id=?1",
                [id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| e.to_string())
    }

    pub fn verify(&self, log_id: Option<&str>) -> Result<Verification, String> {
        let mut logs = self
            .connection
            .prepare(if log_id.is_some() {
                "SELECT log_id,sequence,head_hash FROM logs WHERE log_id=?1"
            } else {
                "SELECT log_id,sequence,head_hash FROM logs ORDER BY log_id"
            })
            .map_err(|e| e.to_string())?;
        let values = if let Some(id) = log_id {
            let rows = logs
                .query_map([id], read_log_row)
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?
        } else {
            let rows = logs
                .query_map([], read_log_row)
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?
        };
        let mut errors = Vec::new();
        let mut checked_entries = 0;
        for (id, head_sequence, head_hash) in &values {
            let rows: Vec<(u64, String, String, String, String)> = {
                let mut statement=self.connection.prepare("SELECT sequence,previous_hash,entry_hash,event_json,entry_json FROM events WHERE log_id=?1 ORDER BY sequence").map_err(|e| e.to_string())?;
                let mapped = statement
                    .query_map([id], |r| {
                        Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
                    })
                    .map_err(|e| e.to_string())?;
                mapped
                    .collect::<Result<_, _>>()
                    .map_err(|e| e.to_string())?
            };
            let mut previous = GENESIS_HASH.to_string();
            let mut expected = 1;
            for (sequence, row_previous, row_hash, accepted_json, entry_json) in &rows {
                checked_entries += 1;
                let entry: Entry = serde_json::from_str(entry_json).map_err(|e| e.to_string())?;
                let accepted: AcceptedEvent =
                    serde_json::from_str(accepted_json).map_err(|e| e.to_string())?;
                if *sequence != expected {
                    errors.push(format!("{id}: expected sequence {expected}"));
                }
                if row_previous != &previous || entry.body.previous_hash != previous {
                    errors.push(format!("{id}:{sequence}: previous hash mismatch"));
                }
                let calculated = canonical::sha256(&entry.body)?;
                if calculated != *row_hash || calculated != entry.entry_hash {
                    errors.push(format!("{id}:{sequence}: entry hash mismatch"));
                }
                if canonical::sha256(&accepted)?
                    != request_digest(&self.connection, &accepted.event.event_id)?
                {
                    errors.push(format!("{id}:{sequence}: accepted event digest mismatch"));
                }
                if accepted.event.session_id != entry.body.log_id
                    || accepted.event.event_id != entry.body.event_id
                    || accepted.producer_id != entry.body.producer_id
                    || accepted.event.event_type != entry.body.event_type
                    || accepted.event.payload_digest != entry.body.payload_digest
                    || accepted.event.artifact_references != entry.body.artifact_references
                {
                    errors.push(format!(
                        "{id}:{sequence}: accepted event does not match chain entry"
                    ));
                }
                previous = calculated;
                expected += 1;
            }
            if *head_sequence != rows.len() as u64 || *head_hash != previous {
                errors.push(format!("{id}: authoritative head mismatch"));
            }
        }
        let mut statement = self
            .connection
            .prepare(if log_id.is_some() {
                "SELECT checkpoint_json FROM checkpoints WHERE log_id=?1 ORDER BY sequence"
            } else {
                "SELECT checkpoint_json FROM checkpoints ORDER BY log_id,sequence"
            })
            .map_err(|e| e.to_string())?;
        let checkpoint_jsons: Vec<String> = if let Some(id) = log_id {
            let rows = statement
                .query_map([id], read_string)
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        } else {
            let rows = statement
                .query_map([], read_string)
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
        for raw in &checkpoint_jsons {
            let checkpoint: Checkpoint = serde_json::from_str(raw).map_err(|e| e.to_string())?;
            if !self
                .signer
                .verify(&checkpoint.body, &checkpoint.logger_signature)
            {
                errors.push(format!(
                    "{}: invalid checkpoint signature",
                    checkpoint.body.checkpoint_id
                ));
            }
            let found: Option<String> = self
                .connection
                .query_row(
                    "SELECT entry_hash FROM events WHERE log_id=?1 AND sequence=?2",
                    params![checkpoint.body.log_id, checkpoint.body.sequence],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| e.to_string())?;
            if found.as_deref() != Some(&checkpoint.body.entry_hash) {
                errors.push(format!(
                    "{}: checkpoint entry mismatch",
                    checkpoint.body.checkpoint_id
                ));
            }
        }
        Ok(Verification {
            ok: errors.is_empty(),
            checked_logs: values.len(),
            checked_entries,
            checked_checkpoints: checkpoint_jsons.len(),
            errors,
        })
    }

    pub fn projection_counts(&self) -> Result<BTreeMap<String, u64>, String> {
        let mut s = self
            .connection
            .prepare("SELECT status,COUNT(*) FROM projection_queue GROUP BY status")
            .map_err(|e| e.to_string())?;
        let rows = s
            .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))
            .map_err(|e| e.to_string())?;
        rows.collect::<Result<_, _>>().map_err(|e| e.to_string())
    }
    pub fn flush_projections(&mut self, directory: &Path) -> Result<(u64, u64), String> {
        fs::create_dir_all(directory).map_err(|e| e.to_string())?;
        let pending: Vec<(String, u64, Entry)> = {
            let mut s=self.connection.prepare("SELECT q.event_id,q.attempts,e.entry_json FROM projection_queue q JOIN events e ON e.event_id=q.event_id WHERE q.status='pending' AND q.next_attempt_at<=?1 ORDER BY e.committed_at LIMIT 100").map_err(|e|e.to_string())?;
            let now = Utc::now().to_rfc3339_opts(SecondsFormat::Millis, true);
            let rows = s
                .query_map([now], |r| {
                    let raw: String = r.get(2)?;
                    let entry = serde_json::from_str(&raw).map_err(|e| {
                        rusqlite::Error::FromSqlConversionFailure(
                            raw.len(),
                            rusqlite::types::Type::Text,
                            Box::new(e),
                        )
                    })?;
                    Ok((r.get(0)?, r.get(1)?, entry))
                })
                .map_err(|e| e.to_string())?;
            rows.collect::<Result<_, _>>().map_err(|e| e.to_string())?
        };
        let (mut ok, mut failed) = (0, 0);
        for (event_id, attempts, entry) in pending {
            use std::io::Write;
            let result = (|| -> Result<(), String> {
                let file = directory.join(format!("{}.jsonl", entry.body.log_id));
                let mut output = fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(file)
                    .map_err(|e| e.to_string())?;
                writeln!(output, "{}", canonical::string(&entry)?).map_err(|e| e.to_string())
            })();
            match result {
                Ok(()) => {
                    self.connection.execute("UPDATE projection_queue SET status='complete',last_error=NULL WHERE event_id=?1",[event_id]).map_err(|e|e.to_string())?;
                    ok += 1
                }
                Err(error) => {
                    let delay = 1000u64
                        .saturating_mul(2u64.pow((attempts + 1).min(6) as u32))
                        .min(60_000);
                    let next = (Utc::now() + chrono::Duration::milliseconds(delay as i64))
                        .to_rfc3339_opts(SecondsFormat::Millis, true);
                    self.connection.execute("UPDATE projection_queue SET attempts=?1,next_attempt_at=?2,last_error=?3 WHERE event_id=?4",params![attempts+1,next,error.chars().take(1024).collect::<String>(),event_id]).map_err(|e|e.to_string())?;
                    failed += 1
                }
            }
        }
        Ok((ok, failed))
    }
}

fn create_checkpoint(
    tx: &rusqlite::Transaction<'_>,
    signer: &Ed25519Signer,
    log_id: &str,
    sequence: u64,
    entry_hash: &str,
    created_at: &str,
) -> Result<(), StoreError> {
    let digest = format!(
        "{:x}",
        Sha256::digest(format!("{log_id}:{sequence}:{entry_hash}"))
    );
    let body = CheckpointBody {
        api_version: "logger.multiagent.dev/v1".into(),
        kind: "LoggerCheckpoint".into(),
        checkpoint_id: format!("checkpoint-{}", &digest[..48]),
        log_id: log_id.into(),
        sequence,
        entry_hash: entry_hash.into(),
        created_at: created_at.into(),
        logger_identity: signer.logger_id.clone(),
    };
    let checkpoint = Checkpoint {
        logger_signature: signer.sign(&body)?,
        body,
    };
    tx.execute("INSERT INTO checkpoints(checkpoint_id,log_id,sequence,entry_hash,created_at,checkpoint_json) VALUES(?1,?2,?3,?4,?5,?6)",params![checkpoint.body.checkpoint_id,log_id,sequence,entry_hash,created_at,canonical::string(&checkpoint)?])?;
    Ok(())
}
fn json_rows<T: serde::de::DeserializeOwned>(
    connection: &Connection,
    sql: &str,
    params: impl rusqlite::Params,
) -> Result<Vec<T>, String> {
    let mut statement = connection.prepare(sql).map_err(|e| e.to_string())?;
    let rows = statement
        .query_map(params, |row| {
            let raw: String = row.get(0)?;
            serde_json::from_str(&raw).map_err(|e| {
                rusqlite::Error::FromSqlConversionFailure(
                    raw.len(),
                    rusqlite::types::Type::Text,
                    Box::new(e),
                )
            })
        })
        .map_err(|e| e.to_string())?;
    rows.collect::<Result<_, _>>().map_err(|e| e.to_string())
}
fn read_log_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<(String, u64, String)> {
    Ok((row.get(0)?, row.get(1)?, row.get(2)?))
}
fn read_string(row: &rusqlite::Row<'_>) -> rusqlite::Result<String> {
    row.get(0)
}
fn request_digest(connection: &Connection, event_id: &str) -> Result<String, String> {
    connection
        .query_row(
            "SELECT request_digest FROM events WHERE event_id=?1",
            [event_id],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())
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
    fn chain_is_idempotent_checkpointed_and_fail_closed_on_tampering() {
        let directory = tempfile::tempdir().unwrap();
        let database = directory.path().join("ledger.sqlite");
        let signer = Ed25519Signer::from_seed([7; 32]);
        let mut store = Store::open(&database, signer, 1, false).unwrap();
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
        assert_eq!(store.head("session-1").unwrap().unwrap().sequence, 1);
        assert_eq!(store.checkpoints("session-1", 0, 10).unwrap().len(), 1);
        assert!(store.verify(None).unwrap().ok);

        store.connection.execute("UPDATE events SET entry_hash='sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE event_id='event-1'", []).unwrap();
        assert!(!store.verify(None).unwrap().ok);
    }
}
