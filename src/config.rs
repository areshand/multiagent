use std::env;
use std::path::PathBuf;

pub fn root() -> Result<PathBuf, String> {
    match env::var_os("MULTIAGENT_ROOT") {
        Some(value) if !value.is_empty() => Ok(PathBuf::from(value)),
        _ => env::current_dir()
            .map_err(|error| format!("cannot determine current directory: {error}")),
    }
}

pub fn state_dir() -> Result<PathBuf, String> {
    match env::var_os("MULTIAGENT_STATE_DIR") {
        Some(value) if !value.is_empty() => Ok(PathBuf::from(value)),
        _ => Ok(root()?.join(".multiagent")),
    }
}
