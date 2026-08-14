use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::path::Path;
use std::process::Command;

#[derive(Debug, Serialize)]
struct Snapshot {
    final_diff_sha256: String,
    changed_files: usize,
    changed_paths: Vec<String>,
    changed_code_paths: Vec<String>,
}

pub fn run(args: &[String]) -> Result<(), String> {
    let mut root = None;
    let mut base = "HEAD".to_string();
    let mut format = "json".to_string();
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--root" => {
                root = Some(required_value(args, index, "--root")?.to_string());
                index += 2;
            }
            "--base" => {
                base = required_value(args, index, "--base")?.to_string();
                index += 2;
            }
            "--format" => {
                format = required_value(args, index, "--format")?.to_string();
                index += 2;
            }
            option => return Err(format!("unknown option: {option}")),
        }
    }
    let root = root.ok_or_else(|| "snapshot requires --root DIR".to_string())?;
    if !matches!(format.as_str(), "json" | "shell") {
        return Err(format!("invalid format: {format} (expected json|shell)"));
    }
    let snapshot = capture(Path::new(&root), &base)?;
    if format == "shell" {
        println!("{} {}", snapshot.final_diff_sha256, snapshot.changed_files);
    } else {
        println!(
            "{}",
            serde_json::to_string(&snapshot)
                .map_err(|error| format!("serialize snapshot: {error}"))?
        );
    }
    Ok(())
}

fn required_value<'a>(args: &'a [String], index: usize, option: &str) -> Result<&'a str, String> {
    args.get(index + 1)
        .map(String::as_str)
        .ok_or_else(|| format!("{option} requires a value"))
}

fn capture(root: &Path, base: &str) -> Result<Snapshot, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["diff", base, "--binary", "--ignore-submodules=all", "--"])
        .output()
        .map_err(|error| format!("run git diff: {error}"))?;
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if message.is_empty() {
            "git diff failed".into()
        } else {
            message
        });
    }
    let diff = String::from_utf8_lossy(&output.stdout);
    let changed_paths = changed_paths(&diff);
    let changed_code_paths = changed_paths
        .iter()
        .filter(|path| is_source(path) && !is_test_path(path) && !is_ignored(path))
        .cloned()
        .collect();
    Ok(Snapshot {
        final_diff_sha256: format!("{:x}", Sha256::digest(&output.stdout)),
        changed_files: diff
            .lines()
            .filter(|line| line.starts_with("diff --git a/"))
            .count(),
        changed_paths: changed_paths.into_iter().collect(),
        changed_code_paths,
    })
}

fn changed_paths(diff: &str) -> BTreeSet<String> {
    let mut paths = BTreeSet::new();
    for line in diff.lines() {
        let Some(rest) = line.strip_prefix("diff --git a/") else {
            continue;
        };
        let Some((old_path, new_path)) = rest.split_once(" b/") else {
            continue;
        };
        for path in [old_path, new_path.split('\t').next().unwrap_or("").trim()] {
            if !path.is_empty() && path != "/dev/null" {
                paths.insert(path.to_string());
            }
        }
    }
    paths
}

fn is_source(path: &str) -> bool {
    matches!(
        Path::new(path)
            .extension()
            .and_then(|extension| extension.to_str()),
        Some(
            "c" | "cc"
                | "cpp"
                | "go"
                | "h"
                | "hpp"
                | "java"
                | "js"
                | "jsx"
                | "kt"
                | "m"
                | "mm"
                | "php"
                | "py"
                | "pyi"
                | "pyx"
                | "rb"
                | "rs"
                | "scala"
                | "swift"
                | "ts"
                | "tsx"
        )
    )
}

fn is_test_path(path: &str) -> bool {
    let components: Vec<&str> = path.split('/').collect();
    let name = components
        .last()
        .copied()
        .unwrap_or("")
        .to_ascii_lowercase();
    components
        .iter()
        .any(|component| matches!(*component, "test" | "tests" | "__tests__"))
        || name.starts_with("test_")
        || name.ends_with("_test.go")
        || [
            ".test.ts",
            ".test.tsx",
            ".spec.ts",
            ".spec.tsx",
            ".test.js",
            ".spec.js",
        ]
        .iter()
        .any(|suffix| name.ends_with(suffix))
}

fn is_ignored(path: &str) -> bool {
    [".cache/", ".gomodcache/", "node_modules/", "vendor/"]
        .iter()
        .any(|prefix| path.starts_with(prefix))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_rename_paths() {
        let paths = changed_paths("diff --git a/old.rs b/new.rs\n");
        assert_eq!(
            paths,
            BTreeSet::from(["new.rs".to_string(), "old.rs".to_string()])
        );
    }

    #[test]
    fn filters_tests_and_dependencies() {
        assert!(is_source("src/lib.rs"));
        assert!(is_test_path("tests/lib.rs"));
        assert!(is_ignored("vendor/lib.rs"));
    }
}
