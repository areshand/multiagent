use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

#[cfg(unix)]
use std::os::unix::ffi::OsStringExt;

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
    let bytes = canonical_diff(root, base)?;
    let diff = String::from_utf8_lossy(&bytes);
    let changed_paths = changed_paths(&diff);
    let changed_code_paths = changed_paths
        .iter()
        .filter(|path| is_source(path) && !is_test_path(path) && !is_ignored(path))
        .cloned()
        .collect();
    Ok(Snapshot {
        final_diff_sha256: format!("{:x}", Sha256::digest(&bytes)),
        changed_files: diff
            .lines()
            .filter(|line| line.starts_with("diff --git a/"))
            .count(),
        changed_paths: changed_paths.into_iter().collect(),
        changed_code_paths,
    })
}

pub(crate) fn canonical_diff(root: &Path, base: &str) -> Result<Vec<u8>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["diff", base, "--binary", "--ignore-submodules=all", "--"])
        .output()
        .map_err(|error| format!("run git diff: {error}"))?;
    if !output.status.success() {
        return Err(git_error("git diff failed", &output.stderr));
    }

    let mut diff = output.stdout;
    for path in untracked_paths(root)? {
        let output = Command::new("git")
            .arg("-C")
            .arg(root)
            .args(["diff", "--no-index", "--binary", "--"])
            .arg("/dev/null")
            .arg(&path)
            .output()
            .map_err(|error| format!("run git diff for {}: {error}", path.display()))?;
        if !matches!(output.status.code(), Some(0 | 1)) {
            return Err(git_error(
                &format!("git diff failed for untracked path {}", path.display()),
                &output.stderr,
            ));
        }
        diff.extend_from_slice(&output.stdout);
    }
    Ok(diff)
}

fn untracked_paths(root: &Path) -> Result<Vec<PathBuf>, String> {
    let output = Command::new("git")
        .arg("-C")
        .arg(root)
        .args(["ls-files", "--others", "--exclude-standard", "-z"])
        .output()
        .map_err(|error| format!("list untracked files: {error}"))?;
    if !output.status.success() {
        return Err(git_error("git ls-files failed", &output.stderr));
    }
    let baseline = baseline_untracked()?;
    let mut paths = output
        .stdout
        .split(|byte| *byte == 0)
        .filter(|bytes| !bytes.is_empty())
        .map(path_from_git_bytes)
        .filter(|path| !baseline.contains(&path.to_string_lossy().into_owned()))
        .filter(|path| {
            fs::symlink_metadata(root.join(path))
                .map(|metadata| metadata.is_file() || metadata.file_type().is_symlink())
                .unwrap_or(false)
        })
        .collect::<Vec<_>>();
    paths.sort();
    Ok(paths)
}

fn baseline_untracked() -> Result<BTreeSet<String>, String> {
    let Ok(path) = std::env::var("MULTIAGENT_BASELINE_UNTRACKED_FILE") else {
        return Ok(BTreeSet::new());
    };
    if path.is_empty() {
        return Ok(BTreeSet::new());
    }
    let contents = fs::read_to_string(&path)
        .map_err(|error| format!("read baseline untracked file {path}: {error}"))?;
    Ok(contents
        .lines()
        .filter(|line| !line.is_empty())
        .map(str::to_string)
        .collect())
}

#[cfg(unix)]
fn path_from_git_bytes(bytes: &[u8]) -> PathBuf {
    PathBuf::from(OsString::from_vec(bytes.to_vec()))
}

#[cfg(not(unix))]
fn path_from_git_bytes(bytes: &[u8]) -> PathBuf {
    PathBuf::from(String::from_utf8_lossy(bytes).into_owned())
}

fn git_error(fallback: &str, stderr: &[u8]) -> String {
    let message = String::from_utf8_lossy(stderr).trim().to_string();
    if message.is_empty() {
        fallback.to_string()
    } else {
        message
    }
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
