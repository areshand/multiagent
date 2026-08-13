use std::env;
use std::path::PathBuf;
use std::process::{Command, ExitCode};

pub fn run(
    script: &str,
    args: &[String],
    environment: &[(&str, &str)],
) -> Result<ExitCode, String> {
    let root = env::var_os("MULTIAGENT_FRAMEWORK_ROOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    let path = root.join(script);
    if !path.is_file() {
        return Err(format!("adapter script not found: {}", path.display()));
    }
    let mut command = Command::new("bash");
    command.arg(&path).args(args);
    for (key, value) in environment {
        command.env(key, value);
    }
    let status = command
        .status()
        .map_err(|error| format!("run adapter {}: {error}", path.display()))?;
    Ok(ExitCode::from(
        status.code().unwrap_or(1).clamp(0, 255) as u8
    ))
}
