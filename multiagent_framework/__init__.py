"""Reusable runtime primitives for the multiagent framework."""

from .snapshot import (
    RepositorySnapshot,
    changed_code_paths_from_diff,
    changed_paths_from_diff,
    final_diff_sha256,
    is_test_path,
)
from .gate import structured_repair_gate_blockers
from .cli import multiagent_command, multiagent_subcommand
from .provenance import (
    capture_git_identity,
    copy_artifact_bundle,
    sha256_file,
    validate_artifact_bundle,
)
from .state import AtomicStatusStore
from .verification import (
    behavior_verification_has_evidence,
    build_verification_has_evidence,
    verifier_passing_commands,
    verifier_rechecked_todo,
    verifier_text_covers_resolution_commands,
)

__all__ = [
    "AtomicStatusStore",
    "RepositorySnapshot",
    "behavior_verification_has_evidence",
    "build_verification_has_evidence",
    "capture_git_identity",
    "changed_code_paths_from_diff",
    "changed_paths_from_diff",
    "copy_artifact_bundle",
    "final_diff_sha256",
    "is_test_path",
    "multiagent_command",
    "multiagent_subcommand",
    "sha256_file",
    "structured_repair_gate_blockers",
    "validate_artifact_bundle",
    "verifier_passing_commands",
    "verifier_rechecked_todo",
    "verifier_text_covers_resolution_commands",
]
