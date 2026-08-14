"""Portable evaluation provenance primitives for Git checkouts and artifacts."""

import hashlib
import ntpath
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Mapping, Union


PathLike = Union[str, Path]
_SAFE_KIND = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def sha256_file(path: PathLike) -> str:
    """Return the hexadecimal SHA-256 digest of a file."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: PathLike, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "git command failed")
    return result.stdout.decode("utf-8", errors="strict").strip()


def capture_git_identity(repo: PathLike) -> Dict[str, object]:
    """Capture the full HEAD commit and tree IDs plus working-tree dirtiness."""

    return {
        "commit": _git(repo, "rev-parse", "--verify", "HEAD"),
        "tree": _git(repo, "rev-parse", "--verify", "HEAD^{tree}"),
        "dirty": bool(
            _git(
                repo,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            )
        ),
    }


def _validate_kind(kind: object) -> str:
    if not isinstance(kind, str) or not _SAFE_KIND.fullmatch(kind):
        raise ValueError("artifact kind must match [A-Za-z0-9][A-Za-z0-9._-]*")
    if kind in (".", ".."):
        raise ValueError("artifact kind cannot be '.' or '..'")
    return kind


def _artifact_path(kind: str) -> str:
    return "artifacts/" + kind


def copy_artifact_bundle(
    bundle_root: PathLike, mapping: Mapping[str, PathLike]
) -> List[Dict[str, str]]:
    """Copy named files into a relocatable bundle and return digest records."""

    root = Path(bundle_root)
    artifact_root = root / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    records = []  # type: List[Dict[str, str]]
    for raw_kind in sorted(mapping):
        kind = _validate_kind(raw_kind)
        source = Path(mapping[raw_kind])
        if not source.is_file():
            raise ValueError("artifact source is not a file: {}".format(source))
        relative_path = _artifact_path(kind)
        destination = root / Path(relative_path)
        try:
            same_file = source.resolve() == destination.resolve()
        except OSError:
            same_file = False
        if not same_file:
            shutil.copyfile(str(source), str(destination))
        records.append(
            {
                "kind": kind,
                "path": relative_path,
                "sha256": sha256_file(destination),
            }
        )
    return records


def _validate_relative_path(path: object) -> str:
    if not isinstance(path, str) or not path or "\\" in path or ntpath.isabs(path):
        raise ValueError("artifact path must be a relative POSIX path")
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute() or ".." in pure_path.parts or pure_path.as_posix() != path:
        raise ValueError("artifact path is absolute, traversing, or non-canonical")
    return path


def validate_artifact_bundle(
    bundle_root: PathLike,
    records: Iterable[Mapping[str, object]],
    required_kinds: Iterable[str],
) -> None:
    """Validate bundle record uniqueness, paths, required kinds, and hashes."""

    root = Path(bundle_root).resolve()
    seen_kinds = set()
    seen_paths = set()
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("artifact record must be a mapping")
        kind = _validate_kind(record.get("kind"))
        path = _validate_relative_path(record.get("path"))
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
            raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
        if kind in seen_kinds:
            raise ValueError("duplicate artifact kind: {}".format(kind))
        if path in seen_paths:
            raise ValueError("duplicate artifact path: {}".format(path))
        seen_kinds.add(kind)
        seen_paths.add(path)
        if path != _artifact_path(kind):
            raise ValueError("artifact kind/path mismatch: {}".format(kind))
        artifact = root / Path(path)
        try:
            artifact.resolve().relative_to(root)
        except (OSError, ValueError):
            raise ValueError("artifact path escapes bundle root: {}".format(path))
        if not artifact.is_file():
            raise ValueError("artifact file is missing: {}".format(path))
        if sha256_file(artifact) != expected_hash:
            raise ValueError("artifact hash mismatch: {}".format(path))

    required = {_validate_kind(kind) for kind in required_kinds}
    missing = sorted(required - seen_kinds)
    if missing:
        raise ValueError("missing required artifact kinds: {}".format(", ".join(missing)))


__all__ = [
    "capture_git_identity",
    "copy_artifact_bundle",
    "sha256_file",
    "validate_artifact_bundle",
]
