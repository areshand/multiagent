use crate::{authority::AuthorityRequest, config, state::read_env as read_env_file};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::env;
use std::fs;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
#[cfg(any(target_os = "linux", target_os = "macos"))]
use std::process::{Command, Stdio};
#[cfg(target_os = "linux")]
use std::thread;
#[cfg(target_os = "linux")]
use std::time::{Duration, Instant};

#[cfg(any(target_os = "linux", target_os = "macos"))]
use std::os::unix::net::UnixListener;
#[cfg(unix)]
use std::os::unix::net::UnixStream;

const SERVER_CHILD_ENV: &str = "MULTIAGENT_AUTHORITY_SERVER_CHILD";
#[cfg(target_os = "linux")]
const AUTHORITY_REGISTRY: &str = "/run/multiagent/authority-state-10001";
#[cfg(target_os = "linux")]
const CONTROL_DIRECTORIES: &[&str] = &[
    "assignments",
    "contract-evidence",
    "decisions",
    "findings",
    "launch-authorizations",
    "prod-ops",
    "reviewer-evidence",
    "role-io",
    "todos",
    "validation-leases",
    "workflows",
];

#[derive(Deserialize, Serialize)]
struct Response {
    code: i32,
    stdout: String,
    stderr: String,
}

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    match args {
        [command] if command == "bootstrap-test" => bootstrap_test(),
        [command] if command == "serve" => serve(&authority_socket(&config::state_dir()?)),
        [command] if command == "stop" => proxy_request(AuthorityRequest::shutdown()),
        [command, rest @ ..] if command == "register-launch" && server_child() => {
            register_launch(rest, false)?;
            Ok(ExitCode::SUCCESS)
        }
        [command, rest @ ..] if command == "renew-launch" && server_child() => {
            register_launch(rest, true)?;
            Ok(ExitCode::SUCCESS)
        }
        [command] if command == "shutdown" && server_child() => Ok(ExitCode::SUCCESS),
        _ => Err("usage: multiagent supervisor stop".into()),
    }
}

fn bootstrap_test() -> Result<ExitCode, String> {
    if env::var("MULTIAGENT_TEST_MODE").as_deref() != Ok("1") {
        return Err("supervisor bootstrap-test requires MULTIAGENT_TEST_MODE=1".into());
    }
    #[cfg(unix)]
    if unsafe { libc::getuid() } != 0 || unsafe { libc::geteuid() } != 0 {
        return Err("supervisor bootstrap-test requires real root".into());
    }
    let state = config::state_dir()?;
    fs::create_dir_all(&state).map_err(|error| format!("create test authority state: {error}"))?;
    register_runtime_state(&state)?;
    prepare_state_permissions(&state)?;
    let executable = env::current_exe()
        .map_err(|error| format!("resolve test supervisor executable: {error}"))?;
    let pid = start(&state, &executable)?;
    println!("{pid}");
    Ok(ExitCode::SUCCESS)
}

pub fn proxy_if_required(command: &str, args: &[String]) -> Option<Result<ExitCode, String>> {
    if command == "supervisor" && args.first().map(String::as_str) == Some("stop") {
        return None;
    }
    if !uid_sandbox() || !authority_client_uid() || server_child() {
        return None;
    }
    AuthorityRequest::from_cli(command, args).map(proxy_request)
}

#[derive(Clone, Debug)]
pub struct LaunchAuthorization {
    pub role: String,
    pub access: String,
    pub workflow_id: String,
    pub cli: String,
    pub cli_bin: String,
    pub instruction: PathBuf,
    pub owned_paths: Vec<PathBuf>,
    /// Optional explicit model override, currently used only by the
    /// production-operations safety-reviewer/operations-reviewer roles.
    pub model: Option<String>,
}

fn register_launch(args: &[String], renew: bool) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| valid_name(value))
        .ok_or_else(|| "register-launch requires a valid NAME".to_string())?;
    let options = parse_options(&args[1..])?;
    let role = required_option(&options, "--role")?;
    let cli = required_option(&options, "--cli")?;
    let cli_bin = required_option(&options, "--cli-bin")?;
    let model = options.get("--model").filter(|value| !value.is_empty());
    let instruction_source = PathBuf::from(required_option(&options, "--instruction-file")?);
    if !matches!(role, "worker" | "verifier" | "reviewer" | "scout") {
        return Err("register-launch role must be worker, verifier, reviewer, or scout".into());
    }
    if !matches!(cli, "codex" | "claude" | "qwen") {
        return Err("register-launch backend must be codex, claude, or qwen".into());
    }
    let expected_binary = env::var(match cli {
        "codex" => "CODEX_BIN",
        "claude" => "CLAUDE_BIN",
        "qwen" => "QWEN_BIN",
        _ => unreachable!(),
    })
    .map_err(|_| format!("authority supervisor has no configured {cli} binary"))?;
    if cli_bin != expected_binary {
        return Err("register-launch binary does not match the launch manifest".into());
    }
    let state = config::state_dir()?;
    let expected_instruction = state.join("subagents").join(name).join(if renew {
        "restore-instruction.txt"
    } else {
        "instruction.txt"
    });
    if fs::canonicalize(&instruction_source).ok() != fs::canonicalize(&expected_instruction).ok()
        || !instruction_source.is_file()
    {
        return Err(format!(
            "register-launch instruction must be the persisted subagent instruction: {}",
            expected_instruction.display()
        ));
    }
    let access = if role == "worker" {
        "workspace-write"
    } else {
        "read-only"
    };
    let workflow_id = env::var("MULTIAGENT_WORKFLOW_ID").unwrap_or_default();
    let assignment = state.join("assignments").join(name);
    let owned_paths = if access == "workspace-write" {
        if !assignment.join("assignment.env").is_file() {
            return Err(format!(
                "workspace writer requires a supervisor-owned assignment: {name}"
            ));
        }
        let status = fs::read_to_string(assignment.join("status")).unwrap_or_default();
        if matches!(status.trim(), "done" | "failed" | "released" | "cancelled") {
            return Err(format!("assignment is not active: {name}"));
        }
        read_owned_paths(&state, name)?
    } else {
        Vec::new()
    };
    let directory = state.join("launch-authorizations").join(name);
    if directory.exists() {
        if !renew {
            return Err(format!("launch authorization already exists: {name}"));
        }
        let current = read_env_file(&directory.join("launch.env"))?;
        if current.get("state").map(String::as_str) != Some("completed") {
            return Err(format!("launch authorization is not renewable: {name}"));
        }
        if current.get("role").map(String::as_str) != Some(role)
            || current.get("cli").map(String::as_str) != Some(cli)
            || current.get("cli_bin").map(String::as_str) != Some(cli_bin)
            || current.get("model").map(String::as_str).unwrap_or("")
                != model.map(String::as_str).unwrap_or("")
        {
            return Err(format!(
                "renewed launch cannot change role or coding-agent identity: {name}"
            ));
        }
    } else if renew {
        return Err(format!("launch authorization does not exist: {name}"));
    }
    fs::create_dir_all(&directory)
        .map_err(|error| format!("create launch authorization: {error}"))?;
    let instruction = fs::read(&instruction_source)
        .map_err(|error| format!("read registered instruction: {error}"))?;
    let instruction_path = directory.join("instruction.txt");
    atomic_write_bytes(&instruction_path, &instruction)?;
    let metadata = format!(
        "name={name}\nrole={role}\naccess={access}\nworkflow_id={workflow_id}\ncli={cli}\ncli_bin={cli_bin}\nmodel={}\ninstruction_sha256={:x}\nstate=registered\n",
        model.map(String::as_str).unwrap_or(""),
        Sha256::digest(&instruction)
    );
    atomic_write_bytes(&directory.join("launch.env"), metadata.as_bytes())?;
    if !owned_paths.is_empty() {
        let text = owned_paths
            .iter()
            .map(|path| format!("{}\n", path.display()))
            .collect::<String>();
        atomic_write_bytes(&directory.join("owned-paths"), text.as_bytes())?;
    }
    println!("launch authorized\t{name}\t{role}\t{access}");
    Ok(())
}

pub fn claim_launch(state: &Path, name: &str) -> Result<LaunchAuthorization, String> {
    let directory = state.join("launch-authorizations").join(name);
    let metadata = read_env_file(&directory.join("launch.env"))?;
    if metadata.get("name").map(String::as_str) != Some(name)
        || metadata.get("state").map(String::as_str) != Some("registered")
    {
        return Err(format!(
            "launch authorization is missing or already consumed: {name}"
        ));
    }
    let instruction = directory.join("instruction.txt");
    let bytes =
        fs::read(&instruction).map_err(|error| format!("read authorized instruction: {error}"))?;
    let actual = format!("{:x}", Sha256::digest(&bytes));
    if metadata.get("instruction_sha256") != Some(&actual) {
        return Err(format!("authorized instruction hash changed: {name}"));
    }
    let owned_paths = read_owned_paths(state, name)?;
    let authorization = LaunchAuthorization {
        role: required_field(&metadata, "role")?.into(),
        access: required_field(&metadata, "access")?.into(),
        workflow_id: metadata.get("workflow_id").cloned().unwrap_or_default(),
        cli: required_field(&metadata, "cli")?.into(),
        cli_bin: required_field(&metadata, "cli_bin")?.into(),
        instruction,
        owned_paths,
        model: metadata
            .get("model")
            .filter(|value| !value.is_empty())
            .cloned(),
    };
    write_launch_state(&directory, &metadata, "running")?;
    Ok(authorization)
}

pub fn launch_requires_writer(state: &Path, name: &str) -> Result<bool, String> {
    let metadata = read_env_file(
        &state
            .join("launch-authorizations")
            .join(name)
            .join("launch.env"),
    )?;
    if metadata.get("name").map(String::as_str) != Some(name)
        || metadata.get("state").map(String::as_str) != Some("registered")
    {
        return Err(format!(
            "launch authorization is missing or already consumed: {name}"
        ));
    }
    Ok(metadata.get("role").map(String::as_str) == Some("worker")
        && metadata.get("access").map(String::as_str) == Some("workspace-write"))
}

pub fn finish_launch(state: &Path, name: &str) -> Result<(), String> {
    let directory = state.join("launch-authorizations").join(name);
    let metadata = read_env_file(&directory.join("launch.env"))?;
    if metadata.get("state").map(String::as_str) != Some("running") {
        return Err(format!("launch authorization is not running: {name}"));
    }
    write_launch_state(&directory, &metadata, "completed")
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn prepare_private_output(state: &Path, name: &str, uid: u32) -> Result<PathBuf, String> {
    use std::os::unix::fs::PermissionsExt;

    let directory = state.join("role-io").join(name);
    fs::create_dir_all(&directory)
        .map_err(|error| format!("create private role output directory: {error}"))?;
    chown(&directory, uid, config::ROLE_GID)?;
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o700))
        .map_err(|error| format!("protect private role output directory: {error}"))?;
    let output = directory.join(format!("final-message.{}.txt", std::process::id()));
    fs::write(&output, []).map_err(|error| format!("create private role output: {error}"))?;
    chown(&output, uid, config::ROLE_GID)?;
    fs::set_permissions(&output, fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("protect private role output: {error}"))?;
    Ok(output)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn prepare_private_output(_state: &Path, _name: &str, _uid: u32) -> Result<PathBuf, String> {
    Err("private role output requires Linux or macOS UID isolation".into())
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
pub fn seal_role_output(
    state: &Path,
    name: &str,
    role: &str,
    workflow_id: &str,
    private_output: &Path,
    public_output: &Path,
) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    let bytes =
        fs::read(private_output).map_err(|error| format!("read private role output: {error}"))?;
    atomic_write_bytes(public_output, &bytes)?;
    chown(public_output, config::ORCHESTRATOR_UID, config::ROLE_GID)?;
    fs::set_permissions(public_output, fs::Permissions::from_mode(0o660))
        .map_err(|error| format!("set public role output permissions: {error}"))?;
    if matches!(role, "reviewer" | "scout") {
        let evidence_root = if role == "reviewer" {
            "reviewer-evidence"
        } else {
            "contract-evidence"
        };
        let directory = state.join(evidence_root).join(name);
        fs::create_dir_all(&directory)
            .map_err(|error| format!("create reviewer evidence directory: {error}"))?;
        chown(&directory, config::SUPERVISOR_UID, config::ROLE_GID)?;
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o2750))
            .map_err(|error| format!("protect reviewer evidence directory: {error}"))?;
        atomic_write_bytes(&directory.join("last-message.txt"), &bytes)?;
        let metadata = format!(
            "name={name}\nrole={role}\naccess=read-only\nworkflow_id={workflow_id}\nstate=completed\noutput_sha256={:x}\n",
            Sha256::digest(&bytes)
        );
        atomic_write_bytes(&directory.join("evidence.env"), metadata.as_bytes())?;
    }
    Ok(())
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
pub fn seal_role_output(
    _state: &Path,
    _name: &str,
    _role: &str,
    _workflow_id: &str,
    _private_output: &Path,
    _public_output: &Path,
) -> Result<(), String> {
    Err("sealed role output requires Linux or macOS UID isolation".into())
}

fn write_launch_state(
    directory: &Path,
    metadata: &BTreeMap<String, String>,
    state: &str,
) -> Result<(), String> {
    let mut text = String::new();
    for key in [
        "name",
        "role",
        "access",
        "cli",
        "cli_bin",
        "instruction_sha256",
    ] {
        text.push_str(&format!("{key}={}\n", required_field(metadata, key)?));
    }
    text.push_str(&format!(
        "workflow_id={}\n",
        metadata
            .get("workflow_id")
            .map(String::as_str)
            .unwrap_or("")
    ));
    text.push_str(&format!(
        "model={}\n",
        metadata.get("model").map(String::as_str).unwrap_or("")
    ));
    text.push_str(&format!("state={state}\n"));
    atomic_write_bytes(&directory.join("launch.env"), text.as_bytes())
}

fn read_owned_paths(state: &Path, name: &str) -> Result<Vec<PathBuf>, String> {
    let root = fs::canonicalize(config::root()?)
        .map_err(|error| format!("resolve authority workspace: {error}"))?;
    let path = state.join("assignments").join(name).join("owned-paths");
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let mut values = Vec::new();
    for relative in fs::read_to_string(path)
        .map_err(|error| format!("read authorized owned paths: {error}"))?
        .lines()
        .filter(|line| !line.is_empty())
    {
        let candidate = root.join(relative);
        let canonical = fs::canonicalize(&candidate).map_err(|_| {
            format!(
                "secure writer owned path must already exist: {}",
                candidate.display()
            )
        })?;
        if canonical == root || !canonical.starts_with(&root) {
            return Err(format!("authorized path escaped the workspace: {relative}"));
        }
        values.push(canonical);
    }
    Ok(values)
}

fn parse_options(args: &[String]) -> Result<BTreeMap<String, String>, String> {
    if args.len() % 2 != 0 {
        return Err("register-launch options require flag/value pairs".into());
    }
    let mut values = BTreeMap::new();
    for pair in args.chunks_exact(2) {
        if !matches!(
            pair[0].as_str(),
            "--role" | "--cli" | "--cli-bin" | "--instruction-file" | "--model"
        ) || pair[1].contains(['\n', '\r'])
        {
            return Err(format!("invalid register-launch option: {}", pair[0]));
        }
        values.insert(pair[0].clone(), pair[1].clone());
    }
    Ok(values)
}

fn required_option<'a>(
    values: &'a BTreeMap<String, String>,
    name: &str,
) -> Result<&'a str, String> {
    values
        .get(name)
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .ok_or_else(|| format!("register-launch requires {name}"))
}

fn required_field<'a>(values: &'a BTreeMap<String, String>, name: &str) -> Result<&'a str, String> {
    values
        .get(name)
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .ok_or_else(|| format!("launch authorization is missing {name}"))
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && !name.starts_with('-')
        && name != "orchestrator"
        && name
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, '_' | '.' | '-'))
}

fn atomic_write_bytes(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("create authority directory {}: {error}", parent.display()))?;
    let temporary = parent.join(format!(
        ".{}.tmp.{}",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("authority"),
        std::process::id()
    ));
    fs::write(&temporary, bytes)
        .map_err(|error| format!("write authority temporary file: {error}"))?;
    fs::rename(&temporary, path).map_err(|error| format!("publish authority file: {error}"))?;
    #[cfg(unix)]
    set_mode(path, 0o640)?;
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    if unsafe { libc::geteuid() } == 0 {
        chown(path, config::SUPERVISOR_UID, config::ROLE_GID)?;
    }
    Ok(())
}

fn uid_sandbox() -> bool {
    env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1")
}

fn server_child() -> bool {
    env::var(SERVER_CHILD_ENV).as_deref() == Ok("1")
}

#[cfg(unix)]
fn authority_client_uid() -> bool {
    matches!(
        unsafe { libc::getuid() },
        config::ORCHESTRATOR_UID | config::WRITER_UID | config::READER_UID
    )
}

#[cfg(not(unix))]
fn authority_client_uid() -> bool {
    false
}

pub fn authority_socket(state: &Path) -> PathBuf {
    state.join("authority.sock")
}

/// True only when the four fixed role UIDs are pairwise distinct.
///
/// The UIDs are hardcoded literals today, so this is cheap, but it guards
/// against a future change that makes them configurable: UID isolation
/// between the orchestrator, writer, reader, and supervisor identities is
/// fictitious the moment any two of them collide.
#[cfg_attr(not(any(target_os = "linux", target_os = "macos")), allow(dead_code))]
fn uids_are_pairwise_distinct(
    orchestrator: u32,
    writer: u32,
    reader: u32,
    supervisor: u32,
) -> bool {
    let uids = [orchestrator, writer, reader, supervisor];
    for i in 0..uids.len() {
        for j in (i + 1)..uids.len() {
            if uids[i] == uids[j] {
                return false;
            }
        }
    }
    true
}

#[cfg(target_os = "linux")]
pub fn register_runtime_state(state: &Path) -> Result<(), String> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    if unsafe { libc::geteuid() } != 0 {
        return Err("registering authority state requires root".into());
    }
    let canonical = fs::canonicalize(state)
        .map_err(|error| format!("canonicalize authority state {}: {error}", state.display()))?;
    let registry = Path::new(AUTHORITY_REGISTRY);
    let parent = registry
        .parent()
        .ok_or_else(|| "authority registry has no parent".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("create authority registry directory: {error}"))?;
    let parent_metadata = fs::metadata(parent)
        .map_err(|error| format!("inspect authority registry directory: {error}"))?;
    if parent_metadata.uid() != 0 || parent_metadata.permissions().mode() & 0o022 != 0 {
        return Err("authority registry directory must be root-owned and non-writable".into());
    }
    if registry.exists() {
        let existing = fs::read_to_string(registry)
            .map_err(|error| format!("read authority registry: {error}"))?;
        if Path::new(existing.trim()) != canonical {
            return Err(format!(
                "another UID-isolated authority state is already registered: {}",
                existing.trim()
            ));
        }
        return Ok(());
    }
    fs::write(registry, format!("{}\n", canonical.display()))
        .map_err(|error| format!("write authority registry: {error}"))?;
    fs::set_permissions(registry, fs::Permissions::from_mode(0o600))
        .map_err(|error| format!("protect authority registry: {error}"))?;
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn register_runtime_state(_state: &Path) -> Result<(), String> {
    Err("authority state registration requires Linux".into())
}

#[cfg(target_os = "linux")]
pub fn validate_runtime_state(state: &Path) -> Result<(), String> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let registry = Path::new(AUTHORITY_REGISTRY);
    let metadata = fs::metadata(registry)
        .map_err(|_| "trusted authority state is not registered".to_string())?;
    if metadata.uid() != 0 || metadata.permissions().mode() & 0o077 != 0 {
        return Err("trusted authority state registry has unsafe ownership or mode".into());
    }
    let expected = fs::read_to_string(registry)
        .map_err(|error| format!("read trusted authority state: {error}"))?;
    let actual = fs::canonicalize(state)
        .map_err(|error| format!("canonicalize requested authority state: {error}"))?;
    if actual != Path::new(expected.trim()) {
        return Err(format!(
            "requested state is not the registered authority state: {}",
            state.display()
        ));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn validate_runtime_state(_state: &Path) -> Result<(), String> {
    Err("authority state validation requires Linux".into())
}

#[cfg(unix)]
fn proxy_request(request: AuthorityRequest) -> Result<ExitCode, String> {
    let state = config::state_dir()?;
    let socket = authority_socket(&state);
    let mut stream = UnixStream::connect(&socket)
        .map_err(|error| format!("connect authority supervisor {}: {error}", socket.display()))?;
    let payload = serde_json::to_vec(&request)
        .map_err(|error| format!("encode authority request: {error}"))?;
    stream
        .write_all(&payload)
        .map_err(|error| format!("send authority request: {error}"))?;
    stream
        .shutdown(std::net::Shutdown::Write)
        .map_err(|error| format!("finish authority request: {error}"))?;
    let mut bytes = Vec::new();
    stream
        .read_to_end(&mut bytes)
        .map_err(|error| format!("read authority response: {error}"))?;
    let response: Response = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode authority response: {error}"))?;
    print!("{}", response.stdout);
    eprint!("{}", response.stderr);
    Ok(ExitCode::from(response.code.clamp(0, 255) as u8))
}

#[cfg(not(unix))]
fn proxy_request(_request: AuthorityRequest) -> Result<ExitCode, String> {
    Err("authority supervisor requires Unix".into())
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn serve(socket: &Path) -> Result<ExitCode, String> {
    if !uids_are_pairwise_distinct(
        config::ORCHESTRATOR_UID,
        config::WRITER_UID,
        config::READER_UID,
        config::SUPERVISOR_UID,
    ) {
        return Err(
            "authority supervisor refuses to start: ORCHESTRATOR_UID, WRITER_UID, READER_UID, and SUPERVISOR_UID must be pairwise distinct"
                .into(),
        );
    }
    if unsafe { libc::getuid() } != config::SUPERVISOR_UID {
        return Err(format!(
            "authority supervisor must run as uid {}",
            config::SUPERVISOR_UID
        ));
    }
    if socket.exists() {
        fs::remove_file(socket).map_err(|error| {
            format!(
                "remove stale authority socket {}: {error}",
                socket.display()
            )
        })?;
    }
    let listener = UnixListener::bind(socket)
        .map_err(|error| format!("bind authority socket {}: {error}", socket.display()))?;
    set_mode(socket, 0o660)?;
    for incoming in listener.incoming() {
        let mut stream = match incoming {
            Ok(stream) => stream,
            Err(error) => {
                eprintln!("authority supervisor: accept request: {error}");
                continue;
            }
        };
        if serve_connection(&mut stream)? {
            let _ = fs::remove_file(socket);
            return Ok(ExitCode::SUCCESS);
        }
    }
    Ok(ExitCode::SUCCESS)
}

/// Serves one authority client. Client disconnects and malformed requests are
/// isolated to this connection so they cannot take down the workflow's only
/// trusted state writer. Returns true only for an authorized shutdown request.
#[cfg(any(target_os = "linux", target_os = "macos"))]
fn serve_connection(stream: &mut UnixStream) -> Result<bool, String> {
    let peer_uid = match peer_uid(stream) {
        Ok(uid) => uid,
        Err(error) => {
            eprintln!("authority supervisor: {error}");
            return Ok(false);
        }
    };
    if !matches!(
        peer_uid,
        0 | config::ORCHESTRATOR_UID | config::WRITER_UID | config::READER_UID
    ) {
        let _ = write_response(
            stream,
            &Response {
                code: 1,
                stdout: String::new(),
                stderr: "authority supervisor: unauthorized peer\n".into(),
            },
        );
        return Ok(false);
    }
    let mut bytes = Vec::new();
    if let Err(error) = stream.read_to_end(&mut bytes) {
        eprintln!("authority supervisor: read request: {error}");
        return Ok(false);
    }
    let request: AuthorityRequest = match serde_json::from_slice(&bytes) {
        Ok(request) => request,
        Err(error) => {
            let _ = write_response(
                stream,
                &Response {
                    code: 1,
                    stdout: String::new(),
                    stderr: format!("authority supervisor: invalid request: {error}\n"),
                },
            );
            return Ok(false);
        }
    };
    if request.is_shutdown() {
        let _ = write_response(
            stream,
            &Response {
                code: 0,
                stdout: String::new(),
                stderr: String::new(),
            },
        );
        return Ok(true);
    }
    if !request.authorized_for(peer_uid) {
        let _ = write_response(
            stream,
            &Response {
                code: 1,
                stdout: String::new(),
                stderr: format!(
                    "authority supervisor: caller uid {peer_uid} is not authorized for: {}\n",
                    request.display()
                ),
            },
        );
        return Ok(false);
    }
    let response = execute(request)?;
    if let Err(error) = write_response(stream, &response) {
        eprintln!("authority supervisor: {error}");
    }
    Ok(false)
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn execute(request: AuthorityRequest) -> Result<Response, String> {
    let executable =
        env::current_exe().map_err(|error| format!("resolve authority executable: {error}"))?;
    let (command, args) = request.into_cli();
    let output = Command::new(executable)
        .arg(command)
        .args(args)
        .env(SERVER_CHILD_ENV, "1")
        .stdin(Stdio::null())
        .output()
        .map_err(|error| format!("execute authority transaction: {error}"))?;
    Ok(Response {
        code: output.status.code().unwrap_or(1),
        stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
        stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
    })
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn write_response(stream: &mut UnixStream, response: &Response) -> Result<(), String> {
    let bytes = serde_json::to_vec(response)
        .map_err(|error| format!("encode authority response: {error}"))?;
    stream
        .write_all(&bytes)
        .map_err(|error| format!("write authority response: {error}"))
}

#[cfg(target_os = "linux")]
fn peer_uid(stream: &UnixStream) -> Result<u32, String> {
    use std::os::fd::AsRawFd;

    let mut credentials: libc::ucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::ucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut credentials as *mut _ as *mut libc::c_void,
            &mut length,
        )
    };
    if result != 0 {
        return Err(format!(
            "read authority peer credentials: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(credentials.uid)
}

/// Darwin peer-credential identification. `SOL_LOCAL`/`LOCAL_PEERCRED` and
/// `libc::xucred` are exposed by the pinned `libc` crate (0.2.189) for
/// `target_os = "macos"`; if a future libc bump ever drops one of these three
/// symbols, replace it with a local `extern "C"` binding using the same
/// layout rather than blocking macOS support on the upstream crate.
#[cfg(target_os = "macos")]
fn peer_uid(stream: &UnixStream) -> Result<u32, String> {
    use std::os::fd::AsRawFd;

    let mut credentials: libc::xucred = unsafe { std::mem::zeroed() };
    let mut length = std::mem::size_of::<libc::xucred>() as libc::socklen_t;
    let result = unsafe {
        libc::getsockopt(
            stream.as_raw_fd(),
            libc::SOL_LOCAL,
            libc::LOCAL_PEERCRED,
            &mut credentials as *mut _ as *mut libc::c_void,
            &mut length,
        )
    };
    if result != 0 {
        return Err(format!(
            "read authority peer credentials: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(credentials.cr_uid)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn serve(_socket: &Path) -> Result<ExitCode, String> {
    Err("authority supervisor requires Linux or macOS".into())
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    fs::set_permissions(path, fs::Permissions::from_mode(mode))
        .map_err(|error| format!("set permissions {}: {error}", path.display()))
}

#[cfg(target_os = "linux")]
pub fn prepare_state_permissions(state: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    fs::create_dir_all(state).map_err(|error| format!("create state directory: {error}"))?;
    for name in CONTROL_DIRECTORIES {
        let directory = state.join(name);
        fs::create_dir_all(&directory)
            .map_err(|error| format!("create authority directory {name}: {error}"))?;
        let metadata = fs::symlink_metadata(&directory)
            .map_err(|error| format!("inspect authority directory {name}: {error}"))?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(format!(
                "authority path must be a real directory: {}",
                directory.display()
            ));
        }
    }
    for entry in fs::read_dir(state).map_err(|error| format!("read state directory: {error}"))? {
        let path = entry
            .map_err(|error| format!("read state entry: {error}"))?
            .path();
        let control = path
            .file_name()
            .and_then(|name| name.to_str())
            .is_some_and(|name| CONTROL_DIRECTORIES.contains(&name));
        prepare_tree(
            &path,
            if control {
                config::SUPERVISOR_UID
            } else {
                config::ORCHESTRATOR_UID
            },
            control,
        )?;
    }
    chown(state, config::SUPERVISOR_UID, config::ROLE_GID)?;
    fs::set_permissions(state, fs::Permissions::from_mode(0o3770))
        .map_err(|error| format!("set state root permissions: {error}"))?;
    Ok(())
}

#[cfg(target_os = "linux")]
fn prepare_tree(path: &Path, uid: u32, authority: bool) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect state path {}: {error}", path.display()))?;
    chown(path, uid, config::ROLE_GID)?;
    if metadata.is_dir() {
        fs::set_permissions(
            path,
            fs::Permissions::from_mode(if authority { 0o2750 } else { 0o2770 }),
        )
        .map_err(|error| {
            format!(
                "set state directory permissions {}: {error}",
                path.display()
            )
        })?;
        for entry in fs::read_dir(path)
            .map_err(|error| format!("read state directory {}: {error}", path.display()))?
        {
            prepare_tree(
                &entry
                    .map_err(|error| format!("read state entry: {error}"))?
                    .path(),
                uid,
                authority,
            )?;
        }
    } else if metadata.is_file() {
        let executable = metadata.permissions().mode() & 0o111 != 0;
        fs::set_permissions(
            path,
            fs::Permissions::from_mode(if authority {
                0o640
            } else if executable {
                0o770
            } else {
                0o660
            }),
        )
        .map_err(|error| format!("set state file permissions {}: {error}", path.display()))?;
    }
    Ok(())
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn chown(path: &Path, uid: u32, gid: u32) -> Result<(), String> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    let raw = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| format!("path contains NUL: {}", path.display()))?;
    if unsafe { libc::lchown(raw.as_ptr(), uid, gid) } != 0 {
        return Err(format!(
            "chown {}: {}",
            path.display(),
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
pub fn prepare_state_permissions(_state: &Path) -> Result<(), String> {
    Err("authority supervisor UID isolation requires Linux".into())
}

#[cfg(target_os = "linux")]
pub fn start(state: &Path, executable: &Path) -> Result<u32, String> {
    if !uids_are_pairwise_distinct(
        config::ORCHESTRATOR_UID,
        config::WRITER_UID,
        config::READER_UID,
        config::SUPERVISOR_UID,
    ) {
        return Err(
            "authority supervisor refuses to start: ORCHESTRATOR_UID, WRITER_UID, READER_UID, and SUPERVISOR_UID must be pairwise distinct"
                .into(),
        );
    }
    // `role-exec --uid SUPERVISOR_UID` below drops this process's privilege
    // to the fixed supervisor UID; that drop is fictitious unless this
    // process is actually root-capable right now.
    if unsafe { libc::geteuid() } != 0 {
        return Err(
            "authority supervisor refuses to start: process lacks the effective root capability required to drop to the supervisor UID"
                .into(),
        );
    }
    let socket = authority_socket(state);
    if socket.exists() && UnixStream::connect(&socket).is_ok() {
        let pid_path = state.join("runtime_state/authority-supervisor.pid");
        return fs::read_to_string(&pid_path)
            .map_err(|error| format!("read existing authority supervisor pid: {error}"))?
            .trim()
            .parse::<u32>()
            .map_err(|error| format!("parse existing authority supervisor pid: {error}"));
    }
    let log_path = state.join("runtime_state/authority-supervisor.log");
    if let Some(parent) = log_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("create authority log directory: {error}"))?;
    }
    let log = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .map_err(|error| format!("open authority supervisor log: {error}"))?;
    let log_stdout = log
        .try_clone()
        .map_err(|error| format!("clone authority supervisor log: {error}"))?;
    let mut command = Command::new(executable);
    if let Some(root) = env::var_os("MULTIAGENT_CODEX_HOME_ROOT").filter(|value| !value.is_empty())
    {
        let home = PathBuf::from(root).join("supervisor");
        command.env("HOME", &home).env("CODEX_HOME", &home);
    }
    let child = command
        .arg("role-exec")
        .arg("--uid")
        .arg(config::SUPERVISOR_UID.to_string())
        .arg("--gid")
        .arg(config::ROLE_GID.to_string())
        .arg("--")
        .arg(executable)
        .arg("supervisor")
        .arg("serve")
        .stdin(Stdio::null())
        .stdout(Stdio::from(log_stdout))
        .stderr(Stdio::from(log))
        .spawn()
        .map_err(|error| format!("start authority supervisor: {error}"))?;
    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if socket.exists() {
            return Ok(child.id());
        }
        thread::sleep(Duration::from_millis(25));
    }
    let detail = fs::read_to_string(&log_path).unwrap_or_default();
    Err(format!(
        "authority supervisor did not create socket: {}: {}",
        socket.display(),
        detail.trim()
    ))
}

#[cfg(not(target_os = "linux"))]
pub fn start(_state: &Path, _executable: &Path) -> Result<u32, String> {
    Err("authority supervisor UID isolation requires Linux".into())
}

#[cfg(test)]
mod tests {
    use super::uids_are_pairwise_distinct;
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    use super::{peer_uid, serve_connection};
    #[cfg(any(target_os = "linux", target_os = "macos"))]
    use std::os::unix::net::UnixStream;

    #[test]
    fn distinct_uids_are_required() {
        assert!(uids_are_pairwise_distinct(10001, 10002, 10003, 10004));
        assert!(!uids_are_pairwise_distinct(10001, 10001, 10003, 10004));
        assert!(!uids_are_pairwise_distinct(10001, 10002, 10001, 10004));
        assert!(!uids_are_pairwise_distinct(10001, 10002, 10003, 10002));
        assert!(!uids_are_pairwise_distinct(7, 7, 7, 7));
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn disconnected_client_does_not_fail_the_supervisor_loop() {
        let (mut server, client) = UnixStream::pair().expect("create authority socket pair");
        drop(client);

        assert!(!serve_connection(&mut server).expect("isolate disconnected client"));
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn peer_uid_of_a_self_connected_socket_is_the_real_uid() {
        let (server, client) = UnixStream::pair().expect("create peer credential socket pair");
        let expected = unsafe { libc::getuid() };
        assert_eq!(peer_uid(&server).expect("read peer credentials"), expected);
        drop(client);
    }
}
