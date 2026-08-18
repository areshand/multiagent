use crate::{
    config, runtime,
    state::{atomic_write, read_env, timestamp},
    workflow,
};
use fs2::FileExt;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const ROLES: &[&str] = &[
    "exploitation",
    "exploration",
    "reflection",
    "architecture",
    "qa",
    "verifier",
    "scout",
];
const TERMINAL_STATUSES: &[&str] = &[
    "done",
    "completed",
    "closed",
    "cancelled",
    "canceled",
    "failed",
    "released",
    "skipped",
];

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    if args.is_empty() {
        return Err("missing command".into());
    }
    let result = match args[0].as_str() {
        "assignment-create" => assignment_create(&args[1..]),
        "assignment-show" => assignment_show(&args[1..]),
        "assignment-status" => assignment_status(&args[1..]),
        "assignment-check" => assignment_check(&args[1..]),
        "checkpoint-update" => checkpoint_update(&args[1..]),
        "checkpoint-show" => checkpoint_show(&args[1..]),
        "worktree-create" => worktree_create(&args[1..]),
        "worktree-show" => worktree_show(&args[1..]),
        "worktree-remove" => worktree_remove(&args[1..]),
        "finding-create" => finding_create(&args[1..]),
        "finding-show" => finding_show(&args[1..]),
        "finding-list" => finding_list(&args[1..]),
        "finding-dismiss" => finding_dismiss(&args[1..]),
        "todo-create" => todo_create(&args[1..]),
        "todo-show" => todo_show(&args[1..]),
        "todo-list" => todo_list(&args[1..]),
        "todo-assign" => todo_assign(&args[1..]),
        "todo-status" => todo_status(&args[1..]),
        "resolution-create" => resolution_create(&args[1..]),
        "todo-close" => todo_close(&args[1..]),
        "validation-lease-acquire" => validation_lease_acquire(&args[1..]),
        "validation-lease-status" => validation_lease_status(&args[1..]),
        "validation-lease-show" => validation_lease_show(&args[1..]),
        "validation-lease-list" => validation_lease_list(&args[1..]),
        "validation-run" => return validation_run(&args[1..]),
        "gate-check" => gate_check(&args[1..]),
        _ => return runtime::subagent(args),
    };
    result.map(|_| ExitCode::SUCCESS)
}

fn assignment_dir(name: &str) -> Result<PathBuf, String> {
    validate_name(name)?;
    Ok(config::state_dir()?.join("assignments").join(name))
}

fn require_assignment(name: &str) -> Result<PathBuf, String> {
    let dir = assignment_dir(name)?;
    if !dir.join("assignment.env").is_file() {
        return Err(format!("no assignment for agent: {name}"));
    }
    Ok(dir)
}

fn assignment_show(args: &[String]) -> Result<(), String> {
    let name = one_agent("assignment-show", args)?;
    let dir = require_assignment(name)?;
    print!(
        "{}",
        fs::read_to_string(dir.join("assignment.env")).map_err(io_error("read assignment"))?
    );
    let status = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown\n".into());
    println!("status={}", status.trim_end());
    let checkpoint = dir.join("checkpoint.env");
    if checkpoint.is_file() {
        println!("checkpoint=");
        for line in fs::read_to_string(checkpoint)
            .map_err(io_error("read checkpoint"))?
            .lines()
        {
            println!("  {line}");
        }
    }
    println!("owned_paths=");
    for line in fs::read_to_string(dir.join("owned-paths"))
        .map_err(io_error("read owned paths"))?
        .lines()
    {
        println!("  {line}");
    }
    Ok(())
}

fn assignment_status(args: &[String]) -> Result<(), String> {
    if args.len() != 2 {
        return Err("assignment-status requires NAME STATUS".into());
    }
    let name = &args[0];
    reject_newline("status", &args[1])?;
    let dir = require_assignment(name)?;
    let base = dir
        .parent()
        .ok_or_else(|| "invalid assignment directory".to_string())?;
    let _lock = lock_file(&base.join(".lock"), "assignments")?;
    atomic_write(&dir.join("status"), &format!("{}\n", args[1]))?;
    println!("assignment status\t{name}\t{}", args[1]);
    Ok(())
}

fn assignment_check(args: &[String]) -> Result<(), String> {
    let name = one_agent("assignment-check", args)?;
    let dir = require_assignment(name)?;
    let metadata = read_env(&dir.join("assignment.env"))?;
    let root =
        fs::canonicalize(config::root()?).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    let current_branch = git_output(&root, &["rev-parse", "--abbrev-ref", "HEAD"])?;
    let expected_branch = env_value(&metadata, "branch");
    println!(
        "assignment\t{name}\t{}",
        env_value(&metadata, "assignment_id")
    );
    println!("branch\t{expected_branch}\t{current_branch}");
    let mut failed = false;
    if current_branch != expected_branch {
        println!("reject\tbranch-mismatch\texpected={expected_branch}\tactual={current_branch}");
        failed = true;
    }
    let start = env_value(&metadata, "start_commit");
    let mut changed = BTreeSet::new();
    for command in [
        vec!["diff", "--name-only", &format!("{start}..HEAD")],
        vec!["diff", "--name-only"],
        vec!["diff", "--name-only", "--cached"],
        vec!["ls-files", "--others", "--exclude-standard"],
    ] {
        for line in git_output(&root, &command)?
            .lines()
            .filter(|line| !line.is_empty())
        {
            changed.insert(line.to_string());
        }
    }
    let owned: Vec<String> = fs::read_to_string(dir.join("owned-paths"))
        .map_err(io_error("read owned paths"))?
        .lines()
        .filter(|line| !line.is_empty())
        .map(String::from)
        .collect();
    if changed.is_empty() {
        println!("ok\tno-changes");
    } else {
        for path in changed {
            if owned.iter().any(|base| {
                path == *base
                    || path
                        .strip_prefix(base)
                        .is_some_and(|suffix| suffix.starts_with('/'))
            }) {
                println!("ok\t{path}");
            } else {
                println!("reject\toutside-owned-path\t{path}");
                failed = true;
            }
        }
    }
    if failed {
        Err(String::new())
    } else {
        println!("accepted\t{name}");
        Ok(())
    }
}

fn checkpoint_update(args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|v| !v.is_empty())
        .ok_or_else(|| "checkpoint-update requires NAME".to_string())?;
    let dir = require_assignment(name)?;
    let values = repeated_options(&args[1..], &[])?;
    let step = option_required(&values, "--step", "checkpoint-update requires --step TEXT")?;
    let blocker = option_first(&values, "--blocker");
    let idempotency = option_first(&values, "--idempotency");
    let requested_commit = option_first(&values, "--last-commit");
    let root =
        fs::canonicalize(config::root()?).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    let last_commit = resolve_named_commit(&root, requested_commit, "last")?;
    let persisted_status =
        fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
    let status = if !option_first(&values, "--status").is_empty() {
        option_first(&values, "--status")
    } else if !blocker.is_empty() {
        "blocked"
    } else {
        persisted_status.trim()
    };
    for (label, value) in [
        ("--step", step),
        ("--blocker", blocker),
        ("--idempotency", idempotency),
        ("--status", status),
    ] {
        reject_newline(label, value)?;
    }
    let metadata = read_env(&dir.join("assignment.env"))?;
    let owned_paths_file = dir.join("owned-paths").to_string_lossy().into_owned();
    let updated_at = timestamp();
    let role = {
        let value = env_value(&metadata, "role");
        if value.is_empty() {
            "exploitation"
        } else {
            value
        }
    };
    let text = [
        ("agent_name", name.as_str()),
        ("assignment_id", env_value(&metadata, "assignment_id")),
        ("branch", env_value(&metadata, "branch")),
        ("owned_paths_file", owned_paths_file.as_str()),
        ("last_commit", last_commit.as_str()),
        ("completed_step", step),
        ("blocker", blocker),
        ("idempotency", idempotency),
        ("status", status),
        ("role", role),
        ("responsibility", env_value(&metadata, "responsibility")),
        ("decision_id", env_value(&metadata, "decision_id")),
        ("plan_id", env_value(&metadata, "plan_id")),
        ("workflow_id", env_value(&metadata, "workflow_id")),
        ("node_id", env_value(&metadata, "node_id")),
        ("depends_on", env_value(&metadata, "depends_on")),
        ("updated_at", updated_at.as_str()),
    ]
    .into_iter()
    .map(|(key, value)| format!("{key}={value}\n"))
    .collect::<String>();
    let assignments = dir
        .parent()
        .ok_or_else(|| "invalid assignment directory".to_string())?;
    let _lock = lock_file(&assignments.join(".lock"), "assignments")?;
    atomic_write(&dir.join("checkpoint.env"), &text)?;
    atomic_write(&dir.join("status"), &format!("{status}\n"))?;
    // Under UID isolation, the authority server owns assignments but must not
    // create files in the orchestrator-owned runtime projection. Doing so
    // would make the later role prompt/status directory unwritable by the
    // orchestrator. Runtime spawn/poll remains the sole owner of that mirror.
    if !(env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1")
        && env::var("MULTIAGENT_AUTHORITY_SERVER_CHILD").as_deref() == Ok("1"))
    {
        let subagent = config::state_dir()?.join("subagents").join(name);
        fs::create_dir_all(&subagent).map_err(io_error("create subagent state"))?;
        atomic_write(&subagent.join("status"), &format!("{status}\n"))?;
    }
    println!("checkpoint updated\t{name}\t{status}");
    Ok(())
}

fn checkpoint_show(args: &[String]) -> Result<(), String> {
    let name = one_agent("checkpoint-show", args)?;
    let path = assignment_dir(name)?.join("checkpoint.env");
    if !path.is_file() {
        return Err(format!("no checkpoint for agent: {name}"));
    }
    print!(
        "{}",
        fs::read_to_string(path).map_err(io_error("read checkpoint"))?
    );
    Ok(())
}

fn worktree_create(args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "worktree-create requires NAME".to_string())?;
    validate_name(name)?;
    let values = repeated_options(&args[1..], &[])?;
    let assignment = assignment_dir(name)?.join("assignment.env");
    let assignment_metadata = read_env(&assignment).unwrap_or_default();
    let branch = {
        let requested = option_first(&values, "--branch");
        if requested.is_empty() {
            env_value(&assignment_metadata, "branch")
        } else {
            requested
        }
    };
    if branch.is_empty() {
        return Err("worktree-create requires --branch BRANCH or assignment metadata".into());
    }
    let state = config::state_dir()?;
    let default_path = state.join("worktrees").join(name);
    let path = {
        let requested = option_first(&values, "--path");
        if requested.is_empty() {
            default_path
        } else {
            PathBuf::from(requested)
        }
    };
    let root =
        fs::canonicalize(config::root()?).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(io_error("create worktree parent"))?;
    }
    let metadata_dir = state.join("worktrees");
    fs::create_dir_all(&metadata_dir).map_err(io_error("create worktree metadata directory"))?;
    let _lock = lock_file(&metadata_dir.join(".lock"), "worktrees")?;
    if !path.join(".git").exists() {
        let reference = format!("refs/heads/{branch}");
        let exists = Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(["show-ref", "--verify", "--quiet", &reference])
            .status()
            .map_err(io_error("check worktree branch"))?
            .success();
        let mut command = Command::new("git");
        command.arg("-C").arg(&root).args(["worktree", "add"]);
        if !exists {
            command.args(["-b", branch]);
        }
        command.arg(&path);
        if exists {
            command.arg(branch);
        } else {
            command.arg("HEAD");
        }
        let output = command.output().map_err(io_error("create git worktree"))?;
        if !output.status.success() {
            return Err(format!(
                "git worktree add failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
    }
    let path_text = path.display().to_string();
    let root_text = root.display().to_string();
    atomic_write(&metadata_dir.join(format!("{name}.env")),&format!("agent_name={name}\nbranch={branch}\npath={path_text}\ncreated_at={}\nroot={root_text}\n",timestamp()))?;
    if assignment.is_file() {
        let text = fs::read_to_string(&assignment).map_err(io_error("read assignment"))?;
        if !text.lines().any(|line| line.starts_with("worktree_path=")) {
            atomic_write(&assignment, &format!("{text}worktree_path={path_text}\n"))?;
        }
    }
    println!("worktree created\t{name}\t{branch}\t{path_text}");
    Ok(())
}

fn worktree_show(args: &[String]) -> Result<(), String> {
    let name = one_agent("worktree-show", args)?;
    let path = config::state_dir()?
        .join("worktrees")
        .join(format!("{name}.env"));
    if !path.is_file() {
        return Err(format!("no worktree metadata for agent: {name}"));
    }
    print!(
        "{}",
        fs::read_to_string(path).map_err(io_error("read worktree metadata"))?
    );
    Ok(())
}

fn worktree_remove(args: &[String]) -> Result<(), String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "worktree-remove requires NAME".to_string())?;
    validate_name(name)?;
    let force = match &args[1..] {
        [] => false,
        [flag] if flag == "--force" => true,
        [other, ..] => return Err(format!("unknown worktree-remove argument: {other}")),
    };
    let state = config::state_dir()?;
    let meta = state.join("worktrees").join(format!("{name}.env"));
    if !meta.is_file() {
        return Err(format!("no worktree metadata for agent: {name}"));
    }
    let metadata = read_env(&meta)?;
    let path = env_value(&metadata, "path").to_string();
    let root =
        fs::canonicalize(config::root()?).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    let mut command = Command::new("git");
    command.arg("-C").arg(&root).args(["worktree", "remove"]);
    if force {
        command.arg("--force");
    }
    let output = command
        .arg(&path)
        .output()
        .map_err(io_error("remove git worktree"))?;
    if !output.status.success() {
        return Err(format!(
            "git worktree remove failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    fs::remove_file(&meta).map_err(io_error("remove worktree metadata"))?;
    println!("worktree removed\t{name}\t{path}");
    Ok(())
}

struct AssignmentOptions {
    name: String,
    assignment_id: String,
    branch: String,
    owned: Vec<String>,
    status: String,
    start_commit: String,
    role: String,
    responsibility: String,
    decision_id: String,
    plan_id: String,
    workflow_id: String,
    node_id: String,
    depends_on: String,
}

fn assignment_create(args: &[String]) -> Result<(), String> {
    let options = parse_assignment(args)?;
    validate_name(&options.name)?;
    if !ROLES.contains(&options.role.as_str()) {
        return Err(format!(
            "invalid role '{}' (expected exploitation|exploration|reflection|architecture|qa|verifier|scout)",
            options.role
        ));
    }
    reject_newline("--responsibility", &options.responsibility)?;

    let root =
        fs::canonicalize(config::root()?).map_err(io_error("canonicalize MULTIAGENT_ROOT"))?;
    let state_dir = config::state_dir()?;
    let assignments = state_dir.join("assignments");
    fs::create_dir_all(&assignments).map_err(io_error("create assignments directory"))?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(assignments.join(".lock"))
        .map_err(io_error("open assignments lock"))?;
    lock.lock_exclusive()
        .map_err(io_error("lock assignments"))?;

    let mut owned = BTreeSet::new();
    for group in &options.owned {
        for value in group.split(',') {
            let value = value.trim();
            if !value.is_empty() {
                owned.insert(normalize_repo_path(&root, Path::new(value))?);
            }
        }
    }
    if owned.is_empty() {
        return Err("assignment must own at least one path".into());
    }
    reject_overlap(&assignments, &options.name, &options.role, &owned)?;

    let start_commit = resolve_commit(&root, &options.start_commit)?;
    let lifecycle_enforced = config::lifecycle_enforced();
    let workflow_id =
        if lifecycle_enforced && options.role == "exploitation" && options.workflow_id.is_empty() {
            env::var("MULTIAGENT_WORKFLOW_ID").unwrap_or_default()
        } else {
            options.workflow_id.clone()
        };
    let context = if lifecycle_enforced && options.role == "exploitation" {
        if workflow_id.is_empty() {
            return Err(
                "lifecycle enforcement requires --workflow-id for exploitation assignments".into(),
            );
        }
        if options.decision_id.is_empty() {
            return Err(
                "lifecycle enforcement requires --decision-id for exploitation assignments".into(),
            );
        }
        if options.plan_id.is_empty() {
            return Err(
                "lifecycle enforcement requires --plan-id for exploitation assignments".into(),
            );
        }
        Some(
            workflow::assignment_context(&workflow_id, &options.decision_id, &options.plan_id)
                .map_err(|_| {
                    format!(
                        "workflow implementation gate rejected assignment for workflow {workflow_id}"
                    )
                })?,
        )
    } else {
        None
    };

    let dir = assignments.join(&options.name);
    fs::create_dir_all(&dir).map_err(io_error("create assignment directory"))?;
    let worker_cli = env::var("WORKER_CLI").unwrap_or_else(|_| "claude".into());
    let subagent_cli = env::var("SUBAGENT_CLI").unwrap_or_else(|_| worker_cli.clone());
    let verifier_cli = env::var("VERIFIER_CLI").unwrap_or_else(|_| "codex".into());
    let metadata = [
        ("agent_name", options.name.as_str()),
        ("assignment_id", options.assignment_id.as_str()),
        ("branch", options.branch.as_str()),
        ("start_commit", start_commit.as_str()),
        ("created_at", timestamp().as_str()),
        ("root", root.to_string_lossy().as_ref()),
        ("worker_cli", worker_cli.as_str()),
        ("subagent_cli", subagent_cli.as_str()),
        ("verifier_cli", verifier_cli.as_str()),
        ("role", options.role.as_str()),
        ("responsibility", options.responsibility.as_str()),
        ("decision_id", options.decision_id.as_str()),
        ("plan_id", options.plan_id.as_str()),
        (
            "decision_revision",
            context
                .as_ref()
                .map(|v| v.decision_revision.as_str())
                .unwrap_or(""),
        ),
        (
            "implementation_context",
            context
                .as_ref()
                .map(|v| v.implementation_context.as_str())
                .unwrap_or(""),
        ),
        (
            "implementation_context_sha256",
            context
                .as_ref()
                .map(|v| v.implementation_context_sha256.as_str())
                .unwrap_or(""),
        ),
        ("workflow_id", workflow_id.as_str()),
        ("node_id", options.node_id.as_str()),
        ("depends_on", options.depends_on.as_str()),
    ]
    .into_iter()
    .map(|(key, value)| format!("{key}={value}\n"))
    .collect::<String>();
    atomic_write(&dir.join("assignment.env"), &metadata)?;
    atomic_write(
        &dir.join("owned-paths"),
        &owned
            .into_iter()
            .map(|p| format!("{p}\n"))
            .collect::<String>(),
    )?;
    atomic_write(&dir.join("status"), &format!("{}\n", options.status))?;
    println!(
        "assignment created\t{}\t{}\t{}",
        options.name, options.assignment_id, options.branch
    );
    Ok(())
}

fn finding_create(args: &[String]) -> Result<(), String> {
    let id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "finding-create requires FINDING_ID".to_string())?;
    validate_name(id)?;
    let values = repeated_options(&args[1..], &[])?;
    let severity = option_required(&values, "--severity", "invalid finding severity: ")?;
    if !matches!(severity, "blocking" | "nonblocking" | "warning") {
        return Err(format!("invalid finding severity: {severity}"));
    }
    let kind = option_required(&values, "--type", "finding-create requires --type TYPE")?;
    let summary = option_required(
        &values,
        "--summary",
        "finding-create requires --summary TEXT",
    )?;
    let evidence_raw = option_required(
        &values,
        "--evidence-json",
        "finding-create requires --evidence-json JSON",
    )?;
    let required_resolution = option_required(
        &values,
        "--required-resolution",
        "finding-create requires --required-resolution TEXT",
    )?;
    for (label, value) in [
        ("--type", kind),
        ("--summary", summary),
        ("--required-resolution", required_resolution),
    ] {
        reject_newline(label, value)?;
    }
    let evidence: Value = serde_json::from_str(evidence_raw)
        .map_err(|error| format!("invalid evidence JSON: {error}"))?;
    validate_finding_evidence(severity, kind, &evidence)?;

    let state = config::state_dir()?;
    let base = state.join("findings");
    fs::create_dir_all(&base).map_err(io_error("create findings directory"))?;
    let _lock = lock_file(&base.join(".lock"), "findings")?;
    let dir = base.join(id);
    if dir.exists() {
        return Err(format!("finding already exists: {id}"));
    }
    fs::create_dir_all(&dir).map_err(io_error("create finding directory"))?;
    let created = timestamp();
    let root = config::root()?.display().to_string();
    atomic_write(
        &dir.join("finding.env"),
        &format!(
            "finding_id={id}\nseverity={severity}\ntype={kind}\nsummary={summary}\nrequired_resolution={required_resolution}\ncreated_at={created}\nroot={root}\n"
        ),
    )?;
    atomic_write(
        &dir.join("evidence.json"),
        &format!(
            "{}\n",
            serde_json::to_string(&evidence).map_err(json_error)?
        ),
    )?;
    let affected = csv_unique(option_first(&values, "--affected"));
    atomic_write(
        &dir.join("affected-paths"),
        &affected
            .iter()
            .map(|value| format!("{value}\n"))
            .collect::<String>(),
    )?;
    let payload = json!({
        "id": id,
        "severity": severity,
        "type": kind,
        "summary": summary,
        "affected_paths": affected,
        "evidence": evidence,
        "required_resolution": required_resolution,
        "created_at": created,
    });
    write_json(&dir.join("finding.json"), &payload)?;
    println!("finding created\t{id}\t{severity}\t{kind}");
    Ok(())
}

fn finding_show(args: &[String]) -> Result<(), String> {
    let id = one_name("finding-show", args)?;
    let path = config::state_dir()?
        .join("findings")
        .join(id)
        .join("finding.json");
    if !path.is_file() {
        return Err(format!("no finding: {id}"));
    }
    print!(
        "{}",
        fs::read_to_string(path).map_err(io_error("read finding"))?
    );
    Ok(())
}

fn finding_list(args: &[String]) -> Result<(), String> {
    let values = repeated_options(args, &[])?;
    let severity_filter = option_first(&values, "--severity");
    let type_filter = option_first(&values, "--type");
    let base = config::state_dir()?.join("findings");
    for dir in sorted_directories(&base)? {
        let metadata = read_env(&dir.join("finding.env"))?;
        let severity = env_value(&metadata, "severity");
        let kind = env_value(&metadata, "type");
        if (!severity_filter.is_empty() && severity_filter != severity)
            || (!type_filter.is_empty() && type_filter != kind)
        {
            continue;
        }
        println!(
            "{}\t{}\t{}\t{}",
            dir.file_name().and_then(|v| v.to_str()).unwrap_or(""),
            severity,
            kind,
            env_value(&metadata, "summary")
        );
    }
    Ok(())
}

fn finding_dismiss(args: &[String]) -> Result<(), String> {
    let id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "finding-dismiss requires FINDING_ID".to_string())?;
    validate_name(id)?;
    let values = repeated_options(&args[1..], &[])?;
    let verified = option_required(
        &values,
        "--verified-by",
        "finding-dismiss requires --verified-by NAME",
    )?;
    validate_name(verified)?;
    let recheck_raw = option_required(
        &values,
        "--recheck-json",
        "finding-dismiss requires --recheck-json JSON",
    )?;
    let notes = option_first(&values, "--notes");
    reject_newline("--notes", notes)?;
    let state = config::state_dir()?;
    let findings = state.join("findings");
    let todos = state.join("todos");
    fs::create_dir_all(&findings).map_err(io_error("create findings directory"))?;
    fs::create_dir_all(&todos).map_err(io_error("create todos directory"))?;
    let _todo_lock = lock_file(&todos.join(".lock"), "todos")?;
    let _finding_lock = lock_file(&findings.join(".lock"), "findings")?;
    let dir = findings.join(id);
    if !dir.join("finding.json").is_file() {
        return Err(format!("no finding: {id}"));
    }
    if dir.join("dismissal.json").is_file() {
        return Err(format!("finding already dismissed: {id}"));
    }
    for todo in sorted_directories(&todos)? {
        let metadata = read_env(&todo.join("todo.env"))?;
        if env_value(&metadata, "source_finding_id") == id {
            return Err(format!(
                "finding-dismiss refuses finding with todo: {}",
                todo.file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("")
            ));
        }
    }
    let (evidence_path, evidence) = verifier_evidence(&state, verified, "finding-dismiss")?;
    let recheck: Value = serde_json::from_str(recheck_raw)
        .map_err(|error| format!("invalid finding dismissal recheck: {error}"))?;
    let object = recheck
        .as_object()
        .ok_or_else(|| "finding dismissal recheck must be an object".to_string())?;
    if object.get("accepted") != Some(&Value::Bool(true)) {
        return Err("finding dismissal recheck must include accepted=true".into());
    }
    let named = ["finding_rechecked", "source_finding_id"]
        .iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .any(|value| value.trim() == id);
    if !named {
        return Err(format!("finding dismissal recheck must name finding {id}"));
    }
    if !matches!(
        object.get("disposition").and_then(Value::as_str),
        Some("invalid" | "superseded" | "not_reproducible")
    ) {
        return Err(
            "finding dismissal disposition must be invalid, superseded, or not_reproducible".into(),
        );
    }
    if !object.get("evidence").is_some_and(nonempty_json) {
        return Err("finding dismissal requires concrete recheck evidence".into());
    }
    let finding_hash = file_sha256(&dir.join("finding.json"))?;
    let final_hash = current_final_diff_sha256()?;
    if !final_hash.is_empty() {
        let reported = object
            .get("final_diff_sha256")
            .or_else(|| object.get("final_diff_hash"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !reported.eq_ignore_ascii_case(&final_hash) {
            return Err(format!(
                "finding dismissal must bind to final diff {final_hash}"
            ));
        }
        let compact = evidence
            .chars()
            .filter(|character| !character.is_whitespace())
            .collect::<String>()
            .to_lowercase();
        let expected = final_hash.to_lowercase();
        if ![
            format!("final-diff-sha256={expected}"),
            format!("\"final_diff_sha256\":\"{expected}\""),
            format!("\"final_diff_hash\":\"{expected}\""),
        ]
        .iter()
        .any(|marker| compact.contains(marker))
        {
            return Err(format!(
                "finding dismissal verifier {verified} is not bound to final diff {final_hash}"
            ));
        }
    }
    let payload = json!({"finding_id":id,"finding_hash":finding_hash,"verified_by":verified,"verifier_evidence":evidence_path.display().to_string(),"recheck":recheck,"notes":notes});
    write_json(&dir.join("dismissal.json"), &payload)?;
    println!("finding dismissed\t{id}\t{verified}");
    Ok(())
}

fn accepted_verdict(text: &str) -> bool {
    let first = text
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty())
        .unwrap_or("")
        .to_ascii_lowercase();
    first == "accepted"
        || first.starts_with("accepted ")
        || first
            .strip_prefix("verdict:")
            .is_some_and(|value| value.trim().starts_with("accepted"))
        || first
            .strip_prefix("verdict=")
            .is_some_and(|value| value.trim().starts_with("accepted"))
}

fn uid_authority_child() -> bool {
    env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1")
        && env::var("MULTIAGENT_AUTHORITY_SERVER_CHILD").as_deref() == Ok("1")
}

fn verifier_evidence(
    state: &Path,
    verified: &str,
    operation: &str,
) -> Result<(PathBuf, String), String> {
    let evidence_path = if uid_authority_child() {
        let directory = state.join("reviewer-evidence").join(verified);
        let metadata = read_env(&directory.join("evidence.env"))?;
        if env_value(&metadata, "role") != "reviewer"
            || env_value(&metadata, "access") != "read-only"
            || env_value(&metadata, "state") != "completed"
        {
            return Err(format!(
                "{operation} requires completed supervisor-sealed reviewer evidence: {verified}"
            ));
        }
        let workflow = env::var("MULTIAGENT_WORKFLOW_ID").unwrap_or_default();
        if !workflow.is_empty() && env_value(&metadata, "workflow_id") != workflow {
            return Err(format!(
                "{operation} reviewer evidence {verified} belongs to a different workflow"
            ));
        }
        let path = directory.join("last-message.txt");
        let expected = env_value(&metadata, "output_sha256");
        if expected.is_empty() || !file_sha256(&path)?.eq_ignore_ascii_case(expected) {
            return Err(format!(
                "{operation} reviewer evidence {verified} failed its supervisor seal"
            ));
        }
        path
    } else {
        state
            .join("subagents")
            .join(verified)
            .join("last-message.txt")
    };
    if !evidence_path.is_file() {
        return Err(format!(
            "{operation} requires verifier evidence: {verified}"
        ));
    }
    let evidence =
        fs::read_to_string(&evidence_path).map_err(io_error("read verifier evidence"))?;
    if !accepted_verdict(&evidence) {
        return Err(format!("{operation} verifier {verified} did not ACCEPT"));
    }
    Ok((evidence_path, evidence))
}

fn current_final_diff_sha256() -> Result<String, String> {
    if env::var("MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER").as_deref() != Ok("1") {
        return Ok(String::new());
    }
    let root = config::root()?;
    if !root.is_dir() {
        return Ok(String::new());
    }
    let base = env::var("MULTIAGENT_START_HEAD")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "HEAD".into());
    let diff = crate::snapshot::canonical_diff(&root, &base)?;
    if diff.iter().all(u8::is_ascii_whitespace) {
        return Ok(String::new());
    }
    use sha2::{Digest, Sha256};
    let mut digest = Sha256::new();
    digest.update(&diff);
    Ok(format!("{:x}", digest.finalize()))
}

fn current_diff_requires_route_probe() -> Result<bool, String> {
    if env::var("MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER").as_deref() != Ok("1") {
        return Ok(false);
    }
    let root = config::root()?;
    if !root.is_dir() {
        return Ok(false);
    }
    let base = env::var("MULTIAGENT_START_HEAD")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "HEAD".into());
    let diff = crate::snapshot::canonical_diff(&root, &base)?;
    let text = String::from_utf8_lossy(&diff);
    Ok(text.lines().any(|line| {
        let Some(path) = line
            .strip_prefix("diff --git a/")
            .and_then(|line| line.split_once(" b/").map(|(_, path)| path))
        else {
            return false;
        };
        path.split('/').any(|component| {
            let component = component.to_ascii_lowercase();
            let stem = component.split('.').next().unwrap_or(&component);
            matches!(stem, "route" | "routes" | "router" | "routers" | "routing")
        })
    }))
}

fn gate_check(args: &[String]) -> Result<(), String> {
    if !args.is_empty() {
        return Err("gate-check takes no arguments".into());
    }
    let state = config::state_dir()?;
    reconcile_terminal_verifiers(&state)?;
    let final_hash = current_final_diff_sha256()?;
    let route_probe_required = current_diff_requires_route_probe()?;
    let mut failed = false;

    for (name, status) in active_verifiers(&state)? {
        println!("reject\tactive-verifier\t{name}\t{status}");
        failed = true;
    }
    if let Some((verdict, name, evidence_path)) = latest_verifier_verdict(&state)? {
        match verdict.as_str() {
            "BLOCKING" => {
                println!(
                    "reject\tlatest-verifier-blocking\tverifier={name}\tevidence={}",
                    evidence_path.display()
                );
                failed = true;
            }
            "MISSING" => {
                println!(
                    "reject\tlatest-verifier-missing-verdict\tverifier={name}\tevidence={}",
                    evidence_path.display()
                );
                failed = true;
            }
            "ACCEPTED" if !final_hash.is_empty() => {
                let evidence = fs::read_to_string(&evidence_path).unwrap_or_default();
                if !evidence_matches_hash(&evidence, &final_hash) {
                    println!("reject\tlatest-verifier-final-diff-hash-mismatch\tverifier={name}\texpected={final_hash}\tevidence={}", evidence_path.display());
                    failed = true;
                }
                if route_probe_required
                    && !evidence.contains(&format!(
                        "route-integration-probe-passed: final-diff-sha256={final_hash}"
                    ))
                {
                    println!("reject\tmissing-route-integration-probe\tverifier={name}\texpected={final_hash}\tevidence={}", evidence_path.display());
                    failed = true;
                }
            }
            _ => {}
        }
    } else if !final_hash.is_empty() {
        println!("reject\tmissing-verifier-acceptance\texpected={final_hash}");
        failed = true;
    }

    let findings = state.join("findings");
    let todos = state.join("todos");
    for finding_dir in sorted_directories(&findings)? {
        let finding_id = finding_dir
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        let metadata = read_env(&finding_dir.join("finding.env")).unwrap_or_default();
        if env_value(&metadata, "severity") != "blocking" {
            continue;
        }
        if finding_dir.join("dismissal.json").is_file() {
            if !audit_dismissed_finding(&finding_dir, finding_id, &final_hash) {
                failed = true;
            }
            continue;
        }
        let mut found_todo = false;
        for todo_dir in sorted_directories(&todos)? {
            let todo_id = todo_dir
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("");
            let metadata = read_env(&todo_dir.join("todo.env")).unwrap_or_default();
            if env_value(&metadata, "source_finding_id") != finding_id {
                continue;
            }
            found_todo = true;
            let status = fs::read_to_string(todo_dir.join("status")).unwrap_or_default();
            if status.trim() != "closed" {
                println!(
                    "reject\topen-blocking-todo\tfinding={finding_id}\ttodo={todo_id}\tstatus={}",
                    status.trim()
                );
                failed = true;
            }
        }
        if !found_todo {
            println!("reject\tunqueued-blocking-finding\tfinding={finding_id}");
            failed = true;
        }
    }
    for todo_dir in sorted_directories(&todos)? {
        let todo_id = todo_dir
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        let status = fs::read_to_string(todo_dir.join("status")).unwrap_or_default();
        if status.trim() != "closed" {
            println!(
                "reject\topen-todo\ttodo={todo_id}\tstatus={}",
                status.trim()
            );
            failed = true;
        } else if !audit_closed_todo(&state, &todo_dir, todo_id, &final_hash) {
            failed = true;
        }
    }
    if failed {
        Err(String::new())
    } else {
        println!("accepted\tfinal-gate");
        Ok(())
    }
}

pub fn completion_gate_check() -> Result<(), String> {
    gate_check(&[])
}

fn verifier_dirs(state: &Path) -> Result<Vec<PathBuf>, String> {
    Ok(sorted_directories(&state.join("subagents"))?
        .into_iter()
        .filter(|path| {
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            name.contains("verifier") && !name.contains("build-verifier")
        })
        .collect())
}

fn report_verdict(text: &str) -> String {
    let first = text
        .lines()
        .find(|line| !line.trim().is_empty())
        .unwrap_or("")
        .trim();
    let lower = first.to_ascii_lowercase();
    let normalized = lower
        .strip_prefix("verdict:")
        .or_else(|| lower.strip_prefix("verdict="))
        .unwrap_or(&lower)
        .trim();
    if normalized == "accepted"
        || normalized
            .strip_prefix("accepted ")
            .is_some_and(verifier_hash_suffix)
    {
        return "ACCEPTED".into();
    }
    if normalized == "blocking"
        || normalized == "rejected"
        || normalized
            .strip_prefix("blocking ")
            .is_some_and(verifier_hash_suffix)
    {
        return "BLOCKING".into();
    }
    for line in text.lines() {
        let lower = line.trim().to_ascii_lowercase();
        let Some(value) = lower
            .strip_prefix("final recommendation:")
            .or_else(|| lower.strip_prefix("final-recommendation:"))
            .or_else(|| lower.strip_prefix("recommendation:"))
            .or_else(|| lower.strip_prefix("recommendation="))
        else {
            continue;
        };
        let value = value.trim();
        let recommendation = value
            .split(|character: char| {
                character.is_whitespace() || matches!(character, ';' | ',' | '.' | ':')
            })
            .next()
            .unwrap_or("");
        if matches!(recommendation, "accept" | "accepted") {
            return "ACCEPTED".into();
        }
        if matches!(recommendation, "block" | "blocking" | "reject" | "rejected") {
            return "BLOCKING".into();
        }
    }
    "MISSING".into()
}

fn verifier_hash_suffix(value: &str) -> bool {
    let mut parts = value.split_whitespace();
    parts.all(|part| {
        let Some((key, hash)) = part.split_once('=') else {
            return false;
        };
        matches!(
            key,
            "final_diff_sha256" | "final-diff-sha256" | "final_diff_hash" | "final-diff-hash"
        ) && hash.len() == 64
            && hash.chars().all(|value| value.is_ascii_hexdigit())
    })
}

fn reconcile_terminal_verifiers(state: &Path) -> Result<(), String> {
    for dir in verifier_dirs(state)? {
        let status_path = dir.join("status");
        let status = fs::read_to_string(&status_path).unwrap_or_default();
        if !matches!(status.trim(), "running" | "starting" | "pending") {
            continue;
        }
        let report = fs::read_to_string(dir.join("last-message.txt")).unwrap_or_default();
        match report_verdict(&report).as_str() {
            "ACCEPTED" => atomic_write(&status_path, "done\n")?,
            "BLOCKING" => atomic_write(&status_path, "blocked\n")?,
            _ => {}
        }
    }
    Ok(())
}

fn active_verifiers(state: &Path) -> Result<Vec<(String, String)>, String> {
    let mut values = Vec::new();
    for dir in verifier_dirs(state)? {
        let status = fs::read_to_string(dir.join("status")).unwrap_or_default();
        if matches!(status.trim(), "running" | "starting" | "pending") {
            values.push((
                dir.file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("")
                    .into(),
                status.trim().into(),
            ));
        }
    }
    Ok(values)
}

fn latest_verifier_verdict(state: &Path) -> Result<Option<(String, String, PathBuf)>, String> {
    let mut candidates = Vec::new();
    for dir in verifier_dirs(state)? {
        let path = dir.join("last-message.txt");
        let Ok(metadata) = path.metadata() else {
            continue;
        };
        let modified = metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH);
        let text = fs::read_to_string(&path).unwrap_or_default();
        candidates.push((
            modified,
            path.clone(),
            report_verdict(&text),
            dir.file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("")
                .to_string(),
        ));
    }
    candidates.sort_by(|left, right| (left.0, &left.1).cmp(&(right.0, &right.1)));
    Ok(candidates
        .pop()
        .map(|(_, path, verdict, name)| (verdict, name, path)))
}

fn evidence_matches_hash(text: &str, expected: &str) -> bool {
    let compact = text
        .chars()
        .filter(|value| !value.is_whitespace())
        .collect::<String>()
        .to_ascii_lowercase();
    let expected = expected.to_ascii_lowercase();
    [
        format!("final-diff-sha256={expected}"),
        format!("\"final_diff_hash\":\"{expected}\""),
        format!("\"final_diff_sha256\":\"{expected}\""),
    ]
    .iter()
    .any(|marker| compact.contains(marker))
}

fn audit_dismissed_finding(dir: &Path, id: &str, final_hash: &str) -> bool {
    let result = (|| -> Result<(), String> {
        let finding_bytes = fs::read(dir.join("finding.json")).map_err(io_error("read finding"))?;
        let dismissal: Value = serde_json::from_str(
            &fs::read_to_string(dir.join("dismissal.json")).map_err(io_error("read dismissal"))?,
        )
        .map_err(|error| format!("invalid dismissal JSON: {error}"))?;
        let recheck = dismissal
            .get("recheck")
            .and_then(Value::as_object)
            .ok_or_else(|| "missing recheck".to_string())?;
        if dismissal.get("finding_id").and_then(Value::as_str) != Some(id) {
            return Err("id-mismatch".into());
        }
        if dismissal.get("finding_hash").and_then(Value::as_str)
            != Some(&format!("{:x}", Sha256::digest(finding_bytes)))
        {
            return Err("hash-mismatch".into());
        }
        let named = ["finding_rechecked", "source_finding_id"]
            .iter()
            .filter_map(|key| recheck.get(*key).and_then(Value::as_str))
            .any(|value| value.trim() == id);
        if recheck.get("accepted") != Some(&Value::Bool(true))
            || !named
            || !matches!(
                recheck.get("disposition").and_then(Value::as_str),
                Some("invalid" | "superseded" | "not_reproducible")
            )
            || !recheck.get("evidence").is_some_and(nonempty_json)
        {
            return Err("invalid-recheck".into());
        }
        let evidence_path = dismissal
            .get("verifier_evidence")
            .and_then(Value::as_str)
            .ok_or_else(|| "missing verifier evidence".to_string())?;
        let evidence =
            fs::read_to_string(evidence_path).map_err(io_error("read verifier evidence"))?;
        if report_verdict(&evidence) != "ACCEPTED" {
            return Err("verifier-not-accepted".into());
        }
        if !final_hash.is_empty() {
            let reported = recheck
                .get("final_diff_sha256")
                .or_else(|| recheck.get("final_diff_hash"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if !reported.eq_ignore_ascii_case(final_hash)
                || !evidence_matches_hash(&evidence, final_hash)
            {
                return Err("final-diff-mismatch".into());
            }
        }
        Ok(())
    })();
    if let Err(reason) = result {
        println!("reject\tinvalid-finding-dismissal-evidence\tfinding={id}\treason={reason}");
        false
    } else {
        true
    }
}

fn audit_closed_todo(state: &Path, dir: &Path, id: &str, final_hash: &str) -> bool {
    let result = (|| -> Result<(), String> {
        let metadata = read_env(&dir.join("todo.env"))?;
        let source = env_value(&metadata, "source_finding_id");
        let expected_hash = env_value(&metadata, "source_finding_hash");
        let finding_path = state.join("findings").join(source).join("finding.json");
        if source.is_empty() || !finding_path.is_file() {
            return Err(format!(
                "closed-todo-missing-source-finding\ttodo={id}\tfinding={source}"
            ));
        }
        if expected_hash.is_empty() {
            return Err(format!(
                "closed-todo-missing-source-finding-hash\ttodo={id}"
            ));
        }
        if file_sha256(&finding_path)? != expected_hash {
            return Err(format!(
                "closed-todo-source-finding-hash-changed\ttodo={id}\tfinding={source}"
            ));
        }
        if !dir.join("resolution.json").is_file() {
            return Err(format!("closed-todo-missing-resolution\ttodo={id}"));
        }
        if !dir.join("closure.json").is_file() {
            return Err(format!("closed-todo-missing-verifier-closure\ttodo={id}"));
        }
        let resolution: Value = serde_json::from_str(
            &fs::read_to_string(dir.join("resolution.json"))
                .map_err(io_error("read resolution"))?,
        )
        .map_err(|error| format!("closed-todo-invalid-evidence\ttodo={id}\treason={error}"))?;
        let closure: Value = serde_json::from_str(
            &fs::read_to_string(dir.join("closure.json")).map_err(io_error("read closure"))?,
        )
        .map_err(|error| format!("closed-todo-invalid-evidence\ttodo={id}\treason={error}"))?;
        if resolution.get("todo_id").and_then(Value::as_str) != Some(id)
            || resolution.get("status").and_then(Value::as_str) != Some("resolved")
        {
            return Err(format!("closed-todo-invalid-resolution\ttodo={id}"));
        }
        let recheck = closure
            .get("recheck")
            .and_then(Value::as_object)
            .ok_or_else(|| format!("closed-todo-invalid-closure\ttodo={id}"))?;
        if closure.get("todo_id").and_then(Value::as_str) != Some(id)
            || recheck.get("accepted") != Some(&Value::Bool(true))
        {
            return Err(format!("closed-todo-invalid-closure\ttodo={id}"));
        }
        if closure.get("source_finding_hash").and_then(Value::as_str) != Some(expected_hash) {
            return Err(format!(
                "closed-todo-closure-finding-hash-mismatch\ttodo={id}"
            ));
        }
        if !final_hash.is_empty() {
            let reported = recheck
                .get("final_diff_sha256")
                .or_else(|| recheck.get("final_diff_hash"))
                .and_then(Value::as_str)
                .unwrap_or("");
            if !reported.eq_ignore_ascii_case(final_hash) {
                return Err(format!("closed-todo-final-diff-hash-mismatch\ttodo={id}"));
            }
        }
        let named = ["finding_rechecked", "source_finding_id"]
            .iter()
            .filter_map(|key| recheck.get(*key).and_then(Value::as_str))
            .any(|value| value.trim() == source);
        if !named {
            return Err(format!(
                "closed-todo-recheck-mismatch\ttodo={id}\tfinding={source}"
            ));
        }
        let resolution_commands = successful_commands(&resolution);
        let recheck_commands = successful_commands(closure.get("recheck").unwrap_or(&Value::Null));
        if let Some(command) = resolution_commands.difference(&recheck_commands).next() {
            return Err(format!(
                "closed-todo-recheck-missing-worker-command\ttodo={id}\tcmd={command}"
            ));
        }
        validate_required_commands(dir, "closed todo resolution", &resolution)
            .map_err(|error| format!("closed-todo-invalid-evidence\ttodo={id}\treason={error}"))?;
        validate_required_commands(
            dir,
            "closed todo verifier recheck",
            closure.get("recheck").unwrap_or(&Value::Null),
        )
        .map_err(|error| format!("closed-todo-invalid-evidence\ttodo={id}\treason={error}"))?;
        Ok(())
    })();
    if let Err(reason) = result {
        println!("reject\t{reason}");
        false
    } else {
        true
    }
}

fn todo_create(args: &[String]) -> Result<(), String> {
    let id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "todo-create requires TODO_ID".to_string())?;
    validate_name(id)?;
    let values = repeated_options(&args[1..], &["--done-criteria", "--required-command"])?;
    let source = values
        .get("--source-finding-id")
        .or_else(|| values.get("--finding"))
        .and_then(|items| items.first())
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "todo-create requires --source-finding-id FINDING_ID".to_string())?;
    validate_name(source)?;
    let task = option_required(&values, "--task", "todo-create requires --task TEXT")?;
    reject_newline("--task", task)?;
    let criteria = values.get("--done-criteria").cloned().unwrap_or_default();
    if criteria.is_empty() {
        return Err("todo-create requires at least one --done-criteria TEXT".into());
    }
    for value in &criteria {
        reject_newline("--done-criteria", value)?;
    }
    let context = option_first(&values, "--context");
    let context_file = option_first(&values, "--context-file");
    if !context.is_empty() && !context_file.is_empty() {
        return Err("todo-create accepts only one of --context or --context-file".into());
    }
    if !context_file.is_empty() && !Path::new(context_file).is_file() {
        return Err(format!("context file not found: {context_file}"));
    }
    let assigned = option_first(&values, "--assigned-to");
    if !assigned.is_empty() {
        validate_name(assigned)?;
    }
    let state = config::state_dir()?;
    let finding_json = state.join("findings").join(source).join("finding.json");
    if !state
        .join("findings")
        .join(source)
        .join("finding.env")
        .is_file()
    {
        return Err(format!("no finding: {source}"));
    }
    let source_hash = file_sha256(&finding_json)?;
    let base = state.join("todos");
    fs::create_dir_all(&base).map_err(io_error("create todos directory"))?;
    let _lock = lock_file(&base.join(".lock"), "todos")?;
    let dir = base.join(id);
    if dir.exists() {
        return Err(format!("todo already exists: {id}"));
    }
    fs::create_dir_all(&dir).map_err(io_error("create todo directory"))?;
    let created = timestamp();
    let updated = timestamp();
    let root = config::root()?.display().to_string();
    atomic_write(
        &dir.join("todo.env"),
        &format!("todo_id={id}\nsource_finding_id={source}\nsource_finding_hash={source_hash}\nassigned_to={assigned}\ntask={task}\ncreated_at={created}\nupdated_at={updated}\nroot={root}\n"),
    )?;
    atomic_write(
        &dir.join("done-criteria"),
        &criteria
            .iter()
            .map(|value| format!("{value}\n"))
            .collect::<String>(),
    )?;
    let mut commands = Vec::new();
    for criterion in &criteria {
        if let Some(command) = criterion
            .strip_prefix("run ")
            .map(str::trim)
            .filter(|v| !v.is_empty())
        {
            push_unique(&mut commands, command);
        }
    }
    for command in values.get("--required-command").into_iter().flatten() {
        reject_newline("--required-command", command)?;
        if command.is_empty() {
            return Err("todo-create --required-command may not be empty".into());
        }
        push_unique(&mut commands, command);
    }
    atomic_write(
        &dir.join("required-commands"),
        &commands
            .iter()
            .map(|v| format!("{v}\n"))
            .collect::<String>(),
    )?;
    let context_text = if !context_file.is_empty() {
        fs::read_to_string(context_file).map_err(io_error("read context file"))?
    } else {
        format!("{context}\n")
    };
    atomic_write(&dir.join("context.txt"), &context_text)?;
    let status = if assigned.is_empty() {
        "open"
    } else {
        "assigned"
    };
    atomic_write(&dir.join("status"), &format!("{status}\n"))?;
    write_todo_json(&dir)?;
    println!("todo created\t{id}\t{source}\t{status}");
    Ok(())
}

fn todo_show(args: &[String]) -> Result<(), String> {
    let id = one_name("todo-show", args)?;
    let dir = config::state_dir()?.join("todos").join(id);
    if !dir.join("todo.json").is_file() {
        return Err(format!("no todo: {id}"));
    }
    write_todo_json(&dir)?;
    print!(
        "{}",
        fs::read_to_string(dir.join("todo.json")).map_err(io_error("read todo"))?
    );
    Ok(())
}

fn todo_list(args: &[String]) -> Result<(), String> {
    let values = repeated_options(args, &[])?;
    let filter = option_first(&values, "--status");
    let base = config::state_dir()?.join("todos");
    for dir in sorted_directories(&base)? {
        let metadata = read_env(&dir.join("todo.env"))?;
        let status = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
        let status = status.trim();
        if !filter.is_empty() && filter != status {
            continue;
        }
        let assigned = env_value(&metadata, "assigned_to");
        println!(
            "{}\t{}\t{}\t{}\t{}",
            dir.file_name().and_then(|v| v.to_str()).unwrap_or(""),
            status,
            env_value(&metadata, "source_finding_id"),
            if assigned.is_empty() { "-" } else { assigned },
            env_value(&metadata, "task")
        );
    }
    Ok(())
}

fn todo_assign(args: &[String]) -> Result<(), String> {
    if args.len() != 2 {
        return Err("todo-assign requires TODO_ID NAME".into());
    }
    validate_name(&args[0])?;
    validate_name(&args[1])?;
    update_todo_state(&args[0], Some(&args[1]), "assigned")?;
    println!("todo assigned\t{}\t{}", args[0], args[1]);
    Ok(())
}

fn todo_status(args: &[String]) -> Result<(), String> {
    if args.len() != 2 {
        return Err("todo-status requires TODO_ID STATUS".into());
    }
    validate_name(&args[0])?;
    if !matches!(
        args[1].as_str(),
        "open" | "assigned" | "resolved" | "reopened" | "closed"
    ) {
        return Err(format!("invalid todo status: {}", args[1]));
    }
    update_todo_state(&args[0], None, &args[1])?;
    println!("todo status\t{}\t{}", args[0], args[1]);
    Ok(())
}

fn update_todo_state(id: &str, assigned_to: Option<&str>, status: &str) -> Result<(), String> {
    let base = config::state_dir()?.join("todos");
    fs::create_dir_all(&base).map_err(io_error("create todos directory"))?;
    let _lock = lock_file(&base.join(".lock"), "todos")?;
    let dir = base.join(id);
    let metadata_path = dir.join("todo.env");
    if !metadata_path.is_file() {
        return Err(format!("no todo: {id}"));
    }
    let mut metadata = read_env(&metadata_path)?;
    if let Some(assigned_to) = assigned_to {
        metadata.insert("assigned_to".into(), assigned_to.into());
    }
    metadata.insert("updated_at".into(), timestamp());
    let order = [
        "todo_id",
        "source_finding_id",
        "source_finding_hash",
        "assigned_to",
        "task",
        "created_at",
        "updated_at",
        "root",
    ];
    let text = order
        .iter()
        .map(|key| format!("{key}={}\n", env_value(&metadata, key)))
        .collect::<String>();
    atomic_write(&metadata_path, &text)?;
    atomic_write(&dir.join("status"), &format!("{status}\n"))?;
    write_todo_json(&dir)
}

fn resolution_create(args: &[String]) -> Result<(), String> {
    let todo_id = args
        .first()
        .filter(|value| !value.is_empty() && !value.starts_with("--"))
        .ok_or_else(|| "resolution-create requires TODO_ID".to_string())?;
    validate_name(todo_id)?;
    let values = repeated_options(&args[1..], &[])?;
    let worker = option_required(
        &values,
        "--worker",
        "resolution-create requires --worker NAME",
    )?;
    validate_name(worker)?;
    let status = option_required(
        &values,
        "--status",
        "resolution-create requires --status resolved|blocked",
    )?;
    if !matches!(status, "resolved" | "blocked") {
        return Err(format!("invalid resolution status: {status}"));
    }
    let validation_raw = option_required(
        &values,
        "--validation-json",
        "resolution-create requires --validation-json JSON",
    )?;
    let why = option_required(&values, "--why", "resolution-create requires --why TEXT")?;
    let state = config::state_dir()?;
    let todo_dir = state.join("todos").join(todo_id);
    if !todo_dir.join("todo.env").is_file()
        && env::var("MULTIAGENT_RESOLUTION_AUTOCREATE_TODO").as_deref() == Ok("1")
    {
        let finding_id = format!("auto-{todo_id}");
        if !state
            .join("findings")
            .join(&finding_id)
            .join("finding.env")
            .is_file()
        {
            let evidence = json!({"source":"resolution-create-autocreate","evidence":why});
            finding_create(&[finding_id.clone(),"--severity".into(),"blocking".into(),"--type".into(),"worker_resolution_without_registered_todo".into(),"--summary".into(),"Worker recorded a resolution for an unregistered todo.".into(),"--evidence-json".into(),serde_json::to_string(&evidence).map_err(json_error)?,"--required-resolution".into(),"Create durable todo state before assigning worker repairs; verifier must close the todo after rechecking the worker resolution.".into()])?;
        }
        todo_create(&[
            todo_id.into(),
            "--source-finding-id".into(),
            finding_id,
            "--task".into(),
            "Record and verify worker resolution evidence.".into(),
            "--context".into(),
            why.into(),
            "--done-criteria".into(),
            "worker records structured resolution evidence".into(),
            "--done-criteria".into(),
            "verifier closes todo only after objective recheck".into(),
        ])?;
    }
    if !todo_dir.join("todo.env").is_file() {
        return Err(format!("no todo: {todo_id}"));
    }
    reject_newline("--why", why)?;
    let validation: Value = serde_json::from_str(validation_raw)
        .map_err(|error| format!("invalid validation JSON: {error}"))?;
    validate_resolution(status, &validation)?;
    if status == "resolved" {
        validate_required_commands(&todo_dir, "worker resolution", &validation)?;
    }
    let base = state.join("todos");
    let _lock = lock_file(&base.join(".lock"), "todos")?;
    let created = timestamp();
    atomic_write(&todo_dir.join("resolution.env"),&format!("todo_id={todo_id}\nstatus={status}\nworker={worker}\nwhy_resolved={why}\ncreated_at={created}\n"))?;
    atomic_write(
        &todo_dir.join("validation.json"),
        &format!(
            "{}\n",
            serde_json::to_string(&validation).map_err(json_error)?
        ),
    )?;
    let changed = csv_unique(option_first(&values, "--changed"));
    atomic_write(
        &todo_dir.join("changed-paths"),
        &changed
            .iter()
            .map(|value| format!("{value}\n"))
            .collect::<String>(),
    )?;
    write_resolution_json(&todo_dir)?;
    update_todo_state_locked(
        &todo_dir,
        None,
        if status == "resolved" {
            "resolved"
        } else {
            "reopened"
        },
    )?;
    println!("resolution recorded\t{todo_id}\t{worker}\t{status}");
    Ok(())
}

fn todo_close(args: &[String]) -> Result<(), String> {
    let todo_id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "todo-close requires TODO_ID".to_string())?;
    validate_name(todo_id)?;
    let values = repeated_options(&args[1..], &[])?;
    let verified = option_required(
        &values,
        "--verified-by",
        "todo-close requires --verified-by NAME",
    )?;
    validate_name(verified)?;
    let recheck_raw = option_required(
        &values,
        "--recheck-json",
        "todo-close requires --recheck-json JSON",
    )?;
    let notes = option_first(&values, "--notes");
    reject_newline("--notes", notes)?;
    let state = config::state_dir()?;
    let base = state.join("todos");
    let dir = base.join(todo_id);
    if !dir.join("todo.env").is_file() {
        return Err(format!("no todo: {todo_id}"));
    }
    let status = fs::read_to_string(dir.join("status")).unwrap_or_default();
    if status.trim() != "resolved" {
        return Err("todo-close requires a resolved todo".into());
    }
    if !dir.join("resolution.json").is_file() {
        return Err("todo-close requires worker resolution evidence".into());
    }
    let recheck: Value = serde_json::from_str(recheck_raw)
        .map_err(|error| format!("invalid recheck JSON: {error}"))?;
    validate_closure(&recheck)?;
    validate_required_commands(&dir, "verifier recheck", &recheck)?;
    let require_verifier_evidence = uid_authority_child()
        || env::var("MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER").as_deref() == Ok("1");
    let (evidence_path, evidence) = if require_verifier_evidence {
        verifier_evidence(&state, verified, "todo-close")?
    } else {
        (
            state
                .join("subagents")
                .join(verified)
                .join("last-message.txt"),
            String::new(),
        )
    };
    let final_hash = current_final_diff_sha256()?;
    if !final_hash.is_empty() {
        let reported = recheck
            .get("final_diff_sha256")
            .or_else(|| recheck.get("final_diff_hash"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if !reported.eq_ignore_ascii_case(&final_hash) {
            return Err(format!("todo-close must bind to final diff {final_hash}"));
        }
        if !evidence_matches_hash(&evidence, &final_hash) {
            return Err(format!(
                "todo-close verifier {verified} is not bound to final diff {final_hash}"
            ));
        }
    }
    let metadata = read_env(&dir.join("todo.env"))?;
    let source = env_value(&metadata, "source_finding_id");
    let source_hash = env_value(&metadata, "source_finding_hash");
    let resolution: Value = serde_json::from_str(
        &fs::read_to_string(dir.join("resolution.json")).map_err(io_error("read resolution"))?,
    )
    .map_err(|error| format!("invalid resolution JSON: {error}"))?;
    validate_closure_matches(todo_id, source, source_hash, &resolution, &recheck)?;
    let _lock = lock_file(&base.join(".lock"), "todos")?;
    let created = timestamp();
    atomic_write(&dir.join("closure.env"),&format!("todo_id={todo_id}\nsource_finding_id={source}\nsource_finding_hash={source_hash}\nverified_by={verified}\nnotes={notes}\ncreated_at={created}\n"))?;
    atomic_write(
        &dir.join("recheck.json"),
        &format!("{}\n", serde_json::to_string(&recheck).map_err(json_error)?),
    )?;
    let closure = json!({"todo_id":todo_id,"source_finding_id":source,"source_finding_hash":if source_hash.is_empty(){Value::Null}else{Value::String(source_hash.into())},"verified_by":verified,"verifier_evidence":evidence_path.display().to_string(),"recheck":recheck,"notes":notes,"created_at":created});
    write_json(&dir.join("closure.json"), &closure)?;
    update_todo_state_locked(&dir, None, "closed")?;
    println!("todo closed\t{todo_id}\t{verified}");
    Ok(())
}

fn update_todo_state_locked(
    dir: &Path,
    assigned_to: Option<&str>,
    status: &str,
) -> Result<(), String> {
    let metadata_path = dir.join("todo.env");
    let mut metadata = read_env(&metadata_path)?;
    if let Some(value) = assigned_to {
        metadata.insert("assigned_to".into(), value.into());
    }
    metadata.insert("updated_at".into(), timestamp());
    let order = [
        "todo_id",
        "source_finding_id",
        "source_finding_hash",
        "assigned_to",
        "task",
        "created_at",
        "updated_at",
        "root",
    ];
    atomic_write(
        &metadata_path,
        &order
            .iter()
            .map(|key| format!("{key}={}\n", env_value(&metadata, key)))
            .collect::<String>(),
    )?;
    atomic_write(&dir.join("status"), &format!("{status}\n"))?;
    write_todo_json(dir)
}
fn write_resolution_json(dir: &Path) -> Result<(), String> {
    let metadata = read_env(&dir.join("resolution.env"))?;
    let validation: Value = serde_json::from_str(
        &fs::read_to_string(dir.join("validation.json")).map_err(io_error("read validation"))?,
    )
    .map_err(|error| format!("invalid validation JSON: {error}"))?;
    let changed = fs::read_to_string(dir.join("changed-paths"))
        .unwrap_or_default()
        .lines()
        .filter(|line| !line.is_empty())
        .map(String::from)
        .collect::<Vec<_>>();
    let payload = json!({"todo_id":env_value(&metadata,"todo_id"),"status":env_value(&metadata,"status"),"worker":env_value(&metadata,"worker"),"changed_paths":changed,"validation":validation,"why_resolved":env_value(&metadata,"why_resolved"),"created_at":env_value(&metadata,"created_at")});
    write_json(&dir.join("resolution.json"), &payload)
}
fn validate_resolution(status: &str, value: &Value) -> Result<(), String> {
    let items = value
        .as_array()
        .filter(|items| !items.is_empty())
        .ok_or_else(|| "validation JSON must be a non-empty array".to_string())?;
    for (index, item) in items.iter().enumerate() {
        let object = item
            .as_object()
            .ok_or_else(|| format!("validation item {index} must be an object"))?;
        let command = object
            .get("cmd")
            .and_then(Value::as_str)
            .is_some_and(|value| !value.trim().is_empty());
        let rc = object.get("rc");
        let source = ["source_reasoning", "source_evidence", "evidence"]
            .iter()
            .any(|key| object.get(*key).is_some_and(nonempty_json));
        if !((command && rc.is_some()) || source) {
            return Err(format!(
                "validation item {index} needs cmd+rc or source evidence"
            ));
        }
        if let Some(raw) = rc {
            let parsed = json_integer(raw)
                .ok_or_else(|| format!("validation item {index} rc must be an integer"))?;
            if status == "resolved" && parsed != 0 {
                return Err(format!(
                    "resolved validation item {index} has nonzero rc={parsed}"
                ));
            }
        }
    }
    Ok(())
}
fn validate_closure(value: &Value) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or_else(|| "recheck JSON must be an object".to_string())?;
    if object.get("accepted") != Some(&Value::Bool(true)) {
        return Err("recheck JSON must include accepted=true".into());
    }
    if ![
        "finding_rechecked",
        "source_finding_id",
        "commands",
        "evidence",
        "final_diff_hash",
    ]
    .iter()
    .any(|key| object.contains_key(*key))
    {
        return Err(
            "recheck JSON must name the finding, commands, evidence, or final diff hash".into(),
        );
    }
    if let Some(commands) = object.get("commands") {
        let commands = commands
            .as_array()
            .ok_or_else(|| "recheck commands must be an array when present".to_string())?;
        for (index, item) in commands.iter().enumerate() {
            let row = item
                .as_object()
                .ok_or_else(|| format!("recheck command {index} must be an object"))?;
            if !row
                .get("cmd")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty())
            {
                return Err(format!("recheck command {index} missing cmd"));
            }
            let rc = row
                .get("rc")
                .ok_or_else(|| format!("recheck command {index} missing rc"))?;
            let rc = json_integer(rc)
                .ok_or_else(|| format!("recheck command {index} rc must be an integer"))?;
            if rc != 0 {
                return Err(format!("recheck command {index} has nonzero rc={rc}"));
            }
        }
    }
    Ok(())
}
fn validate_closure_matches(
    todo: &str,
    source: &str,
    source_hash: &str,
    resolution: &Value,
    recheck: &Value,
) -> Result<(), String> {
    let object = recheck
        .as_object()
        .ok_or_else(|| "recheck JSON must be an object".to_string())?;
    let names = ["finding_rechecked", "source_finding_id"]
        .iter()
        .filter_map(|key| object.get(*key).and_then(Value::as_str))
        .collect::<Vec<_>>();
    if !names.contains(&source) {
        return Err(format!(
            "recheck JSON for todo {todo} must name source finding {source}"
        ));
    }
    if let Some(hash) = object
        .get("source_finding_hash")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
    {
        if hash != source_hash {
            return Err(format!(
                "recheck JSON for todo {todo} must match source finding hash {source_hash}"
            ));
        }
    }
    let resolution_commands = successful_commands(resolution);
    let recheck_commands = successful_commands(recheck);
    let missing = resolution_commands
        .difference(&recheck_commands)
        .cloned()
        .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(format!(
            "recheck JSON for todo {todo} must cover worker validation command(s): {}",
            missing.join(", ")
        ));
    }
    Ok(())
}
fn validate_required_commands(dir: &Path, label: &str, value: &Value) -> Result<(), String> {
    let required = fs::read_to_string(dir.join("required-commands")).unwrap_or_default();
    let covered = successful_commands(value);
    let todo = dir
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("");
    for command in required.lines().filter(|line| !line.is_empty()) {
        let normalized = command.split_whitespace().collect::<Vec<_>>().join(" ");
        if !covered.contains(&normalized) {
            return Err(format!(
                "{label} for todo {todo} missing required command: {command}"
            ));
        }
    }
    Ok(())
}
fn successful_commands(value: &Value) -> BTreeSet<String> {
    let items = if let Some(object) = value.as_object() {
        object
            .get("commands")
            .or_else(|| object.get("validation"))
            .and_then(Value::as_array)
    } else {
        value.as_array()
    };
    let mut output = BTreeSet::new();
    for item in items.into_iter().flatten() {
        let Some(row) = item.as_object() else {
            continue;
        };
        let rc = row
            .get("rc")
            .or_else(|| row.get("returncode"))
            .and_then(json_integer)
            .unwrap_or(0);
        if rc != 0 {
            continue;
        }
        let command = row
            .get("cmd")
            .or_else(|| row.get("command_text"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| {
                row.get("command").and_then(Value::as_array).map(|parts| {
                    parts
                        .iter()
                        .map(|part| part.as_str().unwrap_or(""))
                        .collect::<Vec<_>>()
                        .join(" ")
                })
            });
        if let Some(command) = command {
            let normalized = command.split_whitespace().collect::<Vec<_>>().join(" ");
            if !normalized.is_empty() {
                output.insert(normalized);
            }
        }
    }
    output
}
fn json_integer(value: &Value) -> Option<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str().and_then(|raw| raw.parse().ok()))
}
fn nonempty_json(value: &Value) -> bool {
    value
        .as_str()
        .map(str::trim)
        .is_some_and(|value| !value.is_empty())
        || (!value.is_null() && !value.is_string())
}

const LEASE_STATES: &[&str] = &[
    "planned",
    "running",
    "passed",
    "failed",
    "timed-out",
    "stale",
    "released",
];

fn validation_lease_acquire(args: &[String]) -> Result<(), String> {
    validation_lease_acquire_impl(args, false)
}

fn validation_lease_acquire_impl(args: &[String], quiet: bool) -> Result<(), String> {
    let id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "validation-lease-acquire requires LEASE_ID".to_string())?;
    validate_name(id)?;
    let values = repeated_options(&args[1..], &[])?;
    let owner = option_required(
        &values,
        "--owner",
        "validation-lease-acquire requires --owner NAME",
    )?;
    validate_name(owner)?;
    let target = option_required(
        &values,
        "--target",
        "validation-lease-acquire requires --target TEXT",
    )?;
    let command = option_required(
        &values,
        "--command",
        "validation-lease-acquire requires --command TEXT",
    )?;
    let state = {
        let value = option_first(&values, "--state");
        if value.is_empty() {
            "running"
        } else {
            value
        }
    };
    let risk = option_first(&values, "--resource-risk");
    for (label, value) in [
        ("--target", target),
        ("--command", command),
        ("--resource-risk", risk),
    ] {
        reject_newline(label, value)?;
    }
    validate_lease_state(state)?;
    if !matches!(state, "planned" | "running") {
        return Err("validation-lease-acquire state must be planned or running".into());
    }
    let base = config::state_dir()?.join("validation-leases");
    fs::create_dir_all(&base).map_err(io_error("create validation leases directory"))?;
    let _lock = lock_file(&base.join(".lock"), "validation leases")?;
    for dir in sorted_directories(&base)? {
        let existing_id = dir
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("");
        if existing_id == id {
            continue;
        }
        let metadata = read_env(&dir.join("lease.env"))?;
        if env_value(&metadata, "target") != target {
            continue;
        }
        let existing_state =
            fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
        let existing_state = existing_state.trim();
        if matches!(existing_state, "planned" | "running") {
            return Err(format!("validation lease conflict: target={target} lease={existing_id} owner={} state={existing_state}",env_value(&metadata,"owner")));
        }
    }
    let dir = base.join(id);
    if dir.exists() {
        return Err(format!("validation lease already exists: {id}"));
    }
    fs::create_dir_all(&dir).map_err(io_error("create validation lease"))?;
    let created = timestamp();
    let updated = timestamp();
    let root = config::root()?.display().to_string();
    atomic_write(&dir.join("lease.env"),&format!("lease_id={id}\nowner={owner}\ntarget={target}\ncommand={command}\nresource_risk={risk}\ncreated_at={created}\nupdated_at={updated}\nroot={root}\n"))?;
    atomic_write(&dir.join("result.json"), "{}\n")?;
    atomic_write(&dir.join("status"), &format!("{state}\n"))?;
    write_validation_lease_json(&dir)?;
    if !quiet {
        println!("validation lease acquired\t{id}\t{owner}\t{state}");
    }
    Ok(())
}

fn validation_lease_status(args: &[String]) -> Result<(), String> {
    validation_lease_status_impl(args, false)
}

fn validation_lease_status_impl(args: &[String], quiet: bool) -> Result<(), String> {
    if args.len() < 2 {
        return Err("validation-lease-status requires LEASE_ID STATUS".into());
    }
    let id = &args[0];
    validate_name(id)?;
    let state = &args[1];
    validate_lease_state(state)?;
    let values = repeated_options(&args[2..], &[])?;
    let base = config::state_dir()?.join("validation-leases");
    fs::create_dir_all(&base).map_err(io_error("create validation leases directory"))?;
    let _lock = lock_file(&base.join(".lock"), "validation leases")?;
    let dir = base.join(id);
    let metadata_path = dir.join("lease.env");
    if !metadata_path.is_file() {
        return Err(format!("no validation lease: {id}"));
    }
    let result = option_first(&values, "--result-json");
    if !result.is_empty() {
        let value: Value = serde_json::from_str(result)
            .map_err(|error| format!("invalid result JSON: {error}"))?;
        atomic_write(
            &dir.join("result.json"),
            &format!("{}\n", serde_json::to_string(&value).map_err(json_error)?),
        )?;
    }
    let mut metadata = read_env(&metadata_path)?;
    metadata.insert("updated_at".into(), timestamp());
    write_lease_env(&metadata_path, &metadata)?;
    atomic_write(&dir.join("status"), &format!("{state}\n"))?;
    write_validation_lease_json(&dir)?;
    if !quiet {
        println!("validation lease status\t{id}\t{state}");
    }
    Ok(())
}

fn validation_lease_show(args: &[String]) -> Result<(), String> {
    let id = one_lease("validation-lease-show", args)?;
    let dir = config::state_dir()?.join("validation-leases").join(id);
    if !dir.join("lease.json").is_file() {
        return Err(format!("no validation lease: {id}"));
    }
    write_validation_lease_json(&dir)?;
    print!(
        "{}",
        fs::read_to_string(dir.join("lease.json")).map_err(io_error("read validation lease"))?
    );
    Ok(())
}

fn validation_lease_list(args: &[String]) -> Result<(), String> {
    let values = repeated_options(args, &[])?;
    let filter = option_first(&values, "--state");
    if !filter.is_empty() {
        validate_lease_state(filter)?;
    }
    let base = config::state_dir()?.join("validation-leases");
    for dir in sorted_directories(&base)? {
        let metadata = read_env(&dir.join("lease.env"))?;
        let state = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
        let state = state.trim();
        if !filter.is_empty() && filter != state {
            continue;
        }
        println!(
            "{}\t{}\t{}\t{}\t{}",
            dir.file_name().and_then(|v| v.to_str()).unwrap_or(""),
            state,
            env_value(&metadata, "owner"),
            env_value(&metadata, "target"),
            env_value(&metadata, "command")
        );
    }
    Ok(())
}

fn validation_run(args: &[String]) -> Result<ExitCode, String> {
    use std::os::unix::process::CommandExt;

    let lease_id = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "validation-run requires LEASE_ID".to_string())?;
    validate_name(lease_id)?;
    let separator = args
        .iter()
        .position(|value| value == "--")
        .ok_or_else(|| "validation-run requires COMMAND after --".to_string())?;
    let values = repeated_options(&args[1..separator], &[]).map_err(|message| {
        message.replace(
            "unknown argument",
            "unknown validation-run argument before --",
        )
    })?;
    let command_args = &args[separator + 1..];
    if command_args.is_empty() {
        return Err("validation-run requires COMMAND after --".into());
    }
    let owner = option_required(&values, "--owner", "validation-run requires --owner NAME")?;
    validate_name(owner)?;
    let target = option_required(&values, "--target", "validation-run requires --target TEXT")?;
    let resource_risk = option_first(&values, "--resource-risk");
    let timeout_text = {
        let requested = option_first(&values, "--timeout-seconds");
        if requested.is_empty() {
            env::var("MULTIAGENT_VALIDATION_TIMEOUT_SECONDS").unwrap_or_else(|_| "600".into())
        } else {
            requested.into()
        }
    };
    let timeout_seconds = timeout_text
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| "validation-run --timeout-seconds must be a positive integer".to_string())?;
    let root = fs::canonicalize(config::root()?).map_err(|_| {
        format!(
            "validation-run root does not exist: {}",
            config::root().unwrap_or_default().display()
        )
    })?;
    if !root.is_dir() {
        return Err(format!(
            "validation-run root does not exist: {}",
            root.display()
        ));
    }

    let command_text = command_args.join(" ");
    validation_lease_acquire_impl(
        &[
            lease_id.clone(),
            "--owner".into(),
            owner.into(),
            "--target".into(),
            target.into(),
            "--command".into(),
            command_text.clone(),
            "--state".into(),
            "running".into(),
            "--resource-risk".into(),
            resource_risk.into(),
        ],
        true,
    )?;

    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("read system clock: {error}"))?
        .as_nanos();
    let temporary = env::temp_dir().join(format!(
        "multiagent-validation-run.{}.{}",
        std::process::id(),
        unique
    ));
    fs::create_dir_all(&temporary).map_err(io_error("create validation temporary directory"))?;
    let stdout_path = temporary.join("stdout");
    let stderr_path = temporary.join("stderr");
    let stdout_file = File::create(&stdout_path).map_err(io_error("create validation stdout"))?;
    let stderr_file = File::create(&stderr_path).map_err(io_error("create validation stderr"))?;
    let started_at = timestamp();
    let mut command = Command::new(&command_args[0]);
    command
        .args(&command_args[1..])
        .current_dir(&root)
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file))
        .process_group(0);
    let mut child = command
        .spawn()
        .map_err(io_error("start validation command"))?;
    let deadline = Instant::now() + Duration::from_secs(timeout_seconds);
    let (return_code, timed_out) = loop {
        if let Some(status) = child
            .try_wait()
            .map_err(io_error("wait for validation command"))?
        {
            break (status.code().unwrap_or(1), false);
        }
        if Instant::now() >= deadline {
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGTERM);
            }
            let term_deadline = Instant::now() + Duration::from_secs(10);
            loop {
                if child
                    .try_wait()
                    .map_err(io_error("wait for timed-out validation command"))?
                    .is_some()
                {
                    break;
                }
                if Instant::now() >= term_deadline {
                    unsafe {
                        libc::kill(-(child.id() as i32), libc::SIGKILL);
                    }
                    child.wait().map_err(io_error("reap validation command"))?;
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            break (124, true);
        }
        thread::sleep(Duration::from_millis(20));
    };
    let finished_at = timestamp();
    let stdout = fs::read(&stdout_path).map_err(io_error("read validation stdout"))?;
    let mut stderr = fs::read(&stderr_path).map_err(io_error("read validation stderr"))?;
    if timed_out {
        stderr.extend_from_slice(
            format!("\nvalidation-run timed out after {timeout_seconds} seconds\n").as_bytes(),
        );
    }
    std::io::stdout()
        .write_all(&stdout)
        .map_err(io_error("print validation stdout"))?;
    std::io::stderr()
        .write_all(&stderr)
        .map_err(io_error("print validation stderr"))?;
    let result = json!({
        "command": command_args,
        "command_text": command_text,
        "returncode": return_code,
        "cwd": root.display().to_string(),
        "started_at": started_at,
        "finished_at": finished_at,
        "timeout_seconds": timeout_seconds,
        "timed_out": timed_out,
        "stdout_tail": byte_tail(&stdout, 4000),
        "stderr_tail": byte_tail(&stderr, 4000),
    });
    let state = if timed_out {
        "timed-out"
    } else if return_code == 0 {
        "passed"
    } else {
        "failed"
    };
    validation_lease_status_impl(
        &[
            lease_id.clone(),
            state.into(),
            "--result-json".into(),
            serde_json::to_string(&result).map_err(json_error)?,
        ],
        true,
    )?;
    fs::remove_dir_all(&temporary).map_err(io_error("remove validation temporary directory"))?;
    Ok(ExitCode::from(return_code.clamp(0, 255) as u8))
}

fn byte_tail(bytes: &[u8], maximum: usize) -> String {
    let start = bytes.len().saturating_sub(maximum);
    String::from_utf8_lossy(&bytes[start..]).into_owned()
}

fn write_validation_lease_json(dir: &Path) -> Result<(), String> {
    let metadata = read_env(&dir.join("lease.env"))?;
    let state = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
    let result = serde_json::from_str::<Value>(
        &fs::read_to_string(dir.join("result.json")).unwrap_or_else(|_| "{}".into()),
    )
    .map_err(|error| format!("invalid validation result JSON: {error}"))?;
    let updated = env_value(&metadata, "updated_at");
    let payload = json!({"lease_id":env_value(&metadata,"lease_id"),"owner":env_value(&metadata,"owner"),"target":env_value(&metadata,"target"),"command":env_value(&metadata,"command"),"state":state.trim(),"resource_risk":env_value(&metadata,"resource_risk"),"result":result,"created_at":env_value(&metadata,"created_at"),"updated_at":if updated.is_empty(){env_value(&metadata,"created_at")}else{updated}});
    write_json(&dir.join("lease.json"), &payload)
}
fn write_lease_env(path: &Path, metadata: &BTreeMap<String, String>) -> Result<(), String> {
    let order = [
        "lease_id",
        "owner",
        "target",
        "command",
        "resource_risk",
        "created_at",
        "updated_at",
        "root",
    ];
    let text = order
        .iter()
        .map(|key| format!("{key}={}\n", env_value(metadata, key)))
        .collect::<String>();
    atomic_write(path, &text)
}
fn validate_lease_state(state: &str) -> Result<(), String> {
    if LEASE_STATES.contains(&state) {
        Ok(())
    } else {
        Err(format!("invalid validation lease status: {state}"))
    }
}
fn one_lease<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    if args.len() != 1 {
        return Err(format!("{command} requires LEASE_ID"));
    }
    validate_name(&args[0])?;
    Ok(&args[0])
}

fn write_todo_json(dir: &Path) -> Result<(), String> {
    let metadata = read_env(&dir.join("todo.env"))?;
    let lines = |name: &str| -> Vec<String> {
        fs::read_to_string(dir.join(name))
            .unwrap_or_default()
            .lines()
            .filter(|v| !v.is_empty())
            .map(String::from)
            .collect()
    };
    let context = fs::read_to_string(dir.join("context.txt")).unwrap_or_default();
    let status = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
    let nullable = |key: &str| {
        let value = env_value(&metadata, key);
        if value.is_empty() {
            Value::Null
        } else {
            Value::String(value.into())
        }
    };
    let updated = env_value(&metadata, "updated_at");
    let updated = if updated.is_empty() {
        env_value(&metadata, "created_at")
    } else {
        updated
    };
    let payload = json!({
        "todo_id": env_value(&metadata, "todo_id"),
        "source_finding_id": env_value(&metadata, "source_finding_id"),
        "source_finding_hash": nullable("source_finding_hash"),
        "assigned_to": nullable("assigned_to"),
        "status": status.trim(),
        "task": env_value(&metadata, "task"),
        "context": context,
        "done_criteria": lines("done-criteria"),
        "required_commands": lines("required-commands"),
        "created_at": env_value(&metadata, "created_at"),
        "updated_at": updated,
    });
    write_json(&dir.join("todo.json"), &payload)
}

fn validate_finding_evidence(severity: &str, kind: &str, evidence: &Value) -> Result<(), String> {
    let object = evidence
        .as_object()
        .ok_or_else(|| "evidence JSON must be an object".to_string())?;
    if object.is_empty() {
        return Err("evidence JSON must be non-empty".into());
    }
    let text_present = |key: &str| {
        object
            .get(key)
            .and_then(Value::as_str)
            .is_some_and(|v| !v.trim().is_empty())
    };
    let has_command = text_present("command") || text_present("cmd");
    let rc = object.get("returncode").or_else(|| object.get("rc"));
    let has_source = [
        "source_evidence",
        "source_reasoning",
        "evidence",
        "stderr_excerpt",
        "stdout_excerpt",
    ]
    .iter()
    .any(|key| text_present(key));
    if severity == "blocking" && !((has_command && rc.is_some()) || has_source) {
        return Err("blocking finding evidence needs command+returncode or source evidence".into());
    }
    if let Some(value) = rc {
        if value.as_i64().is_none() && value.as_str().and_then(|v| v.parse::<i64>().ok()).is_none()
        {
            return Err("finding evidence returncode/rc must be an integer".into());
        }
    }
    if severity == "blocking"
        && matches!(
            kind,
            "compile_failure" | "build_failure" | "test_failure" | "validation_failure"
        )
        && !(has_command && rc.is_some())
    {
        return Err(format!(
            "{kind} finding evidence requires command and returncode"
        ));
    }
    Ok(())
}

fn parse_assignment(args: &[String]) -> Result<AssignmentOptions, String> {
    let name = args
        .first()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "assignment-create requires NAME".to_string())?;
    let mut values = BTreeMap::<String, String>::new();
    let mut owned = Vec::new();
    let mut index = 1;
    while index < args.len() {
        let key = &args[index];
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("unknown assignment-create argument: {key}"))?;
        match key.as_str() {
            "--owned" => owned.push(value.clone()),
            "--assignment-id" | "--branch" | "--status" | "--start-commit" | "--role"
            | "--responsibility" | "--decision-id" | "--plan-id" | "--workflow-id"
            | "--node-id" | "--depends-on" => {
                values.insert(key.clone(), value.clone());
            }
            _ => return Err(format!("unknown assignment-create argument: {key}")),
        }
        index += 2;
    }
    let required = |key: &str, message: &str| {
        values
            .get(key)
            .filter(|value| !value.is_empty())
            .cloned()
            .ok_or_else(|| message.to_string())
    };
    Ok(AssignmentOptions {
        name: name.clone(),
        assignment_id: required(
            "--assignment-id",
            "assignment-create requires --assignment-id ID",
        )?,
        branch: required("--branch", "assignment-create requires --branch BRANCH")?,
        owned,
        status: values
            .get("--status")
            .cloned()
            .unwrap_or_else(|| "assigned".into()),
        start_commit: values.get("--start-commit").cloned().unwrap_or_default(),
        role: values
            .get("--role")
            .cloned()
            .unwrap_or_else(|| "exploitation".into()),
        responsibility: values.get("--responsibility").cloned().unwrap_or_default(),
        decision_id: values.get("--decision-id").cloned().unwrap_or_default(),
        plan_id: values.get("--plan-id").cloned().unwrap_or_default(),
        workflow_id: values.get("--workflow-id").cloned().unwrap_or_default(),
        node_id: values.get("--node-id").cloned().unwrap_or_default(),
        depends_on: values.get("--depends-on").cloned().unwrap_or_default(),
    })
}

fn reject_overlap(
    assignments: &Path,
    new_name: &str,
    new_role: &str,
    new_owned: &BTreeSet<String>,
) -> Result<(), String> {
    if matches!(new_role, "verifier" | "scout") {
        return Ok(());
    }
    for entry in fs::read_dir(assignments).map_err(io_error("read assignments"))? {
        let entry = entry.map_err(io_error("read assignment"))?;
        if !entry.path().is_dir() || entry.file_name() == new_name {
            continue;
        }
        let dir = entry.path();
        let status = fs::read_to_string(dir.join("status")).unwrap_or_else(|_| "unknown".into());
        if TERMINAL_STATUSES.contains(&status.trim()) {
            continue;
        }
        let metadata = read_env(&dir.join("assignment.env"))?;
        if matches!(
            metadata.get("role").map(String::as_str),
            Some("verifier" | "scout")
        ) {
            continue;
        }
        let existing = fs::read_to_string(dir.join("owned-paths")).unwrap_or_default();
        for left in new_owned {
            for right in existing.lines().filter(|line| !line.is_empty()) {
                if paths_overlap(left, right) {
                    let name = entry.file_name().to_string_lossy().into_owned();
                    return Err(format!("active assignment owned-path overlap: new={new_name} path={left} existing={name} status={} existing_path={right}",status.trim()));
                }
            }
        }
    }
    Ok(())
}

fn normalize_repo_path(root: &Path, requested: &Path) -> Result<String, String> {
    let absolute = if requested.is_absolute() {
        requested.to_path_buf()
    } else {
        root.join(requested)
    };
    let canonical = canonicalize_missing(&absolute)?;
    if canonical != root && !canonical.starts_with(root) {
        return Err(format!(
            "assigned path is outside MULTIAGENT_ROOT: {}",
            requested.display()
        ));
    }
    let relative = canonical
        .strip_prefix(root)
        .map_err(|_| "assigned path is outside MULTIAGENT_ROOT".to_string())?;
    if relative.as_os_str().is_empty() {
        return Err("assigned path may not be the whole repo root".into());
    }
    Ok(relative.to_string_lossy().trim_end_matches('/').to_string())
}

fn canonicalize_missing(path: &Path) -> Result<PathBuf, String> {
    if path.exists() {
        return fs::canonicalize(path).map_err(io_error("canonicalize assigned path"));
    }
    let mut ancestor = path;
    let mut missing = Vec::new();
    while !ancestor.exists() {
        missing.push(
            ancestor
                .file_name()
                .ok_or_else(|| format!("cannot resolve assigned path: {}", path.display()))?
                .to_os_string(),
        );
        ancestor = ancestor
            .parent()
            .ok_or_else(|| format!("cannot resolve assigned path: {}", path.display()))?;
    }
    let mut result = fs::canonicalize(ancestor).map_err(io_error("canonicalize assigned path"))?;
    for part in missing.into_iter().rev() {
        result.push(part);
    }
    let mut normalized = PathBuf::new();
    for component in result.components() {
        match component {
            Component::ParentDir => {
                normalized.pop();
            }
            Component::CurDir => {}
            other => normalized.push(other.as_os_str()),
        }
    }
    Ok(normalized)
}

fn resolve_commit(root: &Path, requested: &str) -> Result<String, String> {
    let revision = if requested.is_empty() {
        "HEAD".to_string()
    } else {
        format!("{requested}^{{commit}}")
    };
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .arg("rev-parse")
        .arg(&revision)
        .output()
        .map_err(io_error("run git rev-parse"))?;
    if !output.status.success() {
        return Err(if requested.is_empty() {
            "cannot resolve HEAD".into()
        } else {
            format!("invalid start commit: {requested}")
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn repeated_options(
    args: &[String],
    repeatable: &[&str],
) -> Result<BTreeMap<String, Vec<String>>, String> {
    let mut values = BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let key = &args[index];
        if !key.starts_with("--") {
            return Err(format!("unknown argument: {key}"));
        }
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("{key} requires a value"))?;
        if !repeatable.contains(&key.as_str()) && values.contains_key(key) {
            values.insert(key.clone(), vec![value.clone()]);
        } else {
            values
                .entry(key.clone())
                .or_insert_with(Vec::new)
                .push(value.clone());
        }
        index += 2;
    }
    Ok(values)
}
fn option_first<'a>(values: &'a BTreeMap<String, Vec<String>>, key: &str) -> &'a str {
    values
        .get(key)
        .and_then(|v| v.first())
        .map(String::as_str)
        .unwrap_or("")
}
fn option_required<'a>(
    values: &'a BTreeMap<String, Vec<String>>,
    key: &str,
    message: &str,
) -> Result<&'a str, String> {
    let value = option_first(values, key);
    if value.is_empty() {
        Err(message.into())
    } else {
        Ok(value)
    }
}
fn one_name<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    if args.len() != 1 {
        return Err(format!(
            "{command} requires {}",
            if command.starts_with("finding") {
                "FINDING_ID"
            } else {
                "TODO_ID"
            }
        ));
    }
    validate_name(&args[0])?;
    Ok(&args[0])
}
fn one_agent<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    if args.len() != 1 {
        return Err(format!("{command} requires NAME"));
    }
    validate_name(&args[0])?;
    Ok(&args[0])
}
fn reject_newline(label: &str, value: &str) -> Result<(), String> {
    if value.contains('\n') {
        Err(format!("{label} may not contain newlines"))
    } else {
        Ok(())
    }
}
fn csv_unique(raw: &str) -> Vec<String> {
    let mut output = Vec::new();
    for item in raw.split(',').map(str::trim).filter(|v| !v.is_empty()) {
        push_unique(&mut output, item);
    }
    output
}
fn push_unique(output: &mut Vec<String>, value: &str) {
    if !output.iter().any(|item| item == value) {
        output.push(value.into());
    }
}
fn lock_file(path: &Path, label: &str) -> Result<File, String> {
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(path)
        .map_err(io_error("open state lock"))?;
    file.lock_exclusive()
        .map_err(|error| format!("lock {label}: {error}"))?;
    Ok(file)
}
fn sorted_directories(base: &Path) -> Result<Vec<PathBuf>, String> {
    if !base.is_dir() {
        return Ok(Vec::new());
    }
    let mut dirs = fs::read_dir(base)
        .map_err(io_error("read state directory"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect::<Vec<_>>();
    dirs.sort();
    Ok(dirs)
}
fn env_value<'a>(values: &'a BTreeMap<String, String>, key: &str) -> &'a str {
    values.get(key).map(String::as_str).unwrap_or("")
}
fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let mut text = serde_json::to_string_pretty(value).map_err(json_error)?;
    text.push('\n');
    atomic_write(path, &text)
}
fn json_error(error: serde_json::Error) -> String {
    format!("serialize JSON: {error}")
}
fn file_sha256(path: &Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    use std::io::Read;
    let mut file = File::open(path).map_err(io_error("read artifact"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 8192];
    loop {
        let count = file.read(&mut buffer).map_err(io_error("read artifact"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
fn git_output(root: &Path, args: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(args)
        .output()
        .map_err(io_error("run git"))?;
    if !output.status.success() {
        return Err(format!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}
fn resolve_named_commit(root: &Path, requested: &str, label: &str) -> Result<String, String> {
    if requested.is_empty() {
        return git_output(root, &["rev-parse", "HEAD"]);
    }
    let revision = format!("{requested}^{{commit}}");
    git_output(root, &["rev-parse", &revision])
        .map_err(|_| format!("invalid {label} commit: {requested}"))
}
fn paths_overlap(left: &str, right: &str) -> bool {
    left == right
        || left
            .strip_prefix(right)
            .is_some_and(|suffix| suffix.starts_with('/'))
        || right
            .strip_prefix(left)
            .is_some_and(|suffix| suffix.starts_with('/'))
}
fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty()
        || name.starts_with('-')
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
    {
        return Err(format!("invalid subagent name: {name}"));
    }
    if name == "orchestrator" {
        return Err(format!("reserved subagent name: {name}"));
    }
    Ok(())
}
fn io_error(action: &'static str) -> impl Fn(std::io::Error) -> String {
    move |error| format!("{action}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn overlap_is_component_aware() {
        assert!(paths_overlap("src", "src/lib.rs"));
        assert!(!paths_overlap("src", "src2/lib.rs"));
    }
    #[test]
    fn names_reject_paths_and_reserved() {
        assert!(validate_name("worker-01").is_ok());
        assert!(validate_name("../worker").is_err());
        assert!(validate_name("orchestrator").is_err());
    }
}
