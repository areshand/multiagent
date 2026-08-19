use crate::{
    config,
    state::{atomic_write, atomic_write_bytes, read_env_optional, timestamp},
};
use fs2::FileExt;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};

const PHASES: &[&str] = &[
    "pre-implementation",
    "implementation",
    "post-implementation",
    "complete",
];
const ACTIVE: &[&str] = &["open", "assigned", "in-progress"];
const TODO_KINDS: &[&str] = &["direct", "evidence", "decision"];
const REVIEW_TYPES: &[&str] = &[
    "decision-authority",
    "decision-drift",
    "scope",
    "technical",
    "reflection",
];
const POST_REVIEWS: &[&str] = &["decision-drift", "scope", "technical", "reflection"];
const ENV_ORDER: &[&str] = &[
    "workflow_id",
    "phase",
    "iteration",
    "original_task",
    "original_task_sha256",
    "contract_scout",
    "contract_artifact",
    "contract_artifact_sha256",
    "preimplementation_gate",
    "decision_id",
    "plan_id",
    "decision_revision",
    "implementation_context",
    "implementation_context_sha256",
    "authority_review_id",
    "candidate_diff_hash",
    "reviewed_diff_hash",
    "resume_count",
    "created_at",
    "updated_at",
];
const TODO_HEADER: &str = "todo_id\tkind\tsummary\torigin\tstatus\tassignment_id\tresolution\treason_code\treason\tevidence\tauthority\tdestination\tresume_condition\titeration\tupdated_at";
const REVIEW_HEADER: &str =
    "review_id\ttype\tverdict\tdiff_hash\tevidence\titeration\trecorded_at\treviewer";

const USAGE: &str = r#"Usage:
  multiagent workflow init WORKFLOW_ID
  multiagent workflow init-or-resume WORKFLOW_ID --resume 0|1
  multiagent workflow status WORKFLOW_ID
  multiagent workflow contract-register WORKFLOW_ID --scout NAME
  multiagent workflow prepare-implementation WORKFLOW_ID --decision-id ID --plan-id ID --decision-revision REV --implementation-context PATH --authority-review ID
  multiagent workflow transition WORKFLOW_ID PHASE [--diff-hash HASH]
  multiagent workflow add-todo WORKFLOW_ID TODO_ID --kind KIND --summary TEXT [--origin TEXT]
  multiagent workflow todo-status WORKFLOW_ID TODO_ID STATUS [--assignment-id ID]
  multiagent workflow resolve-todo WORKFLOW_ID TODO_ID --resolution STATUS --evidence TEXT [OPTIONS]
  multiagent workflow record-review WORKFLOW_ID REVIEW_ID --type TYPE --verdict VERDICT [--diff-hash HASH] --evidence TEXT [--reviewer NAME]
  multiagent workflow gate WORKFLOW_ID implementation|completion [--decision-id ID] [--plan-id ID]
  multiagent workflow completion-check WORKFLOW_ID
  multiagent workflow value WORKFLOW_ID KEY"#;

pub fn run(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        println!("{USAGE}");
        return Err("missing command".into());
    }
    if matches!(args[0].as_str(), "-h" | "--help" | "help") {
        println!("{USAGE}");
        return Ok(());
    }
    match args[0].as_str() {
        "init" => initialize(&args[1..], false),
        "init-or-resume" => init_or_resume(&args[1..]),
        "status" => status(&args[1..]),
        "contract-register" => register_contract(&args[1..]),
        "prepare-implementation" => prepare(&args[1..]),
        "transition" => transition(&args[1..]),
        "add-todo" => add_todo(&args[1..]),
        "todo-status" => todo_status(&args[1..]),
        "resolve-todo" => resolve_todo(&args[1..]),
        "record-review" => record_review(&args[1..]),
        "gate" => gate(&args[1..]),
        "completion-check" => completion_ready(&args[1..]),
        "value" => value(&args[1..]),
        command => Err(format!("unknown command: {command}")),
    }
}

pub struct AssignmentContext {
    pub decision_revision: String,
    pub implementation_context: String,
    pub implementation_context_sha256: String,
}

pub struct SemanticEnvelope {
    pub original_task: String,
    pub original_task_sha256: String,
    pub contract_artifact: String,
    pub contract_artifact_sha256: String,
    pub candidate_diff_hash: String,
}

pub fn assignment_context(
    workflow_id: &str,
    decision_id: &str,
    plan_id: &str,
) -> Result<AssignmentContext, String> {
    let store = Store::configured()?;
    let state = implementation_gate_state(&store, workflow_id, decision_id, plan_id, false)?;
    Ok(AssignmentContext {
        decision_revision: state_value(&state, "decision_revision").to_string(),
        implementation_context: state_value(&state, "implementation_context").to_string(),
        implementation_context_sha256: state_value(&state, "implementation_context_sha256")
            .to_string(),
    })
}

pub fn semantic_envelope(workflow_id: &str) -> Result<SemanticEnvelope, String> {
    let store = Store::configured()?;
    let p = store.paths(workflow_id)?;
    let state = read_env(&p.state, workflow_id)?;
    validate_original_task(&state)?;
    validate_contract(&state)?;
    Ok(SemanticEnvelope {
        original_task: read_optional_artifact(state_value(&state, "original_task"))?,
        original_task_sha256: state_value(&state, "original_task_sha256").to_string(),
        contract_artifact: read_optional_artifact(state_value(&state, "contract_artifact"))?,
        contract_artifact_sha256: state_value(&state, "contract_artifact_sha256").to_string(),
        candidate_diff_hash: state_value(&state, "candidate_diff_hash").to_string(),
    })
}

struct Store {
    state_dir: PathBuf,
}
struct Paths {
    base: PathBuf,
    state: PathBuf,
    todos: PathBuf,
    reviews: PathBuf,
    events: PathBuf,
    lock: PathBuf,
}

impl Store {
    fn configured() -> Result<Self, String> {
        Ok(Self {
            state_dir: config::state_dir()?,
        })
    }
    fn paths(&self, id: &str) -> Result<Paths, String> {
        valid_id("workflow ID", id)?;
        let base = self.state_dir.join("workflows").join(id).join("lifecycle");
        Ok(Paths {
            state: base.join("lifecycle.env"),
            todos: base.join("todos.tsv"),
            reviews: base.join("reviews.tsv"),
            events: base.join("events.log"),
            lock: base.join(".lock"),
            base,
        })
    }
    fn lock(&self, paths: &Paths) -> Result<File, String> {
        fs::create_dir_all(&paths.base).map_err(io_error("create lifecycle directory"))?;
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(&paths.lock)
            .map_err(io_error("open lifecycle lock"))?;
        file.lock_exclusive().map_err(io_error("lock lifecycle"))?;
        Ok(file)
    }
}

#[derive(Clone)]
struct Todo {
    fields: [String; 15],
}
impl Todo {
    fn parse(line: &str) -> Self {
        Self {
            fields: parse_fields(line),
        }
    }
    fn line(&self) -> String {
        encode_fields(&self.fields)
    }
    fn get(&self, index: usize) -> &str {
        &self.fields[index]
    }
    fn set(&mut self, index: usize, value: &str) {
        self.fields[index] = value.to_string();
    }
}

#[derive(Clone)]
struct Review {
    fields: [String; 8],
}
impl Review {
    fn parse(line: &str) -> Self {
        Self {
            fields: parse_fields(line),
        }
    }
    fn line(&self) -> String {
        encode_fields(&self.fields)
    }
    fn get(&self, index: usize) -> &str {
        &self.fields[index]
    }
}

fn initialize(args: &[String], fixed_resume: bool) -> Result<(), String> {
    if args.len() != 1 {
        return Err("init requires WORKFLOW_ID".into());
    }
    initialize_id(&args[0], fixed_resume)
}

fn init_or_resume(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        return Err("init-or-resume requires WORKFLOW_ID".into());
    }
    let options = options(&args[1..])?;
    let resume = required(&options, "--resume")?;
    if !matches!(resume, "0" | "1") {
        return Err("argument --resume: invalid choice".into());
    }
    initialize_id(&args[0], resume == "1")
}

fn initialize_id(id: &str, resume: bool) -> Result<(), String> {
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    if p.state.is_file() {
        let mut state = read_env(&p.state, id)?;
        if !resume {
            return Err(format!("workflow already exists: {id}; use resume mode"));
        }
        let phase = state.get("phase").cloned().unwrap_or_default();
        if !PHASES.contains(&phase.as_str()) {
            return Err(format!("persisted workflow has invalid phase: {phase}"));
        }
        let count = state
            .get("resume_count")
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(0)
            + 1;
        state.insert("resume_count".into(), count.to_string());
        state.insert("updated_at".into(), timestamp());
        write_env(&p.state, &state)?;
        init_table(&p.todos, TODO_HEADER)?;
        init_table(&p.reviews, REVIEW_HEADER)?;
        event(&p.events, "workflow_resumed", &format!("phase={phase}"))?;
        println!("workflow resumed\t{id}\t{phase}");
        return Ok(());
    }
    let stamp = timestamp();
    let original_task_source = std::env::var("MULTIAGENT_ORIGINAL_TASK_FILE")
        .ok()
        .filter(|value| !value.is_empty())
        .map(PathBuf::from);
    let (original_task, original_task_sha256) = if let Some(source) = original_task_source {
        if !source.is_file() {
            return Err(format!(
                "original task artifact not found: {}",
                source.display()
            ));
        }
        let destination = p.base.join("original-task.md");
        let bytes = fs::read(&source).map_err(io_error("read original task artifact"))?;
        atomic_write_bytes(&destination, &bytes)?;
        (
            destination.display().to_string(),
            format!("{:x}", Sha256::digest(&bytes)),
        )
    } else {
        (String::new(), String::new())
    };
    let mut state = BTreeMap::new();
    for (key, value) in [
        ("workflow_id", id),
        ("phase", "pre-implementation"),
        ("iteration", "1"),
        ("original_task", original_task.as_str()),
        ("original_task_sha256", original_task_sha256.as_str()),
        ("contract_scout", ""),
        ("contract_artifact", ""),
        ("contract_artifact_sha256", ""),
        ("preimplementation_gate", "pending"),
        ("decision_id", ""),
        ("plan_id", ""),
        ("decision_revision", ""),
        ("implementation_context", ""),
        ("implementation_context_sha256", ""),
        ("authority_review_id", ""),
        ("candidate_diff_hash", ""),
        ("reviewed_diff_hash", ""),
        ("resume_count", "0"),
    ] {
        state.insert(key.into(), value.into());
    }
    state.insert("created_at".into(), stamp.clone());
    state.insert("updated_at".into(), stamp);
    write_env(&p.state, &state)?;
    init_table(&p.todos, TODO_HEADER)?;
    init_table(&p.reviews, REVIEW_HEADER)?;
    event(
        &p.events,
        "workflow_initialized",
        &format!("resume_requested={}", usize::from(resume)),
    )?;
    println!("workflow initialized\t{id}\tpre-implementation");
    Ok(())
}

fn register_contract(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        return Err("contract-register requires WORKFLOW_ID".into());
    }
    let id = &args[0];
    let options = options(&args[1..])?;
    let scout = required(&options, "--scout")?;
    valid_id("scout name", scout)?;
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let mut state = read_env(&p.state, id)?;
    if state_value(&state, "phase") != "pre-implementation" {
        return Err("contract-register requires phase=pre-implementation".into());
    }
    let secure = secure_reviewer_evidence();
    let directory = store
        .state_dir
        .join(if secure {
            "contract-evidence"
        } else {
            "subagents"
        })
        .join(scout);
    let metadata =
        read_simple_env(&directory.join(if secure { "evidence.env" } else { "meta.env" }))?;
    if state_value(&metadata, "role") != "scout"
        || state_value(&metadata, if secure { "access" } else { "codex_access" }) != "read-only"
    {
        return Err(format!(
            "contract artifact must come from a read-only scout role: {scout}"
        ));
    }
    if secure {
        if state_value(&metadata, "state") != "completed"
            || state_value(&metadata, "workflow_id") != id
        {
            return Err(format!(
                "contract scout evidence is not sealed for workflow {id}: {scout}"
            ));
        }
    } else {
        let status = fs::read_to_string(directory.join("status")).unwrap_or_default();
        if status.trim() != "finalized" || !directory.join("finalized_at").is_file() {
            return Err(format!("contract scout is not finalized: {scout}"));
        }
    }
    let artifact = directory.join("last-message.txt");
    let bytes = fs::read(&artifact)
        .map_err(|_| format!("contract scout final message is missing: {scout}"))?;
    let text = String::from_utf8(bytes.clone())
        .map_err(|_| format!("contract scout artifact is not UTF-8: {scout}"))?;
    let original_task = read_optional_artifact(state_value(&state, "original_task"))?;
    validate_contract_schema(&text, &original_task)?;
    let digest = format!("{:x}", Sha256::digest(&bytes));
    for (key, value) in [
        ("contract_scout", scout.to_string()),
        ("contract_artifact", artifact.display().to_string()),
        ("contract_artifact_sha256", digest.clone()),
        ("updated_at", timestamp()),
    ] {
        state.insert(key.into(), value);
    }
    write_env(&p.state, &state)?;
    event(
        &p.events,
        "contract_registered",
        &format!("scout={scout}\tartifact_sha256={digest}"),
    )?;
    println!("contract registered\t{id}\t{scout}\t{digest}");
    Ok(())
}

fn status(args: &[String]) -> Result<(), String> {
    let id = one_id("status", args)?;
    let p = Store::configured()?.paths(id)?;
    let text = fs::read_to_string(&p.state)
        .map_err(|_| format!("workflow lifecycle does not exist: {id}"))?;
    print!("{text}");
    println!(
        "active_todo_count={}",
        read_todos(&p.todos)?
            .iter()
            .filter(|r| active(r.get(4)))
            .count()
    );
    println!("review_count={}", read_reviews(&p.reviews)?.len());
    Ok(())
}

fn prepare(args: &[String]) -> Result<(), String> {
    if args.is_empty() {
        return Err("prepare-implementation requires WORKFLOW_ID".into());
    }
    let id = &args[0];
    let o = options(&args[1..])?;
    let decision = required(&o, "--decision-id")?;
    let plan = required(&o, "--plan-id")?;
    let revision = required(&o, "--decision-revision")?;
    let context_arg = required(&o, "--implementation-context")?;
    let authority = required(&o, "--authority-review")?;
    valid_id("decision ID", decision)?;
    valid_id("plan ID", plan)?;
    valid_id("review ID", authority)?;
    validate_committed_decision(decision, plan)?;
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let mut state = read_env(&p.state, id)?;
    if state_value(&state, "phase") != "pre-implementation" {
        return Err("prepare-implementation requires phase=pre-implementation".into());
    }
    let reviews = read_reviews(&p.reviews)?;
    if !reviews
        .iter()
        .any(|r| r.get(0) == authority && r.get(1) == "decision-authority" && r.get(2) == "pass")
    {
        return Err("prepare-implementation requires a passing decision-authority review".into());
    }
    let todos = read_todos(&p.todos)?;
    let blockers: Vec<&str> = todos
        .iter()
        .filter(|r| active(r.get(4)) && matches!(r.get(1), "evidence" | "decision"))
        .map(|r| r.get(0))
        .collect();
    if !blockers.is_empty() {
        return Err(format!(
            "pre-implementation blocked by active evidence/decision TODOs: {}",
            blockers.join(",")
        ));
    }
    let requested_context = absolute_path(context_arg)?;
    let context = fs::canonicalize(context_arg).map_err(|_| {
        format!(
            "approved implementation context not found: {}",
            requested_context.display()
        )
    })?;
    if !context.is_file() {
        return Err(format!(
            "approved implementation context not found: {}",
            context.display()
        ));
    }
    validate_original_task(&state)?;
    validate_contract(&state)?;
    let contract_path = state_value(&state, "contract_artifact");
    if !contract_path.is_empty() {
        let contract = fs::read_to_string(contract_path)
            .map_err(io_error("read registered contract artifact"))?;
        let approved = fs::read_to_string(&context)
            .map_err(io_error("read approved implementation context"))?;
        if !approved.contains(&contract) {
            return Err(
                "approved implementation context must contain the registered contract artifact verbatim"
                    .into(),
            );
        }
        let binding = format!(
            "contract-artifact-sha256={}",
            state_value(&state, "contract_artifact_sha256")
        );
        if !approved.lines().any(|line| line.trim() == binding) {
            return Err(format!(
                "approved implementation context is missing contract binding: {binding}"
            ));
        }
    }
    for (key, value) in [
        ("preimplementation_gate", "passed".to_string()),
        ("decision_id", decision.to_string()),
        ("plan_id", plan.to_string()),
        ("decision_revision", revision.to_string()),
        ("implementation_context", context.display().to_string()),
        ("implementation_context_sha256", sha256(&context)?),
        ("authority_review_id", authority.to_string()),
        ("updated_at", timestamp()),
    ] {
        state.insert(key.into(), value);
    }
    write_env(&p.state, &state)?;
    event(
        &p.events,
        "implementation_prepared",
        &format!("decision_id={decision}\tplan_id={plan}\treview_id={authority}"),
    )?;
    println!("implementation prepared\t{id}\t{decision}\t{plan}");
    Ok(())
}

fn transition(args: &[String]) -> Result<(), String> {
    if args.len() < 2 {
        return Err("transition requires WORKFLOW_ID PHASE".into());
    }
    let id = &args[0];
    let target = &args[1];
    if !PHASES.contains(&target.as_str()) {
        return Err(format!("invalid phase: {target}"));
    }
    if target == "complete" {
        return Err(
            "complete is supervisor-owned; request it with `multiagent orchestrator complete`"
                .into(),
        );
    }
    let o = options(&args[2..])?;
    let diff = o.get("--diff-hash").map(String::as_str).unwrap_or("");
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let mut state = read_env(&p.state, id)?;
    let current = state_value(&state, "phase").to_string();
    let allowed = matches!(
        (current.as_str(), target.as_str()),
        ("pre-implementation", "implementation")
            | ("implementation", "post-implementation")
            | ("post-implementation", "pre-implementation")
    );
    if !allowed {
        return Err(format!(
            "invalid lifecycle transition: {current} -> {target}"
        ));
    }
    if current == "pre-implementation" {
        implementation_gate_state(&store, id, "", "", true)?;
        state.insert("phase".into(), "implementation".into());
    } else if current == "implementation" {
        if diff.is_empty() {
            return Err("implementation -> post-implementation requires --diff-hash".into());
        }
        state.insert("phase".into(), "post-implementation".into());
        state.insert("candidate_diff_hash".into(), diff.into());
        state.insert("reviewed_diff_hash".into(), "".into());
    } else if target == "pre-implementation" {
        if !read_todos(&p.todos)?.iter().any(|r| active(r.get(4))) {
            return Err("post-implementation -> pre-implementation requires an active TODO".into());
        }
        let iteration = state_value(&state, "iteration").parse::<u64>().unwrap_or(1) + 1;
        for key in [
            "decision_revision",
            "implementation_context",
            "implementation_context_sha256",
            "authority_review_id",
            "candidate_diff_hash",
            "reviewed_diff_hash",
        ] {
            state.insert(key.into(), "".into());
        }
        state.insert("phase".into(), "pre-implementation".into());
        state.insert("iteration".into(), iteration.to_string());
        state.insert("preimplementation_gate".into(), "pending".into());
    }
    state.insert("updated_at".into(), timestamp());
    write_env(&p.state, &state)?;
    event(
        &p.events,
        "phase_transitioned",
        &format!(
            "from={current}\tto={target}\titeration={}",
            state_value(&state, "iteration")
        ),
    )?;
    println!("workflow transitioned\t{id}\t{current}\t{target}");
    Ok(())
}

fn add_todo(args: &[String]) -> Result<(), String> {
    if args.len() < 2 {
        return Err("add-todo requires WORKFLOW_ID TODO_ID".into());
    }
    let id = &args[0];
    let todo_id = &args[1];
    valid_id("TODO ID", todo_id)?;
    let o = options(&args[2..])?;
    let kind = required(&o, "--kind")?;
    let summary = required(&o, "--summary")?;
    let origin = o
        .get("--origin")
        .map(String::as_str)
        .unwrap_or("orchestrator");
    if !TODO_KINDS.contains(&kind) {
        return Err(format!("invalid TODO kind: {kind}"));
    }
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let state = read_env(&p.state, id)?;
    let mut rows = read_todos(&p.todos)?;
    if rows.iter().any(|r| r.get(0) == todo_id) {
        return Err(format!("TODO already exists: {todo_id}"));
    }
    rows.push(Todo {
        fields: [
            todo_id.clone(),
            kind.into(),
            summary.into(),
            origin.into(),
            "open".into(),
            "".into(),
            "".into(),
            "".into(),
            "".into(),
            "".into(),
            "".into(),
            "".into(),
            "".into(),
            state_value(&state, "iteration").into(),
            timestamp(),
        ],
    });
    write_todos(&p.todos, &rows)?;
    event(
        &p.events,
        "todo_added",
        &format!("todo_id={todo_id}\tkind={kind}"),
    )?;
    println!("TODO added\t{id}\t{todo_id}\t{kind}");
    Ok(())
}

fn todo_status(args: &[String]) -> Result<(), String> {
    if args.len() < 3 {
        return Err("todo-status requires WORKFLOW_ID TODO_ID STATUS".into());
    }
    let id = &args[0];
    let todo_id = &args[1];
    let status = &args[2];
    if !ACTIVE.contains(&status.as_str()) {
        return Err(format!("invalid active TODO status: {status}"));
    }
    let o = options(&args[3..])?;
    let assignment = o.get("--assignment-id").map(String::as_str).unwrap_or("");
    if matches!(status.as_str(), "assigned" | "in-progress") && assignment.is_empty() {
        return Err(format!("TODO status {status} requires --assignment-id"));
    }
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    read_env(&p.state, id)?;
    let mut rows = read_todos(&p.todos)?;
    let row = rows
        .iter_mut()
        .find(|r| r.get(0) == todo_id)
        .ok_or_else(|| format!("TODO does not exist: {todo_id}"))?;
    if !active(row.get(4)) {
        return Err(format!(
            "cannot reactivate resolved TODO without a new TODO: {todo_id}"
        ));
    }
    row.set(4, status);
    row.set(5, assignment);
    row.set(14, &timestamp());
    write_todos(&p.todos, &rows)?;
    event(
        &p.events,
        "todo_status_changed",
        &format!("todo_id={todo_id}\tstatus={status}"),
    )?;
    println!("TODO status\t{id}\t{todo_id}\t{status}");
    Ok(())
}

fn resolve_todo(args: &[String]) -> Result<(), String> {
    if args.len() < 2 {
        return Err("resolve-todo requires WORKFLOW_ID TODO_ID".into());
    }
    let id = &args[0];
    let todo_id = &args[1];
    let o = options(&args[2..])?;
    let resolution = required(&o, "--resolution")?;
    let evidence = required(&o, "--evidence")?;
    if !matches!(resolution, "completed" | "skipped") {
        return Err(format!("invalid TODO resolution: {resolution}"));
    }
    let reason_code = opt(&o, "--reason-code");
    let reason = opt(&o, "--reason");
    let authority = opt(&o, "--authority");
    let destination = opt(&o, "--destination");
    let resume = opt(&o, "--resume-condition");
    if resolution == "skipped" {
        if !matches!(reason_code, "out-of-scope" | "unavailable-now") {
            return Err("skipped TODO requires --reason-code out-of-scope|unavailable-now".into());
        }
        if reason.is_empty() || !matches!(authority, "orchestrator" | "user") {
            return Err("skipped TODO requires --reason and --authority orchestrator|user".into());
        }
        if reason_code == "unavailable-now" && destination.is_empty() && resume.is_empty() {
            return Err("unavailable-now skip requires --destination or --resume-condition".into());
        }
    }
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    read_env(&p.state, id)?;
    let mut rows = read_todos(&p.todos)?;
    let row = rows
        .iter_mut()
        .find(|r| r.get(0) == todo_id)
        .ok_or_else(|| format!("TODO does not exist: {todo_id}"))?;
    if !active(row.get(4)) {
        return Err(format!("TODO is already resolved: {todo_id}"));
    }
    for (index, value) in [
        (4, resolution),
        (6, resolution),
        (7, reason_code),
        (8, reason),
        (9, evidence),
        (10, authority),
        (11, destination),
        (12, resume),
    ] {
        row.set(index, value);
    }
    row.set(14, &timestamp());
    write_todos(&p.todos, &rows)?;
    event(
        &p.events,
        "todo_resolved",
        &format!("todo_id={todo_id}\tresolution={resolution}\treason_code={reason_code}"),
    )?;
    println!("TODO resolved\t{id}\t{todo_id}\t{resolution}");
    Ok(())
}

fn record_review(args: &[String]) -> Result<(), String> {
    if args.len() < 2 {
        return Err("record-review requires WORKFLOW_ID REVIEW_ID".into());
    }
    let id = &args[0];
    let review_id = &args[1];
    valid_id("review ID", review_id)?;
    let o = options(&args[2..])?;
    let kind = required(&o, "--type")?;
    let verdict = required(&o, "--verdict")?;
    let evidence = required(&o, "--evidence")?;
    let reviewer = opt(&o, "--reviewer");
    let requested_diff = opt(&o, "--diff-hash");
    if !REVIEW_TYPES.contains(&kind) {
        return Err(format!("invalid review type: {kind}"));
    }
    if !matches!(verdict, "pass" | "findings") {
        return Err(format!("invalid review verdict: {verdict}"));
    }
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let state = read_env(&p.state, id)?;
    let diff = if kind == "decision-authority" {
        if state_value(&state, "phase") != "pre-implementation" {
            return Err("decision-authority review requires phase=pre-implementation".into());
        }
        "-"
    } else {
        if state_value(&state, "phase") != "post-implementation" {
            return Err(format!("{kind} review requires phase=post-implementation"));
        }
        if requested_diff != state_value(&state, "candidate_diff_hash") {
            return Err(
                "post-implementation review diff hash does not match candidate diff".into(),
            );
        }
        requested_diff
    };
    if reviewer_evidence_required() {
        if reviewer.is_empty() {
            return Err("reviewer-backed lifecycle requires --reviewer NAME".into());
        }
        validate_reviewer_evidence(&store, id, reviewer, kind, verdict, diff)?;
    }
    let mut rows = read_reviews(&p.reviews)?;
    if rows.iter().any(|r| r.get(0) == review_id) {
        return Err(format!("review already exists: {review_id}"));
    }
    rows.push(Review {
        fields: [
            review_id.clone(),
            kind.into(),
            verdict.into(),
            diff.into(),
            evidence.into(),
            state_value(&state, "iteration").into(),
            timestamp(),
            reviewer.into(),
        ],
    });
    write_reviews(&p.reviews, &rows)?;
    event(
        &p.events,
        "review_recorded",
        &format!("review_id={review_id}\ttype={kind}\tverdict={verdict}\tdiff_hash={diff}"),
    )?;
    println!("review recorded\t{id}\t{review_id}\t{kind}\t{verdict}");
    Ok(())
}

fn gate(args: &[String]) -> Result<(), String> {
    if args.len() < 2 {
        return Err("gate requires WORKFLOW_ID implementation|completion".into());
    }
    let id = &args[0];
    let o = options(&args[2..])?;
    let store = Store::configured()?;
    match args[1].as_str() {
        "implementation" => {
            let state = implementation_gate_state(
                &store,
                id,
                opt(&o, "--decision-id"),
                opt(&o, "--plan-id"),
                false,
            )?;
            println!(
                "gate passed\t{id}\timplementation\t{}\t{}",
                state_value(&state, "decision_revision"),
                state_value(&state, "implementation_context_sha256")
            );
        }
        "completion" => {
            let state = completion_state(&store, id)?;
            println!(
                "gate passed\t{id}\tcompletion\t{}",
                state_value(&state, "candidate_diff_hash")
            );
        }
        other => return Err(format!("invalid gate: {other}")),
    }
    Ok(())
}

fn completion_ready(args: &[String]) -> Result<(), String> {
    let id = one_id("completion-check", args)?;
    let state = completion_state(&Store::configured()?, id)?;
    println!(
        "completion ready\t{id}\t{}",
        state_value(&state, "candidate_diff_hash")
    );
    Ok(())
}

/// The only operation allowed to seal a lifecycle. In UID-isolated runs this
/// executes inside the single-threaded supervisor process, while the lifecycle
/// lock prevents a concurrent phase mutation. All deterministic gates run
/// before the phase write, so a rejection leaves the workflow repairable.
pub fn supervisor_complete(id: &str) -> Result<String, String> {
    if config::lifecycle_enforced()
        && std::env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1")
        && std::env::var("MULTIAGENT_AUTHORITY_SERVER_CHILD").as_deref() != Ok("1")
    {
        return Err("lifecycle completion must execute inside the authority supervisor".into());
    }
    let store = Store::configured()?;
    let p = store.paths(id)?;
    let _lock = store.lock(&p)?;
    let before = read_env(&p.state, id)?;
    if state_value(&before, "phase") != "post-implementation" {
        return Err(format!(
            "supervisor completion requires phase=post-implementation, got {}",
            state_value(&before, "phase")
        ));
    }
    let state = completion_state(&store, id)?;
    crate::subagent::completion_gate_check()?;
    let mut state = state;
    let diff = state_value(&state, "candidate_diff_hash").to_string();
    state.insert("phase".into(), "complete".into());
    state.insert("reviewed_diff_hash".into(), diff.clone());
    state.insert("updated_at".into(), timestamp());
    write_env(&p.state, &state)?;
    event(
        &p.events,
        "phase_transitioned",
        &format!(
            "from=post-implementation\tto=complete\titeration={}\tauthority=supervisor",
            state_value(&state, "iteration")
        ),
    )?;
    Ok(diff)
}

fn value(args: &[String]) -> Result<(), String> {
    if args.len() != 2 {
        return Err("value requires WORKFLOW_ID KEY".into());
    }
    let p = Store::configured()?.paths(&args[0])?;
    let state = read_env(&p.state, &args[0])?;
    let value = state
        .get(&args[1])
        .ok_or_else(|| format!("unknown lifecycle field: {}", args[1]))?;
    println!("{value}");
    Ok(())
}

fn implementation_gate_state(
    store: &Store,
    id: &str,
    expected_decision: &str,
    expected_plan: &str,
    allow_pre: bool,
) -> Result<BTreeMap<String, String>, String> {
    let p = store.paths(id)?;
    let state = read_env(&p.state, id)?;
    let phase = state_value(&state, "phase");
    if phase != "implementation" && !(allow_pre && phase == "pre-implementation") {
        return Err(format!(
            "implementation gate requires phase=implementation, got {phase}"
        ));
    }
    if state_value(&state, "preimplementation_gate") != "passed" {
        return Err("implementation gate has not passed".into());
    }
    validate_context(&state)?;
    let todos = read_todos(&p.todos)?;
    let blockers: Vec<&str> = todos
        .iter()
        .filter(|r| active(r.get(4)) && matches!(r.get(1), "evidence" | "decision"))
        .map(|r| r.get(0))
        .collect();
    if !blockers.is_empty() {
        return Err(format!(
            "implementation blocked by active evidence/decision TODOs: {}",
            blockers.join(",")
        ));
    }
    if !expected_decision.is_empty() && expected_decision != state_value(&state, "decision_id") {
        return Err(format!(
            "assignment decision {expected_decision} does not match workflow decision {}",
            state_value(&state, "decision_id")
        ));
    }
    if !expected_plan.is_empty() && expected_plan != state_value(&state, "plan_id") {
        return Err(format!(
            "assignment plan {expected_plan} does not match workflow plan {}",
            state_value(&state, "plan_id")
        ));
    }
    Ok(state)
}

fn completion_state(store: &Store, id: &str) -> Result<BTreeMap<String, String>, String> {
    let p = store.paths(id)?;
    let state = read_env(&p.state, id)?;
    let phase = state_value(&state, "phase");
    if !matches!(phase, "post-implementation" | "complete") {
        return Err(format!(
            "completion requires phase=post-implementation, got {phase}"
        ));
    }
    let todos = read_todos(&p.todos)?;
    let active_rows: Vec<&str> = todos
        .iter()
        .filter(|r| active(r.get(4)))
        .map(|r| r.get(0))
        .collect();
    if !active_rows.is_empty() {
        return Err(format!(
            "completion blocked by active TODOs: {}",
            active_rows.join(",")
        ));
    }
    let diff = state_value(&state, "candidate_diff_hash");
    if diff.is_empty() {
        return Err("completion requires a candidate diff hash".into());
    }
    let iteration = state_value(&state, "iteration");
    let reviews = read_reviews(&p.reviews)?;
    let mut latest = BTreeMap::new();
    for review in reviews
        .iter()
        .filter(|r| r.get(5) == iteration && r.get(3) == diff)
    {
        latest.insert(review.get(1), review);
    }
    let findings: BTreeSet<&str> = latest
        .values()
        .filter(|review| review.get(2) == "findings")
        .map(|review| review.get(1))
        .collect();
    if !findings.is_empty() {
        return Err(format!(
            "completion blocked by current-diff review findings: {}",
            findings.into_iter().collect::<Vec<_>>().join(",")
        ));
    }
    let passed: BTreeSet<&str> = latest
        .values()
        .filter(|review| review.get(2) == "pass")
        .map(|review| review.get(1))
        .collect();
    let missing: Vec<&str> = POST_REVIEWS
        .iter()
        .copied()
        .filter(|kind| !passed.contains(kind))
        .collect();
    if !missing.is_empty() {
        return Err(format!(
            "completion requires passing current-diff reviews: {}",
            missing.join(",")
        ));
    }
    if reviewer_evidence_required() {
        let unrecorded = unrecorded_reviewer_findings(store, id, diff, &reviews)?;
        if !unrecorded.is_empty() {
            return Err(format!(
                "completion blocked by unrecorded reviewer findings: {}",
                unrecorded.join(",")
            ));
        }
        let unfinished = active_reviewers(store)?;
        if !unfinished.is_empty() {
            return Err(format!(
                "completion blocked by active reviewers: {}",
                unfinished.join(",")
            ));
        }
        for review in latest
            .values()
            .filter(|row| row.get(2) == "pass" && POST_REVIEWS.contains(&row.get(1)))
        {
            let reviewer = review.get(7);
            if reviewer.is_empty() {
                return Err(format!(
                    "passing {} review is missing durable reviewer evidence",
                    review.get(1)
                ));
            }
            validate_reviewer_evidence(store, id, reviewer, review.get(1), "pass", diff)?;
        }
    }
    validate_context(&state)?;
    Ok(state)
}

fn unrecorded_reviewer_findings(
    store: &Store,
    workflow_id: &str,
    diff: &str,
    reviews: &[Review],
) -> Result<Vec<String>, String> {
    let secure = secure_reviewer_evidence();
    let root = store.state_dir.join(if secure {
        "reviewer-evidence"
    } else {
        "subagents"
    });
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let mut unrecorded = Vec::new();
    for entry in fs::read_dir(&root).map_err(io_error("read subagents directory"))? {
        let dir = entry.map_err(io_error("read subagent entry"))?.path();
        if !dir.is_dir() {
            continue;
        }
        let metadata =
            read_simple_env(&dir.join(if secure { "evidence.env" } else { "meta.env" }))?;
        if state_value(&metadata, "role") != "reviewer"
            || state_value(&metadata, if secure { "access" } else { "codex_access" }) != "read-only"
        {
            continue;
        }
        let reviewer_workflow = state_value(&metadata, "workflow_id");
        if !reviewer_workflow.is_empty() && reviewer_workflow != workflow_id {
            continue;
        }
        if secure {
            if state_value(&metadata, "state") != "completed" {
                continue;
            }
        } else {
            let status = fs::read_to_string(dir.join("status")).unwrap_or_default();
            if status.trim() != "finalized" || !dir.join("finalized_at").is_file() {
                continue;
            }
        }
        let message = fs::read_to_string(dir.join("last-message.txt")).unwrap_or_default();
        let reviewer = dir
            .file_name()
            .and_then(|value| value.to_str())
            .unwrap_or("unknown");
        for kind in POST_REVIEWS {
            let marker = format!("review-record: type={kind} verdict=findings diff={diff}");
            if !message
                .lines()
                .any(|line| review_marker_matches(line, &marker))
            {
                continue;
            }
            let recorded = reviews.iter().any(|row| {
                row.get(1) == *kind
                    && row.get(2) == "findings"
                    && row.get(3) == diff
                    && row.get(7) == reviewer
            });
            if !recorded {
                unrecorded.push(format!("{reviewer}:{kind}"));
            }
        }
    }
    unrecorded.sort();
    unrecorded.dedup();
    Ok(unrecorded)
}

fn reviewer_evidence_required() -> bool {
    config::lifecycle_enforced()
}

fn validate_reviewer_evidence(
    store: &Store,
    workflow_id: &str,
    reviewer: &str,
    kind: &str,
    verdict: &str,
    diff: &str,
) -> Result<(), String> {
    valid_id("reviewer name", reviewer)?;
    let secure = secure_reviewer_evidence();
    let dir = store
        .state_dir
        .join(if secure {
            "reviewer-evidence"
        } else {
            "subagents"
        })
        .join(reviewer);
    let metadata = read_simple_env(&dir.join(if secure { "evidence.env" } else { "meta.env" }))?;
    if state_value(&metadata, "role") != "reviewer"
        || state_value(&metadata, if secure { "access" } else { "codex_access" }) != "read-only"
    {
        return Err(format!(
            "reviewer evidence must come from a read-only reviewer role: {reviewer}"
        ));
    }
    if secure {
        if state_value(&metadata, "state") != "completed"
            || state_value(&metadata, "workflow_id") != workflow_id
        {
            return Err(format!(
                "reviewer evidence is not sealed for workflow {workflow_id}: {reviewer}"
            ));
        }
    } else {
        let status = fs::read_to_string(dir.join("status"))
            .map_err(|_| format!("reviewer status is missing: {reviewer}"))?;
        if status.trim() != "finalized" || !dir.join("finalized_at").is_file() {
            return Err(format!("reviewer is not finalized: {reviewer}"));
        }
    }
    let message = fs::read_to_string(dir.join("last-message.txt"))
        .map_err(|_| format!("reviewer final message is missing: {reviewer}"))?;
    let marker = format!("review-record: type={kind} verdict={verdict} diff={diff}");
    if !message
        .lines()
        .any(|line| review_marker_matches(line, &marker))
    {
        return Err(format!(
            "reviewer {reviewer} final message is missing marker: {marker}"
        ));
    }
    if matches!(kind, "decision-authority" | "technical") {
        let p = store.paths(workflow_id)?;
        let state = read_env(&p.state, workflow_id)?;
        validate_original_task(&state)?;
        validate_contract(&state)?;
        let contract_hash = state_value(&state, "contract_artifact_sha256");
        if !contract_hash.is_empty() {
            let contract_marker =
                format!("contract-review: artifact-sha256={contract_hash} verdict=pass");
            if verdict == "pass"
                && !message
                    .lines()
                    .any(|line| review_marker_matches(line, &contract_marker))
            {
                return Err(format!(
                    "reviewer {reviewer} final message is missing marker: {contract_marker}"
                ));
            }
        }
    }
    Ok(())
}

fn secure_reviewer_evidence() -> bool {
    std::env::var("MULTIAGENT_UID_SANDBOX").as_deref() == Ok("1")
        && std::env::var("MULTIAGENT_AUTHORITY_SERVER_CHILD").as_deref() == Ok("1")
}

fn review_marker_matches(line: &str, marker: &str) -> bool {
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
    value == marker
}

fn active_reviewers(store: &Store) -> Result<Vec<String>, String> {
    let secure = secure_reviewer_evidence();
    let root = store.state_dir.join(if secure {
        "launch-authorizations"
    } else {
        "subagents"
    });
    if !root.is_dir() {
        return Ok(Vec::new());
    }
    let mut active = Vec::new();
    for entry in fs::read_dir(&root).map_err(io_error("read subagents directory"))? {
        let dir = entry.map_err(io_error("read subagent entry"))?.path();
        if !dir.is_dir() {
            continue;
        }
        let metadata = read_simple_env(&dir.join(if secure { "launch.env" } else { "meta.env" }))?;
        if state_value(&metadata, "role") != "reviewer" {
            continue;
        }
        let status = if secure {
            state_value(&metadata, "state").to_string()
        } else {
            fs::read_to_string(dir.join("status")).unwrap_or_default()
        };
        if matches!(
            status.trim(),
            "starting" | "pending" | "registered" | "running"
        ) {
            active.push(
                dir.file_name()
                    .and_then(|value| value.to_str())
                    .unwrap_or("unknown")
                    .to_string(),
            );
        }
    }
    active.sort();
    Ok(active)
}

fn validate_context(state: &BTreeMap<String, String>) -> Result<(), String> {
    validate_original_task(state)?;
    validate_contract(state)?;
    let text = state_value(state, "implementation_context");
    if text.is_empty() {
        return Err("implementation gate requires approved implementation context".into());
    }
    let path = Path::new(text);
    if !path.is_file() {
        return Err(format!(
            "approved implementation context is missing: {}",
            path.display()
        ));
    }
    if sha256(path)? != state_value(state, "implementation_context_sha256") {
        return Err(
            "approved implementation context changed after pre-implementation approval".into(),
        );
    }
    Ok(())
}

fn validate_original_task(state: &BTreeMap<String, String>) -> Result<(), String> {
    validate_artifact_binding(
        "original task",
        state_value(state, "original_task"),
        state_value(state, "original_task_sha256"),
    )
}

fn validate_contract(state: &BTreeMap<String, String>) -> Result<(), String> {
    validate_artifact_binding(
        "contract",
        state_value(state, "contract_artifact"),
        state_value(state, "contract_artifact_sha256"),
    )
}

fn validate_artifact_binding(label: &str, path: &str, expected: &str) -> Result<(), String> {
    if path.is_empty() && expected.is_empty() {
        return Ok(());
    }
    if path.is_empty() || expected.is_empty() {
        return Err(format!("{label} artifact binding is incomplete"));
    }
    let path = Path::new(path);
    if !path.is_file() {
        return Err(format!("{label} artifact is missing: {}", path.display()));
    }
    if sha256(path)? != expected {
        return Err(format!("{label} artifact changed after registration"));
    }
    Ok(())
}

fn read_optional_artifact(path: &str) -> Result<String, String> {
    if path.is_empty() {
        return Ok(String::new());
    }
    fs::read_to_string(path).map_err(io_error("read semantic artifact"))
}

fn validate_contract_schema(text: &str, original_task: &str) -> Result<(), String> {
    let original = original_task.to_ascii_lowercase();
    let requires_embedding_rule = original.contains("embedded unnecessarily")
        || original.contains("must not embed")
        || original.contains("should not embed")
        || original.contains("without embedding");
    let header = text
        .lines()
        .any(|line| line.trim() == "contract-artifact: version=1");
    let rules = text
        .lines()
        .filter(|line| line.trim_start().starts_with("contract-rule:"))
        .collect::<Vec<_>>();
    if !header {
        return Err("contract scout artifact is missing `contract-artifact: version=1`".into());
    }
    if rules.is_empty() {
        if requires_embedding_rule {
            return Err("contract scout artifact must contain structured `contract-rule:` lines, including a positive structural rule and a separate `polarity=must-not` rule covering the requested embedding prohibition".into());
        }
        return Err("contract scout artifact must contain at least one `contract-rule:`".into());
    }
    for rule in &rules {
        if !rule.contains(" id=")
            || !(rule.contains(" polarity=must ") || rule.contains(" polarity=must-not "))
            || !rule.contains(" statement=")
            || !rule.contains(" evidence=")
        {
            return Err(format!("invalid structured contract rule: {}", rule.trim()));
        }
    }
    if requires_embedding_rule {
        let positive = rules.iter().find_map(|rule| {
            (rule.contains(" polarity=must ") && rule.contains(" structure=positive ")).then(|| {
                (
                    contract_rule_field(rule, "owner"),
                    contract_rule_field(rule, "member"),
                    contract_rule_field(rule, "member-type"),
                )
            })
        });
        let negative = rules.iter().find_map(|rule| {
            let statement = contract_rule_statement(rule).to_ascii_lowercase();
            (rule.contains(" polarity=must-not ")
                && rule.contains(" structure=negative ")
                && statement.contains("embed"))
            .then(|| {
                (
                    contract_rule_field(rule, "owner"),
                    contract_rule_field(rule, "embedded-type"),
                )
            })
        });
        let Some((positive_owner, member, member_type)) = positive else {
            return Err("contract scout artifact requires a machine-readable positive structural rule with `structure=positive owner=OWNER member=FIELD member-type=TYPE`".into());
        };
        let Some((negative_owner, embedded_type)) = negative else {
            return Err("contract scout artifact requires a machine-readable negative structural rule with `structure=negative owner=OWNER embedded-type=TYPE` covering the embedding prohibition".into());
        };
        if positive_owner.is_empty()
            || member.is_empty()
            || member_type.is_empty()
            || negative_owner.is_empty()
            || embedded_type.is_empty()
            || positive_owner != negative_owner
            || member_type != embedded_type
        {
            return Err("contract scout embedding rules must name one matching owner/type pair and a concrete replacement member".into());
        }
    }
    Ok(())
}

fn contract_rule_field<'a>(rule: &'a str, key: &str) -> &'a str {
    rule.split_whitespace()
        .find_map(|part| part.strip_prefix(&format!("{key}=")))
        .unwrap_or("")
}

fn contract_rule_statement(rule: &str) -> &str {
    rule.split_once(" statement=")
        .map(|(_, value)| value)
        .and_then(|value| {
            value
                .split_once(" evidence=")
                .map(|(statement, _)| statement)
        })
        .unwrap_or("")
}
fn validate_committed_decision(decision: &str, plan: &str) -> Result<(), String> {
    let dir = config::state_dir()?.join("decisions").join(decision);
    let meta = read_simple_env(&dir.join("decision.env"))?;
    let outcome = read_simple_env(&dir.join("outcome.env"))?;
    if state_value(&meta, "status") != "committed" {
        return Err(format!("decision ledger is not committed: {decision}"));
    }
    let selected = state_value(&outcome, "selected_plan");
    if selected != plan {
        return Err(format!(
            "decision ledger selected plan {} does not match requested plan {plan}",
            if selected.is_empty() {
                "missing"
            } else {
                selected
            }
        ));
    }
    Ok(())
}

fn read_env(path: &Path, id: &str) -> Result<BTreeMap<String, String>, String> {
    if !path.is_file() {
        return Err(format!("workflow lifecycle does not exist: {id}"));
    }
    read_simple_env(path)
}
fn read_simple_env(path: &Path) -> Result<BTreeMap<String, String>, String> {
    read_env_optional(path)
}
fn write_env(path: &Path, state: &BTreeMap<String, String>) -> Result<(), String> {
    let text = ENV_ORDER
        .iter()
        .map(|k| format!("{k}={}\n", state_value(state, k)))
        .collect::<String>();
    atomic_write(path, &text)
}
fn init_table(path: &Path, header: &str) -> Result<(), String> {
    if !path.exists() {
        atomic_write(path, &format!("{header}\n"))
    } else {
        Ok(())
    }
}
fn read_todos(path: &Path) -> Result<Vec<Todo>, String> {
    read_lines(path).map(|rows| rows.into_iter().map(|line| Todo::parse(&line)).collect())
}
fn read_reviews(path: &Path) -> Result<Vec<Review>, String> {
    read_lines(path).map(|rows| rows.into_iter().map(|line| Review::parse(&line)).collect())
}
fn read_lines(path: &Path) -> Result<Vec<String>, String> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    Ok(fs::read_to_string(path)
        .map_err(io_error("read table"))?
        .lines()
        .skip(1)
        .filter(|line| !line.is_empty())
        .map(String::from)
        .collect())
}
fn write_todos(path: &Path, rows: &[Todo]) -> Result<(), String> {
    write_rows(path, TODO_HEADER, rows.iter().map(Todo::line))
}
fn write_reviews(path: &Path, rows: &[Review]) -> Result<(), String> {
    write_rows(path, REVIEW_HEADER, rows.iter().map(Review::line))
}
fn write_rows(path: &Path, header: &str, rows: impl Iterator<Item = String>) -> Result<(), String> {
    let mut text = format!("{header}\n");
    for row in rows {
        text.push_str(&row);
        text.push('\n');
    }
    atomic_write(path, &text)
}
fn event(path: &Path, name: &str, detail: &str) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(io_error("append lifecycle event"))?;
    writeln!(file, "{}\t{}\t{}", timestamp(), name, detail)
        .map_err(io_error("append lifecycle event"))
}
fn sha256(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(io_error("read implementation context"))?;
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 8192];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(io_error("read implementation context"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    Ok(format!("{:x}", digest.finalize()))
}

fn parse_fields<const N: usize>(line: &str) -> [String; N] {
    let mut values: Vec<String> = line.split('\t').map(String::from).collect();
    values.resize(N, String::new());
    values.truncate(N);
    values.try_into().unwrap_or_else(|_| unreachable!())
}
fn encode_fields<const N: usize>(fields: &[String; N]) -> String {
    fields.join("\t")
}
fn active(status: &str) -> bool {
    ACTIVE.contains(&status)
}
fn state_value<'a>(state: &'a BTreeMap<String, String>, key: &str) -> &'a str {
    state.get(key).map(String::as_str).unwrap_or("")
}
fn valid_id(label: &str, value: &str) -> Result<(), String> {
    if value.is_empty()
        || !value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
    {
        Err(format!("invalid {label}: {value}"))
    } else {
        Ok(())
    }
}
fn one_id<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    if args.len() != 1 {
        Err(format!("{command} requires WORKFLOW_ID"))
    } else {
        Ok(&args[0])
    }
}
fn options(args: &[String]) -> Result<BTreeMap<String, String>, String> {
    let mut out = BTreeMap::new();
    let mut i = 0;
    while i < args.len() {
        let key = &args[i];
        if !key.starts_with("--") {
            return Err(format!("unexpected argument: {key}"));
        }
        let value = args
            .get(i + 1)
            .ok_or_else(|| format!("{key} requires a value"))?;
        out.insert(key.clone(), value.clone());
        i += 2;
    }
    Ok(out)
}
fn required<'a>(options: &'a BTreeMap<String, String>, key: &str) -> Result<&'a str, String> {
    options
        .get(key)
        .filter(|v| !v.is_empty())
        .map(String::as_str)
        .ok_or_else(|| format!("{} requires {key}", key.trim_start_matches("--")))
}
fn opt<'a>(options: &'a BTreeMap<String, String>, key: &str) -> &'a str {
    options.get(key).map(String::as_str).unwrap_or("")
}
fn absolute_path(value: &str) -> Result<PathBuf, String> {
    let path = Path::new(value);
    if path.is_absolute() {
        Ok(path.into())
    } else {
        Ok(std::env::current_dir()
            .map_err(io_error("determine current directory"))?
            .join(path))
    }
}
fn io_error(action: &'static str) -> impl Fn(std::io::Error) -> String {
    move |error| format!("{action}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn ids_match_contract() {
        assert!(valid_id("workflow ID", "WF-1.ok").is_ok());
        assert!(valid_id("workflow ID", "../bad").is_err());
    }
    #[test]
    fn table_rows_round_trip() {
        let row = Todo {
            fields: std::array::from_fn(|i| format!("v{i}")),
        };
        assert_eq!(Todo::parse(&row.line()).fields, row.fields);
    }

    #[test]
    fn review_markers_allow_only_cosmetic_markdown_wrapping() {
        let marker = "review-record: type=scope verdict=pass diff=abc";
        assert!(review_marker_matches(marker, marker));
        assert!(review_marker_matches(&format!("3. `{marker}`"), marker));
        assert!(review_marker_matches(&format!("- `{marker}`"), marker));
        assert!(!review_marker_matches(
            &format!("evidence includes {marker}"),
            marker
        ));
    }

    #[test]
    fn embedding_tasks_require_positive_and_negative_structural_rules() {
        let task = "WidgetConfig fields were embedded unnecessarily.";
        assert!(
            validate_contract_schema("contract-artifact: version=1\n", task)
                .unwrap_err()
                .contains("embedding prohibition")
        );
        let incomplete = "contract-artifact: version=1\n\
contract-rule: id=R1 polarity=must statement=WidgetConfig exposes named fields evidence=task\n\
contract-rule: id=R2 polarity=must-not statement=Old WidgetConfig names must not remain evidence=task mentions unnecessary embedding\n";
        assert!(validate_contract_schema(incomplete, task)
            .unwrap_err()
            .contains("machine-readable positive structural rule"));

        let complete = "contract-artifact: version=1\n\
contract-rule: id=R1 polarity=must structure=positive owner=Widget member=cfg member-type=WidgetConfig statement=Widget must store WidgetConfig in the named cfg field evidence=source\n\
contract-rule: id=R2 polarity=must-not structure=negative owner=Widget embedded-type=WidgetConfig statement=Widget must not anonymously embed WidgetConfig evidence=task\n";
        assert!(validate_contract_schema(&complete, task).is_ok());

        let mismatched = "contract-artifact: version=1\n\
contract-rule: id=R1 polarity=must structure=positive owner=Widget member=cfg member-type=WidgetConfig statement=Widget has a named cfg field evidence=source\n\
contract-rule: id=R2 polarity=must-not structure=negative owner=Other embedded-type=RouterConfig statement=Other must not embed RouterConfig evidence=task\n";
        assert!(validate_contract_schema(mismatched, task)
            .unwrap_err()
            .contains("matching owner/type pair"));
    }
}
