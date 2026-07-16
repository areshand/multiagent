from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .swe_prod_contracts import (
    HELPER_PROBE_PATH,
    RUNTIME_ROOT,
    STATUS_PATH,
    log,
    recovered_validation_text,
)
from .swe_prod_evidence import (
    accepted_without_status_marker,
    active_repair_subagent_summaries,
    active_verifier_subagent_summaries,
    append_adapter_probe_evidence,
    assignment_owned_paths,
    blocked_no_diff_subagent_summaries,
    blocked_without_status_marker,
    capture_session,
    captured_text,
    completed_status_covers_adapter_validation,
    final_verifier_accepted_without_status,
    has_live_agent_process,
    inferred_required_paths_from_worker_text,
    no_diff_blocked_subagent_blockers,
    orchestrator_exited_without_status,
    orchestrator_infrastructure_handoff_needed,
    recovered_validation_with_helper_evidence,
    required_path_outside_owned_reports,
    resolved_repair_todo_ids,
    status_with_recovered_public_evidence,
    status_with_recovered_validation,
    structured_repair_gate_blockers,
    tmux_has_session,
    unresolved_repair_state_exists,
    verifier_exact_followup_available,
    verifier_infrastructure_blockers,
)
from .swe_prod_guardrails import (
    coverage_probe_commands,
    helper_scope_hints,
    implementation_scope_blockers,
)
from .swe_prod_orchestration import (
    send_orchestrator_convergence_review,
    send_orchestrator_followup,
    send_orchestrator_no_diff_checkpoint,
    send_orchestrator_scope_warning,
    send_orchestrator_terminal_deadline,
    spawn_adapter_helper_worker,
)
from .swe_prod_repository import git_diff
from .swe_prod_types import LifecyclePolicy, LifecycleProgress
from .swe_prod_validation import (
    blockers_after_passing_public_probe,
    has_hard_scope_blocker,
    run_validation_coverage_probe,
    validation_coverage_blockers,
)

def handle_repair_readiness_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Recover infrastructure, resolved todos, and no-diff repair readiness."""

    if (
        orchestrator_infrastructure_handoff_needed(
            current_status,
            text,
            RUNTIME_ROOT,
            workdir,
        )
        and not has_live_agent_process()
        and remaining_seconds > 300
    ):
        infrastructure_blockers = [
            *implementation_scope_blockers(issue, diff_snapshot, current_status, task_metadata),
            *validation_coverage_blockers(issue, diff_snapshot, text, current_status, task_metadata),
            *verifier_infrastructure_blockers(text, workdir),
            (
                "The production orchestrator exited without status.json after a tool/infrastructure failure. "
                "Preserve the live /app diff, reconcile terminal worker reports, then run independent exact-hash "
                "build and behavior verification before writing terminal status."
            ),
        ]
        if relaunch_orchestrator_for_blockers(
            "orchestrator exited without terminal status after tool infrastructure failure",
            diff_snapshot,
            list(dict.fromkeys(infrastructure_blockers)),
            "",
            force_live_handoff=True,
        ):
            log("terminal orchestrator infrastructure failure handed off immediately")
            time.sleep(5)
            return "continue"
    if (
        not state
        and diff_bytes > 0
        and resolved_todos
        and not active_repair_workers
        and not active_verifiers
        and remaining_seconds > 300
    ):
        repair_gate_blockers = structured_repair_gate_blockers()
        if repair_gate_blockers and relaunch_orchestrator_for_blockers(
            "resolved repair todo is waiting for verifier closure",
            diff_snapshot,
            [
                *repair_gate_blockers,
                (
                    "Resolved worker todo(s) are ready for objective reverification: "
                    + ", ".join(resolved_todos)
                    + ". Spawn one fresh read-only verifier over the exact current diff, close or reopen each "
                    "todo from its original finding and done criteria, then rerun gate-check."
                ),
            ],
            "",
            force_live_handoff=True,
        ):
            log("resolved repair todo handoff launched before terminal deadline")
            time.sleep(5)
            return "continue"
    blocked_no_diff_subagents = blocked_no_diff_subagent_summaries(RUNTIME_ROOT)
    if (
        not state
        and diff_bytes == 0
        and blocked_no_diff_subagents
        and progress.no_diff_blocked_retries < policy.no_diff_blocked_retry_limit
        and remaining_seconds > 300
    ):
        progress.no_diff_blocked_retries += 1
        blockers = no_diff_blocked_subagent_blockers(RUNTIME_ROOT)
        if relaunch_orchestrator_for_blockers(
            "blocked subagent with no materialized source diff",
            diff_snapshot,
            blockers,
            "",
            force_live_handoff=True,
        ):
            log(f"no-diff blocked subagent retry launched attempt={progress.no_diff_blocked_retries}")
            time.sleep(5)
            return "continue"

    return "wait"


def handle_terminal_deadline_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Enforce the terminal deadline and bounded handoff policy."""

    if (
        not state
        and not progress.terminal_deadline_sent
        and policy.terminal_deadline_remaining > 0
        and remaining_seconds <= policy.terminal_deadline_remaining
        and tmux_has_session(session)
    ):
        diff = diff_snapshot
        terminal_blockers: list[str] = []
        probe_report = ""
        if diff_bytes > 0:
            scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
            coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
            terminal_blockers = [*scope_blockers, *coverage_blockers]
            if progress.coverage_probe_satisfied:
                terminal_blockers = blockers_after_passing_public_probe(terminal_blockers)
            elif coverage_probe_commands(workdir, issue, diff):
                probe_report, probe_passed = run_validation_coverage_probe(
                    workdir,
                    issue,
                    diff,
                    terminal_blockers
                    or [
                        "terminal deadline checkpoint ran public validation before forcing final orchestrator status"
                    ],
                )
                if probe_passed:
                    progress.coverage_probe_satisfied = True
                    terminal_blockers = blockers_after_passing_public_probe(scope_blockers)
                else:
                    terminal_blockers = [
                        *scope_blockers,
                        f"terminal deadline adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                    ]
        else:
            terminal_blockers = [
                "terminal deadline reached with no materialized source diff; write blocked status or produce the narrow source diff now"
            ]
        send_orchestrator_terminal_deadline(
            session,
            remaining_seconds=remaining_seconds,
            diff=diff,
            blockers=terminal_blockers,
            probe_report=probe_report,
            source_hints=helper_scope_hints(workdir, issue, diff, terminal_blockers),
        )
        progress.terminal_deadline_sent = True
        progress.terminal_deadline_at = time.monotonic()
        log(
            "terminal deadline checkpoint sent with "
            f"remaining={remaining_seconds}s blockers={'; '.join(terminal_blockers) if terminal_blockers else 'none'}"
        )
        progress.last_capture = time.monotonic()
        time.sleep(5)
        return "continue"
    if (
        not state
        and progress.terminal_deadline_at is not None
        and policy.terminal_deadline_grace > 0
        and time.monotonic() - progress.terminal_deadline_at >= policy.terminal_deadline_grace
    ):
        diff = git_diff(workdir)
        deadline_blockers = [
            *implementation_scope_blockers(issue, diff, {}, task_metadata),
            *validation_coverage_blockers(issue, diff, text, {}, task_metadata),
        ]
        deadline_probe_report = ""
        if progress.coverage_probe_satisfied:
            deadline_blockers = blockers_after_passing_public_probe(deadline_blockers)
        if not deadline_blockers:
            deadline_blockers = [
                "terminal deadline expired without completed/blocked status after orchestrator checkpoint; wrapper cannot accept an active-run diff without terminal verifier/status"
            ]
        remaining_after_grace = int(progress.deadline - time.monotonic())
        active_repair_workers = active_repair_subagent_summaries(RUNTIME_ROOT)
        if (
            active_repair_workers
            and unresolved_repair_state_exists(RUNTIME_ROOT)
            and remaining_after_grace > 180
        ):
            log(
                "terminal deadline grace extended because active repair worker(s) are still running: "
                + "; ".join(active_repair_workers[:3])
            )
            progress.terminal_deadline_at = time.monotonic()
            progress.last_capture = 0.0
            time.sleep(10)
            return "continue"
        if (
            policy.terminal_force_resume_enabled
            and diff.strip()
            and progress.orchestrator_resume_attempts < policy.orchestrator_resume_limit
            and remaining_after_grace > 240
        ):
            if coverage_probe_commands(workdir, issue, diff):
                deadline_probe_report, deadline_probe_passed = run_validation_coverage_probe(
                    workdir,
                    issue,
                    diff,
                    deadline_blockers
                    or [
                        "terminal handoff ran adapter-selected public validation before replacing a non-converged orchestrator"
                    ],
                )
                if deadline_probe_passed:
                    progress.coverage_probe_satisfied = True
                    deadline_blockers = blockers_after_passing_public_probe(
                        implementation_scope_blockers(issue, diff, {}, task_metadata)
                    )
                elif not deadline_blockers:
                    deadline_blockers = [
                        f"terminal handoff adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}"
                    ]
            handoff_blockers = [
                *deadline_blockers,
                "Terminal handoff: the active production orchestrator did not write completed/blocked status after the deadline checkpoint. Continue from the current /app diff, preserve correct work, run or attempt source-visible validation, then write status.json.",
            ]
            if relaunch_orchestrator_for_blockers(
                "terminal deadline expired with active no-status diff",
                diff,
                handoff_blockers,
                deadline_probe_report,
                force_live_handoff=True,
            ):
                progress.terminal_deadline_sent = False
                progress.terminal_deadline_at = None
                progress.last_capture = 0.0
                time.sleep(5)
                return "continue"
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "terminal deadline expired without machine-readable orchestrator status",
                    "blockers": deadline_blockers,
                }
            ),
            encoding="utf-8",
        )
        log("blocked marker: terminal deadline expired without machine-readable orchestrator status")
        progress.exit_code = 2
        progress.outcome = "blocked"
        return "break"

    return "wait"


def handle_early_scope_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Surface stable source-scope blockers before terminal status."""

    if (
        not state
        and diff_bytes > 0
        and progress.early_scope_followups_sent < policy.early_scope_followup_limit
        and tmux_has_session(session)
        and not orchestrator_exited_without_status(text)
    ):
        diff = git_diff(workdir)
        early_scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
        if early_scope_blockers:
            signature = "; ".join(early_scope_blockers)
            if signature == progress.early_scope_signature:
                progress.early_scope_seen_count += 1
            else:
                progress.early_scope_signature = signature
                progress.early_scope_seen_count = 1
            if progress.early_scope_seen_count >= 2:
                source_hints = helper_scope_hints(workdir, issue, diff, early_scope_blockers)
                send_orchestrator_scope_warning(
                    session,
                    early_scope_blockers,
                    source_hints,
                )
                progress.early_scope_followups_sent += 1
                log(f"early scope warning {progress.early_scope_followups_sent}: {signature}")
                if (
                    policy.early_adapter_helper_spawn_enabled
                    and not has_live_agent_process()
                    and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                    and adapter_helper_repair_allowed("early scope warning")
                ):
                    progress.adapter_helper_workers_spawned += 1
                    try:
                        helper_worker = spawn_adapter_helper_worker(
                            repo_root,
                            workdir,
                            env,
                            issue,
                            diff,
                            early_scope_blockers,
                            source_hints,
                            progress.adapter_helper_workers_spawned,
                        )
                        log(f"adapter helper worker spawned: {helper_worker}")
                        progress.adapter_helper_last_spawn_at = time.monotonic()
                        progress.adapter_helper_reprobe_done = False
                    except Exception as exc:
                        log(f"adapter helper worker spawn failed: {exc}")
                elif not policy.early_adapter_helper_spawn_enabled:
                    log(
                        "adapter helper worker early spawn skipped; preserving orchestrator ownership of active source edits"
                    )
                progress.last_capture = time.monotonic()
                time.sleep(5)
                return "continue"
        else:
            progress.early_scope_signature = ""
            progress.early_scope_seen_count = 0

    return "wait"


def handle_unmarked_terminal_evidence(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Recover accepted or blocked terminal evidence without status markers."""

    if not state and accepted_without_status_marker(text, diff_bytes):
        diff = git_diff(workdir)
        scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
        coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
        blockers = [*scope_blockers, *coverage_blockers]
        if progress.coverage_probe_satisfied:
            blockers = blockers_after_passing_public_probe(blockers)
            scope_blockers = blockers
            coverage_blockers = []
        probe_report = ""
        if blockers and progress.coverage_followups_sent < policy.coverage_followup_limit and tmux_has_session(session):
            if coverage_blockers or coverage_probe_commands(workdir, issue, diff):
                probe_report, probe_passed = run_validation_coverage_probe(workdir, issue, diff, coverage_blockers)
            else:
                probe_passed = False
            if probe_passed:
                progress.coverage_probe_satisfied = True
                blockers = blockers_after_passing_public_probe([*scope_blockers, *coverage_blockers])
                scope_blockers = blockers
                coverage_blockers = []
                log("coverage gate satisfied by adapter public helper probe")
            if blockers:
                progress.coverage_followups_sent += 1
                send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
                log(f"coverage gate follow-up {progress.coverage_followups_sent}: {'; '.join(blockers)}")
                progress.coverage_followup_at = time.monotonic()
                if (
                    orchestrator_exited_without_status(text)
                    and not has_live_agent_process()
                    and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                    and adapter_helper_repair_allowed("rejected recovered completion")
                ):
                    progress.adapter_helper_workers_spawned += 1
                    try:
                        helper_worker = spawn_adapter_helper_worker(
                            repo_root,
                            workdir,
                            env,
                            issue,
                            diff,
                            [
                                *blockers,
                                "The orchestrator already exited after a rejected completion marker; continue from the current /app diff and do not wait for the orchestrator to spawn this follow-up.",
                            ],
                            helper_scope_hints(workdir, issue, diff, blockers),
                            progress.adapter_helper_workers_spawned,
                            probe_report,
                        )
                        log(f"adapter recovery worker spawned immediately after rejected recovered completion: {helper_worker}")
                        progress.adapter_helper_last_spawn_at = time.monotonic()
                        progress.adapter_helper_reprobe_done = False
                        progress.adapter_helper_last_probe_digest = None
                    except Exception as exc:
                        log(f"adapter recovery worker spawn failed after rejected recovered completion: {exc}")
                progress.last_capture = 0.0
                time.sleep(5)
                return "continue"
        if blockers and relaunch_orchestrator_for_blockers(
            "recovered completion rejected by public/source validation",
            diff,
            blockers,
            probe_report,
        ):
            time.sleep(5)
            return "continue"
        if blockers and has_hard_scope_blocker(blockers):
            log(f"hard public scope blockers remain after follow-ups; refusing recovered accepted patch: {'; '.join(blockers)}")
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "hard public scope blocker remains after recovered acceptance",
                        "blockers": blockers,
                    }
                ),
                encoding="utf-8",
            )
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
        if blockers:
            progress.coverage_gate_unresolved = True
            log(f"recovered completion refused because coverage blockers remain after follow-ups: {'; '.join(blockers)}")
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "coverage blockers remain after recovered acceptance",
                        "blockers": blockers,
                    }
                ),
                encoding="utf-8",
            )
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "accepted source diff found; orchestrator failed to write status marker",
                    "validation": recovered_validation_text(
                        task_metadata,
                        text,
                        (
                            f"see captured verifier output; helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                            if progress.coverage_probe_satisfied
                            else "see captured verifier output"
                        ),
                    ),
                    "risk": "status marker was recovered by the benchmark wrapper",
                }
            ),
            encoding="utf-8",
        )
        log("completion marker recovered from accepted diff plus verifier output")
        progress.outcome = "recovered"
        return "break"
    if not state and final_verifier_accepted_without_status(text, diff_bytes):
        diff = git_diff(workdir)
        probe_report = ""
        probe_passed = progress.coverage_probe_satisfied
        if not probe_passed and coverage_probe_commands(workdir, issue, diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                diff,
                ["final verifier accepted without status.json; adapter reran selected public validation before recovery"],
            )
        recovered_base = (
            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
            if probe_passed
            else "final verifier accepted without status.json; adapter public helper probe did not pass"
        )
        recovered_validation = recovered_validation_text(
            task_metadata,
            text,
            recovered_base,
        )
        recovered_validation = recovered_validation_with_helper_evidence(issue, text, recovered_validation)
        recovered_status = status_with_recovered_validation({}, recovered_validation)
        scope_blockers = implementation_scope_blockers(issue, diff, recovered_status, task_metadata)
        if probe_passed:
            blockers = blockers_after_passing_public_probe(scope_blockers)
            if not blockers:
                STATUS_PATH.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "summary": "final verifier accepted source diff; adapter recovered missing status marker",
                            "validation": recovered_validation,
                            "risk": "status marker was recovered by the benchmark wrapper",
                        }
                    ),
                    encoding="utf-8",
                )
                log("completion marker recovered from final verifier accept plus passing adapter probe")
                progress.outcome = "recovered"
                return "break"
            coverage_blockers = []
            log(
                "final verifier accepted and adapter probe passed, but hard implementation blockers remain: "
                + "; ".join(blockers)
            )
        else:
            coverage_blockers = [
                f"final verifier accepted without status.json, but adapter-selected public validation probe failed; inspect {HELPER_PROBE_PATH}"
            ]
            blockers = [*scope_blockers, *coverage_blockers]
        if (
            tmux_has_session(session)
            and not has_live_agent_process()
            and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
            and adapter_helper_repair_allowed("final verifier/probe mismatch")
        ):
            progress.adapter_helper_workers_spawned += 1
            try:
                helper_worker = spawn_adapter_helper_worker(
                    repo_root,
                    workdir,
                    env,
                    issue,
                    diff,
                    [
                        *blockers,
                        "The final verifier accepted too early, but the adapter public probe caught a required source-derived public API mismatch. Continue from the current /app diff, add only the missing public contract, and make the adapter probe pass before any completion marker.",
                    ],
                    helper_scope_hints(workdir, issue, diff, blockers),
                    progress.adapter_helper_workers_spawned,
                    probe_report,
                )
                log(f"adapter recovery worker spawned after final verifier/probe mismatch: {helper_worker}")
                progress.adapter_helper_last_spawn_at = time.monotonic()
                progress.adapter_helper_reprobe_done = False
                progress.adapter_helper_last_probe_digest = None
                progress.coverage_followup_at = time.monotonic()
                progress.last_capture = 0.0
                time.sleep(5)
                return "continue"
            except Exception as exc:
                log(f"adapter recovery worker spawn failed after final verifier/probe mismatch: {exc}")
        if progress.coverage_followups_sent < policy.coverage_followup_limit and tmux_has_session(session):
            progress.coverage_followups_sent += 1
            send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
            log(f"coverage gate follow-up {progress.coverage_followups_sent}: {'; '.join(blockers)}")
            progress.coverage_followup_at = time.monotonic()
            progress.last_capture = 0.0
            time.sleep(5)
            return "continue"
        if blockers and relaunch_orchestrator_for_blockers(
            "final verifier accepted before public/source validation passed",
            diff,
            blockers,
            probe_report,
        ):
            time.sleep(5)
            return "continue"
        progress.coverage_gate_unresolved = True
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "final verifier accepted but adapter public validation probe failed",
                    "blockers": blockers,
                }
            ),
            encoding="utf-8",
        )
        log("blocked marker: final verifier accepted but adapter public validation probe failed")
        progress.exit_code = 2
        progress.outcome = "blocked"
        return "break"
    if not state and blocked_without_status_marker(text):
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "orchestrator reported a terminal blocker without writing status.json",
                }
            ),
            encoding="utf-8",
        )
        log("blocked marker recovered from orchestrator terminal blocker text")
        progress.exit_code = 2
        progress.outcome = "blocked"
        return "break"

    return "wait"


def handle_convergence_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Drive convergence, progress repair, and no-diff checkpoints."""

    if (
        not state
        and diff_bytes > 0
        and not progress.convergence_followup_sent
        and policy.convergence_followup_after > 0
        and time.monotonic() - progress.convergence_start >= policy.convergence_followup_after
        and tmux_has_session(session)
    ):
        diff = git_diff(workdir)
        source_hints = helper_scope_hints(workdir, issue, diff, [])
        send_orchestrator_convergence_review(
            session,
            elapsed_seconds=int(time.monotonic() - progress.convergence_start),
            diff=diff,
            source_hints=source_hints,
        )
        progress.convergence_followup_sent = True
        log(
            "convergence checkpoint sent after "
            f"{int(time.monotonic() - progress.convergence_start)}s with diff_bytes={diff_bytes}"
        )
        progress.last_capture = time.monotonic()
        time.sleep(5)
        return "continue"
    if (
        not state
        and diff_bytes > 0
        and policy.progress_repair_enabled
        and not progress.progress_repair_sent
        and policy.progress_repair_after > 0
        and time.monotonic() - progress.convergence_start >= policy.progress_repair_after
        and time.monotonic() - progress.last_diff_changed_at >= policy.progress_repair_min_stall
        and tmux_has_session(session)
    ):
        diff = diff_snapshot
        scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
        coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
        blockers = [*scope_blockers, *coverage_blockers]
        probe_report = ""
        probe_passed = False
        if coverage_probe_commands(workdir, issue, diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                diff,
                blockers
                or [
                    "progress watchdog observed a stale source diff; adapter ran public validation before repair"
                ],
            )
            if probe_passed:
                progress.coverage_probe_satisfied = True
                blockers = blockers_after_passing_public_probe(scope_blockers)
            elif not coverage_blockers:
                blockers = [
                    *scope_blockers,
                    f"progress watchdog adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                ]
        progress.progress_repair_sent = True
        if blockers and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit:
            if adapter_helper_repair_allowed("progress watchdog stale diff"):
                progress.adapter_helper_workers_spawned += 1
                try:
                    helper_worker = spawn_adapter_helper_worker(
                        repo_root,
                        workdir,
                        env,
                        issue,
                        diff,
                        [
                            *blockers,
                            "Progress watchdog intervention: the same non-empty source diff has not converged to accepted validation/status. Continue from the current /app diff, fix the source-visible blockers, and do not broaden scope.",
                        ],
                        helper_scope_hints(workdir, issue, diff, blockers),
                        progress.adapter_helper_workers_spawned,
                        probe_report,
                        launch_reason="the production-native progress watchdog",
                    )
                    log(f"progress watchdog spawned bounded repair worker: {helper_worker}")
                    progress.adapter_helper_last_spawn_at = time.monotonic()
                    progress.adapter_helper_reprobe_done = False
                    progress.adapter_helper_last_probe_digest = None
                    progress.coverage_followup_at = time.monotonic()
                    progress.last_capture = 0.0
                    time.sleep(5)
                    return "continue"
                except Exception as exc:
                    log(f"progress watchdog repair worker spawn failed: {exc}")
        if blockers and not has_live_agent_process() and relaunch_orchestrator_for_blockers(
            "progress watchdog found stale source diff with no live agent",
            diff,
            blockers,
            probe_report,
        ):
            time.sleep(5)
            return "continue"
        if blockers:
            send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
            log("progress watchdog sent hard follow-up after stale diff: " + "; ".join(blockers))
            progress.coverage_followup_at = time.monotonic()
        else:
            send_orchestrator_convergence_review(
                session,
                elapsed_seconds=int(time.monotonic() - progress.convergence_start),
                diff=diff,
                source_hints=helper_scope_hints(workdir, issue, diff, []),
            )
            log("progress watchdog found no adapter blockers; requested terminal verifier/status")
        progress.last_capture = time.monotonic()
        time.sleep(5)
        return "continue"
    if (
        not state
        and diff_bytes == 0
        and progress.no_diff_checkpoint_sent
        and not progress.no_diff_live_handoff_sent
        and policy.no_diff_live_handoff_after > 0
        and time.monotonic() - progress.convergence_start >= policy.no_diff_live_handoff_after
        and remaining_seconds > 300
        and tmux_has_session(session)
    ):
        progress.no_diff_live_handoff_sent = True
        blockers = [
            "active production worker/orchestrator remained no-diff after the no-diff checkpoint; force an edit-or-exact-blocker handoff instead of continuing read-only source exploration",
            "spawn at most one bounded implementation worker over source-derived ownership hints, or write blocked status with the exact source path/API that prevents a patch",
        ]
        ownership_hints = list(
            dict.fromkeys(
                [
                    *inferred_required_paths_from_worker_text(RUNTIME_ROOT),
                    *assignment_owned_paths(RUNTIME_ROOT),
                    *helper_scope_hints(workdir, issue, diff_snapshot, blockers),
                ]
            )
        )
        if relaunch_orchestrator_for_blockers(
            "active no-diff worker exceeded edit-or-block checkpoint",
            diff_snapshot,
            [
                *blockers,
                *[
                    f"source ownership hint:{path}"
                    for path in ownership_hints[:8]
                ],
            ],
            "",
            force_live_handoff=True,
        ):
            log(
                "no-diff live handoff launched after "
                f"{int(time.monotonic() - progress.convergence_start)}s hints={','.join(ownership_hints[:8])}"
            )
            time.sleep(5)
            return "continue"
    if (
        not state
        and diff_bytes == 0
        and not progress.no_diff_checkpoint_sent
        and policy.no_diff_checkpoint_after > 0
        and time.monotonic() - progress.convergence_start >= policy.no_diff_checkpoint_after
        and tmux_has_session(session)
    ):
        send_orchestrator_no_diff_checkpoint(
            session,
            elapsed_seconds=int(time.monotonic() - progress.convergence_start),
            issue=issue,
        )
        progress.no_diff_checkpoint_sent = True
        log(
            "no-diff planning checkpoint sent after "
            f"{int(time.monotonic() - progress.convergence_start)}s"
        )
        progress.last_capture = time.monotonic()
        time.sleep(5)
        return "continue"

    return "wait"


def handle_orchestrator_exit_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Recover an exited orchestrator while preserving the live diff."""

    if (
        not state
        and diff_bytes > 0
        and not has_live_agent_process()
        and (
            orchestrator_exited_without_status(text)
            or not tmux_has_session(session)
        )
        and not progress.coverage_followup_at
    ):
        diff = git_diff(workdir)
        coverage_status_for_blockers = status_with_recovered_public_evidence(
            {},
            "captured coverage-follow-up verifier/worker text",
            issue,
            text,
        )
        scope_blockers = implementation_scope_blockers(issue, diff, coverage_status_for_blockers, task_metadata)
        coverage_blockers = validation_coverage_blockers(
            issue,
            diff,
            text,
            coverage_status_for_blockers,
            task_metadata,
        )
        infra_blockers = verifier_infrastructure_blockers(text, workdir)
        repair_blockers = structured_repair_gate_blockers()
        blockers = [*scope_blockers, *coverage_blockers, *infra_blockers, *repair_blockers]
        probe_report = ""
        if coverage_probe_commands(workdir, issue, diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                diff,
                blockers or ["orchestrator exited with a source diff but no status marker; adapter ran public validation before recovery"],
            )
            if probe_passed:
                progress.coverage_probe_satisfied = True
                blockers = [*blockers_after_passing_public_probe(scope_blockers), *infra_blockers, *repair_blockers]
            else:
                blockers = [
                    *scope_blockers,
                    f"orchestrator exited without status and adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                ]
        if blockers and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit and adapter_helper_repair_allowed("orchestrator exited with unverified diff"):
            progress.adapter_helper_workers_spawned += 1
            try:
                helper_worker = spawn_adapter_helper_worker(
                    repo_root,
                    workdir,
                    env,
                    issue,
                    diff,
                    [
                        *blockers,
                        "The orchestrator exited after producing a source diff but without a completion status; continue from the current /app diff and resolve these adapter blockers.",
                    ],
                    helper_scope_hints(workdir, issue, diff, blockers),
                    progress.adapter_helper_workers_spawned,
                    probe_report,
                )
                log(f"adapter recovery worker spawned after unverified orchestrator-exit diff: {helper_worker}")
                progress.adapter_helper_last_spawn_at = time.monotonic()
                progress.adapter_helper_reprobe_done = False
                progress.adapter_helper_last_probe_digest = None
                progress.coverage_followup_at = time.monotonic()
                progress.last_capture = 0.0
                time.sleep(5)
                return "continue"
            except Exception as exc:
                log(f"adapter recovery worker spawn failed after unverified orchestrator-exit diff: {exc}")
        if infra_blockers and relaunch_orchestrator_for_blockers(
            "verifier infrastructure failed before semantic recheck",
            diff,
            blockers,
            probe_report,
            force_live_handoff=True,
        ):
            time.sleep(5)
            return "continue"
        if blockers and relaunch_orchestrator_for_blockers(
            "orchestrator exited with unverified source diff",
            diff,
            blockers,
            probe_report,
        ):
            time.sleep(5)
            return "continue"
        if blockers:
            progress.coverage_gate_unresolved = True
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "orchestrator exited with unverified source diff",
                        "blockers": blockers,
                    }
                ),
                encoding="utf-8",
            )
            log("blocked marker: orchestrator exited with unverified source diff")
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
        recovered_base = (
            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
            if progress.coverage_probe_satisfied
            else "no adapter-selected public validation command was available; implementation blockers were clean"
        )
        STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "summary": "orchestrator exited with a source diff; adapter recovered missing status marker",
                    "validation": recovered_validation_with_helper_evidence(
                        issue,
                        text,
                        recovered_validation_text(
                            task_metadata,
                            text,
                            recovered_base,
                        ),
                    ),
                    "risk": "completion marker recovered by benchmark wrapper after orchestrator exit without status.json",
                }
            ),
            encoding="utf-8",
        )
        log("completion marker recovered from orchestrator-exit source diff")
        progress.outcome = "recovered"
        return "break"

    return "wait"


def handle_coverage_followup_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Reconcile coverage followups and active repair workers."""

    if not state and progress.coverage_followup_at and (
        orchestrator_exited_without_status(text)
        or (diff_bytes > 0 and not has_live_agent_process())
    ):
        diff = git_diff(workdir)
        if completed_status_covers_adapter_validation(workdir, issue, diff):
            log("coverage follow-up recovery yielded to completed status with accepted final build and adapter validation gate")
            progress.outcome = "completed"
            return "break"
        coverage_status_for_blockers = status_with_recovered_public_evidence(
            {},
            "captured coverage-follow-up verifier/worker text",
            issue,
            text,
        )
        scope_blockers = implementation_scope_blockers(issue, diff, coverage_status_for_blockers, task_metadata)
        coverage_blockers = validation_coverage_blockers(
            issue,
            diff,
            text,
            coverage_status_for_blockers,
            task_metadata,
        )
        infra_blockers = verifier_infrastructure_blockers(text, workdir)
        blockers = [*scope_blockers, *coverage_blockers, *infra_blockers]
        if progress.coverage_probe_satisfied:
            blockers = blockers_after_passing_public_probe(blockers)
            scope_blockers = blockers
            coverage_blockers = []
        if not blockers and not progress.coverage_probe_satisfied and coverage_probe_commands(workdir, issue, diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                diff,
                [
                    "orchestrator exited after a coverage follow-up; adapter reran selected public validation before recovery"
                ],
            )
            if probe_passed:
                progress.coverage_probe_satisfied = True
                latest_diff = git_diff(workdir)
                latest_status_for_blockers = append_adapter_probe_evidence(
                    status_with_recovered_public_evidence(
                        {},
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                        issue,
                        text,
                    ),
                    workdir=workdir,
                    diff=latest_diff,
                    marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                )
                scope_blockers = implementation_scope_blockers(
                    issue,
                    latest_diff,
                    latest_status_for_blockers,
                    task_metadata,
                )
                blockers = blockers_after_passing_public_probe(scope_blockers)
            else:
                blockers = [
                    *scope_blockers,
                    f"orchestrator exited after coverage follow-up and adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                ]
        if blockers:
            probe_report = ""
            probe_passed = False
            if coverage_probe_commands(workdir, issue, diff):
                probe_report, probe_passed = run_validation_coverage_probe(
                    workdir,
                    issue,
                    diff,
                    blockers,
                )
            if probe_passed:
                progress.coverage_probe_satisfied = True
                latest_diff = git_diff(workdir)
                latest_status_for_blockers = append_adapter_probe_evidence(
                    status_with_recovered_public_evidence(
                        {},
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                        issue,
                        text,
                    ),
                    workdir=workdir,
                    diff=latest_diff,
                    marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                )
                scope_blockers = implementation_scope_blockers(
                    issue,
                    latest_diff,
                    latest_status_for_blockers,
                    task_metadata,
                )
                blockers = blockers_after_passing_public_probe(scope_blockers)
                if not blockers and latest_diff.strip():
                    recovered_status = append_adapter_probe_evidence(
                        {
                            "status": "completed",
                            "summary": "orchestrator exited after adapter public validation; preserving current source diff",
                            "validation": recovered_validation_with_helper_evidence(
                                issue,
                                text,
                                recovered_validation_text(
                                    task_metadata,
                                    text,
                                    f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                ),
                            ),
                            "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                        },
                        workdir=workdir,
                        diff=latest_diff,
                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                    )
                    STATUS_PATH.write_text(
                        json.dumps(recovered_status),
                        encoding="utf-8",
                    )
                    log("completion marker recovered after adapter public probe passed following orchestrator exit")
                    progress.outcome = "recovered"
                    return "break"
                log(
                    "adapter public probe passed after orchestrator exit, but implementation blockers remain: "
                    + "; ".join(blockers)
                )
            if (
                tmux_has_session(session)
                and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                and adapter_helper_repair_allowed("orchestrator exit coverage blockers")
            ):
                progress.adapter_helper_workers_spawned += 1
                try:
                    helper_worker = spawn_adapter_helper_worker(
                        repo_root,
                        workdir,
                        env,
                        issue,
                        diff,
                        [
                            *blockers,
                            "The orchestrator/verifier exited without resolving these blockers; continue from the current /app diff and make the adapter-selected public validation probe pass before any completion marker.",
                        ],
                        helper_scope_hints(workdir, issue, diff, blockers),
                        progress.adapter_helper_workers_spawned,
                        probe_report,
                    )
                    log(f"adapter recovery worker spawned after orchestrator exit: {helper_worker}")
                    progress.adapter_helper_last_spawn_at = time.monotonic()
                    progress.adapter_helper_reprobe_done = False
                    progress.adapter_helper_last_probe_digest = None
                    progress.coverage_followup_at = time.monotonic()
                    progress.last_capture = 0.0
                    time.sleep(5)
                    return "continue"
                except Exception as exc:
                    log(f"adapter recovery worker spawn failed after orchestrator exit: {exc}")
            if (
                progress.adapter_helper_last_spawn_at is not None
                and time.monotonic() - progress.adapter_helper_last_spawn_at >= 30
                and coverage_probe_commands(workdir, issue, diff)
            ):
                probe_digest = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()
                if progress.adapter_helper_reprobe_done and progress.adapter_helper_last_probe_digest == probe_digest:
                    pass
                else:
                    progress.adapter_helper_reprobe_done = True
                    progress.adapter_helper_last_probe_digest = probe_digest
                    probe_report, probe_passed = run_validation_coverage_probe(
                        workdir,
                        issue,
                        diff,
                        blockers,
                    )
                    if probe_passed:
                        progress.coverage_probe_satisfied = True
                        latest_diff = git_diff(workdir)
                        latest_status_for_blockers = append_adapter_probe_evidence(
                            status_with_recovered_public_evidence(
                                {},
                                f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                issue,
                                text,
                            ),
                            workdir=workdir,
                            diff=latest_diff,
                            marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                        )
                        latest_blockers = implementation_scope_blockers(
                            issue,
                            latest_diff,
                            latest_status_for_blockers,
                            task_metadata,
                        )
                        latest_blockers = blockers_after_passing_public_probe(latest_blockers)
                        if not latest_blockers and latest_diff.strip():
                            recovered_status = append_adapter_probe_evidence(
                                {
                                    "status": "completed",
                                    "summary": "adapter recovery worker fixed public contract; preserving current source diff",
                                    "validation": recovered_validation_with_helper_evidence(
                                        issue,
                                        text,
                                        recovered_validation_text(
                                            task_metadata,
                                            text,
                                            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                        ),
                                    ),
                                    "risk": "completion marker recovered by benchmark wrapper after adapter helper fix",
                                },
                                workdir=workdir,
                                diff=latest_diff,
                                marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                            )
                            STATUS_PATH.write_text(
                                json.dumps(recovered_status),
                                encoding="utf-8",
                            )
                            log("completion marker recovered after adapter helper re-probe passed")
                            progress.outcome = "recovered"
                            return "break"
                        blockers = latest_blockers or blockers_after_passing_public_probe(blockers)
                        log(
                            "adapter helper re-probe passed but remaining implementation blockers persist: "
                            + "; ".join(blockers)
                        )
                    else:
                        log(f"adapter helper re-probe still failed; see {HELPER_PROBE_PATH}")
            if (
                progress.adapter_helper_last_spawn_at is not None
                and time.monotonic() - progress.adapter_helper_last_spawn_at < policy.adapter_helper_grace_seconds
            ):
                elapsed = int(time.monotonic() - progress.adapter_helper_last_spawn_at)
                log(
                    "waiting for recently spawned adapter recovery worker before terminal blocker "
                    f"elapsed={elapsed}s grace={policy.adapter_helper_grace_seconds}s"
                )
                progress.last_capture = 0.0
                time.sleep(10)
                return "continue"
            force_verifier_handoff = (
                policy.terminal_force_resume_enabled
                and (verifier_exact_followup_available(text) or bool(infra_blockers))
                and int(progress.deadline - time.monotonic()) > 240
            )
            if blockers and relaunch_orchestrator_for_blockers(
                (
                    "verifier infrastructure failed before semantic recheck"
                    if infra_blockers
                    else "orchestrator exited after unresolved verifier follow-up"
                    if force_verifier_handoff
                    else "orchestrator exited after unresolved coverage follow-up"
                ),
                diff,
                [
                    *blockers,
                    *(
                        [
                            (
                                "Verifier infrastructure handoff: the verifier did not complete a semantic recheck because its tool/path execution failed. "
                                "Preserve the current /app diff, spawn a fresh read-only verifier, require structured findings/todos for any semantic blockers, "
                                "and do not write completed status until gate-check plus final build/provider evidence pass."
                                if infra_blockers
                                else "Verifier exact-follow-up handoff: a verifier produced concrete public/source repair instructions, but the active run did not apply them before exiting. Continue from the current /app diff, apply or disprove those verifier findings from source, rerun the implicated visible validation, then write status.json."
                            )
                        ]
                        if force_verifier_handoff
                        else []
                    ),
                ],
                probe_report,
                force_live_handoff=force_verifier_handoff,
            ):
                time.sleep(5)
                return "continue"
            if completed_status_covers_adapter_validation(workdir, issue, git_diff(workdir)):
                log("coverage follow-up blocker path yielded to completed status with accepted final build and adapter validation gate")
                progress.outcome = "completed"
                return "break"
            no_diff_worker_blockers = no_diff_blocked_subagent_blockers(RUNTIME_ROOT)
            if (
                not diff.strip()
                and no_diff_worker_blockers
                and progress.no_diff_blocked_retries < policy.no_diff_blocked_retry_limit
                and int(progress.deadline - time.monotonic()) > 240
                and relaunch_orchestrator_for_blockers(
                    "orchestrator exited after no-diff blocked worker",
                    diff,
                    [*blockers, *no_diff_worker_blockers],
                    probe_report,
                    force_live_handoff=True,
                )
            ):
                progress.no_diff_blocked_retries += 1
                time.sleep(5)
                return "continue"
            active_no_diff_workers = active_repair_subagent_summaries(RUNTIME_ROOT)
            if (
                not diff.strip()
                and active_no_diff_workers
                and progress.no_diff_blocked_retries < policy.no_diff_blocked_retry_limit
                and int(progress.deadline - time.monotonic()) > 240
                and relaunch_orchestrator_for_blockers(
                    "orchestrator exited while implementation worker remained active with no source diff",
                    diff,
                    [
                        *blockers,
                        "coverage follow-up ended with a live implementation worker and no materialized source diff; reattach to the worker state or spawn a replacement implementation worker over the same source ownership hints",
                        "the next worker must either produce a narrow source diff or write a structured blocking todo/finding with the exact source/API blocker; do not exit with only scout notes",
                        *[
                            f"active no-diff worker:{summary}"
                            for summary in active_no_diff_workers[:3]
                        ],
                    ],
                    probe_report,
                    force_live_handoff=True,
                )
            ):
                progress.no_diff_blocked_retries += 1
                log(
                    "active no-diff worker handoff launched after coverage-followup orchestrator exit: "
                    + "; ".join(active_no_diff_workers[:3])
                )
                time.sleep(5)
                return "continue"
            active_followup_workers = active_repair_subagent_summaries(RUNTIME_ROOT)
            if (
                diff.strip()
                and active_followup_workers
                and progress.active_followup_extensions < policy.active_followup_extension_limit
                and int(progress.deadline - time.monotonic()) > 240
            ):
                progress.active_followup_extensions += 1
                log(
                    "coverage-followup orchestrator exit delayed because active repair worker(s) are still running "
                    f"extension={progress.active_followup_extensions}/{policy.active_followup_extension_limit}: "
                    + "; ".join(active_followup_workers[:3])
                )
                progress.coverage_followup_at = time.monotonic()
                progress.last_capture = 0.0
                time.sleep(30)
                return "continue"
            ownership_paths = list(
                dict.fromkeys(
                    [
                        *required_path_outside_owned_reports(RUNTIME_ROOT),
                        *inferred_required_paths_from_worker_text(RUNTIME_ROOT),
                    ]
                )
            )
            if (
                not diff.strip()
                and ownership_paths
                and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                and adapter_helper_repair_allowed("ownership-boundary no-diff worker")
            ):
                progress.adapter_helper_workers_spawned += 1
                helper_blockers = [
                    *blockers,
                    *[
                        f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                        for path in ownership_paths[:8]
                    ],
                    "The previous worker stopped at a source ownership boundary without producing a diff; implement from public issue/source evidence over the expanded owned paths or report a concrete source-visible blocker.",
                ]
                try:
                    helper_worker = spawn_adapter_helper_worker(
                        repo_root,
                        workdir,
                        env,
                        issue,
                        diff,
                        helper_blockers,
                        list(dict.fromkeys([*ownership_paths, *assignment_owned_paths(RUNTIME_ROOT)])),
                        progress.adapter_helper_workers_spawned,
                        probe_report,
                        launch_reason="ownership-boundary no-diff recovery",
                    )
                    log(f"adapter helper worker spawned after ownership-boundary no-diff worker: {helper_worker}")
                    progress.adapter_helper_last_spawn_at = time.monotonic()
                    progress.adapter_helper_reprobe_done = False
                    progress.adapter_helper_last_probe_digest = None
                    time.sleep(5)
                    return "continue"
                except Exception as exc:
                    log(f"adapter helper worker spawn failed after ownership-boundary no-diff worker: {exc}")
            if (
                not diff.strip()
                and ownership_paths
                and progress.orchestrator_resume_attempts < policy.orchestrator_resume_limit
                and int(progress.deadline - time.monotonic()) > 240
                and relaunch_orchestrator_for_blockers(
                    "orchestrator exited after ownership-boundary no-diff worker",
                    diff,
                    [
                        *blockers,
                        *[
                            f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                            for path in ownership_paths[:8]
                        ],
                        "The previous worker correctly stopped at an ownership boundary without producing a diff. Spawn a fresh bounded worker whose owned paths include the requested outside-owned path plus the original endpoint owner paths.",
                    ],
                    probe_report,
                    force_live_handoff=True,
                )
            ):
                time.sleep(5)
                return "continue"
            progress.coverage_gate_unresolved = True
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "orchestrator exited after coverage follow-up without writing valid completion status",
                        "blockers": blockers,
                    }
                ),
                encoding="utf-8",
            )
            log("blocked marker: orchestrator exited after unresolved coverage follow-up")
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
        if diff.strip() and (progress.coverage_probe_satisfied or not coverage_probe_commands(workdir, issue, diff)):
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "summary": "orchestrator exited after adapter helper validation; preserving current source diff",
                        "validation": recovered_validation_with_helper_evidence(
                            issue,
                            text,
                            recovered_validation_text(
                                task_metadata,
                                text,
                                f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                            ),
                        ),
                        "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                    }
                ),
                encoding="utf-8",
            )
            log("completion marker recovered after adapter helper probe and orchestrator exit")
            progress.outcome = "recovered"
            return "break"
        if diff.strip():
            progress.coverage_gate_unresolved = True
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "adapter public validation was not proven after coverage follow-up",
                        "blockers": [
                            f"adapter-selected public validation did not pass; inspect {HELPER_PROBE_PATH}"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            log("blocked marker: adapter public validation was not proven after coverage follow-up")
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"

    return "wait"


def handle_session_health_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    diff_snapshot: str,
    diff_bytes: int,
    text: str,
    remaining_seconds: int,
    resolved_todos: list[str],
    active_repair_workers: list[str],
    active_verifiers: list[str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Enforce session health and followup timeout boundaries."""

    if not tmux_has_session(session) and diff_bytes == 0 and not state:
        progress.missing_session_captures += 1
        if progress.missing_session_captures >= 3:
            STATUS_PATH.write_text(
                json.dumps({"status": "blocked", "reason": "tmux session disappeared before producing status or diff"}),
                encoding="utf-8",
            )
            log("blocked marker: tmux session disappeared before producing status or diff")
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
    else:
        progress.missing_session_captures = 0
    if progress.coverage_followup_at and time.monotonic() - progress.coverage_followup_at > policy.coverage_followup_timeout:
        diff = git_diff(workdir)
        blockers = validation_coverage_blockers(issue, diff, text, current_status, task_metadata)
        if blockers:
            active_repair_workers = active_repair_subagent_summaries(RUNTIME_ROOT)
            remaining_after_followup = int(progress.deadline - time.monotonic())
            if (
                active_repair_workers
                and unresolved_repair_state_exists(RUNTIME_ROOT)
                and remaining_after_followup > 180
            ):
                log(
                    "coverage follow-up timeout extended because active repair worker(s) are still running: "
                    + "; ".join(active_repair_workers[:3])
                )
                progress.coverage_followup_at = time.monotonic()
                progress.last_capture = 0.0
                time.sleep(10)
                return "continue"
            progress.coverage_gate_unresolved = True
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "validation coverage gate remained unresolved after helper probe follow-up",
                        "blockers": blockers,
                    }
                ),
                encoding="utf-8",
            )
            log(f"blocked marker: coverage gate unresolved after {policy.coverage_followup_timeout}s")
            progress.exit_code = 2
            progress.outcome = "blocked"
            return "break"
        progress.coverage_followup_at = None

    return "wait"


def handle_progress_checkpoint(
    *,
    current_status: dict[str, object],
    state: str,
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    policy: LifecyclePolicy,
    progress: LifecycleProgress,
) -> str:
    """Run the periodic recovery/checkpoint transition for an active solver."""

    capture_session(session)
    diff_snapshot = git_diff(workdir)
    diff_bytes = len(diff_snapshot.encode("utf-8"))
    diff_digest = hashlib.sha256(diff_snapshot.encode("utf-8", errors="replace")).hexdigest() if diff_bytes else ""
    if diff_digest != progress.last_diff_digest:
        progress.last_diff_digest = diff_digest
        progress.last_diff_changed_at = time.monotonic()
    text = captured_text()
    log(f"waiting status={state or 'none'} diff_bytes={diff_bytes}")
    remaining_seconds = int(progress.deadline - time.monotonic())
    resolved_todos = resolved_repair_todo_ids(RUNTIME_ROOT, min_age_seconds=30)
    active_repair_workers = active_repair_subagent_summaries(RUNTIME_ROOT)
    active_verifiers = active_verifier_subagent_summaries(RUNTIME_ROOT)
    checkpoint_handlers = (
        handle_repair_readiness_checkpoint,
        handle_terminal_deadline_checkpoint,
        handle_early_scope_checkpoint,
        handle_unmarked_terminal_evidence,
        handle_convergence_checkpoint,
        handle_orchestrator_exit_checkpoint,
        handle_coverage_followup_checkpoint,
        handle_session_health_checkpoint,
    )
    for checkpoint_handler in checkpoint_handlers:
        transition = checkpoint_handler(
            current_status=current_status,
            state=state,
            workdir=workdir,
            issue=issue,
            task_metadata=task_metadata,
            session=session,
            repo_root=repo_root,
            env=env,
            diff_snapshot=diff_snapshot,
            diff_bytes=diff_bytes,
            text=text,
            remaining_seconds=remaining_seconds,
            resolved_todos=resolved_todos,
            active_repair_workers=active_repair_workers,
            active_verifiers=active_verifiers,
            adapter_helper_repair_allowed=adapter_helper_repair_allowed,
            relaunch_orchestrator_for_blockers=relaunch_orchestrator_for_blockers,
            policy=policy,
            progress=progress,
        )
        if transition != "wait":
            return transition

    progress.last_capture = time.monotonic()
    return "wait"
