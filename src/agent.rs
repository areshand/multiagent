use serde::Serialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::env;
use std::ffi::OsString;
use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
#[cfg(unix)]
use std::sync::atomic::{AtomicI32, Ordering};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
static AGENT_CHILD_GROUP: AtomicI32 = AtomicI32::new(0);
#[cfg(unix)]
static AGENT_CANCEL_SIGNAL: AtomicI32 = AtomicI32::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum BackendId {
    Codex,
    Claude,
    Qwen,
}

impl BackendId {
    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "codex" => Ok(Self::Codex),
            "claude" => Ok(Self::Claude),
            "qwen" => Ok(Self::Qwen),
            _ => Err(format!(
                "unsupported coding-agent backend '{value}' (expected codex, claude, or qwen)"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::Qwen => "qwen",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RoleAccess {
    ReadOnly,
    WorkspaceWrite,
}

impl RoleAccess {
    pub fn parse(value: &str) -> Result<Self, String> {
        match value {
            "read-only" => Ok(Self::ReadOnly),
            "workspace-write" => Ok(Self::WorkspaceWrite),
            _ => Err(format!(
                "invalid coding-agent access '{value}' (expected read-only or workspace-write)"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InvocationMode {
    Interactive,
    Headless,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct AgentCapabilities {
    pub structured_events: bool,
    pub native_resume: bool,
    pub usage_events: bool,
    pub interactive: bool,
}

#[derive(Clone, Debug)]
pub struct BackendPaths {
    pub codex: String,
    pub claude: String,
    pub qwen: String,
}

impl BackendPaths {
    pub fn from_env() -> Self {
        Self {
            codex: env_nonempty("CODEX_BIN").unwrap_or_else(|| "codex".into()),
            claude: env_nonempty("CLAUDE_BIN").unwrap_or_else(|| "claude".into()),
            qwen: env_nonempty("QWEN_BIN").unwrap_or_else(|| "qwen".into()),
        }
    }
}

#[derive(Clone, Debug)]
pub struct AgentRequest {
    pub cwd: PathBuf,
    pub prompt_file: Option<PathBuf>,
    pub final_output: Option<PathBuf>,
    pub access: RoleAccess,
    pub mode: InvocationMode,
    pub resume_session: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CommandSpec {
    pub program: String,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub stdin_file: Option<PathBuf>,
    // Interactive compatibility only. Headless backends must use stdin_file.
    pub legacy_prompt_argument: Option<PathBuf>,
}

impl CommandSpec {
    pub fn render_shell(&self) -> String {
        let mut command = shell_escape(&self.program);
        for arg in &self.args {
            command.push(' ');
            command.push_str(&shell_escape(&arg.to_string_lossy()));
        }
        if let Some(path) = &self.legacy_prompt_argument {
            command.push_str(&format!(
                " \"$(cat {})\"",
                shell_escape(&path.display().to_string())
            ));
        }
        if let Some(path) = &self.stdin_file {
            command.push_str(&format!(" < {}", shell_escape(&path.display().to_string())));
        }
        command
    }
}

pub trait AgentBackend {
    fn id(&self) -> BackendId;
    fn executable(&self) -> &str;
    fn capabilities(&self) -> AgentCapabilities;
    fn command(&self, request: &AgentRequest) -> Result<CommandSpec, String>;

    fn preflight(&self) -> Result<BackendVersion, String> {
        let output = Command::new(self.executable())
            .arg("--version")
            .output()
            .map_err(|error| {
                format!(
                    "run {} coding-agent preflight ({}): {error}",
                    self.id().as_str(),
                    self.executable()
                )
            })?;
        if !output.status.success() {
            return Err(format!(
                "{} coding-agent preflight failed for {}: {}",
                self.id().as_str(),
                self.executable(),
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        let version = String::from_utf8_lossy(if output.stdout.is_empty() {
            &output.stderr
        } else {
            &output.stdout
        })
        .trim()
        .to_string();
        Ok(BackendVersion {
            backend: self.id(),
            executable: self.executable().into(),
            version: if version.is_empty() {
                "unknown".into()
            } else {
                version
            },
        })
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct BackendVersion {
    pub backend: BackendId,
    pub executable: String,
    pub version: String,
}

struct CodexBackend {
    executable: String,
}

struct ClaudeBackend {
    executable: String,
}

struct QwenBackend {
    executable: String,
}

pub fn backend(id: BackendId, paths: &BackendPaths) -> Box<dyn AgentBackend + '_> {
    match id {
        BackendId::Codex => Box::new(CodexBackend {
            executable: paths.codex.clone(),
        }),
        BackendId::Claude => Box::new(ClaudeBackend {
            executable: paths.claude.clone(),
        }),
        BackendId::Qwen => Box::new(QwenBackend {
            executable: paths.qwen.clone(),
        }),
    }
}

impl AgentBackend for CodexBackend {
    fn id(&self) -> BackendId {
        BackendId::Codex
    }

    fn executable(&self) -> &str {
        &self.executable
    }

    fn capabilities(&self) -> AgentCapabilities {
        AgentCapabilities {
            structured_events: true,
            native_resume: false,
            usage_events: true,
            interactive: true,
        }
    }

    fn command(&self, request: &AgentRequest) -> Result<CommandSpec, String> {
        let mut args = Vec::<OsString>::new();
        match request.mode {
            InvocationMode::Headless => {
                if request.resume_session.is_some() {
                    return Err(
                        "codex native resume is not enabled by the v1 backend contract".into(),
                    );
                }
                args.extend(["exec".into(), "--cd".into(), request.cwd.as_os_str().into()]);
                args.push("--skip-git-repo-check".into());
                for value in codex_safety_args(request.access, true) {
                    args.push(value.into());
                }
                args.push("-c".into());
                args.push(codex_shell_environment_config().into());
                if let Some(path) = &request.final_output {
                    args.push("--output-last-message".into());
                    args.push(path.as_os_str().into());
                }
                args.push("-".into());
                Ok(CommandSpec {
                    program: self.executable.clone(),
                    args,
                    cwd: request.cwd.clone(),
                    stdin_file: request.prompt_file.clone(),
                    legacy_prompt_argument: None,
                })
            }
            InvocationMode::Interactive => {
                args.extend(["--cd".into(), request.cwd.as_os_str().into()]);
                for value in codex_safety_args(request.access, false) {
                    args.push(value.into());
                }
                args.push("-c".into());
                args.push(codex_shell_environment_config().into());
                args.push("-c".into());
                args.push("check_for_update_on_startup=false".into());
                // Interactive server sessions run from a newly created,
                // control-server-owned state directory rather than a Git
                // checkout. Mark that exact directory trusted for this
                // invocation so Codex cannot block unattended bootstrap on
                // its first-run project trust prompt.
                let trusted_cwd = request
                    .cwd
                    .to_string_lossy()
                    .replace('\\', "\\\\")
                    .replace('"', "\\\"");
                args.push("-c".into());
                args.push(
                    format!("projects={{\"{trusted_cwd}\"={{trust_level=\"trusted\"}}}}").into(),
                );
                args.push("--no-alt-screen".into());
                // The prompt bundle starts with a delimiter line. Terminate
                // option parsing so a leading `-` in that prompt cannot be
                // interpreted as another Codex CLI flag.
                args.push("--".into());
                Ok(CommandSpec {
                    program: self.executable.clone(),
                    args,
                    cwd: request.cwd.clone(),
                    stdin_file: None,
                    legacy_prompt_argument: request.prompt_file.clone(),
                })
            }
        }
    }
}

fn codex_shell_environment_config() -> String {
    const ALLOWED: &[&str] = &[
        "PATH",
        "HOME",
        "USER",
        "SHELL",
        "TMPDIR",
        "TERM",
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "MULTIAGENT_*",
        "ORCHESTRATOR_CLI",
        "WORKER_CLI",
        "SUBAGENT_CLI",
        "VERIFIER_CLI",
        "CODEX_BIN",
        "CLAUDE_BIN",
        "QWEN_BIN",
    ];
    format!(
        "shell_environment_policy.include_only=[{}]",
        ALLOWED
            .iter()
            .map(|value| format!("\"{value}\""))
            .collect::<Vec<_>>()
            .join(",")
    )
}

impl AgentBackend for ClaudeBackend {
    fn id(&self) -> BackendId {
        BackendId::Claude
    }

    fn executable(&self) -> &str {
        &self.executable
    }

    fn capabilities(&self) -> AgentCapabilities {
        AgentCapabilities {
            structured_events: true,
            native_resume: true,
            usage_events: true,
            interactive: true,
        }
    }

    fn command(&self, request: &AgentRequest) -> Result<CommandSpec, String> {
        let mut args = Vec::<OsString>::new();
        match request.mode {
            InvocationMode::Headless => {
                args.extend([
                    "-p".into(),
                    "--input-format".into(),
                    "text".into(),
                    "--output-format".into(),
                    "stream-json".into(),
                    "--verbose".into(),
                    "--dangerously-skip-permissions".into(),
                ]);
                if let Some(prompt) = env_nonempty("MULTIAGENT_CLAUDE_APPEND_SYSTEM_PROMPT") {
                    args.push("--append-system-prompt".into());
                    args.push(prompt.into());
                }
                if let Some(session) = &request.resume_session {
                    args.push("--resume".into());
                    args.push(session.into());
                }
                Ok(CommandSpec {
                    program: self.executable.clone(),
                    args,
                    cwd: request.cwd.clone(),
                    stdin_file: request.prompt_file.clone(),
                    legacy_prompt_argument: None,
                })
            }
            InvocationMode::Interactive => Ok(CommandSpec {
                program: self.executable.clone(),
                args: vec!["--dangerously-skip-permissions".into()],
                cwd: request.cwd.clone(),
                stdin_file: None,
                legacy_prompt_argument: request.prompt_file.clone(),
            }),
        }
    }
}

impl AgentBackend for QwenBackend {
    fn id(&self) -> BackendId {
        BackendId::Qwen
    }

    fn executable(&self) -> &str {
        &self.executable
    }

    fn capabilities(&self) -> AgentCapabilities {
        AgentCapabilities {
            structured_events: true,
            native_resume: true,
            usage_events: true,
            interactive: false,
        }
    }

    fn command(&self, request: &AgentRequest) -> Result<CommandSpec, String> {
        if request.mode != InvocationMode::Headless {
            return Err("Qwen Code v1 backend supports headless workflow roles only".into());
        }
        let mut args = vec![
            "--output-format".into(),
            "stream-json".into(),
            "--approval-mode".into(),
            match request.access {
                RoleAccess::ReadOnly => "plan".into(),
                RoleAccess::WorkspaceWrite => "yolo".into(),
            },
        ];
        #[cfg(not(target_os = "linux"))]
        args.push("--sandbox".into());
        if let Some(session) = &request.resume_session {
            args.push("--resume".into());
            args.push(session.into());
        }
        for (key, flag) in [
            ("MULTIAGENT_AGENT_MAX_TURNS", "--max-session-turns"),
            ("MULTIAGENT_AGENT_MAX_WALL_TIME", "--max-wall-time"),
            ("MULTIAGENT_AGENT_MAX_TOOL_CALLS", "--max-tool-calls"),
        ] {
            if let Some(value) = env_nonempty(key) {
                args.push(flag.into());
                args.push(value.into());
            }
        }
        Ok(CommandSpec {
            program: self.executable.clone(),
            args,
            cwd: request.cwd.clone(),
            stdin_file: request.prompt_file.clone(),
            legacy_prompt_argument: None,
        })
    }
}

#[cfg(target_os = "linux")]
fn codex_safety_args(_access: RoleAccess, _headless: bool) -> Vec<&'static str> {
    vec!["--dangerously-bypass-approvals-and-sandbox"]
}

#[cfg(not(target_os = "linux"))]
fn codex_safety_args(access: RoleAccess, headless: bool) -> Vec<&'static str> {
    if !headless {
        // The interactive orchestrator must reach the tmux server so it can
        // create and coordinate role-isolated subagent windows. Headless role
        // processes retain their requested Codex sandbox below.
        return vec!["--dangerously-bypass-approvals-and-sandbox"];
    }
    vec!["--sandbox", access.as_str(), "-c", "approval_policy=never"]
}

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    let Some(command) = args.first().map(String::as_str) else {
        print_usage();
        return Ok(ExitCode::SUCCESS);
    };
    match command {
        "run" => run_backend(&args[1..]),
        "backend-info" => backend_info(&args[1..]),
        "-h" | "--help" | "help" => {
            print_usage();
            Ok(ExitCode::SUCCESS)
        }
        _ => Err(format!("unknown agent command: {command}")),
    }
}

fn print_usage() {
    println!(
        "Usage:\n  multiagent agent backend-info BACKEND\n  multiagent agent run --backend BACKEND --cwd DIR --prompt-file FILE --final-output FILE --trace-dir DIR --access read-only|workspace-write [--resume-session ID]"
    );
}

fn backend_info(args: &[String]) -> Result<ExitCode, String> {
    if args.len() != 1 {
        return Err("agent backend-info requires BACKEND".into());
    }
    let id = BackendId::parse(&args[0])?;
    let paths = BackendPaths::from_env();
    let selected = backend(id, &paths);
    let version = selected.preflight()?;
    println!(
        "{}",
        serde_json::to_string(&json!({
            "backend": id,
            "capabilities": selected.capabilities(),
            "executable": version.executable,
            "version": version.version,
        }))
        .map_err(|error| format!("serialize backend info: {error}"))?
    );
    Ok(ExitCode::SUCCESS)
}

fn run_backend(args: &[String]) -> Result<ExitCode, String> {
    let mut values = BTreeMap::<String, String>::new();
    let mut index = 0;
    while index < args.len() {
        let key = match args[index].as_str() {
            "--backend" | "--cwd" | "--prompt-file" | "--final-output" | "--trace-dir"
            | "--access" | "--resume-session" => args[index].trim_start_matches("--"),
            other => return Err(format!("unknown agent run argument: {other}")),
        };
        let value = args
            .get(index + 1)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| format!("agent run --{key} requires a value"))?;
        values.insert(key.into(), value.clone());
        index += 2;
    }
    let required = |key: &str| {
        values
            .get(key)
            .cloned()
            .ok_or_else(|| format!("agent run requires --{key}"))
    };
    let id = BackendId::parse(&required("backend")?)?;
    let cwd = PathBuf::from(required("cwd")?);
    let prompt_file = PathBuf::from(required("prompt-file")?);
    let final_output = PathBuf::from(required("final-output")?);
    let trace_root = PathBuf::from(required("trace-dir")?);
    let access = RoleAccess::parse(&required("access")?)?;
    if !cwd.is_dir() {
        return Err(format!(
            "agent working directory is missing: {}",
            cwd.display()
        ));
    }
    if !prompt_file.is_file() {
        return Err(format!(
            "agent prompt file is missing: {}",
            prompt_file.display()
        ));
    }

    let paths = BackendPaths::from_env();
    let selected = backend(id, &paths);
    let version = selected.preflight()?;
    let request = AgentRequest {
        cwd,
        prompt_file: Some(prompt_file.clone()),
        final_output: Some(final_output.clone()),
        access,
        mode: InvocationMode::Headless,
        resume_session: values.get("resume-session").cloned(),
    };
    let spec = selected.command(&request)?;
    let timeout = agent_timeout()?;
    let trace_dir = next_trace_attempt(&trace_root)?;
    run_spec(
        id,
        version,
        selected.capabilities(),
        spec,
        RunFiles {
            prompt: &prompt_file,
            final_output: &final_output,
            trace_dir: &trace_dir,
        },
        timeout,
    )
}

struct RunFiles<'a> {
    prompt: &'a Path,
    final_output: &'a Path,
    trace_dir: &'a Path,
}

fn agent_timeout() -> Result<Option<Duration>, String> {
    let timeout = env_nonempty("MULTIAGENT_AGENT_TIMEOUT_SECONDS")
        .map(|value| {
            value.parse::<u64>().map(Duration::from_secs).map_err(|_| {
                "MULTIAGENT_AGENT_TIMEOUT_SECONDS must be a positive integer".to_string()
            })
        })
        .transpose()?;
    if timeout.is_some_and(|value| value.is_zero()) {
        return Err("MULTIAGENT_AGENT_TIMEOUT_SECONDS must be a positive integer".into());
    }
    Ok(timeout)
}

fn next_trace_attempt(root: &Path) -> Result<PathBuf, String> {
    create_private_dir(root)?;
    for number in 1..=9999 {
        let name = format!("attempt-{number:04}");
        let path = root.join(&name);
        match fs::create_dir(&path) {
            Ok(()) => {
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o2770)).map_err(
                        |error| {
                            format!(
                                "set agent trace attempt permissions {}: {error}",
                                path.display()
                            )
                        },
                    )?;
                }
                write_private(&root.join("latest"), format!("{name}\n").as_bytes())?;
                return Ok(path);
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(error) => {
                return Err(format!(
                    "create agent trace attempt {}: {error}",
                    path.display()
                ))
            }
        }
    }
    Err(format!(
        "agent trace attempt limit reached under {}",
        root.display()
    ))
}

fn run_spec(
    id: BackendId,
    version: BackendVersion,
    capabilities: AgentCapabilities,
    spec: CommandSpec,
    files: RunFiles<'_>,
    timeout: Option<Duration>,
) -> Result<ExitCode, String> {
    let prompt = fs::read(files.prompt)
        .map_err(|error| format!("read agent prompt {}: {error}", files.prompt.display()))?;
    create_private_dir(files.trace_dir)?;
    // Never let a restored attempt inherit a stale success message from the
    // previous process. Provider output or normalized events repopulate it.
    write_private(files.final_output, b"")?;
    let raw_stdout = files.trace_dir.join("raw-stdout.log");
    let raw_stderr = files.trace_dir.join("raw-stderr.log");
    let normalized = files.trace_dir.join("events.jsonl");
    let metadata = json!({
        "schema_version": 2,
        "backend": id,
        "executable": version.executable,
        "version": version.version,
        "capabilities": capabilities,
        "cwd": spec.cwd,
        "prompt_bytes": prompt.len(),
        "prompt_file": files.prompt,
        "final_output": files.final_output,
        "usage_file": files.trace_dir.join("usage.json"),
        "workflow_id": env::var("MULTIAGENT_WORKFLOW_ID").unwrap_or_default(),
        "role": env::var("MULTIAGENT_SUBAGENT_NAME").unwrap_or_else(|_| "orchestrator".into()),
    });
    write_private(
        &files.trace_dir.join("metadata.json"),
        serde_json::to_string_pretty(&metadata)
            .map_err(|error| format!("serialize agent metadata: {error}"))?
            .as_bytes(),
    )?;

    let mut command = Command::new(&spec.program);
    command
        .args(&spec.args)
        .current_dir(&spec.cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    configure_agent_process(&mut command)?;
    let mut child = command.spawn().map_err(|error| {
        format!(
            "start {} coding agent ({}): {error}",
            id.as_str(),
            spec.program
        )
    })?;
    #[cfg(unix)]
    AGENT_CHILD_GROUP.store(child.id() as i32, Ordering::SeqCst);
    let prompt_write = child
        .stdin
        .take()
        .ok_or_else(|| "coding-agent stdin was not captured".to_string())?
        .write_all(&prompt);
    if let Err(error) = prompt_write {
        terminate_agent_process(&mut child);
        let _ = child.wait();
        #[cfg(unix)]
        AGENT_CHILD_GROUP.store(0, Ordering::SeqCst);
        return Err(format!("write coding-agent prompt: {error}"));
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "coding-agent stdout was not captured".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "coding-agent stderr was not captured".to_string())?;
    let stdout_path = raw_stdout.clone();
    let stderr_path = raw_stderr.clone();
    let stdout_thread = thread::spawn(move || tee_stream(stdout, &stdout_path, true));
    let stderr_thread = thread::spawn(move || tee_stream(stderr, &stderr_path, false));
    let started = Instant::now();
    let mut timed_out = false;
    let status = loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("wait for {} coding agent: {error}", id.as_str()))?
        {
            break status;
        }
        if timeout.is_some_and(|limit| started.elapsed() >= limit) {
            timed_out = true;
            terminate_agent_process(&mut child);
            break child.wait().map_err(|error| {
                format!("wait for timed-out {} coding agent: {error}", id.as_str())
            })?;
        }
        thread::sleep(Duration::from_millis(25));
    };
    #[cfg(unix)]
    AGENT_CHILD_GROUP.store(0, Ordering::SeqCst);
    stdout_thread
        .join()
        .map_err(|_| "coding-agent stdout capture panicked".to_string())??;
    stderr_thread
        .join()
        .map_err(|_| "coding-agent stderr capture panicked".to_string())??;

    let stdout_bytes =
        fs::read(&raw_stdout).map_err(|error| format!("read raw coding-agent stdout: {error}"))?;
    let decoded = normalize_output(id, &stdout_bytes);
    let mut event_bytes = Vec::new();
    for event in &decoded.events {
        serde_json::to_writer(&mut event_bytes, event)
            .map_err(|error| format!("serialize normalized agent event: {error}"))?;
        event_bytes.push(b'\n');
    }
    write_private(&normalized, &event_bytes)?;
    write_private(
        &files.trace_dir.join("usage.json"),
        serde_json::to_string_pretty(&summarize_usage(id, &decoded.events))
            .map_err(|error| format!("serialize token usage summary: {error}"))?
            .as_bytes(),
    )?;
    if let Some(session_id) = decoded.session_id.as_deref() {
        write_private(
            &files.trace_dir.join("session-id"),
            format!("{session_id}\n").as_bytes(),
        )?;
    }
    if id != BackendId::Codex
        || fs::metadata(files.final_output).is_ok_and(|value| value.len() == 0)
    {
        if let Some(message) = decoded.final_message.as_deref() {
            write_private(files.final_output, format!("{message}\n").as_bytes())?;
        }
    }
    #[cfg(unix)]
    let signal = {
        use std::os::unix::process::ExitStatusExt;
        status.signal()
    };
    #[cfg(not(unix))]
    let signal = None::<i32>;
    #[cfg(unix)]
    let cancel_signal = AGENT_CANCEL_SIGNAL.swap(0, Ordering::SeqCst);
    #[cfg(not(unix))]
    let cancel_signal = 0;
    let canceled = cancel_signal != 0;
    let code = if timed_out {
        124
    } else if canceled {
        (128 + cancel_signal).min(255)
    } else {
        status
            .code()
            .unwrap_or_else(|| signal.map_or(1, |value| (128 + value).min(255)))
    };
    let reason = if timed_out {
        "timeout"
    } else if canceled {
        "canceled"
    } else if signal.is_some() {
        "signal"
    } else if status.success() {
        "completed"
    } else {
        "nonzero-exit"
    };
    write_private(
        &files.trace_dir.join("exit.json"),
        serde_json::to_string_pretty(&json!({
            "success": status.success() && !timed_out && !canceled,
            "code": code,
            "signal": signal,
            "timed_out": timed_out,
            "canceled": canceled,
            "reason": reason,
        }))
        .map_err(|error| format!("serialize coding-agent exit: {error}"))?
        .as_bytes(),
    )?;
    Ok(ExitCode::from(code.clamp(0, 255) as u8))
}

#[cfg(unix)]
fn configure_agent_process(command: &mut Command) -> Result<(), String> {
    use std::os::unix::process::CommandExt;

    install_agent_signal_handlers()?;
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            #[cfg(target_os = "linux")]
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    Ok(())
}

#[cfg(not(unix))]
fn configure_agent_process(_command: &mut Command) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
fn install_agent_signal_handlers() -> Result<(), String> {
    for signal in [libc::SIGHUP, libc::SIGINT, libc::SIGTERM, libc::SIGQUIT] {
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = forward_agent_signal as usize;
        if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0
            || unsafe { libc::sigaction(signal, &action, std::ptr::null_mut()) } != 0
        {
            return Err(format!(
                "install coding-agent signal handler: {}",
                std::io::Error::last_os_error()
            ));
        }
    }
    Ok(())
}

#[cfg(unix)]
extern "C" fn forward_agent_signal(signal: libc::c_int) {
    AGENT_CANCEL_SIGNAL.store(signal, Ordering::SeqCst);
    let child = AGENT_CHILD_GROUP.load(Ordering::SeqCst);
    if child > 0 {
        unsafe {
            libc::kill(-child, libc::SIGKILL);
            libc::kill(child, libc::SIGKILL);
        }
    }
}

#[cfg(unix)]
fn terminate_agent_process(child: &mut std::process::Child) {
    let pid = child.id() as i32;
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
        libc::kill(pid, libc::SIGKILL);
    }
}

#[cfg(not(unix))]
fn terminate_agent_process(child: &mut std::process::Child) {
    let _ = child.kill();
}

fn tee_stream(mut source: impl Read, path: &Path, stdout: bool) -> Result<(), String> {
    let mut file = private_file(path)?;
    let mut buffer = [0_u8; 8192];
    loop {
        let read = source
            .read(&mut buffer)
            .map_err(|error| format!("read coding-agent output: {error}"))?;
        if read == 0 {
            break;
        }
        file.write_all(&buffer[..read])
            .map_err(|error| format!("write raw coding-agent trace: {error}"))?;
        if stdout {
            std::io::stdout()
                .write_all(&buffer[..read])
                .map_err(|error| format!("forward coding-agent stdout: {error}"))?;
            std::io::stdout().flush().ok();
        } else {
            std::io::stderr()
                .write_all(&buffer[..read])
                .map_err(|error| format!("forward coding-agent stderr: {error}"))?;
            std::io::stderr().flush().ok();
        }
    }
    file.sync_all()
        .map_err(|error| format!("sync raw coding-agent trace: {error}"))
}

#[derive(Serialize)]
struct NormalizedEvent {
    backend: BackendId,
    sequence: usize,
    kind: String,
    raw_type: String,
    session_id: Option<String>,
    text: Option<String>,
    tool_id: Option<String>,
    tool_name: Option<String>,
    success: Option<bool>,
    usage: Option<Value>,
    raw: Value,
}

#[derive(Serialize)]
struct UsageSummary {
    schema_version: u8,
    backend: BackendId,
    input_tokens: u64,
    cached_input_tokens: u64,
    output_tokens: u64,
    cache_creation_input_tokens: u64,
    cache_read_input_tokens: u64,
    total_tokens: u64,
    observed_usage_events: usize,
    aggregation: &'static str,
}

#[derive(Default)]
struct TokenCounts {
    input: u64,
    cached_input: u64,
    output: u64,
    cache_creation: u64,
    cache_read: u64,
    total: Option<u64>,
}

struct DecodedOutput {
    events: Vec<NormalizedEvent>,
    final_message: Option<String>,
    session_id: Option<String>,
}

fn normalize_output(id: BackendId, bytes: &[u8]) -> DecodedOutput {
    let text = String::from_utf8_lossy(bytes);
    let mut events = Vec::new();
    let mut final_message = None;
    let mut session_id = None;
    for (sequence, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let raw = serde_json::from_str::<Value>(line)
            .unwrap_or_else(|_| json!({ "type": "text", "text": line }));
        let raw_type = raw
            .get("type")
            .and_then(Value::as_str)
            .or_else(|| raw.pointer("/event/type").and_then(Value::as_str))
            .unwrap_or("unknown")
            .to_string();
        let event_session = find_string(&raw, &["session_id", "sessionId"]);
        if event_session.is_some() {
            session_id = event_session.clone();
        }
        let event_text = extract_event_text(&raw);
        if is_final_event(&raw_type, &raw) {
            if let Some(value) = event_text.as_ref().filter(|value| !value.trim().is_empty()) {
                final_message = Some(value.clone());
            }
        } else if matches!(raw_type.as_str(), "assistant" | "message" | "text") {
            if let Some(value) = event_text.as_ref().filter(|value| !value.trim().is_empty()) {
                final_message = Some(value.clone());
            }
        }
        let kind = normalized_kind(&raw_type, &raw);
        let tool_id = find_string(&raw, &["tool_use_id", "toolUseId"]).or_else(|| {
            (kind == "tool-started")
                .then(|| find_string(&raw, &["id"]))
                .flatten()
        });
        let tool_name = (kind == "tool-started")
            .then(|| find_string(&raw, &["name"]))
            .flatten();
        let success = if raw_type == "result" {
            raw.get("is_error")
                .and_then(Value::as_bool)
                .map(|value| !value)
        } else if kind == "tool-finished" {
            raw.get("is_error")
                .and_then(Value::as_bool)
                .map(|value| !value)
                .or(Some(true))
        } else {
            None
        };
        let usage = find_value(&raw, &["usage"]).cloned();
        events.push(NormalizedEvent {
            backend: id,
            sequence,
            kind,
            raw_type,
            session_id: event_session,
            text: event_text,
            tool_id,
            tool_name,
            success,
            usage,
            raw,
        });
    }
    DecodedOutput {
        events,
        final_message,
        session_id,
    }
}

fn summarize_usage(id: BackendId, events: &[NormalizedEvent]) -> UsageSummary {
    let observed = events.iter().filter(|event| event.usage.is_some()).count();
    let turn_usage = events
        .iter()
        .filter(|event| {
            matches!(event.raw_type.as_str(), "turn.completed" | "turn_completed")
                && event.usage.is_some()
        })
        .filter_map(|event| event.usage.as_ref())
        .collect::<Vec<_>>();
    let terminal = events.iter().rev().find_map(|event| {
        (matches!(
            event.raw_type.as_str(),
            "result" | "completed" | "complete" | "final" | "message.completed"
        ))
        .then_some(event.usage.as_ref())
        .flatten()
    });

    let (selected, aggregation): (Vec<&Value>, &'static str) =
        if id == BackendId::Codex && !turn_usage.is_empty() {
            (turn_usage, "turn-sum")
        } else if let Some(usage) = terminal {
            (vec![usage], "terminal")
        } else {
            let mut seen = std::collections::BTreeSet::new();
            let unique = events
                .iter()
                .filter_map(|event| event.usage.as_ref())
                .filter(|usage| seen.insert(usage.to_string()))
                .collect::<Vec<_>>();
            let mode = if unique.is_empty() {
                "unavailable"
            } else {
                "unique-event-sum"
            };
            (unique, mode)
        };

    let mut aggregate = TokenCounts::default();
    let mut total_tokens = 0_u64;
    for usage in selected {
        let counts = token_counts(usage);
        aggregate.input = aggregate.input.saturating_add(counts.input);
        aggregate.cached_input = aggregate.cached_input.saturating_add(counts.cached_input);
        aggregate.output = aggregate.output.saturating_add(counts.output);
        aggregate.cache_creation = aggregate
            .cache_creation
            .saturating_add(counts.cache_creation);
        aggregate.cache_read = aggregate.cache_read.saturating_add(counts.cache_read);
        total_tokens = total_tokens.saturating_add(
            counts
                .total
                .unwrap_or_else(|| counts.input.saturating_add(counts.output)),
        );
    }

    UsageSummary {
        schema_version: 1,
        backend: id,
        input_tokens: aggregate.input,
        cached_input_tokens: aggregate.cached_input,
        output_tokens: aggregate.output,
        cache_creation_input_tokens: aggregate.cache_creation,
        cache_read_input_tokens: aggregate.cache_read,
        total_tokens,
        observed_usage_events: observed,
        aggregation,
    }
}

fn token_counts(usage: &Value) -> TokenCounts {
    TokenCounts {
        input: token_value(
            usage,
            &[
                "input_tokens",
                "inputTokens",
                "prompt_tokens",
                "promptTokens",
            ],
        ),
        cached_input: token_value(usage, &["cached_input_tokens", "cachedInputTokens"]),
        output: token_value(
            usage,
            &[
                "output_tokens",
                "outputTokens",
                "completion_tokens",
                "completionTokens",
            ],
        ),
        cache_creation: token_value(usage, &["cache_creation_input_tokens"]),
        cache_read: token_value(usage, &["cache_read_input_tokens"]),
        total: token_value_optional(usage, &["total_tokens", "totalTokens"]),
    }
}

fn token_value(value: &Value, names: &[&str]) -> u64 {
    token_value_optional(value, names).unwrap_or(0)
}

fn token_value_optional(value: &Value, names: &[&str]) -> Option<u64> {
    match value {
        Value::Object(values) => {
            for name in names {
                if let Some(number) = values.get(*name).and_then(Value::as_u64) {
                    return Some(number);
                }
            }
            values
                .values()
                .find_map(|nested| token_value_optional(nested, names))
        }
        Value::Array(values) => values
            .iter()
            .find_map(|nested| token_value_optional(nested, names)),
        _ => None,
    }
}

fn normalized_kind(raw_type: &str, raw: &Value) -> String {
    if raw_type == "system" {
        "started"
    } else if matches!(raw_type, "result" | "completed" | "complete" | "final") {
        if raw.get("is_error").and_then(Value::as_bool) == Some(true) {
            "failed"
        } else {
            "completed"
        }
    } else if raw_type.contains("tool_result") || contains_type(raw, "tool_result") {
        "tool-finished"
    } else if raw_type.contains("tool_use") || contains_type(raw, "tool_use") {
        "tool-started"
    } else if matches!(raw_type, "assistant" | "message" | "text") {
        "text"
    } else {
        "diagnostic"
    }
    .into()
}

fn contains_type(value: &Value, expected: &str) -> bool {
    match value {
        Value::Object(values) => {
            values.get("type").and_then(Value::as_str) == Some(expected)
                || values.values().any(|value| contains_type(value, expected))
        }
        Value::Array(values) => values.iter().any(|value| contains_type(value, expected)),
        _ => false,
    }
}

fn find_string(value: &Value, keys: &[&str]) -> Option<String> {
    match value {
        Value::Object(values) => {
            for key in keys {
                if let Some(value) = values.get(*key).and_then(Value::as_str) {
                    return Some(value.into());
                }
            }
            values.values().find_map(|value| find_string(value, keys))
        }
        Value::Array(values) => values.iter().find_map(|value| find_string(value, keys)),
        _ => None,
    }
}

fn find_value<'a>(value: &'a Value, keys: &[&str]) -> Option<&'a Value> {
    match value {
        Value::Object(values) => {
            for key in keys {
                if let Some(value) = values.get(*key) {
                    return Some(value);
                }
            }
            values.values().find_map(|value| find_value(value, keys))
        }
        Value::Array(values) => values.iter().find_map(|value| find_value(value, keys)),
        _ => None,
    }
}

fn extract_event_text(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        Value::Array(values) => {
            let text = values
                .iter()
                .filter_map(extract_event_text)
                .filter(|value| !value.trim().is_empty())
                .collect::<Vec<_>>()
                .join("");
            (!text.is_empty()).then_some(text)
        }
        Value::Object(values) => {
            for key in ["result", "text", "content"] {
                if let Some(text) = values.get(key).and_then(extract_event_text) {
                    return Some(text);
                }
            }
            for key in ["message", "event", "delta"] {
                if let Some(text) = values.get(key).and_then(extract_event_text) {
                    return Some(text);
                }
            }
            None
        }
        _ => None,
    }
}

fn is_final_event(raw_type: &str, raw: &Value) -> bool {
    matches!(raw_type, "result" | "completed" | "complete" | "final")
        || raw.get("stop_reason").is_some_and(|value| !value.is_null())
        || raw.pointer("/event/type").and_then(Value::as_str) == Some("message_stop")
}

fn create_private_dir(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path)
        .map_err(|error| format!("create agent trace directory {}: {error}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o2770)).map_err(|error| {
            format!(
                "set agent trace directory permissions {}: {error}",
                path.display()
            )
        })?;
    }
    Ok(())
}

fn private_file(path: &Path) -> Result<File, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("create agent output directory: {error}"))?;
    }
    let file = File::create(path)
        .map_err(|error| format!("create private agent file {}: {error}", path.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o660)).map_err(|error| {
            format!(
                "set private agent file permissions {}: {error}",
                path.display()
            )
        })?;
    }
    Ok(file)
}

fn write_private(path: &Path, bytes: &[u8]) -> Result<(), String> {
    let mut file = private_file(path)?;
    file.write_all(bytes)
        .map_err(|error| format!("write private agent file {}: {error}", path.display()))?;
    file.sync_all()
        .map_err(|error| format!("sync private agent file {}: {error}", path.display()))
}

fn env_nonempty(key: &str) -> Option<String> {
    env::var(key).ok().filter(|value| !value.is_empty())
}

fn shell_escape(value: &str) -> String {
    if !value.is_empty()
        && value.chars().all(|character| {
            character.is_ascii_alphanumeric()
                || matches!(
                    character,
                    '_' | '@' | '%' | '+' | '=' | ':' | ',' | '.' | '/' | '-'
                )
        })
    {
        return value.into();
    }
    format!("'{}'", value.replace(char::from(39), "'\\''"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(mode: InvocationMode) -> AgentRequest {
        AgentRequest {
            cwd: PathBuf::from("/tmp/project with spaces"),
            prompt_file: Some(PathBuf::from("/tmp/prompt file")),
            final_output: Some(PathBuf::from("/tmp/final message")),
            access: RoleAccess::ReadOnly,
            mode,
            resume_session: None,
        }
    }

    #[test]
    fn backend_names_are_strict() {
        assert_eq!(BackendId::parse("qwen").unwrap(), BackendId::Qwen);
        assert!(BackendId::parse("ollama").is_err());
    }

    #[test]
    fn codex_headless_uses_argv_and_stdin() {
        let paths = BackendPaths {
            codex: "codex".into(),
            claude: "claude".into(),
            qwen: "qwen".into(),
        };
        let command = backend(BackendId::Codex, &paths)
            .command(&request(InvocationMode::Headless))
            .unwrap();
        assert_eq!(command.stdin_file, Some(PathBuf::from("/tmp/prompt file")));
        assert!(command.legacy_prompt_argument.is_none());
        assert!(command
            .args
            .iter()
            .any(|arg| arg == "--output-last-message"));
        assert!(!command.render_shell().contains("$(cat"));
    }

    #[test]
    fn codex_rejects_native_resume_and_preserves_interactive_compatibility() {
        let paths = BackendPaths {
            codex: "codex".into(),
            claude: "claude".into(),
            qwen: "qwen".into(),
        };
        let selected = backend(BackendId::Codex, &paths);
        let mut headless = request(InvocationMode::Headless);
        headless.resume_session = Some("session-123".into());
        assert!(selected.command(&headless).is_err());
        assert!(!selected.capabilities().native_resume);

        let interactive = selected
            .command(&request(InvocationMode::Interactive))
            .unwrap();
        assert!(interactive.stdin_file.is_none());
        assert_eq!(
            interactive.legacy_prompt_argument,
            Some(PathBuf::from("/tmp/prompt file"))
        );
        assert!(interactive.args.iter().any(|arg| arg == "--no-alt-screen"));
        assert!(interactive
            .args
            .iter()
            .any(|arg| arg == "--dangerously-bypass-approvals-and-sandbox"));
        assert_eq!(interactive.args.last(), Some(&OsString::from("--")));
        assert!(interactive.args.iter().any(|arg| {
            arg == &OsString::from(
                "projects={\"/tmp/project with spaces\"={trust_level=\"trusted\"}}",
            )
        }));
        assert!(interactive.args.iter().any(|arg| {
            arg.to_string_lossy()
                .starts_with("shell_environment_policy.include_only=[")
        }));
        assert!(interactive
            .args
            .iter()
            .any(|arg| arg == "check_for_update_on_startup=false"));
    }

    #[test]
    fn claude_headless_uses_stream_json_stdin_and_native_resume() {
        let paths = BackendPaths {
            codex: "codex".into(),
            claude: "claude".into(),
            qwen: "qwen".into(),
        };
        let mut value = request(InvocationMode::Headless);
        value.resume_session = Some("session-123".into());
        let selected = backend(BackendId::Claude, &paths);
        let command = selected.command(&value).unwrap();
        let args = command
            .args
            .iter()
            .map(|value| value.to_string_lossy())
            .collect::<Vec<_>>();
        assert_eq!(command.stdin_file, Some(PathBuf::from("/tmp/prompt file")));
        assert!(command.legacy_prompt_argument.is_none());
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--output-format", "stream-json"]));
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--resume", "session-123"]));
        assert!(args
            .iter()
            .any(|arg| arg == "--dangerously-skip-permissions"));
        assert!(selected.capabilities().native_resume);
    }

    #[test]
    fn qwen_headless_declares_streaming_and_resume() {
        let paths = BackendPaths {
            codex: "codex".into(),
            claude: "claude".into(),
            qwen: "qwen".into(),
        };
        let mut value = request(InvocationMode::Headless);
        value.resume_session = Some("session-123".into());
        let selected = backend(BackendId::Qwen, &paths);
        let command = selected.command(&value).unwrap();
        let args = command
            .args
            .iter()
            .map(|value| value.to_string_lossy())
            .collect::<Vec<_>>();
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--output-format", "stream-json"]));
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--resume", "session-123"]));
        assert!(args
            .windows(2)
            .any(|pair| pair == ["--approval-mode", "plan"]));
        assert!(selected.capabilities().native_resume);
        assert!(selected
            .command(&request(InvocationMode::Interactive))
            .is_err());

        let mut writer = request(InvocationMode::Headless);
        writer.access = RoleAccess::WorkspaceWrite;
        let writer_args = selected
            .command(&writer)
            .unwrap()
            .args
            .into_iter()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(writer_args
            .windows(2)
            .any(|pair| pair == ["--approval-mode", "yolo"]));
    }

    #[test]
    fn normalizes_json_and_plain_text_without_discarding_raw_events() {
        let decoded = normalize_output(
            BackendId::Qwen,
            b"{\"type\":\"system\",\"session_id\":\"s-1\"}\n{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"done\"}]}}\n",
        );
        assert_eq!(decoded.session_id.as_deref(), Some("s-1"));
        assert_eq!(decoded.final_message.as_deref(), Some("done"));
        assert_eq!(decoded.events.len(), 2);
        assert_eq!(decoded.events[0].kind, "started");
        assert_eq!(decoded.events[1].kind, "text");

        let plain = normalize_output(BackendId::Codex, b"plain final text\n");
        assert_eq!(plain.final_message.as_deref(), Some("plain final text"));
        assert_eq!(plain.events[0].raw_type, "text");
    }

    #[test]
    fn normalizes_tool_usage_and_completion_fields() {
        let decoded = normalize_output(
            BackendId::Qwen,
            b"{\"type\":\"assistant\",\"message\":{\"content\":[{\"type\":\"tool_use\",\"id\":\"tool-1\",\"name\":\"shell\"}],\"usage\":{\"input_tokens\":12}}}\n{\"type\":\"result\",\"is_error\":false,\"result\":\"done\"}\n",
        );
        assert_eq!(decoded.events[0].kind, "tool-started");
        assert_eq!(decoded.events[0].tool_id.as_deref(), Some("tool-1"));
        assert_eq!(decoded.events[0].tool_name.as_deref(), Some("shell"));
        assert_eq!(
            decoded.events[0]
                .usage
                .as_ref()
                .and_then(|value| value.get("input_tokens"))
                .and_then(Value::as_u64),
            Some(12)
        );
        assert_eq!(decoded.events[1].kind, "completed");
        assert_eq!(decoded.events[1].success, Some(true));
    }

    #[test]
    fn summarizes_terminal_usage_without_double_counting_cached_tokens() {
        let decoded = normalize_output(
            BackendId::Claude,
            b"{\"type\":\"assistant\",\"usage\":{\"input_tokens\":100,\"output_tokens\":10}}\n{\"type\":\"result\",\"usage\":{\"input_tokens\":120,\"cache_read_input_tokens\":80,\"output_tokens\":20,\"total_tokens\":140}}\n",
        );
        let usage = summarize_usage(BackendId::Claude, &decoded.events);
        assert_eq!(usage.input_tokens, 120);
        assert_eq!(usage.cache_read_input_tokens, 80);
        assert_eq!(usage.output_tokens, 20);
        assert_eq!(usage.total_tokens, 140);
        assert_eq!(usage.aggregation, "terminal");
    }

    #[test]
    fn sums_codex_turn_usage() {
        let decoded = normalize_output(
            BackendId::Codex,
            b"{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":10,\"output_tokens\":2}}\n{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":20,\"output_tokens\":3}}\n",
        );
        let usage = summarize_usage(BackendId::Codex, &decoded.events);
        assert_eq!(usage.input_tokens, 30);
        assert_eq!(usage.output_tokens, 5);
        assert_eq!(usage.total_tokens, 35);
        assert_eq!(usage.aggregation, "turn-sum");
    }

    #[test]
    fn prompt_content_is_never_part_of_headless_command() {
        let spec = CommandSpec {
            program: "qwen".into(),
            args: vec!["--output-format".into(), "stream-json".into()],
            cwd: PathBuf::from("/tmp"),
            stdin_file: Some(PathBuf::from("/tmp/prompt")),
            legacy_prompt_argument: None,
        };
        let rendered = spec.render_shell();
        assert_eq!(rendered, "qwen --output-format stream-json < /tmp/prompt");
        assert!(!rendered.contains("secret prompt contents"));
    }
}
