use chrono::{SecondsFormat, Utc};
use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::Write;
use std::path::Path;

/// Publish state with one crash-safe filesystem primitive.
///
/// Domain modules still decide their schemas and locking boundaries; this
/// module owns the shared write mechanics so those guarantees do not drift.
pub fn atomic_write(path: &Path, text: &str) -> Result<(), String> {
    atomic_write_bytes(path, text.as_bytes())
}

pub fn atomic_write_bytes(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("state path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("create state directory {}: {error}", parent.display()))?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("state"),
        std::process::id()
    ));
    let mut file = File::create(&temporary)
        .map_err(|error| format!("create temporary state {}: {error}", temporary.display()))?;
    file.write_all(bytes)
        .map_err(|error| format!("write temporary state {}: {error}", temporary.display()))?;
    file.sync_all()
        .map_err(|error| format!("sync temporary state {}: {error}", temporary.display()))?;
    fs::rename(&temporary, path).map_err(|error| {
        format!(
            "publish state {} as {}: {error}",
            temporary.display(),
            path.display()
        )
    })
}

pub fn read_env(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("read state {}: {error}", path.display()))?;
    Ok(parse_env(&text))
}

pub fn read_env_optional(path: &Path) -> Result<BTreeMap<String, String>, String> {
    if path.is_file() {
        read_env(path)
    } else {
        Ok(BTreeMap::new())
    }
}

fn parse_env(text: &str) -> BTreeMap<String, String> {
    text.lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, value)| (key.to_string(), value.to_string()))
        .collect()
}

pub fn timestamp() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::{atomic_write, parse_env};
    use std::fs;

    #[test]
    fn atomic_write_replaces_complete_contents() {
        let root = std::env::temp_dir().join(format!(
            "multiagent-state-test-{}-{}",
            std::process::id(),
            std::thread::current().name().unwrap_or("unnamed")
        ));
        let path = root.join("state.env");
        atomic_write(&path, "phase=implementation\n").expect("initial state");
        atomic_write(&path, "phase=complete\n").expect("replacement state");
        assert_eq!(
            fs::read_to_string(path).expect("read state"),
            "phase=complete\n"
        );
        fs::remove_dir_all(root).expect("remove state test directory");
    }

    #[test]
    fn env_values_preserve_additional_equals_signs() {
        let values = parse_env("name=worker\ncommand=printf a=b\nignored\n");
        assert_eq!(values.get("name").map(String::as_str), Some("worker"));
        assert_eq!(
            values.get("command").map(String::as_str),
            Some("printf a=b")
        );
        assert!(!values.contains_key("ignored"));
    }
}
