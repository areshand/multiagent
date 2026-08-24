use std::env;
use std::fs;
use std::io::{Read, Write};
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};

use crate::config::{CONTROL_UID, ORCHESTRATOR_UID, ROLE_GID};
use crate::linux_privilege::{self, IdentitySpec};

const STATE_ROOT: &str = "/var/lib/multiagent/state";
const TMUX_BIN: &str = "/usr/bin/tmux";
const MAX_INPUT_BYTES: u64 = 32_768;

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    #[cfg(not(target_os = "linux"))]
    return Err("session-control requires Linux".into());

    #[cfg(target_os = "linux")]
    run_linux(args)
}

#[cfg(target_os = "linux")]
fn run_linux(args: &[String]) -> Result<ExitCode, String> {
    if unsafe { libc::getuid() } != CONTROL_UID || unsafe { libc::geteuid() } != 0 {
        return Err(
            "session-control requires the trusted control UID through the setuid launcher".into(),
        );
    }
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() != Ok("1") {
        return Err("session-control requires MULTIAGENT_UID_SANDBOX=1".into());
    }
    let session = args.first().ok_or_else(usage)?;
    let action = args.get(1).ok_or_else(usage)?;
    validate_session_id(session)?;
    validate_tmux_binary()?;

    let state_root = canonical_state_root()?;
    let socket = state_root
        .join("sessions")
        .join(session)
        .join("runtime_state/tmux.sock");
    if action == "status" && !socket.exists() {
        return Ok(ExitCode::from(1));
    }
    validate_socket(&socket)?;

    let capture_lines = match action.as_str() {
        "status" | "submit" | "stop" if args.len() == 2 => None,
        "capture" if args.len() == 3 => Some(parse_capture_lines(&args[2])?),
        _ => return Err(usage()),
    };
    let input = if action == "submit" {
        let mut input = String::new();
        io::stdin()
            .take(MAX_INPUT_BYTES + 1)
            .read_to_string(&mut input)
            .map_err(|error| format!("read submitted session input: {error}"))?;
        if input.is_empty() || input.len() as u64 > MAX_INPUT_BYTES {
            return Err("submitted session input must contain 1 to 32768 bytes".into());
        }
        Some(input)
    } else {
        None
    };

    drop_to_orchestrator()?;
    let orchestrator_target = format!("{session}:orchestrator");
    match action.as_str() {
        "status" => run_tmux(&socket, &["has-session", "-t", &orchestrator_target]),
        "capture" => run_tmux(
            &socket,
            &[
                "capture-pane",
                "-p",
                "-J",
                "-S",
                &format!("-{}", capture_lines.expect("validated capture lines")),
                "-t",
                &orchestrator_target,
            ],
        ),
        "submit" => submit(&socket, session, input.as_deref().expect("validated input")),
        "stop" => run_tmux(&socket, &["kill-session", "-t", session]),
        _ => unreachable!(),
    }
}

fn usage() -> String {
    "usage: multiagent session-control SESSION status|capture LINES|submit|stop".into()
}

fn validate_session_id(session: &str) -> Result<(), String> {
    let valid = !session.is_empty()
        && session.len() <= 63
        && (session.as_bytes()[0].is_ascii_lowercase()
            || session.as_bytes()[0].is_ascii_digit())
        && session
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'-');
    if valid {
        Ok(())
    } else {
        Err("invalid session id".into())
    }
}

fn canonical_state_root() -> Result<PathBuf, String> {
    let configured = env::var("MULTIAGENT_STATE_DIR")
        .map_err(|_| "session-control requires MULTIAGENT_STATE_DIR".to_string())?;
    let canonical = fs::canonicalize(&configured)
        .map_err(|error| format!("resolve session-control state root: {error}"))?;
    if canonical != Path::new(STATE_ROOT) {
        return Err(format!("session-control state root must be {STATE_ROOT}"));
    }
    Ok(canonical)
}

fn validate_tmux_binary() -> Result<(), String> {
    let metadata =
        fs::metadata(TMUX_BIN).map_err(|error| format!("inspect trusted tmux binary: {error}"))?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.permissions().mode() & 0o022 != 0 {
        return Err("trusted tmux binary must be root-owned and non-writable".into());
    }
    Ok(())
}

fn validate_socket(socket: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(socket)
        .map_err(|error| format!("inspect orchestrator tmux socket: {error}"))?;
    if !metadata.file_type().is_socket()
        || metadata.uid() != ORCHESTRATOR_UID
        || metadata.gid() != ROLE_GID
        || metadata.permissions().mode() & 0o002 != 0
    {
        return Err("orchestrator tmux socket has invalid ownership or permissions".into());
    }
    Ok(())
}

fn parse_capture_lines(value: &str) -> Result<usize, String> {
    let lines = value
        .parse::<usize>()
        .map_err(|_| "capture lines must be an integer".to_string())?;
    if (1..=5000).contains(&lines) {
        Ok(lines)
    } else {
        Err("capture lines must be between 1 and 5000".into())
    }
}

#[cfg(target_os = "linux")]
fn drop_to_orchestrator() -> Result<(), String> {
    linux_privilege::apply_identity(&IdentitySpec::new(ORCHESTRATOR_UID, ROLE_GID))
        .map_err(|error| format!("drop to orchestrator identity: {error}"))
}

fn run_tmux(socket: &Path, args: &[&str]) -> Result<ExitCode, String> {
    let status = Command::new(TMUX_BIN)
        .arg("-S")
        .arg(socket)
        .args(args)
        .status()
        .map_err(|error| format!("run trusted tmux operation: {error}"))?;
    Ok(status_code(status.code()))
}

fn submit(socket: &Path, session: &str, input: &str) -> Result<ExitCode, String> {
    let mut child = Command::new(TMUX_BIN)
        .arg("-S")
        .arg(socket)
        .args(["load-buffer", "-"])
        .stdin(Stdio::piped())
        .spawn()
        .map_err(|error| format!("start trusted tmux input load: {error}"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| "trusted tmux input pipe is unavailable".to_string())?
        .write_all(input.as_bytes())
        .map_err(|error| format!("write trusted tmux input: {error}"))?;
    let loaded = child
        .wait()
        .map_err(|error| format!("wait for trusted tmux input load: {error}"))?;
    if !loaded.success() {
        return Ok(status_code(loaded.code()));
    }
    let target = format!("{session}:orchestrator");
    let pasted = Command::new(TMUX_BIN)
        .arg("-S")
        .arg(socket)
        .args(["paste-buffer", "-d", "-t", &target])
        .status()
        .map_err(|error| format!("paste trusted tmux input: {error}"))?;
    if !pasted.success() {
        return Ok(status_code(pasted.code()));
    }
    run_tmux(socket, &["send-keys", "-t", &target, "Enter"])
}

fn status_code(code: Option<i32>) -> ExitCode {
    match code {
        Some(code) if (0..=255).contains(&code) => ExitCode::from(code as u8),
        _ => ExitCode::from(1),
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_capture_lines, validate_session_id};

    #[test]
    fn session_ids_match_the_shared_control_plane_contract() {
        let vectors: serde_json::Value = serde_json::from_str(include_str!(
            "../contracts/session-id-vectors.json"
        ))
        .unwrap();
        for value in vectors["valid"].as_array().unwrap() {
            assert!(validate_session_id(value.as_str().unwrap()).is_ok());
        }
        for value in vectors["invalid"].as_array().unwrap() {
            assert!(validate_session_id(value.as_str().unwrap()).is_err());
        }
    }

    #[test]
    fn capture_bounds_are_strict() {
        assert_eq!(parse_capture_lines("120").unwrap(), 120);
        assert_eq!(parse_capture_lines("1").unwrap(), 1);
        assert_eq!(parse_capture_lines("5000").unwrap(), 5000);
        assert!(parse_capture_lines("0").is_err());
        assert!(parse_capture_lines("5001").is_err());
        assert!(parse_capture_lines("not-a-number").is_err());
    }
}
