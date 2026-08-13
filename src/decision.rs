use crate::config;
use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const USAGE: &str = r#"Usage:
  bin/decision.sh init DECISION_ID --title TEXT [--owner NAME]
  bin/decision.sh add-alternative DECISION_ID --plan-id PLAN_ID --summary TEXT --proposed-by AGENT [--branch BRANCH] [--assignment-name NAME] [--expected-outcome TEXT] [--risk TEXT]
  bin/decision.sh add-assumption DECISION_ID --assumption-id ID --statement TEXT [--confidence VALUE] [--validation-method TEXT] [--expected-signal TEXT]
  bin/decision.sh commit DECISION_ID --selected-plan PLAN_ID --reason TEXT [--rollback-policy TEXT] [--reflection-due TEXT]
  bin/decision.sh record-metric DECISION_ID --name NAME [--expected VALUE] [--actual VALUE]
  bin/decision.sh reflect DECISION_ID --recommendation continue|adjust|rollback|pivot --reason TEXT [--follow-up-assignment NAME]
  bin/decision.sh show DECISION_ID
  bin/decision.sh list"#;

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
        "init" => init(&args[1..]),
        "add-alternative" => add_alternative(&args[1..]),
        "add-assumption" => add_assumption(&args[1..]),
        "commit" => commit(&args[1..]),
        "record-metric" => record_metric(&args[1..]),
        "reflect" => reflect(&args[1..]),
        "show" => show(&args[1..]),
        "list" => list(),
        command => Err(format!("unknown command: {command}")),
    }
}

struct Store {
    base: PathBuf,
}

impl Store {
    fn configured() -> Result<Self, String> {
        Ok(Self {
            base: config::state_dir()?.join("decisions"),
        })
    }

    fn decision_dir(&self, decision_id: &str) -> PathBuf {
        self.base.join(decision_id)
    }

    fn lock(&self) -> Result<File, String> {
        fs::create_dir_all(&self.base).map_err(io_error("create decision state directory"))?;
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(self.base.join(".lock"))
            .map_err(io_error("open decision lock"))?;
        file.lock_exclusive()
            .map_err(io_error("lock decision state"))?;
        Ok(file)
    }

    fn exists(&self, decision_id: &str) -> bool {
        self.decision_dir(decision_id)
            .join("decision.env")
            .is_file()
    }

    fn status(&self, decision_id: &str) -> Result<String, String> {
        read_env(&self.decision_dir(decision_id).join("decision.env"))?
            .get("status")
            .cloned()
            .ok_or_else(|| format!("decision status is missing: {decision_id}"))
    }

    fn log_event(&self, decision_id: &str, event: &str) -> Result<(), String> {
        let path = self.decision_dir(decision_id).join("events.log");
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .map_err(io_error("append decision event"))?;
        writeln!(file, "{}\t{}", timestamp(), event).map_err(io_error("append decision event"))
    }
}

fn init(args: &[String]) -> Result<(), String> {
    let (decision_id, options) = id_and_options("init", args, &["title", "owner"])?;
    validate_id("decision ID", decision_id)?;
    let title = required(&options, "title", "init requires --title")?;
    let owner = value(&options, "owner");
    reject_newline("--title", title)?;
    reject_newline("--owner", owner)?;

    let store = Store::configured()?;
    let _lock = store.lock()?;
    if store.exists(decision_id) {
        return Err(format!("decision already exists: {decision_id}"));
    }
    let directory = store.decision_dir(decision_id);
    fs::create_dir_all(&directory).map_err(io_error("create decision directory"))?;
    atomic_write(
        &directory.join("decision.env"),
        &format!(
            "decision_id={decision_id}\ntitle={title}\nowner={owner}\nstatus=open\ncreated_at={}\n",
            timestamp()
        ),
    )?;
    atomic_write(
        &directory.join("alternatives.tsv"),
        "plan_id\tsummary\tproposed_by\tbranch\tassignment_name\texpected_outcome\trisk\tadded_at\n",
    )?;
    atomic_write(
        &directory.join("assumptions.tsv"),
        "assumption_id\tstatement\tconfidence\tvalidation_method\texpected_signal\tadded_at\n",
    )?;
    atomic_write(
        &directory.join("metrics.tsv"),
        "name\texpected\tactual\trecorded_at\n",
    )?;
    store.log_event(
        decision_id,
        &format!("decision_created\ttitle={title}\towner={owner}"),
    )?;
    println!("decision created\t{decision_id}\t{title}");
    Ok(())
}

fn add_alternative(args: &[String]) -> Result<(), String> {
    let allowed = [
        "plan-id",
        "summary",
        "proposed-by",
        "branch",
        "assignment-name",
        "expected-outcome",
        "risk",
    ];
    let (decision_id, options) = id_and_options("add-alternative", args, &allowed)?;
    validate_id("decision ID", decision_id)?;
    let plan_id = required(&options, "plan-id", "add-alternative requires --plan-id")?;
    let summary = required(&options, "summary", "add-alternative requires --summary")?;
    let proposed_by = required(
        &options,
        "proposed-by",
        "add-alternative requires --proposed-by",
    )?;
    validate_id("plan ID", plan_id)?;
    for (label, current) in [
        ("--plan-id", plan_id),
        ("--summary", summary),
        ("--proposed-by", proposed_by),
        ("--branch", value(&options, "branch")),
        ("--assignment-name", value(&options, "assignment-name")),
        ("--expected-outcome", value(&options, "expected-outcome")),
        ("--risk", value(&options, "risk")),
    ] {
        reject_newline(label, current)?;
    }

    let store = Store::configured()?;
    let _lock = store.lock()?;
    require_open(&store, decision_id, "add alternatives")?;
    let path = store.decision_dir(decision_id).join("alternatives.tsv");
    if tsv_first_column_contains(&path, plan_id)? {
        return Err(format!("plan ID already exists: {plan_id}"));
    }
    append_tsv(
        &path,
        &[
            plan_id,
            summary,
            proposed_by,
            value(&options, "branch"),
            value(&options, "assignment-name"),
            value(&options, "expected-outcome"),
            value(&options, "risk"),
            &timestamp(),
        ],
    )?;
    store.log_event(
        decision_id,
        &format!("alternative_added\tplan_id={plan_id}\tproposed_by={proposed_by}"),
    )?;
    println!("alternative added\t{decision_id}\t{plan_id}\t{summary}");
    Ok(())
}

fn add_assumption(args: &[String]) -> Result<(), String> {
    let allowed = [
        "assumption-id",
        "statement",
        "confidence",
        "validation-method",
        "expected-signal",
    ];
    let (decision_id, options) = id_and_options("add-assumption", args, &allowed)?;
    validate_id("decision ID", decision_id)?;
    let assumption_id = required(
        &options,
        "assumption-id",
        "add-assumption requires --assumption-id",
    )?;
    let statement = required(&options, "statement", "add-assumption requires --statement")?;
    validate_id("assumption ID", assumption_id)?;
    for (label, current) in [
        ("--assumption-id", assumption_id),
        ("--statement", statement),
        ("--confidence", value(&options, "confidence")),
        ("--validation-method", value(&options, "validation-method")),
        ("--expected-signal", value(&options, "expected-signal")),
    ] {
        reject_newline(label, current)?;
    }

    let store = Store::configured()?;
    let _lock = store.lock()?;
    require_open(&store, decision_id, "add assumptions")?;
    let path = store.decision_dir(decision_id).join("assumptions.tsv");
    if tsv_first_column_contains(&path, assumption_id)? {
        return Err(format!("assumption ID already exists: {assumption_id}"));
    }
    append_tsv(
        &path,
        &[
            assumption_id,
            statement,
            value(&options, "confidence"),
            value(&options, "validation-method"),
            value(&options, "expected-signal"),
            &timestamp(),
        ],
    )?;
    store.log_event(
        decision_id,
        &format!("assumption_added\tassumption_id={assumption_id}"),
    )?;
    println!("assumption added\t{decision_id}\t{assumption_id}\t{statement}");
    Ok(())
}

fn commit(args: &[String]) -> Result<(), String> {
    let allowed = [
        "selected-plan",
        "reason",
        "rollback-policy",
        "reflection-due",
    ];
    let (decision_id, options) = id_and_options("commit", args, &allowed)?;
    validate_id("decision ID", decision_id)?;
    let selected_plan = required(&options, "selected-plan", "commit requires --selected-plan")?;
    let reason = required(&options, "reason", "commit requires --reason")?;
    validate_id("plan ID", selected_plan)?;
    for (label, current) in [
        ("--selected-plan", selected_plan),
        ("--reason", reason),
        ("--rollback-policy", value(&options, "rollback-policy")),
        ("--reflection-due", value(&options, "reflection-due")),
    ] {
        reject_newline(label, current)?;
    }

    let store = Store::configured()?;
    let _lock = store.lock()?;
    require_open(&store, decision_id, "commit")?;
    let directory = store.decision_dir(decision_id);
    if !tsv_first_column_contains(&directory.join("alternatives.tsv"), selected_plan)? {
        return Err(format!("selected plan does not exist: {selected_plan}"));
    }
    let stamp = timestamp();
    rewrite_status(
        &directory.join("decision.env"),
        &["status=committed", &format!("committed_at={stamp}")],
    )?;
    atomic_write(
        &directory.join("outcome.env"),
        &format!(
            "selected_plan={selected_plan}\nreason={reason}\nrollback_policy={}\nreflection_due={}\ncommitted_at={}\nstatus=implementation\n",
            value(&options, "rollback-policy"),
            value(&options, "reflection-due"),
            timestamp()
        ),
    )?;
    store.log_event(
        decision_id,
        &format!("decision_committed\tselected_plan={selected_plan}\treason={reason}"),
    )?;
    println!("decision committed\t{decision_id}\t{selected_plan}\t{reason}");
    Ok(())
}

fn record_metric(args: &[String]) -> Result<(), String> {
    let (decision_id, options) =
        id_and_options("record-metric", args, &["name", "expected", "actual"])?;
    validate_id("decision ID", decision_id)?;
    let name = required(&options, "name", "record-metric requires --name")?;
    for (label, current) in [
        ("--name", name),
        ("--expected", value(&options, "expected")),
        ("--actual", value(&options, "actual")),
    ] {
        reject_newline(label, current)?;
    }
    let store = Store::configured()?;
    let _lock = store.lock()?;
    require_status(
        &store,
        decision_id,
        "committed",
        "can only record metrics for",
    )?;
    append_tsv(
        &store.decision_dir(decision_id).join("metrics.tsv"),
        &[
            name,
            value(&options, "expected"),
            value(&options, "actual"),
            &timestamp(),
        ],
    )?;
    store.log_event(
        decision_id,
        &format!(
            "metric_recorded\tname={name}\texpected={}\tactual={}",
            value(&options, "expected"),
            value(&options, "actual")
        ),
    )?;
    println!(
        "metric recorded\t{decision_id}\t{name}\texpected={}\tactual={}",
        value(&options, "expected"),
        value(&options, "actual")
    );
    Ok(())
}

fn reflect(args: &[String]) -> Result<(), String> {
    let (decision_id, options) = id_and_options(
        "reflect",
        args,
        &["recommendation", "reason", "follow-up-assignment"],
    )?;
    validate_id("decision ID", decision_id)?;
    let recommendation = required(
        &options,
        "recommendation",
        "reflect requires --recommendation",
    )?;
    let reason = required(&options, "reason", "reflect requires --reason")?;
    if !matches!(recommendation, "continue" | "adjust" | "rollback" | "pivot") {
        return Err(format!(
            "invalid recommendation: {recommendation} (expected continue|adjust|rollback|pivot)"
        ));
    }
    for (label, current) in [
        ("--recommendation", recommendation),
        ("--reason", reason),
        (
            "--follow-up-assignment",
            value(&options, "follow-up-assignment"),
        ),
    ] {
        reject_newline(label, current)?;
    }
    let store = Store::configured()?;
    let _lock = store.lock()?;
    require_status(&store, decision_id, "committed", "can only reflect on")?;
    let directory = store.decision_dir(decision_id);
    rewrite_status(
        &directory.join("decision.env"),
        &["status=reflected", &format!("reflected_at={}", timestamp())],
    )?;
    let outcome_path = directory.join("outcome.env");
    if !outcome_path.is_file() {
        return Err(format!("no outcome record found: {decision_id}"));
    }
    let mut outcome =
        fs::read_to_string(&outcome_path).map_err(io_error("read decision outcome"))?;
    outcome.push_str(&format!(
        "recommendation={recommendation}\nreflection_reason={reason}\nfollow_up_assignment={}\nreflected_at={}\nstatus=reflected\n",
        value(&options, "follow-up-assignment"),
        timestamp()
    ));
    atomic_write(&outcome_path, &outcome)?;
    store.log_event(
        decision_id,
        &format!(
            "decision_reflected\trecommendation={recommendation}\treason={reason}\tfollow_up={}",
            value(&options, "follow-up-assignment")
        ),
    )?;
    println!("decision reflected\t{decision_id}\t{recommendation}\t{reason}");
    Ok(())
}

fn show(args: &[String]) -> Result<(), String> {
    let decision_id = args
        .first()
        .ok_or_else(|| "show requires DECISION_ID".to_string())?;
    validate_id("decision ID", decision_id)?;
    let store = Store::configured()?;
    if !store.exists(decision_id) {
        return Err(format!("decision does not exist: {decision_id}"));
    }
    let directory = store.decision_dir(decision_id);
    println!("Decision: {decision_id}");
    println!("{}", "=".repeat(50));
    print_section("Metadata", &directory.join("decision.env"), false)?;
    print_section("Alternatives", &directory.join("alternatives.tsv"), true)?;
    print_section("Assumptions", &directory.join("assumptions.tsv"), true)?;
    print_section("Metrics", &directory.join("metrics.tsv"), true)?;
    if directory.join("outcome.env").is_file() {
        print_section("Outcome", &directory.join("outcome.env"), false)?;
    }
    print_section("Events", &directory.join("events.log"), false)?;
    Ok(())
}

fn list() -> Result<(), String> {
    let store = Store::configured()?;
    println!("DECISION_ID\tSTATUS\tTITLE\tOWNER\tCREATED_AT");
    if !store.base.is_dir() {
        return Ok(());
    }
    let mut directories: Vec<PathBuf> = fs::read_dir(&store.base)
        .map_err(io_error("list decisions"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    directories.sort();
    for directory in directories {
        let path = directory.join("decision.env");
        if !path.is_file() {
            continue;
        }
        let values = read_env(&path)?;
        let decision_id = directory
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        println!(
            "{}\t{}\t{}\t{}\t{}",
            decision_id,
            values
                .get("status")
                .map(String::as_str)
                .unwrap_or("unknown"),
            values.get("title").map(String::as_str).unwrap_or(""),
            values.get("owner").map(String::as_str).unwrap_or(""),
            values.get("created_at").map(String::as_str).unwrap_or("")
        );
    }
    Ok(())
}

fn id_and_options<'a>(
    command: &str,
    args: &'a [String],
    allowed: &[&str],
) -> Result<(&'a str, BTreeMap<String, String>), String> {
    let id = args
        .first()
        .ok_or_else(|| format!("{command} requires DECISION_ID"))?;
    let mut options = BTreeMap::new();
    let mut index = 1;
    while index < args.len() {
        let raw = &args[index];
        if matches!(raw.as_str(), "-h" | "--help") {
            println!("{USAGE}");
            return Err("help requested".into());
        }
        let key = raw
            .strip_prefix("--")
            .ok_or_else(|| format!("unknown option: {raw}"))?;
        if !allowed.contains(&key) {
            return Err(format!("unknown option: {raw}"));
        }
        let option_value = args
            .get(index + 1)
            .ok_or_else(|| format!("{raw} requires a value"))?;
        options.insert(key.to_string(), option_value.clone());
        index += 2;
    }
    Ok((id, options))
}

fn required<'a>(
    options: &'a BTreeMap<String, String>,
    key: &str,
    message: &str,
) -> Result<&'a str, String> {
    match options.get(key).map(String::as_str) {
        Some(current) if !current.is_empty() => Ok(current),
        _ => Err(message.into()),
    }
}

fn value<'a>(options: &'a BTreeMap<String, String>, key: &str) -> &'a str {
    options.get(key).map(String::as_str).unwrap_or("")
}

fn validate_id(label: &str, current: &str) -> Result<(), String> {
    if current.is_empty()
        || !current
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-'))
    {
        return Err(format!("invalid {label}: {current}"));
    }
    Ok(())
}

fn reject_newline(label: &str, current: &str) -> Result<(), String> {
    if current.contains('\n') || current.contains('\r') {
        return Err(format!("{label} may not contain newlines"));
    }
    Ok(())
}

fn require_open(store: &Store, decision_id: &str, action: &str) -> Result<(), String> {
    if !store.exists(decision_id) {
        return Err(format!("decision does not exist: {decision_id}"));
    }
    let status = store.status(decision_id)?;
    if status != "open" {
        return Err(format!(
            "cannot {action} to {status} decision: {decision_id}"
        ));
    }
    Ok(())
}

fn require_status(
    store: &Store,
    decision_id: &str,
    expected: &str,
    action: &str,
) -> Result<(), String> {
    if !store.exists(decision_id) {
        return Err(format!("decision does not exist: {decision_id}"));
    }
    let status = store.status(decision_id)?;
    if status != expected {
        return Err(format!("{action} {expected} decisions, got: {status}"));
    }
    Ok(())
}

fn read_env(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let text = fs::read_to_string(path).map_err(io_error("read decision state"))?;
    Ok(text
        .lines()
        .filter_map(|line| line.split_once('='))
        .map(|(key, current)| (key.to_string(), current.to_string()))
        .collect())
}

fn tsv_first_column_contains(path: &Path, expected: &str) -> Result<bool, String> {
    let text = fs::read_to_string(path).map_err(io_error("read decision table"))?;
    Ok(text
        .lines()
        .skip(1)
        .filter_map(|line| line.split('\t').next())
        .any(|current| current == expected))
}

fn append_tsv(path: &Path, fields: &[&str]) -> Result<(), String> {
    let mut file = OpenOptions::new()
        .append(true)
        .open(path)
        .map_err(io_error("append decision table"))?;
    writeln!(file, "{}", fields.join("\t")).map_err(io_error("append decision table"))
}

fn rewrite_status(path: &Path, appended: &[&str]) -> Result<(), String> {
    let text = fs::read_to_string(path).map_err(io_error("read decision metadata"))?;
    let mut output = String::new();
    for line in text.lines() {
        if !line.starts_with("status=") {
            output.push_str(line);
            output.push('\n');
        }
    }
    for line in appended {
        output.push_str(line);
        output.push('\n');
    }
    atomic_write(path, &output)
}

fn atomic_write(path: &Path, text: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent).map_err(io_error("create state directory"))?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("state"),
        std::process::id()
    ));
    let mut file = File::create(&temporary).map_err(io_error("create temporary state"))?;
    file.write_all(text.as_bytes())
        .map_err(io_error("write temporary state"))?;
    file.sync_all().map_err(io_error("sync temporary state"))?;
    fs::rename(&temporary, path).map_err(io_error("replace state"))
}

fn print_section(label: &str, path: &Path, header_only_empty: bool) -> Result<(), String> {
    println!("\n{label}:");
    let text = fs::read_to_string(path).unwrap_or_default();
    let empty = text.is_empty() || (header_only_empty && text.lines().count() <= 1);
    if empty {
        println!("(none)");
    } else {
        print!("{text}");
    }
    Ok(())
}

fn timestamp() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn io_error(context: &'static str) -> impl FnOnce(std::io::Error) -> String {
    move |error| format!("{context}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_match_the_legacy_contract() {
        assert!(validate_id("decision ID", "DEC_1.alpha-beta").is_ok());
        assert!(validate_id("decision ID", "bad/id").is_err());
        assert!(validate_id("decision ID", "").is_err());
    }

    #[test]
    fn newline_values_are_rejected() {
        assert!(reject_newline("--title", "one\ntwo").is_err());
        assert!(reject_newline("--title", "one line").is_ok());
    }
}
