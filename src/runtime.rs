use crate::{config, policy, role_sandbox};
use chrono::{Local, SecondsFormat, Utc};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Output};
use std::thread;
use std::time::{Duration, Instant};

const STATUS_HEADER: &str =
    "TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID\tWORKFLOW_ID\tNODE_ID\n";

#[derive(Clone)]
struct RuntimeConfig {
    session: String,
    root: PathBuf,
    state: PathBuf,
    logs: PathBuf,
    policy: PathBuf,
    prompt_root: PathBuf,
    worker_cli: String,
    subagent_cli: String,
    verifier_cli: String,
    codex_bin: String,
    claude_bin: String,
    code_exec: bool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CodexAccess {
    ReadOnly,
    WorkspaceWrite,
}

const ORCHESTRATOR_UID: u32 = config::ORCHESTRATOR_UID;
const WRITER_UID: u32 = 10002;
const READER_UID: u32 = 10003;
const ROLE_GID: u32 = 10001;

impl CodexAccess {
    fn sandbox(self) -> &'static str {
        match self {
            Self::ReadOnly => "read-only",
            Self::WorkspaceWrite => "workspace-write",
        }
    }
}

impl RuntimeConfig {
    fn load() -> Result<Self, String> {
        let root = config::root()?;
        let state = config::state_dir()?;
        let logs = env_path("MULTIAGENT_LOG_DIR").unwrap_or_else(|| state.join("logs"));
        let policy = env_path("MULTIAGENT_WRITE_POLICY")
            .unwrap_or_else(|| root.join("docs/write-policy.paths"));
        let prompt_root = env_path("MULTIAGENT_PROMPT_MODULE_ROOT").unwrap_or_else(framework_root);
        let worker_cli = env_nonempty("WORKER_CLI").unwrap_or_else(|| "claude".into());
        let subagent_cli = env_nonempty("SUBAGENT_CLI").unwrap_or_else(|| worker_cli.clone());
        let verifier_cli = env_nonempty("VERIFIER_CLI").unwrap_or_else(|| "codex".into());
        for value in [&worker_cli, &subagent_cli, &verifier_cli] {
            validate_cli(value)?;
        }
        Ok(Self {
            session: env_nonempty("MULTIAGENT_SESSION").unwrap_or_else(|| "multiagent".into()),
            root,
            state,
            logs,
            policy,
            prompt_root,
            worker_cli,
            subagent_cli,
            verifier_cli,
            codex_bin: env_nonempty("CODEX_BIN").unwrap_or_else(|| "codex".into()),
            claude_bin: env_nonempty("CLAUDE_BIN").unwrap_or_else(|| "claude".into()),
            code_exec: env::var("MULTIAGENT_CODEX_EXEC").as_deref() == Ok("1"),
        })
    }

    fn cli_bin(&self, cli: &str) -> Result<&str, String> {
        match cli {
            "codex" => Ok(&self.codex_bin),
            "claude" => Ok(&self.claude_bin),
            _ => Err(format!(
                "unsupported CLI '{cli}' (expected codex or claude)"
            )),
        }
    }
}

pub fn role_agent_exec(args: &[String]) -> Result<ExitCode, String> {
    let (name, restored) = match args {
        [name] => (name.as_str(), false),
        [name, flag] if flag == "--restore" => (name.as_str(), true),
        _ => return Err("usage: multiagent role-agent-exec NAME [--restore]".into()),
    };
    validate_name(name)?;
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() != Ok("1") {
        return Err("role-agent-exec requires MULTIAGENT_UID_SANDBOX=1".into());
    }
    #[cfg(unix)]
    if unsafe { libc::getuid() } != ORCHESTRATOR_UID {
        return Err("role-agent-exec is reserved for the orchestrator UID".into());
    }

    let cfg = RuntimeConfig::load()?;
    if !cfg.code_exec {
        return Err("role-agent-exec requires MULTIAGENT_CODEX_EXEC=1".into());
    }
    let dir = cfg.state.join("subagents").join(name);
    let metadata = read_env(&dir.join("meta.env"))?;
    if metadata.get("name").map(String::as_str) != Some(name)
        || metadata.get("cli").map(String::as_str) != Some("codex")
        || metadata.get("cli_bin").map(String::as_str) != Some(cfg.codex_bin.as_str())
    {
        return Err("role-agent-exec metadata does not match the requested Codex agent".into());
    }
    let access = match metadata.get("codex_access").map(String::as_str) {
        Some("read-only") => CodexAccess::ReadOnly,
        Some("workspace-write") => CodexAccess::WorkspaceWrite,
        _ => return Err("role-agent-exec metadata has invalid codex_access".into()),
    };
    validate_privileged_codex_bridge(Path::new(&cfg.codex_bin))?;
    let prompt = dir.join(if restored {
        "restore-instruction.txt"
    } else {
        "instruction.txt"
    });
    if !prompt.is_file() {
        return Err(format!(
            "role-agent-exec instruction is missing: {}",
            prompt.display()
        ));
    }
    let instruction = fs::read_to_string(&prompt).map_err(io_error("read role instruction"))?;
    if access == CodexAccess::WorkspaceWrite {
        // The setuid bridge is the final write-authority boundary. Recheck the
        // assignment against live lifecycle state here so an orchestrator
        // cannot gain a writer by overriding launch-time environment flags.
        validate_implementation_context(&cfg, name, Some(&prompt), &instruction)?;
    }
    let output = dir.join("last-message.txt");
    let command = build_cli_command(
        "codex",
        &cfg.root,
        Some(&prompt),
        Some(&output),
        &cfg.codex_bin,
        &cfg.claude_bin,
        true,
        access,
    )?;
    let supervisor_pid = dir.join("supervisor.pid");
    atomic_write(
        &supervisor_pid,
        &format!("{}\n", std::process::id()),
        "role supervisor pid",
    )?;
    let result = role_sandbox::run_supervised(
        if access == CodexAccess::WorkspaceWrite {
            WRITER_UID
        } else {
            READER_UID
        },
        ROLE_GID,
        "/bin/sh",
        &["-c".into(), command],
    );
    let _ = fs::remove_file(supervisor_pid);
    result
}

#[cfg(unix)]
fn validate_privileged_codex_bridge(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    let metadata = fs::metadata(path).map_err(io_error("inspect privileged Codex bridge"))?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.permissions().mode() & 0o022 != 0 {
        return Err(format!(
            "privileged Codex bridge must be a root-owned, non-group-writable executable: {}",
            path.display()
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn validate_privileged_codex_bridge(_path: &Path) -> Result<(), String> {
    Err("role-agent-exec requires Unix".into())
}

pub fn launch(args: &[String]) -> Result<ExitCode, String> {
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
    {
        print_launch_usage();
        return Ok(ExitCode::SUCCESS);
    }
    let framework = framework_root();
    let mut session = env_nonempty("MULTIAGENT_SESSION").unwrap_or_else(|| "multiagent".into());
    let mut root = env_path("MULTIAGENT_ROOT").unwrap_or_else(|| framework.clone());
    let mut resume = false;
    let mut attach = true;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--session" => {
                session = required_value(args, index, "--session")?.into();
                index += 2;
            }
            "--root" => {
                root = fs::canonicalize(required_value(args, index, "--root")?)
                    .map_err(io_error("resolve --root"))?;
                index += 2;
            }
            "--resume" => {
                resume = true;
                index += 1;
            }
            "--attach" => {
                attach = true;
                index += 1;
            }
            "--no-attach" => {
                attach = false;
                index += 1;
            }
            other => {
                print_launch_usage();
                return Err(format!("unknown argument: {other}"));
            }
        }
    }

    let prompt =
        env_path("MULTIAGENT_PROMPT").unwrap_or_else(|| framework.join("orchestrator_prompt.md"));
    let lifecycle_prompt = env_path("MULTIAGENT_LIFECYCLE_PROMPT")
        .unwrap_or_else(|| framework.join("prompts/playbooks/implementation-lifecycle.md"));
    let prompt_root =
        env_path("MULTIAGENT_PROMPT_MODULE_ROOT").unwrap_or_else(|| framework.clone());
    let state_dir = env_path("MULTIAGENT_STATE_DIR").unwrap_or_else(|| root.join(".multiagent"));
    let log_dir = env_path("MULTIAGENT_LOG_DIR").unwrap_or_else(|| state_dir.join("logs"));
    let policy_file =
        env_path("MULTIAGENT_WRITE_POLICY").unwrap_or_else(|| root.join("docs/write-policy.paths"));
    let worker_cli = env_nonempty("WORKER_CLI").unwrap_or_else(|| "claude".into());
    let subagent_cli = env_nonempty("SUBAGENT_CLI").unwrap_or_else(|| worker_cli.clone());
    let verifier_cli = env_nonempty("VERIFIER_CLI").unwrap_or_else(|| "codex".into());
    let orchestrator_cli = env_nonempty("ORCHESTRATOR_CLI").unwrap_or_else(|| "codex".into());
    for value in [&worker_cli, &subagent_cli, &verifier_cli, &orchestrator_cli] {
        validate_cli(value)?;
    }
    let codex_bin = env_nonempty("CODEX_BIN").unwrap_or_else(|| "codex".into());
    let claude_bin = env_nonempty("CLAUDE_BIN").unwrap_or_else(|| "claude".into());
    let verifier_max =
        env_nonempty("MULTIAGENT_VERIFIER_MAX_ITERATIONS").unwrap_or_else(|| "3".into());
    if verifier_max
        .parse::<u32>()
        .ok()
        .filter(|value| *value > 0)
        .is_none()
    {
        return Err("MULTIAGENT_VERIFIER_MAX_ITERATIONS must be a positive integer".into());
    }
    let lifecycle_enforcement = if config::lifecycle_enforced() {
        "1".into()
    } else {
        env_nonempty("MULTIAGENT_LIFECYCLE_ENFORCEMENT").unwrap_or_else(|| "1".into())
    };
    if !matches!(lifecycle_enforcement.as_str(), "0" | "1") {
        return Err("MULTIAGENT_LIFECYCLE_ENFORCEMENT must be 0 or 1".into());
    }
    require_command("tmux")?;
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        fs::create_dir_all(state_dir.join("runtime_state"))
            .map_err(io_error("create tmux socket directory"))?;
        env::set_var(
            "MULTIAGENT_TMUX_SOCKET",
            state_dir.join("runtime_state/tmux.sock"),
        );
    }
    let orchestrator_bin = if orchestrator_cli == "codex" {
        &codex_bin
    } else {
        &claude_bin
    };
    require_command(orchestrator_bin)?;
    if !prompt.is_file() {
        return Err(format!("missing orchestrator prompt: {}", prompt.display()));
    }
    if !lifecycle_prompt.is_file() {
        return Err(format!(
            "missing implementation lifecycle prompt: {}",
            lifecycle_prompt.display()
        ));
    }
    if tmux_success(&["has-session", "-t", &session]) {
        return Err(format!(
            "tmux session already exists: {session}\nAttach with: tmux attach -t {session}"
        ));
    }

    let run_id = env_nonempty("MULTIAGENT_RUN_ID").unwrap_or_else(|| {
        format!(
            "run_{}_{}",
            Utc::now().format("%Y%m%dT%H%M%SZ"),
            std::process::id()
        )
    });
    let active_workflow_file = state_dir.join("runtime_state/active-workflow-id");
    let mut workflow_id = env_nonempty("MULTIAGENT_WORKFLOW_ID").unwrap_or_default();
    if resume && workflow_id.is_empty() && active_workflow_file.is_file() {
        workflow_id = fs::read_to_string(&active_workflow_file)
            .map_err(io_error("read active workflow"))?
            .trim()
            .to_string();
    }
    if workflow_id.is_empty() {
        workflow_id = run_id.clone();
    }

    for directory in [
        state_dir.join("subagents"),
        state_dir.join("assignments"),
        state_dir.join("worktrees"),
        state_dir.join("runtime_state"),
        state_dir.join("tmp"),
        log_dir.clone(),
    ] {
        fs::create_dir_all(directory).map_err(io_error("create runtime directory"))?;
    }

    let executable = env::current_exe().map_err(io_error("resolve multiagent executable"))?;
    let shared_env = launch_environment(
        &session,
        &root,
        resume,
        &prompt,
        &lifecycle_prompt,
        &prompt_root,
        &state_dir,
        &log_dir,
        &policy_file,
        &verifier_max,
        &run_id,
        &workflow_id,
        &lifecycle_enforcement,
        &orchestrator_cli,
        &worker_cli,
        &subagent_cli,
        &verifier_cli,
        &codex_bin,
        &claude_bin,
        &executable,
    );
    for (key, value) in &shared_env {
        env::set_var(key, value);
    }

    policy::run(&["init".into()])?;
    let prompt_bundle = state_dir.join("runtime_state/orchestrator-prompt-bundle.md");
    run_self_quiet(&[
        "prompt-bundle",
        "--orchestrator",
        &prompt.display().to_string(),
        "--lifecycle",
        &lifecycle_prompt.display().to_string(),
        "--output",
        &prompt_bundle.display().to_string(),
    ])?;
    write_prompt_hashes(
        &state_dir.join("runtime_state/prompt-sha256.tsv"),
        [&prompt, &lifecycle_prompt, &prompt_bundle],
    )?;
    run_self_quiet(&[
        "workflow",
        "init-or-resume",
        &workflow_id,
        "--resume",
        if resume { "1" } else { "0" },
    ])?;
    atomic_write(
        &active_workflow_file,
        &format!("{workflow_id}\n"),
        "active workflow",
    )?;

    let bootstrap = state_dir.join("orchestrator-bootstrap.sh");
    let mut bootstrap_env = shared_env.clone();
    bootstrap_env.insert(
        "MULTIAGENT_PROMPT".into(),
        prompt_bundle.display().to_string(),
    );
    write_bootstrap(
        &bootstrap,
        &root,
        &bootstrap_env,
        &orchestrator_cli,
        &codex_bin,
        &claude_bin,
        &prompt_bundle,
        &state_dir.join("orchestrator-last-message.txt"),
        resume,
    )?;
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        prepare_uid_state_permissions(&state_dir)?;
    }
    let bootstrap_command = format!("bash {}", shell_escape(&bootstrap.display().to_string()));
    let new_session = [
        "new-session",
        "-d",
        "-s",
        &session,
        "-n",
        "orchestrator",
        &bootstrap_command,
    ];
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        tmux_checked_as_uid(&new_session, &executable, ORCHESTRATOR_UID)?;
    } else {
        tmux_checked(&new_session)?;
    }
    tmux_checked(&["select-window", "-t", &format!("{session}:orchestrator")])?;
    pipe_log(&session, "orchestrator", &log_dir)?;
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        atomic_write(
            &state_dir.join("runtime_state/tmux-access-ready"),
            "ready\n",
            "tmux access marker",
        )?;
    }

    println!("Started tmux session: {session}");
    println!("Attach with: tmux attach -t {session}");
    println!("Resume mode: {}", u8::from(resume));
    println!("Workflow ID: {workflow_id}");
    println!("Lifecycle enforcement: {lifecycle_enforcement}");
    println!("Prompt bundle: {}", prompt_bundle.display());
    println!("Subagent state: {}", state_dir.display());
    println!("Logs: {}", log_dir.display());
    println!(
        "Dashboard: MULTIAGENT_SESSION={} MULTIAGENT_ROOT={} {} watch",
        shell_escape(&session),
        shell_escape(&root.display().to_string()),
        shell_escape(&executable.display().to_string())
    );
    println!("Verifier max iterations: {verifier_max}");
    println!("Worker CLI: {worker_cli}");
    println!("Subagent CLI: {subagent_cli}");
    println!("Verifier CLI: {verifier_cli}");
    println!("Write policy:");
    policy::run(&["show".into()])?;
    if attach {
        tmux_checked(&["attach", "-t", &session])?;
    }
    Ok(ExitCode::SUCCESS)
}

fn print_launch_usage() {
    println!(
        "Usage: multiagent launch [--session NAME] [--root DIR] [--resume] [--attach|--no-attach]\n\nStarts a tmux multi-agent session with one orchestrator window."
    );
}

#[allow(clippy::too_many_arguments)]
fn launch_environment(
    session: &str,
    root: &Path,
    resume: bool,
    prompt: &Path,
    lifecycle_prompt: &Path,
    prompt_root: &Path,
    state: &Path,
    logs: &Path,
    policy: &Path,
    verifier_max: &str,
    run_id: &str,
    workflow_id: &str,
    lifecycle_enforcement: &str,
    orchestrator_cli: &str,
    worker_cli: &str,
    subagent_cli: &str,
    verifier_cli: &str,
    codex_bin: &str,
    claude_bin: &str,
    executable: &Path,
) -> BTreeMap<String, String> {
    let mut values = BTreeMap::new();
    for (key, value) in [
        ("MULTIAGENT_SESSION", session.to_string()),
        ("MULTIAGENT_ROOT", root.display().to_string()),
        ("MULTIAGENT_RESUME", u8::from(resume).to_string()),
        ("MULTIAGENT_PROMPT", prompt.display().to_string()),
        (
            "MULTIAGENT_LIFECYCLE_PROMPT",
            lifecycle_prompt.display().to_string(),
        ),
        (
            "MULTIAGENT_PROMPT_MODULE_ROOT",
            prompt_root.display().to_string(),
        ),
        (
            "MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER",
            env_nonempty("MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER").unwrap_or_else(|| "1".into()),
        ),
        ("MULTIAGENT_STATE_DIR", state.display().to_string()),
        ("MULTIAGENT_LOG_DIR", logs.display().to_string()),
        ("MULTIAGENT_WRITE_POLICY", policy.display().to_string()),
        (
            "MULTIAGENT_VERIFIER_MAX_ITERATIONS",
            verifier_max.to_string(),
        ),
        ("MULTIAGENT_RUN_ID", run_id.to_string()),
        ("MULTIAGENT_WORKFLOW_ID", workflow_id.to_string()),
        (
            "MULTIAGENT_LIFECYCLE_ENFORCEMENT",
            lifecycle_enforcement.to_string(),
        ),
        ("ORCHESTRATOR_CLI", orchestrator_cli.to_string()),
        ("WORKER_CLI", worker_cli.to_string()),
        ("SUBAGENT_CLI", subagent_cli.to_string()),
        ("VERIFIER_CLI", verifier_cli.to_string()),
        ("CODEX_BIN", codex_bin.to_string()),
        ("CLAUDE_BIN", claude_bin.to_string()),
        (
            "MULTIAGENT_CODEX_EXEC",
            env_nonempty("MULTIAGENT_CODEX_EXEC").unwrap_or_else(|| "0".into()),
        ),
        (
            "MULTIAGENT_EXTRA_PATH",
            env_nonempty("MULTIAGENT_EXTRA_PATH").unwrap_or_default(),
        ),
        (
            "MULTIAGENT_UID_SANDBOX",
            env_nonempty("MULTIAGENT_UID_SANDBOX").unwrap_or_else(|| "0".into()),
        ),
        (
            "MULTIAGENT_CODEX_HOME_ROOT",
            env_nonempty("MULTIAGENT_CODEX_HOME_ROOT").unwrap_or_default(),
        ),
        (
            "MULTIAGENT_TMUX_SOCKET",
            env_nonempty("MULTIAGENT_TMUX_SOCKET").unwrap_or_default(),
        ),
        ("TMPDIR", state.join("tmp").display().to_string()),
        ("MULTIAGENT_BIN", executable.display().to_string()),
        ("PATH", env::var("PATH").unwrap_or_default()),
    ] {
        values.insert(key.into(), value);
    }
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        if let Some(root) = env_nonempty("MULTIAGENT_CODEX_HOME_ROOT") {
            let home = Path::new(&root).join("orchestrator");
            values.insert("CODEX_HOME".into(), home.display().to_string());
            values.insert("HOME".into(), home.display().to_string());
        }
    }
    values
}

#[allow(clippy::too_many_arguments)]
fn write_bootstrap(
    path: &Path,
    root: &Path,
    environment: &BTreeMap<String, String>,
    cli: &str,
    codex_bin: &str,
    claude_bin: &str,
    prompt: &Path,
    last_message: &Path,
    resume: bool,
) -> Result<(), String> {
    let mut text = format!(
        "#!/usr/bin/env bash\ncd {}\n",
        shell_escape(&root.display().to_string())
    );
    for (key, value) in environment {
        text.push_str(&format!("export {key}={}\n", shell_escape(value)));
    }
    if environment
        .get("MULTIAGENT_UID_SANDBOX")
        .map(String::as_str)
        == Some("1")
    {
        text.push_str("umask 0007\n");
        text.push_str(&format!(
            "until [[ -f {} ]]; do sleep 0.05; done\n",
            shell_escape(
                &Path::new(&environment["MULTIAGENT_STATE_DIR"])
                    .join("runtime_state/tmux-access-ready")
                    .display()
                    .to_string()
            )
        ));
    }
    text.push_str(&format!(
        "printf 'Multiagent launch mode: MULTIAGENT_RESUME=%s (%s)\\n' {} {}\n",
        u8::from(resume),
        if resume { "resume" } else { "clean" }
    ));
    let command = build_cli_command(
        cli,
        environment
            .get("MULTIAGENT_STATE_DIR")
            .map(Path::new)
            .unwrap_or(root),
        Some(prompt),
        Some(last_message),
        codex_bin,
        claude_bin,
        env::var("MULTIAGENT_CODEX_EXEC").as_deref() == Ok("1"),
        CodexAccess::WorkspaceWrite,
    )?;
    let command = if environment
        .get("MULTIAGENT_UID_SANDBOX")
        .map(String::as_str)
        == Some("1")
    {
        // The tmux server and every ordinary pane already run as the
        // non-writing orchestrator UID. Only the narrowly gated setuid role
        // launcher may transition a subagent pane to a writer/reader UID.
        command
    } else {
        wrap_linux_role_sandbox(
            &command,
            Path::new(
                environment
                    .get("MULTIAGENT_BIN")
                    .ok_or_else(|| "missing MULTIAGENT_BIN in launch environment".to_string())?,
            ),
            role_write_roots(root, Path::new(&environment["MULTIAGENT_STATE_DIR"]), false),
            ORCHESTRATOR_UID,
        )
    };
    text.push_str(&command);
    text.push('\n');
    atomic_write(path, &text, "orchestrator bootstrap")?;
    set_executable(path, 0o700)?;
    Ok(())
}

fn write_prompt_hashes<'a>(
    output: &Path,
    paths: impl IntoIterator<Item = &'a PathBuf>,
) -> Result<(), String> {
    let mut text = String::new();
    for path in paths {
        let bytes = fs::read(path).map_err(io_error("read prompt for hashing"))?;
        text.push_str(&format!(
            "{:x}\t{}\n",
            Sha256::digest(bytes),
            path.display()
        ));
    }
    atomic_write(output, &text, "prompt hashes")
}

pub fn orchestrator(args: &[String]) -> Result<ExitCode, String> {
    if args.is_empty()
        || args
            .iter()
            .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
    {
        println!("Usage:\n  multiagent orchestrator complete\n\nRuns the normal-path completion gates for the active orchestrated workflow.");
        return Ok(ExitCode::SUCCESS);
    }
    if args != ["complete"] {
        return Err(format!("unknown command: {}", args[0]));
    }
    if config::lifecycle_enforced() {
        let workflow_id = env_nonempty("MULTIAGENT_WORKFLOW_ID")
            .ok_or_else(|| "lifecycle enforcement requires MULTIAGENT_WORKFLOW_ID".to_string())?;
        run_self_quiet(&["workflow", "completion-check", &workflow_id])?;
        let output = run_self_output(&["workflow", "value", &workflow_id, "phase"])?;
        let phase = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if phase != "complete" {
            return Err(format!(
                "workflow must transition to complete before run completion (current: {phase})"
            ));
        }
    }
    run_self_quiet(&["subagent", "gate-check"])?;
    println!(
        "run completed\t{}",
        env_nonempty("MULTIAGENT_RUN_ID")
            .or_else(|| env_nonempty("MULTIAGENT_WORKFLOW_ID"))
            .unwrap_or_else(|| "unknown".into())
    );
    Ok(ExitCode::SUCCESS)
}

pub fn status(args: &[String]) -> Result<ExitCode, String> {
    if !args.is_empty() {
        if args
            .iter()
            .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
        {
            println!("Usage: multiagent status");
            return Ok(ExitCode::SUCCESS);
        }
        return Err(format!("unknown argument: {}", args[0]));
    }
    print!("{}", status_text()?);
    Ok(ExitCode::SUCCESS)
}

fn status_text() -> Result<String, String> {
    require_command("tmux")?;
    let cfg = RuntimeConfig::load()?;
    if !tmux_success(&["has-session", "-t", &cfg.session]) {
        return Err(format!("missing tmux session: {}", cfg.session));
    }
    let windows = tmux_output(&["list-windows", "-t", &cfg.session, "-F", "#W"])?;
    let window_names = String::from_utf8_lossy(&windows.stdout)
        .lines()
        .filter(|line| !line.is_empty())
        .map(str::to_string)
        .collect::<BTreeSet<_>>();
    let mut result = STATUS_HEADER.to_string();
    for name in &window_names {
        if name == "orchestrator" || cfg.state.join("subagents").join(name).is_dir() {
            continue;
        }
        let capture = capture_window(&cfg.session, name, 300).unwrap_or_default();
        append_status_row(
            &mut result,
            "worker",
            name,
            classify_capture(&capture),
            "open",
            &last_nonempty_line(&capture),
            "-",
            &assignment_fields(&cfg.state, name),
        );
    }
    for dir in sorted_directories(&cfg.state.join("subagents"))? {
        let name = file_name(&dir)?;
        let open = window_names.contains(&name);
        if open {
            let _ = poll(&cfg, &name, false);
        }
        let persisted = read_trimmed(&dir.join("status")).unwrap_or_else(|| "unknown".into());
        let progress = fs::read_to_string(dir.join("current.txt"))
            .ok()
            .map(|text| last_nonempty_line(&text))
            .unwrap_or_default();
        append_status_row(
            &mut result,
            "subagent",
            &name,
            &persisted,
            if open { "open" } else { "closed" },
            &progress,
            &dir.display().to_string(),
            &assignment_fields(&cfg.state, &name),
        );
    }
    Ok(result)
}

#[allow(clippy::too_many_arguments)]
fn append_status_row(
    output: &mut String,
    kind: &str,
    name: &str,
    status: &str,
    window: &str,
    progress: &str,
    state: &str,
    fields: &[String; 5],
) {
    output.push_str(&format!(
        "{kind}\t{name}\t{status}\t{window}\t{}\t{state}\t{}\t{}\t{}\t{}\t{}\n",
        progress.replace(['\r', '\n', '\t'], " "),
        fields[0],
        fields[1],
        fields[2],
        fields[3],
        fields[4]
    ));
}

fn assignment_fields(state: &Path, name: &str) -> [String; 5] {
    let values =
        read_env(&state.join("assignments").join(name).join("assignment.env")).unwrap_or_default();
    ["role", "decision_id", "plan_id", "workflow_id", "node_id"].map(|key| {
        values
            .get(key)
            .filter(|value| !value.is_empty())
            .cloned()
            .unwrap_or_else(|| "-".into())
    })
}

fn classify_capture(capture: &str) -> &'static str {
    let lower = capture.to_ascii_lowercase();
    if ["blocked", "need input", "waiting for", "cannot proceed"]
        .iter()
        .any(|value| lower.contains(value))
    {
        "blocked"
    } else if [
        "final status",
        "completed",
        "complete_task",
        "assignment complete",
        "task complete",
        "finished assignment",
        "work completed",
        "done with",
        "worked for ",
    ]
    .iter()
    .any(|value| lower.contains(value))
    {
        "done"
    } else if capture.lines().last().is_some_and(|line| {
        let value = line.trim_end().to_ascii_lowercase();
        value.ends_with('│')
            || value.ends_with('>')
            || (value.contains("codex") && value.ends_with('?'))
    }) {
        "idle"
    } else if capture.is_empty() {
        "unknown"
    } else {
        "busy"
    }
}

pub fn watch(args: &[String]) -> Result<ExitCode, String> {
    let cfg = RuntimeConfig::load()?;
    let mut once = false;
    let mut interval = env_nonempty("MULTIAGENT_WATCH_INTERVAL")
        .unwrap_or_else(|| "5".into())
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| "--interval must be a positive integer".to_string())?;
    let mut log_lines = env_nonempty("MULTIAGENT_WATCH_LOG_LINES")
        .unwrap_or_else(|| "40".into())
        .parse::<usize>()
        .map_err(|_| "--log-lines must be a non-negative integer".to_string())?;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--once" => {
                once = true;
                index += 1;
            }
            "--interval" => {
                interval = required_value(args, index, "--interval")?
                    .parse::<u64>()
                    .ok()
                    .filter(|value| *value > 0)
                    .ok_or_else(|| "--interval must be a positive integer".to_string())?;
                index += 2;
            }
            "--log-lines" => {
                log_lines = required_value(args, index, "--log-lines")?
                    .parse::<usize>()
                    .map_err(|_| "--log-lines must be a non-negative integer".to_string())?;
                index += 2;
            }
            "-h" | "--help" => {
                println!("Usage: multiagent watch [--once] [--interval SECONDS] [--log-lines N]");
                return Ok(ExitCode::SUCCESS);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    loop {
        if !once {
            print!("\x1b[H\x1b[2J");
        }
        print!("{}", render_dashboard(&cfg, log_lines));
        std::io::stdout()
            .flush()
            .map_err(io_error("flush dashboard"))?;
        if once {
            break;
        }
        thread::sleep(Duration::from_secs(interval));
    }
    Ok(ExitCode::SUCCESS)
}

fn render_dashboard(cfg: &RuntimeConfig, log_lines: usize) -> String {
    let snapshot = status_text().unwrap_or_else(|_| STATUS_HEADER.into());
    let rows = snapshot
        .lines()
        .skip(1)
        .map(|line| line.split('\t').map(str::to_string).collect::<Vec<_>>())
        .filter(|fields| fields.len() >= 11)
        .collect::<Vec<_>>();
    let mut result = format!(
        "Multiagent Dashboard\nSession: {}  Root: {}\nState: {}\nLogs: {}\nUpdated: {}\n\nAgent Status Summary\n",
        cfg.session,
        cfg.root.display(),
        cfg.state.display(),
        cfg.logs.display(),
        Local::now().format("%Y-%m-%d %H:%M:%S")
    );
    let mut counts = BTreeMap::<String, usize>::new();
    for row in &rows {
        *counts.entry(row[2].clone()).or_default() += 1;
    }
    if counts.is_empty() {
        result.push_str("none\t0\n");
    } else {
        for (status, count) in counts {
            result.push_str(&format!("{status}\t{count}\n"));
        }
    }
    result.push_str("\nAgents\n");
    if rows.is_empty() {
        result.push_str("none\n");
    } else {
        for row in &rows {
            result.push_str(&format!(
                "{:<9} {:<28} {:<10} {:<7} {}\n",
                row[0],
                row[1],
                row[2],
                row[3],
                truncate(&row[4], 90)
            ));
        }
    }
    result.push_str("\nBlocked Agents\n");
    let blocked = rows
        .iter()
        .filter(|row| row[2].to_ascii_lowercase().contains("blocked"))
        .collect::<Vec<_>>();
    if blocked.is_empty() {
        result.push_str("none\n");
    } else {
        for row in blocked {
            result.push_str(&format!(
                "{:<28} {:<16} {}\n",
                row[1],
                row[2],
                truncate(&row[4], 110)
            ));
        }
    }
    result.push_str("\nDAG Summary\n");
    let workflows = sorted_directories(&cfg.state.join("workflows")).unwrap_or_default();
    let mut any_workflow = false;
    let mut blocked_nodes = Vec::new();
    for dir in workflows {
        let nodes = dir.join("nodes.tsv");
        if !nodes.is_file() {
            continue;
        }
        any_workflow = true;
        let workflow_name = file_name(&dir).unwrap_or_default();
        result.push_str(&format!("{workflow_name}\n"));
        let mut node_counts = BTreeMap::<String, usize>::new();
        if let Ok(text) = fs::read_to_string(nodes) {
            for line in text.lines().skip(1) {
                let fields = line.split('\t').collect::<Vec<_>>();
                if fields.len() > 6 && !fields[6].is_empty() {
                    *node_counts.entry(fields[6].into()).or_default() += 1;
                    if matches!(fields[6], "blocked" | "failed") {
                        blocked_nodes.push(format!(
                            "{workflow_name}\t{}\t{}\t{}",
                            fields[0], fields[6], fields[1]
                        ));
                    }
                }
            }
        }
        for (status, count) in node_counts {
            result.push_str(&format!("  {status:<8} {count}\n"));
        }
    }
    if !any_workflow {
        result.push_str("No workflows found.\n");
    }
    result.push_str("\nBlocked DAG Nodes\n");
    if blocked_nodes.is_empty() {
        result.push_str("none\n");
    } else {
        for node in blocked_nodes {
            result.push_str(&format!("{node}\n"));
        }
    }
    result.push_str("\nOrchestrator Tail\n");
    let log = cfg.logs.join("orchestrator.log");
    if log_lines == 0 {
        result.push_str("(disabled)\n");
    } else if let Ok(text) = fs::read_to_string(log) {
        result.push_str(&tail_lines(&text, log_lines));
        if !result.ends_with('\n') {
            result.push('\n');
        }
    } else {
        result.push_str("No orchestrator log yet. Start with ./launch.sh or pipe the pane manually with tmux pipe-pane.\n");
    }
    result
}

pub fn subagent(args: &[String]) -> Result<ExitCode, String> {
    if args.is_empty() || matches!(args[0].as_str(), "-h" | "--help") {
        print_subagent_usage();
        return Ok(ExitCode::SUCCESS);
    }
    let cfg = RuntimeConfig::load()?;
    match args[0].as_str() {
        "spawn" => spawn(&cfg, &args[1..])?,
        "list" => list_subagents(&cfg, &args[1..])?,
        "poll" => {
            let name = one_name("poll", &args[1..])?;
            poll(&cfg, name, true)?;
        }
        "wait" => wait(&cfg, &args[1..])?,
        "inspect" => inspect(&cfg, &args[1..])?,
        "recover-plan" => recover_plan(&cfg, &args[1..])?,
        "restore" => restore(&cfg, &args[1..])?,
        "restore-all" => restore_all(&cfg, &args[1..])?,
        "finalize" => finalize(&cfg, &args[1..])?,
        "kill" => kill(&cfg, &args[1..])?,
        command => return Err(format!("unknown command: {command}")),
    }
    Ok(ExitCode::SUCCESS)
}

fn print_subagent_usage() {
    println!(
        "Usage:\n  multiagent subagent spawn NAME [--own PATH[,PATH...] ...] [--role ROLE] [--instruction TEXT | --instruction-file PATH | -- TEXT]\n  multiagent subagent list|recover-plan|restore-all|gate-check\n  multiagent subagent poll|inspect|restore|finalize|kill NAME [OPTIONS]\n  multiagent subagent wait NAME [--timeout SECONDS] [--poll-interval SECONDS]\n\nAll durable state and tmux subprocess orchestration are implemented by the Rust CLI."
    );
}

fn spawn(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "spawn requires NAME".to_string())?;
    validate_name(name)?;
    let mut instruction = String::new();
    let mut instruction_file = None::<PathBuf>;
    let mut owned = Vec::new();
    let mut role = String::new();
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--own" | "--owned-path" => {
                owned.push(required_value(args, index, "spawn --own")?.to_string());
                index += 2;
            }
            "--role" => {
                role = required_value(args, index, "spawn --role")?.to_string();
                if !matches!(role.as_str(), "worker" | "verifier" | "reviewer" | "scout") {
                    return Err("spawn --role must be worker, verifier, reviewer, or scout".into());
                }
                index += 2;
            }
            "--instruction" => {
                instruction = required_value(args, index, "spawn --instruction")?.to_string();
                index += 2;
            }
            "--instruction-file" => {
                instruction_file = Some(PathBuf::from(required_value(
                    args,
                    index,
                    "spawn --instruction-file",
                )?));
                index += 2;
            }
            "--" => {
                if index + 1 >= args.len() {
                    return Err("spawn -- requires instruction text".into());
                }
                instruction = args[index + 1..].join(" ");
                break;
            }
            "-h" | "--help" => {
                print_subagent_usage();
                return Ok(());
            }
            other => return Err(format!("unknown spawn argument: {other}")),
        }
    }
    if !instruction.is_empty() && instruction_file.is_some() {
        return Err("spawn accepts only one of --instruction or --instruction-file".into());
    }
    if let Some(path) = &instruction_file {
        if !path.is_file() {
            return Err(format!("instruction file not found: {}", path.display()));
        }
        instruction = fs::read_to_string(path).map_err(io_error("read instruction file"))?;
    }
    if cfg.code_exec && cfg.subagent_cli == "codex" && instruction.is_empty() {
        return Err(format!(
            "codex exec subagent spawn requires --instruction or --instruction-file: {name}"
        ));
    }
    instruction = compose_role_instruction(cfg, name, &role, &instruction)?;
    instruction = append_verifier_diff_binding(cfg, name, &role, &instruction)?;
    let assignment_role = assignment_role_for_spawn(cfg, name, &role);
    let access = codex_access_for_spawn(cfg, name, &role);

    require_command("tmux")?;
    let cli = &cfg.subagent_cli;
    let binary = cfg.cli_bin(cli)?;
    require_command(binary)?;
    if !tmux_success(&["has-session", "-t", &cfg.session]) {
        return Err(format!("missing tmux session: {}", cfg.session));
    }
    if window_exists(&cfg.session, name) {
        return Err(format!("subagent window already exists: {name}"));
    }
    reject_parallel_generic_worker_spawn(cfg, name)?;
    if !owned.is_empty() {
        let assignment_dir = cfg.state.join("assignments").join(name);
        if assignment_dir.join("assignment.env").is_file() {
            let allowed = fs::read_to_string(assignment_dir.join("owned-paths"))
                .map_err(io_error("read assignment owned paths"))?
                .lines()
                .map(str::to_string)
                .collect::<Vec<_>>();
            for raw in &owned {
                for requested in csv_values(raw) {
                    let normalized = normalize_repo_path(&cfg.root, &requested)?;
                    if !allowed.iter().any(|path| {
                        normalized == *path || normalized.starts_with(&format!("{path}/"))
                    }) {
                        return Err(format!(
                            "spawn requested path outside existing assignment: agent={name} path={normalized}"
                        ));
                    }
                }
            }
        } else {
            let branch = git_text(&cfg.root, &["rev-parse", "--abbrev-ref", "HEAD"])?;
            let joined = owned.join(",");
            run_self_quiet(&[
                "subagent",
                "assignment-create",
                name,
                "--assignment-id",
                &format!("spawn-{name}"),
                "--branch",
                &branch,
                "--owned",
                &joined,
                "--role",
                assignment_role,
            ])?;
        }
    }
    validate_implementation_context(cfg, name, instruction_file.as_deref(), &instruction)?;

    let dir = cfg.state.join("subagents").join(name);
    fs::create_dir_all(&dir).map_err(io_error("create subagent state"))?;
    fs::create_dir_all(&cfg.logs).map_err(io_error("create subagent log directory"))?;
    let executable = env::current_exe().map_err(io_error("resolve multiagent executable"))?;
    let metadata = format!(
        "name={name}\nsession={}\nroot={}\nrole={}\ncodex_access={}\nworkflow_id={}\nwrite_policy={}\nlog_file={}\ncli={cli}\ncli_bin={binary}\nhelper={}\ncreated_at={}\n",
        cfg.session,
        cfg.root.display(),
        if role.is_empty() { assignment_role } else { &role },
        access.sandbox(),
        env_nonempty("MULTIAGENT_WORKFLOW_ID").unwrap_or_default(),
        cfg.policy.display(),
        cfg.logs.join(format!("{name}.log")).display(),
        executable.display(),
        timestamp()
    );
    atomic_write(&dir.join("meta.env"), &metadata, "subagent metadata")?;
    set_subagent_status(cfg, name, "starting")?;

    let mut prompt_file = None;
    let output_file = dir.join("last-message.txt");
    if cfg.code_exec && cli == "codex" && !instruction.is_empty() {
        let path = dir.join("instruction.txt");
        let prompt = format!("{}{}\n", codex_exec_protocol_prelude(), instruction);
        atomic_write(&path, &prompt, "subagent instruction")?;
        append_file(
            &dir.join("transcript.log"),
            &format!("\n----- instruction {} -----\n{prompt}", timestamp()),
        )?;
        prompt_file = Some(path);
    }
    let cli_command = if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        if cli != "codex" || !cfg.code_exec {
            return Err("UID role isolation requires codex exec subagents".into());
        }
        format!(
            "{} role-agent-exec {}",
            shell_escape(&executable.display().to_string()),
            shell_escape(name)
        )
    } else {
        let command = build_cli_command(
            cli,
            &cfg.root,
            prompt_file.as_deref(),
            Some(&output_file),
            &cfg.codex_bin,
            &cfg.claude_bin,
            cfg.code_exec,
            access,
        )?;
        wrap_linux_role_sandbox(
            &command,
            &executable,
            role_write_roots(&cfg.root, &cfg.state, access == CodexAccess::WorkspaceWrite),
            if access == CodexAccess::WorkspaceWrite {
                WRITER_UID
            } else {
                READER_UID
            },
        )
    };
    let command = subagent_shell_command(cfg, name, cli, &executable, &cli_command, access, false);
    tmux_checked(&["new-window", "-d", "-t", &cfg.session, "-n", name, &command])?;
    pipe_log(&cfg.session, name, &cfg.logs)?;
    set_subagent_status(cfg, name, "running")?;
    if cfg
        .state
        .join("assignments")
        .join(name)
        .join("assignment.env")
        .is_file()
    {
        run_self_quiet(&["subagent", "assignment-status", name, "running"])?;
    }
    let _ = capture_subagent(cfg, name);
    if !(instruction.is_empty() || cfg.code_exec && cli == "codex") {
        deliver_instruction(cfg, name, &instruction)?;
    }
    println!("spawned {name}");
    Ok(())
}

fn list_subagents(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    if !args.is_empty() {
        return Err("list takes no arguments".into());
    }
    for dir in sorted_directories(&cfg.state.join("subagents"))? {
        let name = file_name(&dir)?;
        let status = read_trimmed(&dir.join("status")).unwrap_or_else(|| "unknown".into());
        println!(
            "{name}\t{status}\t{}",
            if window_exists(&cfg.session, &name) {
                "open"
            } else {
                "closed"
            }
        );
    }
    Ok(())
}

fn poll(cfg: &RuntimeConfig, name: &str, report: bool) -> Result<(), String> {
    validate_name(name)?;
    require_command("tmux")?;
    if capture_subagent(cfg, name).is_ok() {
        let status = infer_status(cfg, name);
        set_subagent_status(cfg, name, &status)?;
        if report {
            println!("{name}\t{status}");
        }
        Ok(())
    } else {
        set_subagent_status(cfg, name, "missing")?;
        Err(format!("could not capture subagent: {name}"))
    }
}

fn wait(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "wait requires NAME".to_string())?;
    validate_name(name)?;
    let mut timeout = 900.0f64;
    let mut interval = 1.0f64;
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--timeout" => {
                timeout = required_value(args, index, "wait --timeout")?
                    .parse()
                    .map_err(|_| "wait --timeout must be a non-negative number".to_string())?;
                index += 2;
            }
            "--poll-interval" => {
                interval = required_value(args, index, "wait --poll-interval")?
                    .parse()
                    .map_err(|_| {
                        "wait --poll-interval must be a non-negative number".to_string()
                    })?;
                index += 2;
            }
            other => return Err(format!("unknown wait argument: {other}")),
        }
    }
    if !timeout.is_finite() || timeout < 0.0 {
        return Err("wait --timeout must be a non-negative number".into());
    }
    if !interval.is_finite() || interval < 0.0 {
        return Err("wait --poll-interval must be a non-negative number".into());
    }

    let deadline = Instant::now() + Duration::from_secs_f64(timeout);
    loop {
        poll(cfg, name, false)?;
        let status = read_trimmed(&cfg.state.join("subagents").join(name).join("status"))
            .unwrap_or_else(|| "unknown".into());
        if matches!(
            status.as_str(),
            "done" | "failed" | "blocked" | "exited" | "finalized" | "killed" | "missing"
        ) {
            println!("{name}\t{status}");
            return Ok(());
        }
        if Instant::now() >= deadline {
            println!("{name}\t{status}");
            return Err(format!(
                "timed out after {timeout} seconds waiting for subagent: {name}"
            ));
        }
        thread::sleep(Duration::from_secs_f64(interval));
    }
}

fn inspect(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "inspect requires NAME".to_string())?;
    validate_name(name)?;
    let mut lines = 120usize;
    let mut index = 1;
    while index < args.len() {
        match args[index].as_str() {
            "--lines" => {
                lines = required_value(args, index, "--lines")?
                    .parse()
                    .map_err(|_| "--lines must be a non-negative integer".to_string())?;
                index += 2;
            }
            other => return Err(format!("unknown inspect argument: {other}")),
        }
    }
    if window_exists(&cfg.session, name) {
        let _ = capture_subagent(cfg, name);
    }
    let current = cfg.state.join("subagents").join(name).join("current.txt");
    let text = fs::read_to_string(&current)
        .map_err(|_| format!("no captured output for subagent: {name}"))?;
    print!("{}", tail_lines(&text, lines));
    Ok(())
}

#[derive(Clone)]
struct Recovery {
    name: String,
    action: String,
    reason: String,
    status: String,
    window: String,
    dir: PathBuf,
}

impl Recovery {
    fn row(&self) -> String {
        format!(
            "{}\t{}\t{}\t{}\t{}\t{}",
            self.name,
            self.action,
            self.reason,
            self.status,
            self.window,
            self.dir.display()
        )
    }
}

fn recover_plan(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    if !args.is_empty() {
        return Err("recover-plan takes no arguments".into());
    }
    println!("NAME\tACTION\tREASON\tSTATUS\tWINDOW\tSTATE_DIR");
    for dir in sorted_directories(&cfg.state.join("subagents"))? {
        let name = file_name(&dir)?;
        println!("{}", classify_recovery(cfg, &name)?.row());
    }
    Ok(())
}

fn classify_recovery(cfg: &RuntimeConfig, name: &str) -> Result<Recovery, String> {
    validate_name(name)?;
    let dir = cfg.state.join("subagents").join(name);
    let status = read_trimmed(&dir.join("status")).unwrap_or_else(|| "unknown".into());
    let lowered = status.to_ascii_lowercase();
    let window = if window_exists(&cfg.session, name) {
        "open"
    } else {
        "closed"
    };
    let (action, reason): (&str, String) = if window == "open" {
        ("skip-open", "tmux-window-already-open".into())
    } else if !dir.is_dir() {
        ("skip-unknown", "missing-state-dir".into())
    } else if matches!(
        lowered.as_str(),
        "finalized" | "done" | "complete" | "completed"
    ) {
        ("skip-finalized", format!("status-{lowered}"))
    } else if matches!(
        lowered.as_str(),
        "killed" | "stopped" | "cancelled" | "canceled"
    ) {
        ("skip-finalized", format!("intentionally-stopped-{lowered}"))
    } else if cfg
        .state
        .join("assignments")
        .join(name)
        .join("checkpoint.env")
        .is_file()
    {
        let checkpoint = read_env(
            &cfg.state
                .join("assignments")
                .join(name)
                .join("checkpoint.env"),
        )?;
        let checkpoint_status = checkpoint
            .get("status")
            .map(String::as_str)
            .unwrap_or("")
            .to_ascii_lowercase();
        let blocker = checkpoint.get("blocker").map(String::as_str).unwrap_or("");
        if !blocker.is_empty() || checkpoint_status == "blocked" {
            ("skip-blocked", "checkpoint-blocked".into())
        } else if matches!(
            checkpoint_status.as_str(),
            "done" | "complete" | "completed" | "finalized"
        ) {
            ("skip-finalized", format!("checkpoint-{checkpoint_status}"))
        } else if !has_recovery_context(&dir) {
            ("skip-unknown", "checkpoint-without-captured-context".into())
        } else {
            ("restore", "checkpoint-resumable".into())
        }
    } else {
        let combined = recovery_text(&dir);
        if lowered == "blocked" || looks_blocked_report(&combined) {
            ("skip-blocked", "requires-orchestrator-decision".into())
        } else if looks_done_report(&combined) {
            ("skip-finalized", "context-looks-final".into())
        } else if !has_recovery_context(&dir) {
            ("skip-unknown", "no-current-or-transcript".into())
        } else if matches!(
            lowered.as_str(),
            "running" | "starting" | "exited" | "missing" | "restoring" | "unknown"
        ) {
            ("restore", "closed-with-recoverable-context".into())
        } else {
            ("skip-unknown", format!("unrecognized-status-{lowered}"))
        }
    };
    Ok(Recovery {
        name: name.into(),
        action: action.into(),
        reason,
        status,
        window: window.into(),
        dir,
    })
}

fn restore(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "restore requires NAME".to_string())?;
    validate_name(name)?;
    let force = match &args[1..] {
        [] => false,
        [value] if value == "--force" => true,
        [value, ..] => return Err(format!("unknown restore argument: {value}")),
    };
    require_command("tmux")?;
    let dir = cfg.state.join("subagents").join(name);
    if !dir.is_dir() {
        return Err(format!("no persisted subagent state: {name}"));
    }
    let metadata = read_env(&dir.join("meta.env")).unwrap_or_default();
    let cli = metadata
        .get("cli")
        .filter(|value| !value.is_empty())
        .cloned()
        .unwrap_or_else(|| cfg.subagent_cli.clone());
    validate_cli(&cli)?;
    let access = match metadata.get("codex_access").map(String::as_str) {
        Some("read-only") => CodexAccess::ReadOnly,
        _ => CodexAccess::WorkspaceWrite,
    };
    let binary = cfg.cli_bin(&cli)?;
    require_command(binary)?;
    if !tmux_success(&["has-session", "-t", &cfg.session]) {
        return Err(format!("missing tmux session: {}", cfg.session));
    }
    let plan = classify_recovery(cfg, name)?;
    if plan.action != "restore" && !force {
        return Err(format!(
            "refusing to restore {name}: {} ({}); use --force only after an explicit orchestrator/user decision",
            plan.action, plan.reason
        ));
    }
    if plan.window == "open" {
        return Err(format!("subagent window already exists: {name}"));
    }
    if !has_recovery_context(&dir) {
        return Err(format!("no captured context to restore: {name}"));
    }
    let mut instruction = format!(
        "You are a restored long-running subagent.\n\nRestoration details:\n- Subagent name: {name}\n- Prior persisted status: {}\n- Persisted state directory: {}\n- This is a fresh tmux window after an orchestrator/session recovery.\n- Do not delete, overwrite, or reset prior memory in the state directory.\n- Read the prior context below, continue only if the assignment is still valid, and report progress/final status in this tmux window.\n- If the prior state shows completion, intentional stop, stale instructions, or a blocker that needs orchestrator/user input, stop and state what you need instead of guessing.\n\nConcise prior context:\n{}\n",
        plan.status,
        dir.display(),
        recovery_text(&dir)
    );
    if let Some(context) = implementation_context(cfg, name)? {
        instruction.push_str("\n## Approved Implementation Context\n\n");
        instruction.push_str(
            &fs::read_to_string(context).map_err(io_error("read implementation context"))?,
        );
    }
    append_file(
        &dir.join("restore_events.log"),
        &format!(
            "{} prior_status={} action={} reason={} force={} cli={}\n",
            timestamp(),
            plan.status,
            plan.action,
            plan.reason,
            u8::from(force),
            cli
        ),
    )?;
    append_file(
        &dir.join("transcript.log"),
        &format!(
            "\n----- restore seed {} -----\n{instruction}\n",
            timestamp()
        ),
    )?;
    set_subagent_status(cfg, name, "restoring")?;
    fs::create_dir_all(&cfg.logs).map_err(io_error("create log directory"))?;
    let output_file = dir.join("last-message.txt");
    let prompt_file = if cfg.code_exec && cli == "codex" {
        let path = dir.join("restore-instruction.txt");
        atomic_write(&path, &instruction, "restore instruction")?;
        Some(path)
    } else {
        None
    };
    let executable = env::current_exe().map_err(io_error("resolve multiagent executable"))?;
    let cli_command = if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        if cli != "codex" || !cfg.code_exec {
            return Err("UID role isolation requires codex exec subagents".into());
        }
        format!(
            "{} role-agent-exec {} --restore",
            shell_escape(&executable.display().to_string()),
            shell_escape(name)
        )
    } else {
        let command = build_cli_command(
            &cli,
            &cfg.root,
            prompt_file.as_deref(),
            Some(&output_file),
            &cfg.codex_bin,
            &cfg.claude_bin,
            cfg.code_exec,
            access,
        )?;
        wrap_linux_role_sandbox(
            &command,
            &executable,
            role_write_roots(&cfg.root, &cfg.state, access == CodexAccess::WorkspaceWrite),
            if access == CodexAccess::WorkspaceWrite {
                WRITER_UID
            } else {
                READER_UID
            },
        )
    };
    let command = subagent_shell_command(cfg, name, &cli, &executable, &cli_command, access, true);
    tmux_checked(&["new-window", "-d", "-t", &cfg.session, "-n", name, &command])?;
    pipe_log(&cfg.session, name, &cfg.logs)?;
    set_subagent_status(cfg, name, "running")?;
    if !(cfg.code_exec && cli == "codex") {
        deliver_instruction(cfg, name, &instruction)?;
    }
    println!("restored {name}");
    Ok(())
}

fn restore_all(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    if !args.is_empty() {
        return Err("restore-all takes no arguments".into());
    }
    let mut restored = 0;
    let mut skipped = 0;
    for dir in sorted_directories(&cfg.state.join("subagents"))? {
        let name = file_name(&dir)?;
        let plan = classify_recovery(cfg, &name)?;
        if plan.action == "restore" {
            restore(cfg, &[name])?;
            restored += 1;
        } else {
            println!("skipped {}\t{}", plan.name, plan.action);
            skipped += 1;
        }
    }
    println!("restore-all complete: restored={restored} skipped={skipped}");
    Ok(())
}

fn finalize(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "finalize requires NAME".to_string())?;
    validate_name(name)?;
    let keep = match &args[1..] {
        [] => false,
        [value] if value == "--keep-window" => true,
        [value, ..] => return Err(format!("unknown finalize argument: {value}")),
    };
    if window_exists(&cfg.session, name) {
        let _ = capture_subagent(cfg, name);
        if !keep {
            tmux_checked(&["kill-window", "-t", &format!("{}:{name}", cfg.session)])?;
        }
    }
    set_subagent_status(cfg, name, "finalized")?;
    if cfg
        .state
        .join("assignments")
        .join(name)
        .join("assignment.env")
        .is_file()
    {
        run_self_quiet(&["subagent", "assignment-status", name, "done"])?;
    }
    atomic_write(
        &cfg.state.join("subagents").join(name).join("finalized_at"),
        &format!("{}\n", timestamp()),
        "finalized timestamp",
    )?;
    println!("finalized {name}");
    Ok(())
}

fn kill(cfg: &RuntimeConfig, args: &[String]) -> Result<(), String> {
    let name = one_name("kill", args)?;
    require_command("tmux")?;
    let supervisor_pid = read_supervisor_pid(cfg, name);
    if window_exists(&cfg.session, name) {
        let _ = capture_subagent(cfg, name);
        tmux_checked(&["kill-window", "-t", &format!("{}:{name}", cfg.session)])?;
    }
    if let Some(pid) = supervisor_pid {
        wait_for_process_exit(pid, name)?;
    }
    set_subagent_status(cfg, name, "killed")?;
    if cfg
        .state
        .join("assignments")
        .join(name)
        .join("assignment.env")
        .is_file()
    {
        run_self_quiet(&["subagent", "assignment-status", name, "failed"])?;
    }
    println!("killed {name}");
    Ok(())
}

fn read_supervisor_pid(cfg: &RuntimeConfig, name: &str) -> Option<u32> {
    read_trimmed(
        &cfg.state
            .join("subagents")
            .join(name)
            .join("supervisor.pid"),
    )
    .and_then(|value| value.parse().ok())
}

#[cfg(unix)]
fn wait_for_process_exit(pid: u32, name: &str) -> Result<(), String> {
    for _ in 0..100 {
        let alive = unsafe { libc::kill(pid as libc::pid_t, 0) } == 0
            || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
        if !alive {
            return Ok(());
        }
        thread::sleep(Duration::from_millis(50));
    }
    Err(format!(
        "subagent process did not exit after pane close: {name} pid={pid}"
    ))
}

#[cfg(not(unix))]
fn wait_for_process_exit(_pid: u32, _name: &str) -> Result<(), String> {
    Ok(())
}

fn compose_role_instruction(
    cfg: &RuntimeConfig,
    name: &str,
    role: &str,
    instruction: &str,
) -> Result<String, String> {
    let Some(path) = role_prompt_path(cfg, name, role) else {
        return Ok(instruction.into());
    };
    if !path.is_file() {
        return Ok(instruction.into());
    }
    let prompt = fs::read_to_string(&path).map_err(io_error("read role prompt"))?;
    let heading = prompt.lines().next().unwrap_or("");
    if !heading.is_empty() && instruction.contains(heading) {
        return Ok(instruction.into());
    }
    Ok(format!("{prompt}\n\n## Task Assignment\n\n{instruction}"))
}

fn role_prompt_path(cfg: &RuntimeConfig, name: &str, role: &str) -> Option<PathBuf> {
    let lower = name.to_ascii_lowercase();
    let relative = if lower.contains("decision-authority-reviewer") {
        "prompts/roles/decision-authority-reviewer.md"
    } else if lower.contains("build-verifier") {
        "prompts/roles/build-verifier.md"
    } else if matches!(role, "verifier" | "reviewer")
        || lower.contains("verifier")
        || lower.contains("review")
    {
        "prompts/verifier.md"
    } else if lower.contains("acceptance-scout") {
        "prompts/roles/acceptance-scout.md"
    } else if lower.contains("contract-scout") || role == "scout" {
        "prompts/roles/contract-scout.md"
    } else if role == "worker" || lower.starts_with("worker-") {
        "prompts/worker.md"
    } else {
        return None;
    };
    Some(cfg.prompt_root.join(relative))
}

fn assignment_role_for_spawn<'a>(cfg: &RuntimeConfig, name: &str, role: &'a str) -> &'a str {
    match role {
        "verifier" | "reviewer" => "verifier",
        "scout" => "scout",
        _ => match role_prompt_path(cfg, name, role)
            .and_then(|path| {
                path.file_name()
                    .map(|value| value.to_string_lossy().to_string())
            })
            .as_deref()
        {
            Some("verifier.md" | "build-verifier.md") => "verifier",
            Some("acceptance-scout.md" | "contract-scout.md") => "scout",
            _ => "exploitation",
        },
    }
}

fn codex_access_for_spawn(cfg: &RuntimeConfig, name: &str, role: &str) -> CodexAccess {
    let lower = name.to_ascii_lowercase();
    let prompt = role_prompt_path(cfg, name, role).and_then(|path| {
        path.file_name()
            .map(|value| value.to_string_lossy().to_string())
    });
    if role == "reviewer"
        || role == "scout"
        || lower.contains("decision-authority-reviewer")
        || matches!(
            prompt.as_deref(),
            Some(
                "acceptance-scout.md"
                    | "contract-scout.md"
                    | "decision-authority-reviewer.md"
                    | "scope-guard.md"
                    | "validation-coordinator.md"
            )
        )
    {
        CodexAccess::ReadOnly
    } else {
        // Workers need source writes. Technical/build verifiers retain workspace
        // writes because repository-local compilers and test runners commonly
        // create build artifacts; their role prompt still forbids source edits.
        CodexAccess::WorkspaceWrite
    }
}

fn append_verifier_diff_binding(
    cfg: &RuntimeConfig,
    name: &str,
    role: &str,
    instruction: &str,
) -> Result<String, String> {
    let Some(role_prompt) = role_prompt_path(cfg, name, role) else {
        return Ok(instruction.into());
    };
    let file = role_prompt
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    if !matches!(file, "verifier.md" | "build-verifier.md") {
        return Ok(instruction.into());
    }
    let diff = git_bytes(
        &cfg.root,
        &["diff", "--binary", "--ignore-submodules=all", "HEAD"],
    )?;
    let changed = git_text(&cfg.root, &["diff", "--name-only", "HEAD"])?
        .lines()
        .filter(|line| !line.is_empty())
        .count();
    if changed == 0 {
        return Ok(instruction.into());
    }
    let hash = format!("{:x}", Sha256::digest(diff));
    let marker = if file == "build-verifier.md" {
        "build-verification-passed:"
    } else {
        "behavior-verification-passed:"
    };
    Ok(format!(
        "{instruction}\n\n## Spawn-Time Final Diff Binding\n\nfinal-diff-sha256={hash}\nchanged-files={changed}\nAcceptance must repeat this hash in `{marker}` after rechecking the live diff.\n"
    ))
}

fn validate_implementation_context(
    cfg: &RuntimeConfig,
    name: &str,
    instruction_file: Option<&Path>,
    instruction: &str,
) -> Result<(), String> {
    let Some(context) = implementation_context(cfg, name)? else {
        return Ok(());
    };
    if instruction_file.is_none() {
        return Err("lifecycle-enforced exploitation spawn requires --instruction-file with the complete approved implementation context".into());
    }
    let required = fs::read_to_string(context).map_err(io_error("read implementation context"))?;
    if required.is_empty() || !instruction.contains(&required) {
        return Err("exploitation instruction does not contain the complete approved implementation context".into());
    }
    Ok(())
}

fn implementation_context(cfg: &RuntimeConfig, name: &str) -> Result<Option<PathBuf>, String> {
    if !config::lifecycle_enforced() {
        return Ok(None);
    }
    let meta_path = cfg
        .state
        .join("assignments")
        .join(name)
        .join("assignment.env");
    if !meta_path.is_file() {
        return Ok(None);
    }
    let meta = read_env(&meta_path)?;
    if meta.get("role").map(String::as_str) != Some("exploitation") {
        return Ok(None);
    }
    let workflow_id = required_env_field(
        &meta,
        "workflow_id",
        "lifecycle enforcement requires --workflow-id for exploitation assignments",
    )?;
    let decision_id = required_env_field(
        &meta,
        "decision_id",
        "lifecycle enforcement requires --decision-id for exploitation assignments",
    )?;
    let plan_id = required_env_field(
        &meta,
        "plan_id",
        "lifecycle enforcement requires --plan-id for exploitation assignments",
    )?;
    run_self_quiet(&[
        "workflow",
        "gate",
        workflow_id,
        "implementation",
        "--decision-id",
        decision_id,
        "--plan-id",
        plan_id,
    ])
    .map_err(|_| {
        format!("workflow implementation gate rejected assignment for workflow {workflow_id}")
    })?;
    let current = run_self_output(&["workflow", "value", workflow_id, "decision_revision"])?;
    let revision = String::from_utf8_lossy(&current.stdout).trim().to_string();
    let assigned_revision = meta
        .get("decision_revision")
        .map(String::as_str)
        .unwrap_or("");
    if assigned_revision.is_empty() || assigned_revision != revision {
        return Err(format!(
            "assignment decision revision is stale: assignment={} workflow={revision}",
            if assigned_revision.is_empty() {
                "missing"
            } else {
                assigned_revision
            }
        ));
    }
    let path = PathBuf::from(
        meta.get("implementation_context")
            .cloned()
            .unwrap_or_default(),
    );
    if !path.is_file() {
        return Err(format!(
            "assignment approved implementation context is missing: {}",
            path.display()
        ));
    }
    Ok(Some(path))
}

fn reject_parallel_generic_worker_spawn(cfg: &RuntimeConfig, name: &str) -> Result<(), String> {
    if env::var("MULTIAGENT_ALLOW_PARALLEL_WORKERS").as_deref() == Ok("1")
        || !name.starts_with("worker-")
    {
        return Ok(());
    }
    for dir in sorted_directories(&cfg.state.join("subagents"))? {
        let existing = file_name(&dir)?;
        if existing == name || !existing.starts_with("worker-") {
            continue;
        }
        let status = read_trimmed(&dir.join("status")).unwrap_or_else(|| "unknown".into());
        if matches!(status.as_str(), "starting" | "running" | "restoring")
            && window_exists(&cfg.session, &existing)
        {
            return Err(format!("active generic worker already running: existing={existing} status={status}; wait, finalize/kill it, or set MULTIAGENT_ALLOW_PARALLEL_WORKERS=1 only with explicit disjoint ownership"));
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn build_cli_command(
    cli: &str,
    cwd: &Path,
    prompt: Option<&Path>,
    output: Option<&Path>,
    codex_bin: &str,
    claude_bin: &str,
    codex_exec: bool,
    access: CodexAccess,
) -> Result<String, String> {
    match cli {
        "codex" if codex_exec => {
            let mut command = format!(
                "{} exec --cd {} --skip-git-repo-check {}",
                shell_escape(codex_bin),
                shell_escape(&cwd.display().to_string()),
                codex_safety_args(access, true),
            );
            if let Some(path) = output {
                command.push_str(&format!(
                    " --output-last-message {}",
                    shell_escape(&path.display().to_string())
                ));
            }
            if let Some(path) = prompt {
                command.push_str(&format!(
                    " - < {}",
                    shell_escape(&path.display().to_string())
                ));
            }
            Ok(command)
        }
        "codex" => {
            let mut command = format!(
                "{} --cd {} {} --no-alt-screen",
                shell_escape(codex_bin),
                shell_escape(&cwd.display().to_string()),
                codex_safety_args(access, false),
            );
            if let Some(path) = prompt {
                command.push_str(&format!(
                    " \"$(cat {})\"",
                    shell_escape(&path.display().to_string())
                ));
            }
            Ok(command)
        }
        "claude" => {
            let mut command = format!(
                "{} --dangerously-skip-permissions",
                shell_escape(claude_bin)
            );
            if let Some(path) = prompt {
                command.push_str(&format!(
                    " \"$(cat {})\"",
                    shell_escape(&path.display().to_string())
                ));
            }
            Ok(command)
        }
        _ => Err(format!(
            "unsupported CLI '{cli}' (expected codex or claude)"
        )),
    }
}

#[cfg(target_os = "linux")]
fn codex_safety_args(_access: CodexAccess, _exec: bool) -> String {
    // Docker's default seccomp profile blocks the user namespaces required by
    // Codex/bubblewrap. The enclosing role-exec Landlock boundary is inherited
    // by Codex and every model-generated child process, so Codex itself must not
    // attempt a second sandbox.
    "--dangerously-bypass-approvals-and-sandbox".into()
}

#[cfg(not(target_os = "linux"))]
fn codex_safety_args(access: CodexAccess, exec: bool) -> String {
    if exec {
        format!("--sandbox {} -c approval_policy=never", access.sandbox())
    } else {
        format!("--sandbox {} --ask-for-approval never", access.sandbox())
    }
}

fn role_write_roots(root: &Path, state: &Path, include_source: bool) -> Vec<PathBuf> {
    let mut paths = BTreeSet::from([state.to_path_buf()]);
    if include_source {
        paths.insert(root.to_path_buf());
    }
    for key in [
        "CODEX_HOME",
        "GOCACHE",
        "GOMODCACHE",
        "MULTIAGENT_ROLE_SHARED_WRITE_DIR",
    ] {
        if let Some(path) = env_path(key) {
            if path.exists() {
                paths.insert(path);
            }
        }
    }
    for path in [PathBuf::from("/dev/null"), PathBuf::from("/dev/tty")] {
        if path.exists() {
            paths.insert(path);
        }
    }
    paths.into_iter().collect()
}

#[cfg(target_os = "linux")]
fn wrap_linux_role_sandbox(
    command: &str,
    executable: &Path,
    write_roots: Vec<PathBuf>,
    _uid: u32,
) -> String {
    let allowances = write_roots
        .into_iter()
        .map(|path| {
            format!(
                "--allow-write {}",
                shell_escape(&path.display().to_string())
            )
        })
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "{} role-exec {allowances} -- /bin/sh -c {}",
        shell_escape(&executable.display().to_string()),
        shell_escape(command)
    )
}

#[cfg(target_os = "linux")]
fn prepare_uid_state_permissions(state: &Path) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;

    fn visit(path: &Path) -> Result<(), String> {
        let metadata = fs::symlink_metadata(path).map_err(io_error("inspect uid sandbox path"))?;
        chown_path(path, ORCHESTRATOR_UID, ROLE_GID)?;
        if metadata.is_dir() {
            fs::set_permissions(path, fs::Permissions::from_mode(0o2770))
                .map_err(io_error("set uid sandbox directory permissions"))?;
            for entry in fs::read_dir(path).map_err(io_error("read uid sandbox directory"))? {
                visit(&entry.map_err(io_error("read uid sandbox entry"))?.path())?;
            }
        } else if metadata.is_file() {
            let executable = metadata.permissions().mode() & 0o111 != 0;
            fs::set_permissions(
                path,
                fs::Permissions::from_mode(if executable { 0o770 } else { 0o660 }),
            )
            .map_err(io_error("set uid sandbox file permissions"))?;
        }
        Ok(())
    }

    visit(state)
}

#[cfg(not(target_os = "linux"))]
fn prepare_uid_state_permissions(_state: &Path) -> Result<(), String> {
    Err("MULTIAGENT_UID_SANDBOX is only supported on Linux".into())
}

#[cfg(target_os = "linux")]
fn chown_path(path: &Path, uid: u32, gid: u32) -> Result<(), String> {
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
fn wrap_linux_role_sandbox(
    command: &str,
    _executable: &Path,
    _write_roots: Vec<PathBuf>,
    _uid: u32,
) -> String {
    command.into()
}

fn subagent_shell_command(
    cfg: &RuntimeConfig,
    name: &str,
    cli: &str,
    executable: &Path,
    cli_command: &str,
    access: CodexAccess,
    restored: bool,
) -> String {
    let workflow_id = env_nonempty("MULTIAGENT_WORKFLOW_ID").unwrap_or_default();
    let lifecycle = u8::from(config::lifecycle_enforced()).to_string();
    let path = env::var("PATH").unwrap_or_default();
    let mut values = vec![
        ("MULTIAGENT_SESSION", cfg.session.clone()),
        ("MULTIAGENT_ROOT", cfg.root.display().to_string()),
        ("MULTIAGENT_STATE_DIR", cfg.state.display().to_string()),
        ("MULTIAGENT_LOG_DIR", cfg.logs.display().to_string()),
        ("MULTIAGENT_WRITE_POLICY", cfg.policy.display().to_string()),
        ("MULTIAGENT_WORKFLOW_ID", workflow_id),
        ("MULTIAGENT_LIFECYCLE_ENFORCEMENT", lifecycle),
        ("MULTIAGENT_SUBAGENT_NAME", name.into()),
        ("MULTIAGENT_BIN", executable.display().to_string()),
        ("WORKER_CLI", cfg.worker_cli.clone()),
        ("SUBAGENT_CLI", cli.into()),
        ("VERIFIER_CLI", cfg.verifier_cli.clone()),
        ("CODEX_BIN", cfg.codex_bin.clone()),
        ("CLAUDE_BIN", cfg.claude_bin.clone()),
        ("MULTIAGENT_CODEX_EXEC", u8::from(cfg.code_exec).to_string()),
        ("PATH", path),
    ];
    if restored {
        values.push(("MULTIAGENT_SUBAGENT_RESTORED", "1".into()));
    }
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") {
        if let Some(root) = env_nonempty("MULTIAGENT_CODEX_HOME_ROOT") {
            let role = if access == CodexAccess::WorkspaceWrite {
                "writer"
            } else {
                "reader"
            };
            let home = Path::new(&root).join(role).display().to_string();
            values.push(("CODEX_HOME", home.clone()));
            values.push(("HOME", home));
        }
    }
    let exports = values
        .into_iter()
        .map(|(key, value)| format!("{key}={}", shell_escape(&value)))
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "cd {} && umask 0007 && export {exports} && {cli_command}; rc=$?; printf '\\nfinal status: codex exec exited rc=%s\\n' $rc; sleep infinity",
        shell_escape(&cfg.root.display().to_string())
    )
}

fn codex_exec_protocol_prelude() -> &'static str {
    "## Codex Exec Tool Protocol\n\nYou are running under `codex exec` in a benchmark container. When you need to run\na shell command, emit a normal Codex shell tool call with a JSON object that\ncontains a `cmd` string, for example:\n\n{\"cmd\":\"cd /app && sed -n '1,120p' lib/example.go\"}\n\nDo not emit raw command arrays, partial JSON, or prose pretending to be a tool\ncall. If a tool call fails with `missing field cmd`, immediately retry the same\noperation as a shell tool call whose arguments include exactly one `cmd` string.\n\n"
}

fn deliver_instruction(cfg: &RuntimeConfig, name: &str, original: &str) -> Result<(), String> {
    wait_for_ready(cfg, name)?;
    let dir = cfg.state.join("subagents").join(name);
    let instruction = if original.contains('\n') || original.len() > 800 {
        atomic_write(
            &dir.join("instruction.txt"),
            &format!("{original}\n"),
            "instruction",
        )?;
        format!("Read and follow the assignment in {}/instruction.txt. Proceed now, then report progress and final status in this window.", dir.display())
    } else {
        original.into()
    };
    tmux_checked(&[
        "send-keys",
        "-t",
        &format!("{}:{name}", cfg.session),
        &instruction,
    ])?;
    sleep_env("MULTIAGENT_DELIVERY_SUBMIT_DELAY", 0.2);
    tmux_checked(&["send-keys", "-t", &format!("{}:{name}", cfg.session), "C-m"])?;
    sleep_env("MULTIAGENT_DELIVERY_SECOND_SUBMIT_DELAY", 0.8);
    tmux_checked(&["send-keys", "-t", &format!("{}:{name}", cfg.session), "C-m"])?;
    let _ = capture_subagent(cfg, name);
    Ok(())
}

fn wait_for_ready(cfg: &RuntimeConfig, name: &str) -> Result<(), String> {
    let attempts = env_nonempty("MULTIAGENT_READY_ATTEMPTS")
        .and_then(|value| value.parse::<usize>().ok())
        .unwrap_or(20);
    let delay = env_nonempty("MULTIAGENT_READY_DELAY")
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(0.5);
    let dir = cfg.state.join("subagents").join(name);
    let mut capture = String::new();
    for _ in 0..attempts {
        if let Ok(value) = capture_window(&cfg.session, name, 200) {
            capture = value;
            match readiness_state(&capture) {
                "ready" => {
                    atomic_write(
                        &dir.join("current.txt"),
                        &format!("{capture}\n"),
                        "current capture",
                    )?;
                    return Ok(());
                }
                "blocked" => {
                    atomic_write(
                        &dir.join("last-error.txt"),
                        &format!("{capture}\n"),
                        "readiness error",
                    )?;
                    set_subagent_status(cfg, name, "delivery-blocked")?;
                    return Err(format!("subagent window is not ready for instruction delivery: {name}; see {}/last-error.txt", dir.display()));
                }
                _ => {}
            }
        }
        thread::sleep(Duration::from_secs_f64(delay.max(0.0)));
    }
    if capture.is_empty() {
        capture = "no capture available".into();
    }
    atomic_write(
        &dir.join("last-error.txt"),
        &format!("{capture}\n"),
        "readiness error",
    )?;
    set_subagent_status(cfg, name, "delivery-blocked")?;
    Err(format!(
        "subagent window is not ready for instruction delivery: {name}; see {}/last-error.txt",
        dir.display()
    ))
}

fn readiness_state(text: &str) -> &'static str {
    let lower = text.to_ascii_lowercase();
    let blocked = [
        "not authenticated",
        "authentication required",
        "login required",
        "sign in",
        "setup required",
        "api key required",
        "failed to authenticate",
        "claude login",
        "log in to claude",
        "not logged in",
        "select theme",
        "choose your setup",
        "trust this folder",
        "do you trust",
        "press enter to continue",
    ];
    if blocked.iter().any(|value| lower.contains(value)) {
        return "blocked";
    }
    let ready = [
        "codex prompt ready",
        "claude prompt ready",
        "prompt ready",
        "restored codex prompt ready",
        "restored claude prompt ready",
        "what can i help",
        "ready for input",
        "type your message",
        "claude code",
        "bypass permissions mode",
        "dangerously-skip-permissions",
        "use /skills to list available skills",
        " default ",
    ];
    if ready.iter().any(|value| lower.contains(value)) {
        "ready"
    } else {
        "waiting"
    }
}

fn capture_subagent(cfg: &RuntimeConfig, name: &str) -> Result<(), String> {
    let dir = cfg.state.join("subagents").join(name);
    fs::create_dir_all(&dir).map_err(io_error("create subagent directory"))?;
    match capture_window(&cfg.session, name, 1000) {
        Ok(capture) => {
            atomic_write(
                &dir.join("current.txt"),
                &format!("{capture}\n"),
                "current capture",
            )?;
            append_file(
                &dir.join("transcript.log"),
                &format!("\n----- capture {} -----\n{capture}\n", timestamp()),
            )
        }
        Err(error) => {
            let last = fs::read_to_string(dir.join("last-message.txt")).unwrap_or_default();
            let transcript = fs::read_to_string(dir.join("transcript.log")).unwrap_or_default();
            if last.is_empty() && transcript.is_empty() {
                atomic_write(
                    &dir.join("last-error.txt"),
                    &format!("{error}\n"),
                    "capture error",
                )?;
                return Err(error);
            }
            let recovered = format!(
                "tmux capture unavailable for {name}; recovered durable subagent output.\ntmux-capture-error: {error}\n{}{}",
                if last.is_empty() { String::new() } else { format!("\n----- last-message.txt -----\n{last}") },
                if transcript.is_empty() { String::new() } else { format!("\n----- transcript tail -----\n{}", tail_lines(&transcript, 240)) }
            );
            atomic_write(&dir.join("current.txt"), &recovered, "durable capture")?;
            append_file(
                &dir.join("transcript.log"),
                &format!(
                    "\n----- durable capture {} -----\n{recovered}\n",
                    timestamp()
                ),
            )
        }
    }
}

fn infer_status(cfg: &RuntimeConfig, name: &str) -> String {
    let dir = cfg.state.join("subagents").join(name);
    let current = fs::read_to_string(dir.join("current.txt")).unwrap_or_default();
    let last = fs::read_to_string(dir.join("last-message.txt")).unwrap_or_default();
    let lower = current.to_ascii_lowercase();
    if nonzero_exec_status(&lower) || lower.contains("warning: no last agent message") {
        "failed".into()
    } else if !last.is_empty() && accepted_report(&tail_lines(&last, 160)) {
        "done".into()
    } else if looks_blocked_report(&tail_lines(&current, 160)) {
        "blocked".into()
    } else if looks_done_report(&current) {
        "done".into()
    } else if window_exists(&cfg.session, name) {
        "running".into()
    } else {
        "exited".into()
    }
}

fn accepted_report(text: &str) -> bool {
    text.lines().any(|line| {
        let value = normalize_report_line(line).to_ascii_lowercase();
        value == "accepted"
            || value.starts_with("accepted ")
            || value.starts_with("verdict: accepted")
            || value.starts_with("verdict=accepted")
            || (value.starts_with("review-record: type=") && value.contains(" verdict=pass diff="))
    })
}

fn normalize_report_line(line: &str) -> &str {
    let mut value = line.trim();
    if let Some((prefix, rest)) = value.split_once(' ') {
        let numbered = prefix
            .strip_suffix('.')
            .or_else(|| prefix.strip_suffix(')'))
            .is_some_and(|number| !number.is_empty() && number.chars().all(|c| c.is_ascii_digit()));
        if numbered || matches!(prefix, "-" | "*") {
            value = rest.trim_start();
        }
    }
    if value.starts_with('`') && value.ends_with('`') && value.len() >= 2 {
        value = &value[1..value.len() - 1];
    }
    value
}

fn looks_blocked_report(text: &str) -> bool {
    text.lines().any(|line| {
        let line = line.trim().to_ascii_lowercase();
        [
            "blocked",
            "blocker",
            "need input",
            "waiting for",
            "cannot proceed",
        ]
        .iter()
        .any(|prefix| {
            line.strip_prefix(prefix).is_some_and(|tail| {
                tail.is_empty()
                    || tail.chars().next().is_some_and(|value| {
                        value.is_whitespace() || matches!(value, ':' | '.' | '-')
                    })
            })
        }) || ["final status:", "status:"].iter().any(|prefix| {
            line.strip_prefix(prefix).is_some_and(|tail| {
                let tail = tail.trim_start();
                tail.starts_with("blocked")
                    || tail.starts_with("needs input")
                    || tail.starts_with("cannot proceed")
            })
        })
    })
}

fn looks_done_report(text: &str) -> bool {
    text.lines().any(|line| {
        let lower = line.trim_start().to_ascii_lowercase();
        [
            "final status:",
            "complete_task",
            "assignment complete",
            "task complete",
            "finished assignment",
            "work completed",
            "done with",
        ]
        .iter()
        .any(|prefix| lower.starts_with(prefix))
            || lower.split_once("worked for ").is_some_and(|(_, tail)| {
                tail.chars()
                    .next()
                    .is_some_and(|value| value.is_ascii_digit())
            })
    })
}

fn nonzero_exec_status(text: &str) -> bool {
    let marker = "final status: codex exec exited rc=";
    text.lines().any(|line| {
        line.find(marker).is_some_and(|index| {
            line[index + marker.len()..]
                .split_whitespace()
                .next()
                .and_then(|value| value.parse::<u32>().ok())
                .is_some_and(|value| value > 0)
        })
    })
}

fn has_recovery_context(dir: &Path) -> bool {
    file_nonempty(&dir.join("current.txt")) || file_nonempty(&dir.join("transcript.log"))
}

fn recovery_text(dir: &Path) -> String {
    let mut text = String::new();
    if let Ok(current) = fs::read_to_string(dir.join("current.txt")) {
        if !current.is_empty() {
            text.push_str("Current pane tail:\n");
            text.push_str(&tail_lines(&current, 80));
        }
    }
    if let Ok(transcript) = fs::read_to_string(dir.join("transcript.log")) {
        if !transcript.is_empty() {
            text.push_str("\nTranscript tail:\n");
            text.push_str(&tail_lines(&transcript, 120));
        }
    }
    tail_lines(&text, 180)
}

fn set_subagent_status(cfg: &RuntimeConfig, name: &str, status: &str) -> Result<(), String> {
    let dir = cfg.state.join("subagents").join(name);
    fs::create_dir_all(&dir).map_err(io_error("create subagent state"))?;
    atomic_write(
        &dir.join("status"),
        &format!("{status}\n"),
        "subagent status",
    )
}

fn window_exists(session: &str, name: &str) -> bool {
    let Ok(output) = tmux_output(&["list-windows", "-t", session, "-F", "#W"]) else {
        return false;
    };
    output.status.success()
        && String::from_utf8_lossy(&output.stdout)
            .lines()
            .any(|line| line == name)
}

fn capture_window(session: &str, name: &str, lines: usize) -> Result<String, String> {
    let output = tmux_output(&[
        "capture-pane",
        "-t",
        &format!("{session}:{name}"),
        "-p",
        "-S",
        &format!("-{lines}"),
    ])?;
    if !output.status.success() {
        return Err(String::from_utf8_lossy(&output.stderr).trim().to_string());
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .trim_end()
        .to_string())
}

fn pipe_log(session: &str, window: &str, logs: &Path) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    use std::os::unix::fs::PermissionsExt;

    fs::create_dir_all(logs).map_err(io_error("create log directory"))?;
    let log = logs.join(format!("{window}.log"));
    OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log)
        .map_err(io_error("create pane log"))?;
    #[cfg(target_os = "linux")]
    if env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1") && unsafe { libc::geteuid() } == 0 {
        chown_path(&log, ORCHESTRATOR_UID, ROLE_GID)?;
        fs::set_permissions(&log, fs::Permissions::from_mode(0o660))
            .map_err(io_error("set orchestrator pane log permissions"))?;
    }
    tmux_checked(&[
        "pipe-pane",
        "-o",
        "-t",
        &format!("{session}:{window}"),
        &format!("cat >> {}", shell_escape(&log.display().to_string())),
    ])
}

fn tmux_success(args: &[&str]) -> bool {
    tmux_command()
        .args(args)
        .output()
        .is_ok_and(|output| output.status.success())
}

fn tmux_output(args: &[&str]) -> Result<Output, String> {
    tmux_command()
        .args(args)
        .output()
        .map_err(io_error("run tmux"))
}

fn tmux_command() -> Command {
    let mut command = Command::new("tmux");
    if let Some(socket) = env_path("MULTIAGENT_TMUX_SOCKET") {
        command.arg("-S").arg(socket);
    }
    command
}

#[cfg(target_os = "linux")]
fn tmux_checked_as_uid(args: &[&str], executable: &Path, uid: u32) -> Result<(), String> {
    let mut command = Command::new(executable);
    command
        .arg("role-exec")
        .arg("--uid")
        .arg(uid.to_string())
        .arg("--gid")
        .arg(ROLE_GID.to_string())
        .arg("--")
        .arg("tmux");
    if let Some(socket) = env_path("MULTIAGENT_TMUX_SOCKET") {
        command.arg("-S").arg(socket);
    }
    let output = command
        .args(args)
        .output()
        .map_err(io_error("run tmux as orchestrator"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "tmux {} as orchestrator failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

#[cfg(not(target_os = "linux"))]
fn tmux_checked_as_uid(_args: &[&str], _executable: &Path, _uid: u32) -> Result<(), String> {
    Err("MULTIAGENT_UID_SANDBOX is only supported on Linux".into())
}

fn tmux_checked(args: &[&str]) -> Result<(), String> {
    let output = tmux_output(args)?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format!(
            "tmux {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

fn run_self_output(args: &[&str]) -> Result<Output, String> {
    let executable = env::current_exe().map_err(io_error("resolve multiagent executable"))?;
    let output = Command::new(executable)
        .args(args)
        .output()
        .map_err(io_error("run multiagent command"))?;
    if output.status.success() {
        Ok(output)
    } else {
        Err(String::from_utf8_lossy(&output.stderr).trim().to_string())
    }
}

fn run_self_quiet(args: &[&str]) -> Result<(), String> {
    run_self_output(args).map(|_| ())
}

fn validate_cli(value: &str) -> Result<(), String> {
    if matches!(value, "codex" | "claude") {
        Ok(())
    } else {
        Err(format!(
            "unsupported CLI '{value}' (expected codex or claude)"
        ))
    }
}

fn require_command(command: &str) -> Result<(), String> {
    let path = Path::new(command);
    if command.contains('/') {
        if is_executable(path) {
            return Ok(());
        }
    } else if let Some(paths) = env::var_os("PATH") {
        for directory in env::split_paths(&paths) {
            if is_executable(&directory.join(command)) {
                return Ok(());
            }
        }
    }
    Err(format!("missing required command: {command}"))
}

#[cfg(unix)]
fn is_executable(path: &Path) -> bool {
    use std::os::unix::fs::PermissionsExt;
    path.metadata()
        .is_ok_and(|metadata| metadata.is_file() && metadata.permissions().mode() & 0o111 != 0)
}

#[cfg(not(unix))]
fn is_executable(path: &Path) -> bool {
    path.is_file()
}

fn framework_root() -> PathBuf {
    env_path("MULTIAGENT_FRAMEWORK_ROOT")
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")))
}

fn env_nonempty(key: &str) -> Option<String> {
    env::var(key).ok().filter(|value| !value.is_empty())
}

fn env_path(key: &str) -> Option<PathBuf> {
    env::var_os(key)
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
}

fn required_value<'a>(args: &'a [String], index: usize, label: &str) -> Result<&'a str, String> {
    args.get(index + 1)
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .ok_or_else(|| format!("{label} requires a value"))
}

fn one_name<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    if args.len() != 1 || args[0].is_empty() {
        return Err(format!("{command} requires NAME"));
    }
    validate_name(&args[0])?;
    Ok(&args[0])
}

fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty()
        || name.starts_with('-')
        || !name
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || matches!(value, '_' | '.' | '-'))
    {
        return Err(format!("invalid subagent name: {name}"));
    }
    if name == "orchestrator" {
        return Err(format!("reserved subagent name: {name}"));
    }
    Ok(())
}

fn normalize_repo_path(root: &Path, requested: &str) -> Result<String, String> {
    let root = fs::canonicalize(root).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    let path = Path::new(requested);
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    let canonical = canonicalize_missing(&absolute)?;
    let relative = canonical
        .strip_prefix(&root)
        .map_err(|_| format!("assigned path is outside MULTIAGENT_ROOT: {requested}"))?;
    let text = relative.to_string_lossy().trim_matches('/').to_string();
    if text.is_empty() || text == "." {
        return Err("assigned path may not be the whole repo root".into());
    }
    Ok(text)
}

fn canonicalize_missing(path: &Path) -> Result<PathBuf, String> {
    let mut missing = Vec::new();
    let mut parent = path;
    while !parent.exists() {
        let name = parent
            .file_name()
            .ok_or_else(|| format!("cannot resolve path: {}", path.display()))?;
        missing.push(name.to_os_string());
        parent = parent
            .parent()
            .ok_or_else(|| format!("cannot resolve path: {}", path.display()))?;
    }
    let mut result = fs::canonicalize(parent).map_err(io_error("canonicalize path"))?;
    for component in missing.iter().rev() {
        if component == ".." {
            result.pop();
        } else if component != "." {
            result.push(component);
        }
    }
    Ok(result)
}

fn csv_values(raw: &str) -> Vec<String> {
    let mut values = Vec::new();
    for value in raw
        .split(',')
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        if !values.iter().any(|existing| existing == value) {
            values.push(value.to_string());
        }
    }
    values
}

fn read_env(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let text = fs::read_to_string(path).map_err(io_error("read environment state"))?;
    let mut values = BTreeMap::new();
    for line in text.lines() {
        if let Some((key, value)) = line.split_once('=') {
            values.insert(key.into(), value.into());
        }
    }
    Ok(values)
}

fn required_env_field<'a>(
    values: &'a BTreeMap<String, String>,
    key: &str,
    message: &str,
) -> Result<&'a str, String> {
    values
        .get(key)
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .ok_or_else(|| message.to_string())
}

fn git_text(root: &Path, args: &[&str]) -> Result<String, String> {
    Ok(String::from_utf8_lossy(&git_bytes(root, args)?)
        .trim()
        .to_string())
}

fn git_bytes(root: &Path, args: &[&str]) -> Result<Vec<u8>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(io_error("run git"))?;
    if output.status.success() {
        Ok(output.stdout)
    } else {
        Err(format!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ))
    }
}

fn sorted_directories(base: &Path) -> Result<Vec<PathBuf>, String> {
    if !base.is_dir() {
        return Ok(Vec::new());
    }
    let mut values = fs::read_dir(base)
        .map_err(io_error("read state directory"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect::<Vec<_>>();
    values.sort();
    Ok(values)
}

fn file_name(path: &Path) -> Result<String, String> {
    path.file_name()
        .and_then(|value| value.to_str())
        .map(str::to_string)
        .ok_or_else(|| format!("invalid state path: {}", path.display()))
}

fn file_nonempty(path: &Path) -> bool {
    path.metadata().is_ok_and(|metadata| metadata.len() > 0)
}

fn read_trimmed(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()
        .map(|value| value.trim_matches(['\r', '\n']).to_string())
}

fn last_nonempty_line(text: &str) -> String {
    text.lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .to_string()
}

fn tail_lines(text: &str, maximum: usize) -> String {
    let lines = text.lines().collect::<Vec<_>>();
    let start = lines.len().saturating_sub(maximum);
    let mut result = lines[start..].join("\n");
    if !result.is_empty() && text.ends_with('\n') {
        result.push('\n');
    }
    result
}

fn truncate(value: &str, maximum: usize) -> String {
    if value.chars().count() <= maximum {
        value.into()
    } else {
        let keep = maximum.saturating_sub(3);
        format!("{}...", value.chars().take(keep).collect::<String>())
    }
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

fn sleep_env(key: &str, default: f64) {
    let seconds = env_nonempty(key)
        .and_then(|value| value.parse::<f64>().ok())
        .unwrap_or(default)
        .max(0.0);
    thread::sleep(Duration::from_secs_f64(seconds));
}

fn append_file(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(io_error("create append directory"))?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(io_error("open append file"))?;
    file.write_all(text.as_bytes())
        .map_err(io_error("append file"))
}

fn atomic_write(path: &Path, text: &str, label: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("{label} path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent).map_err(io_error("create output directory"))?;
    let temporary = path.with_file_name(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("state"),
        std::process::id()
    ));
    let mut file = File::create(&temporary).map_err(io_error("create temporary file"))?;
    file.write_all(text.as_bytes())
        .map_err(io_error("write temporary file"))?;
    file.sync_all().map_err(io_error("sync temporary file"))?;
    fs::rename(temporary, path).map_err(io_error("publish file"))
}

#[cfg(unix)]
fn set_executable(path: &Path, mode: u32) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let mut permissions = fs::metadata(path)
        .map_err(io_error("read file permissions"))?
        .permissions();
    permissions.set_mode(mode);
    fs::set_permissions(path, permissions).map_err(io_error("set file permissions"))
}

#[cfg(not(unix))]
fn set_executable(_path: &Path, _mode: u32) -> Result<(), String> {
    Ok(())
}

fn timestamp() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn io_error(action: &'static str) -> impl Fn(std::io::Error) -> String {
    move |error| format!("{action}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shell_escaping_handles_spaces_and_quotes() {
        assert_eq!(shell_escape("plain/path"), "plain/path");
        assert_eq!(shell_escape("two words"), "'two words'");
        assert_eq!(shell_escape("it's"), "'it'\\''s'");
    }

    #[test]
    fn status_classification_prioritizes_blockers() {
        assert_eq!(
            classify_capture("completed but waiting for input"),
            "blocked"
        );
        assert_eq!(classify_capture("assignment complete"), "done");
        assert_eq!(classify_capture("working"), "busy");
    }

    #[test]
    fn reviewer_pass_marker_is_accepted_evidence() {
        let report = "authority-findings: blocked sets remain unchanged\n\
review-record: type=decision-authority verdict=pass diff=-\n";
        assert!(accepted_report(report));
        assert!(accepted_report(
            "3. `review-record: type=scope verdict=pass diff=abc`"
        ));
    }
}
