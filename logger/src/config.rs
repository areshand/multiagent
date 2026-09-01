use std::{env, path::PathBuf};

#[derive(Clone, Debug)]
pub struct Config {
    pub database: PathBuf,
    pub signing_key_file: PathBuf,
    pub signing_key_id: String,
    pub logger_id: String,
    pub clients_file: PathBuf,
    pub checkpoint_interval: u64,
    pub max_event_bytes: usize,
    pub projection_dir: Option<PathBuf>,
    pub projection_interval_ms: u64,
    pub host: String,
    pub port: u16,
}

impl Config {
    pub fn from_env() -> Result<Self, String> {
        Ok(Self {
            database: path("LOGGER_DATABASE", "/var/lib/logger/ledger.sqlite"),
            signing_key_file: required_path("LOGGER_SIGNING_KEY_FILE")?,
            signing_key_id: env::var("LOGGER_SIGNING_KEY_ID")
                .unwrap_or_else(|_| "logger-signing-key".into()),
            logger_id: required("LOGGER_ID")?,
            clients_file: required_path("LOGGER_CLIENTS_FILE")?,
            checkpoint_interval: integer("LOGGER_CHECKPOINT_INTERVAL", 100, 1, 1_000_000)?,
            max_event_bytes: integer("LOGGER_MAX_EVENT_BYTES", 65_536, 1_024, 1_048_576)? as usize,
            projection_dir: env::var_os("LOGGER_PROJECTION_DIR").map(PathBuf::from),
            projection_interval_ms: integer("LOGGER_PROJECTION_INTERVAL_MS", 1_000, 100, 60_000)?,
            host: env::var("HOST").unwrap_or_else(|_| "0.0.0.0".into()),
            port: integer("PORT", 8090, 1, 65_535)? as u16,
        })
    }
}

fn required(name: &str) -> Result<String, String> {
    env::var(name)
        .map_err(|_| format!("{name} is required"))
        .and_then(|value| {
            if value.is_empty() {
                Err(format!("{name} is required"))
            } else {
                Ok(value)
            }
        })
}

fn required_path(name: &str) -> Result<PathBuf, String> {
    required(name).map(PathBuf::from)
}

fn path(name: &str, fallback: &str) -> PathBuf {
    env::var_os(name)
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(fallback))
}

fn integer(name: &str, fallback: u64, min: u64, max: u64) -> Result<u64, String> {
    let value = env::var(name).ok().map_or(Ok(fallback), |raw| {
        raw.parse::<u64>()
            .map_err(|_| format!("{name} must be an integer between {min} and {max}"))
    })?;
    if !(min..=max).contains(&value) {
        return Err(format!("{name} must be an integer between {min} and {max}"));
    }
    Ok(value)
}
