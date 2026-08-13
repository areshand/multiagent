use crate::config;
use chrono::{SecondsFormat, Utc};
use fs2::FileExt;
use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Component, Path, PathBuf};

const POLICY_TEMPLATE: &str = r#"# Multiagent repo write policy
#
# Default allowed write root is $MULTIAGENT_ROOT for the launched session.
# Orchestrator-owned: workers should not edit this file directly.
# Add approvals only with:
#   bin/write-policy.sh approve PATH --actor ACTOR --assignment-id ID --reason TEXT [--force]
#
# Records are TSV:
#   approval<TAB>timestamp<TAB>actor<TAB>assignment_id<TAB>requested_path<TAB>canonical_path<TAB>reason<TAB>force
# Blank lines and comments are ignored. Legacy bare absolute path lines are read
# for compatibility but new approvals must be structured records.
"#;

const USAGE: &str = r#"Usage:
  bin/write-policy.sh init
  bin/write-policy.sh show
  bin/write-policy.sh check PATH [...]
  bin/write-policy.sh approve PATH --actor ACTOR --assignment-id ID --reason TEXT [--force]"#;

pub fn run(args: &[String]) -> Result<(), String> {
    if args.is_empty() || matches!(args[0].as_str(), "-h" | "--help") {
        println!("{USAGE}");
        return Ok(());
    }
    let policy = Policy::configured()?;
    match args[0].as_str() {
        "init" => {
            if args.len() != 1 {
                return Err("init takes no arguments".into());
            }
            policy.init()
        }
        "show" => {
            if args.len() != 1 {
                return Err("show takes no arguments".into());
            }
            policy.show()
        }
        "check" => policy.check(&args[1..]),
        "approve" => policy.approve(&args[1..]),
        command => Err(format!("unknown command: {command}")),
    }
}

struct Policy {
    root: PathBuf,
    path: PathBuf,
}

impl Policy {
    fn configured() -> Result<Self, String> {
        let root = config::root()?;
        let path = env::var_os("MULTIAGENT_WRITE_POLICY")
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| root.join("docs/write-policy.paths"));
        Ok(Self { root, path })
    }

    fn lock(&self) -> Result<File, String> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| format!("policy path has no parent: {}", self.path.display()))?;
        fs::create_dir_all(parent).map_err(io_error("create policy directory"))?;
        let lock_path = parent.join(format!(
            ".{}.lock",
            self.path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("write-policy")
        ));
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(lock_path)
            .map_err(io_error("open policy lock"))?;
        file.lock_exclusive().map_err(io_error("lock policy"))?;
        Ok(file)
    }

    fn init(&self) -> Result<(), String> {
        let _lock = self.lock()?;
        self.init_locked()
    }

    fn init_locked(&self) -> Result<(), String> {
        if self.path.is_file() {
            return Ok(());
        }
        atomic_write(&self.path, POLICY_TEMPLATE)
    }

    fn canonical_root(&self) -> Result<PathBuf, String> {
        fs::create_dir_all(&self.root).map_err(io_error("create write root"))?;
        fs::canonicalize(&self.root).map_err(io_error("canonicalize write root"))
    }

    fn approved_paths(&self) -> Result<Vec<PathBuf>, String> {
        if !self.path.is_file() {
            return Ok(Vec::new());
        }
        let text = fs::read_to_string(&self.path).map_err(io_error("read write policy"))?;
        let mut approved = Vec::new();
        for raw in text.lines() {
            let line = raw.split('#').next().unwrap_or("").trim();
            if line.is_empty() {
                continue;
            }
            let candidate = if line.starts_with("approval\t") {
                let fields: Vec<&str> = line.split('\t').collect();
                if fields.len() < 6 || fields[5].is_empty() {
                    continue;
                }
                fields[5]
            } else {
                line
            };
            approved.push(canonical_path(Path::new(candidate))?);
        }
        Ok(approved)
    }

    fn show(&self) -> Result<(), String> {
        self.init()?;
        let root = self.canonical_root()?;
        println!("Default write root: {}", root.display());
        println!("Policy file: {}", self.path.display());
        println!("Approved outside write roots:");
        let outside: Vec<PathBuf> = self
            .approved_paths()?
            .into_iter()
            .filter(|path| !inside(path, &root))
            .collect();
        if outside.is_empty() {
            println!("  (none)");
        } else {
            for path in outside {
                println!("  {}", path.display());
            }
        }
        Ok(())
    }

    fn check(&self, paths: &[String]) -> Result<(), String> {
        if paths.is_empty() {
            return Err("check requires at least one PATH".into());
        }
        self.init()?;
        let root = self.canonical_root()?;
        let approved = self.approved_paths()?;
        let mut denied = false;
        for raw in paths {
            let path = canonical_path(Path::new(raw))?;
            let allowed = inside(&path, &root)
                || approved
                    .iter()
                    .any(|approved_path| inside(&path, approved_path));
            if allowed {
                println!("allowed\t{}", path.display());
            } else {
                println!("denied\t{}", path.display());
                denied = true;
            }
        }
        if denied {
            Err(String::new())
        } else {
            Ok(())
        }
    }

    fn approve(&self, args: &[String]) -> Result<(), String> {
        let requested = args
            .first()
            .ok_or_else(|| "approve requires PATH".to_string())?;
        let mut actor = "";
        let mut assignment_id = "";
        let mut reason = "";
        let mut force = false;
        let mut index = 1;
        while index < args.len() {
            match args[index].as_str() {
                "--actor" => {
                    actor = option_value(args, index, "--actor")?;
                    index += 2;
                }
                "--assignment-id" => {
                    assignment_id = option_value(args, index, "--assignment-id")?;
                    index += 2;
                }
                "--reason" => {
                    reason = option_value(args, index, "--reason")?;
                    index += 2;
                }
                "--force" => {
                    force = true;
                    index += 1;
                }
                argument => return Err(format!("unknown approve argument: {argument}")),
            }
        }
        if actor.is_empty() {
            return Err("approve requires --actor ACTOR".into());
        }
        if assignment_id.is_empty() {
            return Err("approve requires --assignment-id ID".into());
        }
        if reason.is_empty() {
            return Err("approve requires --reason TEXT".into());
        }

        let _lock = self.lock()?;
        self.init_locked()?;
        let root = self.canonical_root()?;
        let canonical = canonical_path(Path::new(requested))?;
        if inside(&canonical, &root) {
            println!("already allowed by default root: {}", canonical.display());
            return Ok(());
        }
        if self.approved_paths()?.iter().any(|path| path == &canonical) {
            println!("already approved: {}", canonical.display());
            return Ok(());
        }
        if broad_approval(&canonical, &root) && !force {
            return Err(format!(
                "refusing broad outside approval without --force: {}",
                canonical.display()
            ));
        }
        reject_record_field("actor", actor)?;
        reject_record_field("assignment ID", assignment_id)?;
        reject_record_field("requested path", requested)?;
        reject_record_field("reason", reason)?;
        let mut file = OpenOptions::new()
            .append(true)
            .open(&self.path)
            .map_err(io_error("append write policy"))?;
        writeln!(
            file,
            "approval\t{}\t{}\t{}\t{}\t{}\t{}\t{}",
            timestamp(),
            actor,
            assignment_id,
            requested,
            canonical.display(),
            reason,
            usize::from(force)
        )
        .map_err(io_error("append write policy"))?;
        if force {
            println!(
                "approved outside write root: {} (forced)",
                canonical.display()
            );
        } else {
            println!("approved outside write root: {}", canonical.display());
        }
        Ok(())
    }
}

fn canonical_path(path: &Path) -> Result<PathBuf, String> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        env::current_dir()
            .map_err(io_error("determine current directory"))?
            .join(path)
    };
    if absolute.exists() {
        if absolute.is_dir() {
            return fs::canonicalize(&absolute).map_err(io_error("canonicalize path"));
        }
        let parent = fs::canonicalize(
            absolute
                .parent()
                .ok_or_else(|| format!("path has no parent: {}", absolute.display()))?,
        )
        .map_err(io_error("canonicalize path parent"))?;
        return Ok(parent.join(absolute.file_name().unwrap_or_default()));
    }

    let mut ancestor = absolute.as_path();
    let mut missing = Vec::new();
    while !ancestor.exists() {
        if let Some(name) = ancestor.file_name() {
            missing.push(name.to_os_string());
        }
        ancestor = ancestor
            .parent()
            .ok_or_else(|| format!("cannot resolve path: {}", absolute.display()))?;
    }
    let mut resolved =
        fs::canonicalize(ancestor).map_err(io_error("canonicalize path ancestor"))?;
    for component in missing.into_iter().rev() {
        resolved.push(component);
    }
    Ok(normalize_lexically(&resolved))
}

fn normalize_lexically(path: &Path) -> PathBuf {
    let mut output = PathBuf::new();
    for component in path.components() {
        match component {
            Component::ParentDir => {
                output.pop();
            }
            Component::CurDir => {}
            other => output.push(other.as_os_str()),
        }
    }
    output
}

fn inside(path: &Path, root: &Path) -> bool {
    path == root || path.starts_with(root)
}

fn broad_approval(path: &Path, root: &Path) -> bool {
    let broad = [
        "/",
        "/tmp",
        "/private/tmp",
        "/var/tmp",
        "/Users",
        "/home",
        "/opt",
        "/usr",
        "/var",
        "/private",
        "/Applications",
    ];
    broad.iter().any(|candidate| path == Path::new(candidate))
        || env::var_os("HOME")
            .map(PathBuf::from)
            .is_some_and(|home| path == home)
        || root.parent().is_some_and(|parent| path == parent)
}

fn option_value<'a>(args: &'a [String], index: usize, option: &str) -> Result<&'a str, String> {
    args.get(index + 1)
        .map(String::as_str)
        .ok_or_else(|| format!("{option} requires a value"))
}

fn reject_record_field(label: &str, current: &str) -> Result<(), String> {
    if current.contains(['\n', '\r', '\t']) {
        Err(format!("{label} may not contain tabs or newlines"))
    } else {
        Ok(())
    }
}

fn atomic_write(path: &Path, text: &str) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| format!("path has no parent: {}", path.display()))?;
    fs::create_dir_all(parent).map_err(io_error("create policy directory"))?;
    let temporary = parent.join(format!(
        ".{}.{}.tmp",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("policy"),
        std::process::id()
    ));
    let mut file = File::create(&temporary).map_err(io_error("create temporary policy"))?;
    file.write_all(text.as_bytes())
        .map_err(io_error("write temporary policy"))?;
    file.sync_all().map_err(io_error("sync temporary policy"))?;
    fs::rename(&temporary, path).map_err(io_error("replace policy"))
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
    fn lexical_normalization_removes_parent_segments() {
        assert_eq!(
            normalize_lexically(Path::new("/tmp/root/../outside")),
            PathBuf::from("/tmp/outside")
        );
    }

    #[test]
    fn containment_is_component_aware() {
        assert!(inside(Path::new("/repo/src"), Path::new("/repo")));
        assert!(!inside(Path::new("/repository"), Path::new("/repo")));
    }
}
