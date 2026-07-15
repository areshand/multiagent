from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class LifecycleProgress:
    """Mutable state for one production solver lifecycle."""

    deadline: float = 0.0
    last_capture: float = 0.0
    missing_session_captures: int = 0
    coverage_followups_sent: int = 0
    coverage_followup_at: float | None = None
    early_scope_followups_sent: int = 0
    early_scope_signature: str = ""
    early_scope_seen_count: int = 0
    adapter_helper_workers_spawned: int = 0
    adapter_helper_last_spawn_at: float | None = None
    adapter_helper_reprobe_done: bool = False
    adapter_helper_last_probe_digest: str | None = None
    coverage_gate_unresolved: bool = False
    coverage_probe_satisfied: bool = False
    accepted_completed_status_snapshot: dict[str, object] | None = None
    accepted_completed_status_diff_hash: str = ""
    selected_validation_claim_seen: bool = False
    convergence_followup_sent: bool = False
    no_diff_checkpoint_sent: bool = False
    no_diff_live_handoff_sent: bool = False
    progress_repair_sent: bool = False
    terminal_deadline_sent: bool = False
    terminal_deadline_at: float | None = None
    no_diff_blocked_retries: int = 0
    active_followup_extensions: int = 0
    active_verifier_blocked_at: float | None = None
    convergence_start: float = 0.0
    last_diff_digest: str = ""
    last_diff_changed_at: float = 0.0
    orchestrator_resume_attempts: int = 0
    source_symbol_resume_attempts: int = 0
    verifier_infra_resume_attempts: int = 0
    repair_todo_resume_attempts: int = 0
    verifier_blocking_handoffs: set[str] = field(default_factory=set)
    adapter_helper_advisory_logs: set[str] = field(default_factory=set)
    exit_code: int = 0
    outcome: str = "timeout"


@dataclass(frozen=True)
class LifecyclePolicy:
    """Bounded retry and checkpoint policy for one solver run."""

    coverage_followup_limit: int
    early_scope_followup_limit: int
    convergence_followup_after: int
    no_diff_checkpoint_after: int
    no_diff_live_handoff_after: int
    progress_repair_enabled: bool
    progress_repair_after: int
    progress_repair_min_stall: int
    terminal_deadline_remaining: int
    terminal_deadline_grace: int
    terminal_force_resume_enabled: bool
    no_diff_blocked_retry_limit: int
    active_followup_extension_limit: int
    active_verifier_grace: int
    adapter_helper_worker_limit: int
    orchestrator_resume_limit: int
    source_symbol_resume_limit: int
    verifier_infra_resume_limit: int
    repair_todo_resume_limit: int
    early_adapter_helper_spawn_enabled: bool
    coverage_followup_timeout: int
    adapter_helper_grace_seconds: int

    @classmethod
    def from_environment(cls, truthy: Callable[[str, bool], bool]) -> LifecyclePolicy:
        """Read lifecycle retry policy once at the production boundary."""

        return cls(
            coverage_followup_limit=int(os.environ.get("EVAL_COVERAGE_FOLLOWUP_LIMIT", "3")),
            early_scope_followup_limit=int(os.environ.get("EVAL_EARLY_SCOPE_FOLLOWUP_LIMIT", "3")),
            convergence_followup_after=int(os.environ.get("EVAL_CONVERGENCE_FOLLOWUP_AFTER", "900")),
            no_diff_checkpoint_after=int(os.environ.get("EVAL_NO_DIFF_CHECKPOINT_AFTER", "360")),
            no_diff_live_handoff_after=int(os.environ.get("EVAL_NO_DIFF_LIVE_HANDOFF_AFTER", "720")),
            progress_repair_enabled=truthy("EVAL_PROGRESS_REPAIR_ENABLED", True),
            progress_repair_after=int(os.environ.get("EVAL_PROGRESS_REPAIR_AFTER", "1200")),
            progress_repair_min_stall=int(os.environ.get("EVAL_PROGRESS_REPAIR_MIN_STALL", "240")),
            terminal_deadline_remaining=int(os.environ.get("EVAL_TERMINAL_DEADLINE_REMAINING", "900")),
            terminal_deadline_grace=int(os.environ.get("EVAL_TERMINAL_DEADLINE_GRACE", "300")),
            terminal_force_resume_enabled=truthy("EVAL_TERMINAL_FORCE_RESUME", True),
            no_diff_blocked_retry_limit=int(os.environ.get("EVAL_NO_DIFF_BLOCKED_RETRY_LIMIT", "4")),
            active_followup_extension_limit=int(os.environ.get("EVAL_ACTIVE_FOLLOWUP_EXTENSION_LIMIT", "8")),
            active_verifier_grace=int(os.environ.get("EVAL_ACTIVE_VERIFIER_GRACE", "240")),
            adapter_helper_worker_limit=int(os.environ.get("EVAL_ADAPTER_HELPER_WORKER_LIMIT", "1")),
            orchestrator_resume_limit=int(os.environ.get("EVAL_ORCHESTRATOR_RESUME_LIMIT", "1")),
            source_symbol_resume_limit=int(os.environ.get("EVAL_SOURCE_SYMBOL_RESUME_LIMIT", "1")),
            verifier_infra_resume_limit=int(os.environ.get("EVAL_VERIFIER_INFRA_RESUME_LIMIT", "2")),
            repair_todo_resume_limit=int(os.environ.get("EVAL_REPAIR_TODO_RESUME_LIMIT", "1")),
            early_adapter_helper_spawn_enabled=truthy("EVAL_ADAPTER_HELPER_EARLY_SPAWN", False),
            coverage_followup_timeout=int(os.environ.get("EVAL_COVERAGE_FOLLOWUP_TIMEOUT", "900")),
            adapter_helper_grace_seconds=int(os.environ.get("EVAL_ADAPTER_HELPER_GRACE_SECONDS", "600")),
        )
