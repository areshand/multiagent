"""Exact Git snapshot and changed-code analysis primitives."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


SOURCE_EXTENSIONS = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".pyi",
        ".pyx",
        ".rb",
        ".rs",
        ".scala",
        ".swift",
        ".ts",
        ".tsx",
    }
)
IGNORED_SOURCE_PREFIXES = (".cache/", ".gomodcache/", "node_modules/", "vendor/")


def changed_paths_from_diff(diff: str) -> set[str]:
    """Return both old and new paths represented in a unified Git diff."""

    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git a/") or " b/" not in line:
            continue
        before_b, after_b = line.split(" b/", 1)
        old_path = before_b[len("diff --git a/") :]
        new_path = after_b.split("\t", 1)[0].strip()
        for path in (old_path, new_path):
            if path and path != "/dev/null":
                paths.add(path)
    return paths


def final_diff_sha256(diff: str) -> str:
    """Bind verifier evidence to the exact submitted diff text."""

    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        "test" in parts
        or "tests" in parts
        or "__tests__" in parts
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx", ".test.js", ".spec.js"))
    )


def changed_code_paths_from_diff(diff: str) -> list[str]:
    """Return changed production-code paths, excluding tests and generated caches."""

    return sorted(
        path
        for path in changed_paths_from_diff(diff)
        if Path(path).suffix in SOURCE_EXTENSIONS
        and not is_test_path(path)
        and not path.startswith(IGNORED_SOURCE_PREFIXES)
    )


@dataclass(frozen=True)
class RepositorySnapshot:
    """A final-diff snapshot shared by workers, verifiers, and submission gates."""

    diff: str
    sha256: str
    changed_file_count: int
    changed_paths: tuple[str, ...]
    changed_code_paths: tuple[str, ...]

    @classmethod
    def from_diff(cls, diff: str) -> RepositorySnapshot:
        return cls(
            diff=diff,
            sha256=final_diff_sha256(diff),
            changed_file_count=sum(1 for line in diff.splitlines() if line.startswith("diff --git a/")),
            changed_paths=tuple(sorted(changed_paths_from_diff(diff))),
            changed_code_paths=tuple(changed_code_paths_from_diff(diff)),
        )

    @classmethod
    def capture(cls, root: Path, base: str = "HEAD") -> RepositorySnapshot:
        result = subprocess.run(
            ["git", "-C", str(root), "diff", base, "--binary", "--ignore-submodules=all", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "git diff failed")
        snapshot = cls.from_diff(result.stdout.decode("utf-8", errors="replace"))
        return cls(
            diff=snapshot.diff,
            sha256=hashlib.sha256(result.stdout).hexdigest(),
            changed_file_count=snapshot.changed_file_count,
            changed_paths=snapshot.changed_paths,
            changed_code_paths=snapshot.changed_code_paths,
        )
