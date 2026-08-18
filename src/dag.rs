use crate::{config, state::atomic_write, state::read_env, state::timestamp};
use fs2::FileExt;
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};

const NODE_HEADER: &str =
    "node_id\tagent\tassignment_id\tresponsibility\tbranch\towned_paths\tstatus\tdecision_id\tplan_id\tadded_at";
const EDGE_HEADER: &str = "from_node\tto_node\tadded_at";
const STATUSES: &[&str] = &[
    "pending", "ready", "running", "blocked", "done", "failed", "skipped",
];
const USAGE: &str = r#"Usage:
  multiagent dag init WORKFLOW_ID --title TEXT [--owner NAME]
  multiagent dag add-node WORKFLOW_ID NODE_ID --agent NAME --assignment-id ID --responsibility TEXT --branch BRANCH --owned PATH[,PATH...] [--depends-on NODE[,NODE...]] [--status STATUS] [--decision-id ID] [--plan-id ID]
  multiagent dag status WORKFLOW_ID NODE_ID STATUS [--reason TEXT]
  multiagent dag ready WORKFLOW_ID
  multiagent dag blocked WORKFLOW_ID
  multiagent dag show WORKFLOW_ID
  multiagent dag list"#;

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
        "add-node" => add_node(&args[1..]),
        "status" => update_status(&args[1..]),
        "ready" => ready(&args[1..]),
        "blocked" => blocked(&args[1..]),
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
            base: config::state_dir()?.join("workflows"),
        })
    }

    fn workflow_dir(&self, workflow_id: &str) -> PathBuf {
        self.base.join(workflow_id)
    }

    fn exists(&self, workflow_id: &str) -> bool {
        self.workflow_dir(workflow_id)
            .join("workflow.env")
            .is_file()
    }

    fn lock(&self, workflow_id: &str) -> Result<File, String> {
        let directory = self.workflow_dir(workflow_id);
        fs::create_dir_all(&directory).map_err(io_error("create workflow directory"))?;
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(directory.join(".dag.lock"))
            .map_err(io_error("open workflow lock"))?;
        file.lock_exclusive().map_err(io_error("lock workflow"))?;
        Ok(file)
    }

    fn require(&self, workflow_id: &str) -> Result<PathBuf, String> {
        if !self.exists(workflow_id) {
            return Err(format!("workflow does not exist: {workflow_id}"));
        }
        Ok(self.workflow_dir(workflow_id))
    }

    fn event(&self, workflow_id: &str, event: &str) -> Result<(), String> {
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.workflow_dir(workflow_id).join("events.log"))
            .map_err(io_error("append workflow event"))?;
        writeln!(file, "{}\t{}", timestamp(), event).map_err(io_error("append workflow event"))
    }
}

#[derive(Clone, Debug)]
struct Node {
    node_id: String,
    agent: String,
    assignment_id: String,
    responsibility: String,
    branch: String,
    owned_paths: String,
    status: String,
    decision_id: String,
    plan_id: String,
    added_at: String,
}

impl Node {
    fn parse(line: &str) -> Option<Self> {
        let mut fields: Vec<&str> = line.split('\t').collect();
        fields.resize(10, "");
        Some(Self {
            node_id: fields[0].to_string(),
            agent: fields[1].to_string(),
            assignment_id: fields[2].to_string(),
            responsibility: fields[3].to_string(),
            branch: fields[4].to_string(),
            owned_paths: fields[5].to_string(),
            status: fields[6].to_string(),
            decision_id: fields[7].to_string(),
            plan_id: fields[8].to_string(),
            added_at: fields[9].to_string(),
        })
    }

    fn line(&self) -> String {
        [
            self.node_id.as_str(),
            self.agent.as_str(),
            self.assignment_id.as_str(),
            self.responsibility.as_str(),
            self.branch.as_str(),
            self.owned_paths.as_str(),
            self.status.as_str(),
            self.decision_id.as_str(),
            self.plan_id.as_str(),
            self.added_at.as_str(),
        ]
        .join("\t")
    }
}

#[derive(Clone, Debug)]
struct Edge {
    from: String,
    to: String,
    added_at: String,
}

impl Edge {
    fn parse(line: &str) -> Option<Self> {
        let mut fields = line.split('\t');
        Some(Self {
            from: fields.next()?.to_string(),
            to: fields.next()?.to_string(),
            added_at: fields.next().unwrap_or("").to_string(),
        })
    }

    fn line(&self) -> String {
        format!("{}\t{}\t{}", self.from, self.to, self.added_at)
    }
}

fn init(args: &[String]) -> Result<(), String> {
    let workflow_id = args
        .first()
        .ok_or_else(|| "init requires WORKFLOW_ID".to_string())?;
    validate_id("workflow ID", workflow_id)?;
    let options = parse_options(&args[1..], &["title", "owner"])?;
    let title = required(&options, "title", "init requires --title")?;
    let owner = value(&options, "owner");
    reject_newline("--title", title)?;
    reject_newline("--owner", owner)?;

    let store = Store::configured()?;
    let _lock = store.lock(workflow_id)?;
    if store.exists(workflow_id) {
        return Err(format!("workflow already exists: {workflow_id}"));
    }
    let directory = store.workflow_dir(workflow_id);
    atomic_write(
        &directory.join("workflow.env"),
        &format!(
            "workflow_id={workflow_id}\ntitle={title}\nowner={owner}\nstatus=active\ncreated_at={}\n",
            timestamp()
        ),
    )?;
    atomic_write(&directory.join("nodes.tsv"), &format!("{NODE_HEADER}\n"))?;
    atomic_write(&directory.join("edges.tsv"), &format!("{EDGE_HEADER}\n"))?;
    store.event(
        workflow_id,
        &format!("workflow_created\ttitle={title}\towner={owner}"),
    )?;
    println!("workflow created\t{workflow_id}\t{title}");
    Ok(())
}

fn add_node(args: &[String]) -> Result<(), String> {
    let workflow_id = args
        .first()
        .ok_or_else(|| "add-node requires WORKFLOW_ID".to_string())?;
    let node_id = args
        .get(1)
        .ok_or_else(|| "add-node requires NODE_ID".to_string())?;
    validate_id("workflow ID", workflow_id)?;
    validate_id("node ID", node_id)?;
    let options = parse_options(
        &args[2..],
        &[
            "agent",
            "assignment-id",
            "responsibility",
            "role",
            "branch",
            "owned",
            "depends-on",
            "status",
            "decision-id",
            "plan-id",
        ],
    )?;
    let agent = required(&options, "agent", "add-node requires --agent")?;
    let assignment_id = required(
        &options,
        "assignment-id",
        "add-node requires --assignment-id",
    )?;
    let responsibility = match (
        options.get("responsibility").map(String::as_str),
        options.get("role").map(String::as_str),
    ) {
        (Some(_), Some(_)) => {
            return Err("add-node accepts only one of --responsibility or --role".into())
        }
        (Some(value), None) | (None, Some(value)) if !value.is_empty() => value,
        _ => return Err("add-node requires --responsibility".into()),
    };
    let branch = required(&options, "branch", "add-node requires --branch")?;
    let owned = required(&options, "owned", "add-node requires --owned")?;
    let status = options
        .get("status")
        .map(String::as_str)
        .unwrap_or("pending");
    validate_status(status)?;
    for (label, current) in [
        ("--agent", agent),
        ("--assignment-id", assignment_id),
        ("--responsibility", responsibility),
        ("--branch", branch),
        ("--owned", owned),
        ("--depends-on", value(&options, "depends-on")),
        ("--decision-id", value(&options, "decision-id")),
        ("--plan-id", value(&options, "plan-id")),
    ] {
        reject_newline(label, current)?;
    }

    let store = Store::configured()?;
    let _lock = store.lock(workflow_id)?;
    let directory = store.require(workflow_id)?;
    let mut nodes = read_nodes(&directory.join("nodes.tsv"))?;
    if nodes.iter().any(|node| node.node_id == *node_id) {
        return Err(format!("node ID already exists: {node_id}"));
    }
    let dependencies: Vec<String> = value(&options, "depends-on")
        .split(',')
        .map(str::trim)
        .filter(|dependency| !dependency.is_empty())
        .map(str::to_string)
        .collect();
    for dependency in &dependencies {
        if !nodes.iter().any(|node| node.node_id == *dependency) {
            return Err(format!("dependency does not exist: {dependency}"));
        }
    }
    let mut edges = read_edges(&directory.join("edges.tsv"))?;
    let stamp = timestamp();
    for dependency in &dependencies {
        edges.push(Edge {
            from: dependency.clone(),
            to: node_id.clone(),
            added_at: stamp.clone(),
        });
    }
    if has_cycle(&edges) {
        return Err("dependency cycle detected".into());
    }
    nodes.push(Node {
        node_id: node_id.clone(),
        agent: agent.to_string(),
        assignment_id: assignment_id.to_string(),
        responsibility: responsibility.to_string(),
        branch: branch.to_string(),
        owned_paths: owned.to_string(),
        status: status.to_string(),
        decision_id: value(&options, "decision-id").to_string(),
        plan_id: value(&options, "plan-id").to_string(),
        added_at: stamp,
    });
    write_nodes(&directory.join("nodes.tsv"), &nodes)?;
    write_edges(&directory.join("edges.tsv"), &edges)?;
    store.event(
        workflow_id,
        &format!(
            "node_added\tnode_id={node_id}\tagent={agent}\tassignment_id={assignment_id}\tstatus={status}\tdepends_on={}",
            value(&options, "depends-on")
        ),
    )?;
    println!("node added\t{workflow_id}\t{node_id}\t{agent}");
    Ok(())
}

fn update_status(args: &[String]) -> Result<(), String> {
    let workflow_id = args
        .first()
        .ok_or_else(|| "status requires WORKFLOW_ID".to_string())?;
    let node_id = args
        .get(1)
        .ok_or_else(|| "status requires NODE_ID".to_string())?;
    let status = args
        .get(2)
        .ok_or_else(|| "status requires STATUS".to_string())?;
    validate_id("workflow ID", workflow_id)?;
    validate_id("node ID", node_id)?;
    validate_status(status)?;
    let options = parse_options(&args[3..], &["reason"])?;
    reject_newline("--reason", value(&options, "reason"))?;
    let store = Store::configured()?;
    let _lock = store.lock(workflow_id)?;
    let directory = store.require(workflow_id)?;
    let mut nodes = read_nodes(&directory.join("nodes.tsv"))?;
    let node = nodes
        .iter_mut()
        .find(|node| node.node_id == *node_id)
        .ok_or_else(|| format!("node does not exist: {node_id}"))?;
    node.status = status.clone();
    write_nodes(&directory.join("nodes.tsv"), &nodes)?;
    store.event(
        workflow_id,
        &format!(
            "status_updated\tnode_id={node_id}\tstatus={status}\treason={}",
            value(&options, "reason")
        ),
    )?;
    println!("status updated\t{workflow_id}\t{node_id}\t{status}");
    Ok(())
}

fn ready(args: &[String]) -> Result<(), String> {
    let workflow_id = one_id("ready", args)?;
    let store = Store::configured()?;
    let directory = store.require(workflow_id)?;
    let nodes = read_nodes(&directory.join("nodes.tsv"))?;
    let edges = read_edges(&directory.join("edges.tsv"))?;
    let statuses: BTreeMap<&str, &str> = nodes
        .iter()
        .map(|node| (node.node_id.as_str(), node.status.as_str()))
        .collect();
    for node in &nodes {
        if node.status == "ready" {
            println!("{}", node.node_id);
        } else if node.status == "pending" {
            let dependencies: Vec<&str> = edges
                .iter()
                .filter(|edge| edge.to == node.node_id)
                .map(|edge| edge.from.as_str())
                .collect();
            if dependencies.iter().all(|dependency| {
                matches!(statuses.get(dependency), Some(&"done") | Some(&"skipped"))
            }) {
                println!("{}", node.node_id);
            }
        }
    }
    Ok(())
}

fn blocked(args: &[String]) -> Result<(), String> {
    let workflow_id = one_id("blocked", args)?;
    let store = Store::configured()?;
    let directory = store.require(workflow_id)?;
    let nodes = read_nodes(&directory.join("nodes.tsv"))?;
    let edges = read_edges(&directory.join("edges.tsv"))?;
    let statuses: BTreeMap<&str, &str> = nodes
        .iter()
        .map(|node| (node.node_id.as_str(), node.status.as_str()))
        .collect();
    println!("BLOCKED_NODES\tREASON");
    for node in &nodes {
        if !matches!(node.status.as_str(), "pending" | "ready") {
            continue;
        }
        if let Some(dependency) = edges
            .iter()
            .filter(|edge| edge.to == node.node_id)
            .map(|edge| edge.from.as_str())
            .find(|dependency| statuses.get(dependency) == Some(&"failed"))
        {
            println!("{}\tdependency {} failed", node.node_id, dependency);
        }
    }
    Ok(())
}

fn show(args: &[String]) -> Result<(), String> {
    let workflow_id = one_id("show", args)?;
    let store = Store::configured()?;
    let directory = store.require(workflow_id)?;
    println!("Workflow: {workflow_id}");
    println!("{}", "=".repeat(50));
    print_section("Metadata", &directory.join("workflow.env"), false)?;
    print_section("Nodes", &directory.join("nodes.tsv"), true)?;
    print_section("Dependencies", &directory.join("edges.tsv"), true)?;
    print_section("Events", &directory.join("events.log"), false)?;
    Ok(())
}

fn list() -> Result<(), String> {
    let store = Store::configured()?;
    println!("WORKFLOW_ID\tSTATUS\tTITLE\tOWNER\tCREATED_AT");
    if !store.base.is_dir() {
        return Ok(());
    }
    let mut directories: Vec<PathBuf> = fs::read_dir(&store.base)
        .map_err(io_error("list workflows"))?
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_dir())
        .collect();
    directories.sort();
    for directory in directories {
        let metadata_path = directory.join("workflow.env");
        if !metadata_path.is_file() {
            continue;
        }
        let metadata = read_env(&metadata_path)?;
        let workflow_id = directory
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("");
        println!(
            "{}\t{}\t{}\t{}\t{}",
            workflow_id,
            metadata
                .get("status")
                .map(String::as_str)
                .unwrap_or("unknown"),
            metadata.get("title").map(String::as_str).unwrap_or(""),
            metadata.get("owner").map(String::as_str).unwrap_or(""),
            metadata.get("created_at").map(String::as_str).unwrap_or("")
        );
    }
    Ok(())
}

fn one_id<'a>(command: &str, args: &'a [String]) -> Result<&'a str, String> {
    let id = args
        .first()
        .ok_or_else(|| format!("{command} requires WORKFLOW_ID"))?;
    validate_id("workflow ID", id)?;
    Ok(id)
}

fn parse_options(args: &[String], allowed: &[&str]) -> Result<BTreeMap<String, String>, String> {
    let mut options = BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let raw = &args[index];
        let key = raw
            .strip_prefix("--")
            .ok_or_else(|| format!("unknown option: {raw}"))?;
        if !allowed.contains(&key) {
            return Err(format!("unknown option: {raw}"));
        }
        let current = args
            .get(index + 1)
            .ok_or_else(|| format!("{raw} requires a value"))?;
        options.insert(key.to_string(), current.clone());
        index += 2;
    }
    Ok(options)
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

fn validate_status(status: &str) -> Result<(), String> {
    if STATUSES.contains(&status) {
        Ok(())
    } else {
        Err(format!(
            "invalid status: {status} (expected pending|ready|running|blocked|done|failed|skipped)"
        ))
    }
}

fn reject_newline(label: &str, current: &str) -> Result<(), String> {
    if current.contains(['\n', '\r', '\t']) {
        Err(format!("{label} may not contain tabs or newlines"))
    } else {
        Ok(())
    }
}

fn read_nodes(path: &Path) -> Result<Vec<Node>, String> {
    let text = fs::read_to_string(path).map_err(io_error("read workflow nodes"))?;
    Ok(text.lines().skip(1).filter_map(Node::parse).collect())
}

fn read_edges(path: &Path) -> Result<Vec<Edge>, String> {
    let text = fs::read_to_string(path).map_err(io_error("read workflow edges"))?;
    Ok(text.lines().skip(1).filter_map(Edge::parse).collect())
}

fn write_nodes(path: &Path, nodes: &[Node]) -> Result<(), String> {
    let mut text = format!("{NODE_HEADER}\n");
    for node in nodes {
        text.push_str(&node.line());
        text.push('\n');
    }
    atomic_write(path, &text)
}

fn write_edges(path: &Path, edges: &[Edge]) -> Result<(), String> {
    let mut text = format!("{EDGE_HEADER}\n");
    for edge in edges {
        text.push_str(&edge.line());
        text.push('\n');
    }
    atomic_write(path, &text)
}

fn has_cycle(edges: &[Edge]) -> bool {
    let mut adjacency: BTreeMap<&str, Vec<&str>> = BTreeMap::new();
    let mut nodes = BTreeSet::new();
    for edge in edges {
        adjacency.entry(&edge.from).or_default().push(&edge.to);
        nodes.insert(edge.from.as_str());
        nodes.insert(edge.to.as_str());
    }
    let mut visited = BTreeSet::new();
    let mut active = BTreeSet::new();
    nodes
        .into_iter()
        .any(|node| visit_cycle(node, &adjacency, &mut visited, &mut active))
}

fn visit_cycle<'a>(
    node: &'a str,
    adjacency: &BTreeMap<&'a str, Vec<&'a str>>,
    visited: &mut BTreeSet<&'a str>,
    active: &mut BTreeSet<&'a str>,
) -> bool {
    if active.contains(node) {
        return true;
    }
    if !visited.insert(node) {
        return false;
    }
    active.insert(node);
    let cyclic = adjacency
        .get(node)
        .into_iter()
        .flatten()
        .any(|next| visit_cycle(next, adjacency, visited, active));
    active.remove(node);
    cyclic
}

fn print_section(label: &str, path: &Path, header_only_empty: bool) -> Result<(), String> {
    println!("\n{label}:");
    let text = fs::read_to_string(path).unwrap_or_default();
    if text.is_empty() || (header_only_empty && text.lines().count() <= 1) {
        println!("(none)");
    } else {
        print!("{text}");
    }
    Ok(())
}

fn io_error(context: &'static str) -> impl FnOnce(std::io::Error) -> String {
    move |error| format!("{context}: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn detects_cycles() {
        let edge = |from: &str, to: &str| Edge {
            from: from.into(),
            to: to.into(),
            added_at: String::new(),
        };
        assert!(!has_cycle(&[edge("A", "B"), edge("B", "C")]));
        assert!(has_cycle(&[edge("A", "B"), edge("B", "C"), edge("C", "A")]));
    }
}
