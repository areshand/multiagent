from __future__ import annotations

import json
import time
from pathlib import Path

from evaluation.support.coding.outcomes import (
    SUBMISSION_GATE_REJECTION,
    SUBMISSION_GATE_REJECTION_EXIT_CODE,
    publish_terminal_outcome,
)

from .swe_prod_contracts import (
    HELPER_PROBE_PATH,
    RUNTIME_ROOT,
    STATUS_PATH,
    TERMINAL_OUTCOME_PATH,
    log,
)
from .swe_prod_evidence import (
    active_verifier_subagent_summaries,
    append_adapter_probe_evidence,
    assignment_owned_paths,
    blocked_status_has_no_source_diff,
    blocked_status_waits_for_verifier,
    build_verification_has_evidence,
    capture_session,
    captured_text,
    completed_status_covers_adapter_validation,
    create_no_diff_stall_repair_state,
    emit_failure_diagnostics,
    final_diff_sha256,
    has_live_agent_process,
    inferred_required_paths_from_worker_text,
    orchestrator_exited_without_status,
    persisted_stale_visible_reconciliation_evidence,
    persisted_subagent_final_acceptance_evidence,
    persisted_subagent_visible_validation_evidence,
    publish_status,
    recover_verifier_accepted_todo_closures,
    required_path_outside_owned_reports,
    status,
    status_covers_validation_commands,
    status_with_recovered_public_evidence,
    structured_repair_gate_blockers,
    tmux_has_session,
    visible_validation_passed_in_text,
)
from .swe_prod_guardrails import (
    coverage_probe_commands,
    helper_scope_hints,
    implementation_scope_blockers,
)
from .swe_prod_orchestration import (
    persisted_verifier_blocking_evidence,
    send_orchestrator_followup,
    spawn_adapter_helper_worker,
    verifier_blocking_handoff_key,
)
from .swe_prod_repository import cleanup_patch, clear_blocked_changes, git_diff
from .swe_prod_types import LifecyclePolicy, LifecycleProgress
from .swe_prod_validation import (
    blocked_status_needs_diff_reconciliation,
    blocked_status_recoverable_by_public_probe,
    blockers_after_passing_public_probe,
    completed_status_snapshot_blockers,
    has_hard_scope_blocker,
    non_recoverable_final_validation_blockers,
    run_validation_coverage_probe,
    validation_coverage_blockers,
)

def handle_completed_status(
    *,
    current_status: dict[str, object],
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    repo_root: Path,
    env: dict[str, str],
    policy: LifecyclePolicy,
    adapter_helper_repair_allowed,
    relaunch_orchestrator_for_blockers,
    progress: LifecycleProgress,
) -> str:
    """Validate a completed marker and return the polling-loop transition."""

    capture_session(session)
    diff = git_diff(workdir)
    text = captured_text()
    if recover_verifier_accepted_todo_closures(text, diff):
        current_status = status()
    verifier_acceptance = persisted_subagent_final_acceptance_evidence(diff)
    hash_bound_final_verifier_accepted = bool(verifier_acceptance)
    if verifier_acceptance:
        current_status = status_with_recovered_public_evidence(
            current_status,
            verifier_acceptance,
            issue,
            text,
        )
        current_status = append_adapter_probe_evidence(
            current_status,
            workdir=workdir,
            diff=diff,
            compile_evidence="hash-bound-final-verifier-build",
        )
        log("completed status enriched from hash-bound durable verifier acceptance before final gate")
    if completed_status_covers_adapter_validation(workdir, issue, diff, current_status):
        progress.accepted_completed_status_snapshot = dict(current_status)
        progress.accepted_completed_status_diff_hash = final_diff_sha256(diff)
    scope_blockers = implementation_scope_blockers(issue, diff, current_status, task_metadata)
    coverage_blockers = validation_coverage_blockers(issue, diff, text, current_status, task_metadata)
    structured_gate_blockers = structured_repair_gate_blockers()
    blockers = [*scope_blockers, *coverage_blockers, *structured_gate_blockers]
    probe_report = ""
    if progress.coverage_probe_satisfied:
        blockers = blockers_after_passing_public_probe(blockers)
        scope_blockers = blockers
        coverage_blockers = []
    if (
        not blockers
        and not hash_bound_final_verifier_accepted
        and not progress.coverage_probe_satisfied
        and not completed_status_covers_adapter_validation(workdir, issue, diff, current_status)
        and coverage_probe_commands(workdir, issue, diff)
    ):
        probe_report, probe_passed = run_validation_coverage_probe(
            workdir,
            issue,
            diff,
            ["adapter-selected public validation probe required for this issue/diff"],
        )
        if probe_passed:
            progress.coverage_probe_satisfied = True
            current_status = append_adapter_probe_evidence(
                current_status,
                workdir=workdir,
                diff=diff,
                marker=f"helper-validation-passed: adapter public validation probe ({HELPER_PROBE_PATH})",
                probe_report=probe_report,
            )
            STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
            log("completion marker verified by adapter public validation probe")
        else:
            coverage_blockers = [
                f"adapter-selected public validation probe failed; inspect {HELPER_PROBE_PATH} and fix the final diff"
            ]
            blockers = [*scope_blockers, *coverage_blockers]
    if blockers and progress.coverage_followups_sent < policy.coverage_followup_limit and tmux_has_session(session):
        probe_report = ""
        if not hash_bound_final_verifier_accepted and (
            coverage_blockers or coverage_probe_commands(workdir, issue, diff)
        ):
            probe_report, probe_passed = run_validation_coverage_probe(workdir, issue, diff, coverage_blockers)
        else:
            probe_passed = False
        if probe_passed:
            progress.coverage_probe_satisfied = True
            current_status = append_adapter_probe_evidence(
                current_status,
                workdir=workdir,
                diff=diff,
                marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                probe_report=probe_report,
            )
            STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
            log("coverage gate satisfied by adapter public helper probe")
            blockers = blockers_after_passing_public_probe([*scope_blockers, *coverage_blockers])
            scope_blockers = blockers
            coverage_blockers = []
        if not blockers:
            log("completion marker accepted after adapter public helper probe")
        else:
            progress.coverage_followups_sent += 1
            try:
                STATUS_PATH.unlink(missing_ok=True)
            except OSError as exc:
                log(f"could not remove weak completion marker before follow-up: {exc}")
            if (
                not has_live_agent_process()
                and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                and adapter_helper_repair_allowed("weak completion")
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
                            "The orchestrator/verifier accepted a weak completion marker but no live agent remains to handle the follow-up; continue from the current /app diff and resolve these adapter blockers.",
                        ],
                        helper_scope_hints(workdir, issue, diff, blockers),
                        progress.adapter_helper_workers_spawned,
                        probe_report,
                    )
                    log(f"adapter recovery worker spawned immediately after weak completion: {helper_worker}")
                    progress.adapter_helper_last_spawn_at = time.monotonic()
                    progress.adapter_helper_reprobe_done = False
                    progress.adapter_helper_last_probe_digest = None
                    progress.coverage_followup_at = time.monotonic()
                    progress.last_capture = 0.0
                    time.sleep(5)
                    return "continue"
                except Exception as exc:
                    log(f"adapter recovery worker spawn failed after weak completion: {exc}")
            if orchestrator_exited_without_status(text) and not has_live_agent_process():
                if relaunch_orchestrator_for_blockers(
                    "rejected completion has no live orchestrator for repair follow-up",
                    diff,
                    blockers,
                    probe_report,
                    force_live_handoff=True,
                ):
                    log("rejected completion handed directly to a fresh orchestrator")
                    time.sleep(5)
                    return "continue"
            send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
            log(f"coverage gate follow-up {progress.coverage_followups_sent}: {'; '.join(blockers)}")
            progress.coverage_followup_at = time.monotonic()
            if (
                orchestrator_exited_without_status(text)
                and not has_live_agent_process()
                and progress.adapter_helper_workers_spawned < policy.adapter_helper_worker_limit
                and adapter_helper_repair_allowed("rejected completion")
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
                    log(f"adapter recovery worker spawned immediately after rejected completion: {helper_worker}")
                    progress.adapter_helper_last_spawn_at = time.monotonic()
                    progress.adapter_helper_reprobe_done = False
                    progress.adapter_helper_last_probe_digest = None
                except Exception as exc:
                    log(f"adapter recovery worker spawn failed after rejected completion: {exc}")
            progress.last_capture = 0.0
            time.sleep(5)
            return "continue"
    if blockers and relaunch_orchestrator_for_blockers(
        "completion marker rejected by public/source validation",
        diff,
        blockers,
        probe_report,
    ):
        time.sleep(5)
        return "continue"
    if blockers and has_hard_scope_blocker(blockers):
        log(f"hard public scope blockers remain after follow-ups; refusing to submit known-bad patch: {'; '.join(blockers)}")
        current_status = {
            "status": "blocked",
            "reason": "hard public scope blocker remains after adapter/verifier follow-ups",
            "blockers": blockers,
        }
        STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
        progress.exit_code = 2
        progress.outcome = "blocked"
        progress.terminal_outcome = SUBMISSION_GATE_REJECTION
        return "break"
    if blockers:
        progress.coverage_gate_unresolved = True
        log(f"completion marker refused because coverage blockers remain after follow-ups: {'; '.join(blockers)}")
        current_status = {
            "status": "blocked",
            "reason": "coverage blockers remain after adapter/verifier follow-ups",
            "blockers": blockers,
        }
        STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
        progress.exit_code = 2
        progress.outcome = "blocked"
        progress.terminal_outcome = SUBMISSION_GATE_REJECTION
        return "break"
    # Persist the exact enriched object that passed the gate. The
    # orchestrator's older status may not contain durable verifier
    # evidence recovered above, and post-cleanup must not evaluate
    # a different state object for the same final diff.
    publish_status(current_status)
    log("accepted completed status atomically published for post-cleanup recheck")
    log(f"completion marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
    progress.outcome = "completed"
    return "break"



def handle_blocked_status(
    *,
    current_status: dict[str, object],
    workdir: Path,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    policy: LifecyclePolicy,
    relaunch_orchestrator_for_blockers,
    progress: LifecycleProgress,
) -> str:
    """Reconcile a blocked marker and return the polling-loop transition."""

    diff = git_diff(workdir)
    reason_text = json.dumps(current_status, sort_keys=True).lower()
    active_verifiers = active_verifier_subagent_summaries(RUNTIME_ROOT)
    verifier_lifecycle_blocked = diff.strip() and blocked_status_waits_for_verifier(
        current_status,
        active_verifiers,
    )
    if verifier_lifecycle_blocked:
        if active_verifiers and progress.active_verifier_blocked_at is None:
            progress.active_verifier_blocked_at = time.monotonic()
        verifier_grace_elapsed = (
            time.monotonic() - progress.active_verifier_blocked_at
            if progress.active_verifier_blocked_at is not None
            else policy.active_verifier_grace
        )
        if (
            active_verifiers
            and verifier_grace_elapsed < policy.active_verifier_grace
            and int(progress.deadline - time.monotonic()) > 300
        ):
            log(
                "blocked verifier acceptance delayed because active verifier is still running: "
                + "; ".join(active_verifiers[:3])
            )
            time.sleep(10)
            return "continue"
        status_blockers = current_status.get("blockers")
        blockers = (
            [str(blocker) for blocker in status_blockers]
            if isinstance(status_blockers, list)
            else [str(current_status.get("reason") or "verifier acceptance was not persisted")]
        )
        blockers = list(
            dict.fromkeys(
                [
                    *blockers,
                    "verifier infrastructure failed to persist a terminal verdict; inspect durable verifier evidence for the live final diff, replace a stalled verifier if needed, and write one authoritative completed/blocked status",
                ]
            )
        )
        if (
            int(progress.deadline - time.monotonic()) > 300
            and relaunch_orchestrator_for_blockers(
                "blocked status was written before verifier lifecycle completed",
                diff,
                blockers,
                "",
                force_live_handoff=True,
            )
        ):
            progress.active_verifier_blocked_at = None
            log("verifier-lifecycle blocked status resumed for durable terminal verdict")
            time.sleep(5)
            return "continue"
    semantic_handoff_key = verifier_blocking_handoff_key(
        current_status,
        diff,
        progress.verifier_blocking_handoffs,
        RUNTIME_ROOT,
    )
    if semantic_handoff_key and int(progress.deadline - time.monotonic()) > 300:
        verifier_evidence = persisted_verifier_blocking_evidence(RUNTIME_ROOT)
        if relaunch_orchestrator_for_blockers(
            "verifier-confirmed semantic finding requires structured repair",
            diff,
            [
                (
                    "A completed verifier confirmed a semantic source defect on the live diff, but the "
                    "orchestrator reached terminal blocked status before queuing and repairing it."
                ),
                verifier_evidence,
            ],
            "",
            force_live_handoff=True,
        ):
            progress.verifier_blocking_handoffs.add(semantic_handoff_key)
            log("verifier-confirmed semantic finding handed back for structured repair")
            time.sleep(5)
            return "continue"
    no_diff_blocked = blocked_status_has_no_source_diff(current_status, diff)
    if (
        no_diff_blocked
        and progress.no_diff_blocked_retries < policy.no_diff_blocked_retry_limit
        and int(progress.deadline - time.monotonic()) > 300
    ):
        progress.no_diff_blocked_retries += 1
        ownership_paths = list(
            dict.fromkeys(
                [
                    *required_path_outside_owned_reports(RUNTIME_ROOT),
                    *inferred_required_paths_from_worker_text(RUNTIME_ROOT),
                ]
            )
        )
        blockers = [
            "production orchestrator wrote blocked status after a worker completed without a materialized source diff; restart from issue/source evidence and choose the narrowest implementation path before blocking again",
            *[
                f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                for path in ownership_paths[:8]
            ],
        ]
        if relaunch_orchestrator_for_blockers(
            "blocked with no materialized source diff",
            diff,
            blockers,
            "",
            force_live_handoff=True,
        ):
            log(f"no-diff blocked retry launched attempt={progress.no_diff_blocked_retries}")
            time.sleep(5)
            return "continue"
    if no_diff_blocked:
        ownership_paths = list(
            dict.fromkeys(
                [
                    *required_path_outside_owned_reports(RUNTIME_ROOT),
                    *inferred_required_paths_from_worker_text(RUNTIME_ROOT),
                    *assignment_owned_paths(RUNTIME_ROOT),
                ]
            )
        )
        no_diff_blockers = [
            "no-diff retry budget exhausted before a materialized /app source patch",
            *[
                f"source ownership hint:{path}"
                for path in ownership_paths[:8]
            ],
        ]
        status_blockers = current_status.get("blockers")
        if isinstance(status_blockers, list):
            no_diff_blockers.extend(str(blocker) for blocker in status_blockers)
        elif current_status.get("reason"):
            no_diff_blockers.append(str(current_status.get("reason")))
        created_state = create_no_diff_stall_repair_state(
            status_payload=current_status,
            blockers=list(dict.fromkeys(no_diff_blockers)),
        )
        if created_state:
            log("no-diff stall structured repair state recorded: " + ", ".join(created_state))
        # Exhausting bounded implementation attempts with no source patch is a
        # production submission-gate rejection, not runner infrastructure
        # failure. Publish the typed outcome during finalization so EvalScope
        # can score an explicit no-submission row instead of aborting the shard.
        progress.terminal_outcome = SUBMISSION_GATE_REJECTION
    if (
        diff.strip()
        and blocked_status_needs_diff_reconciliation(current_status)
        and progress.orchestrator_resume_attempts < policy.orchestrator_resume_limit
        and int(progress.deadline - time.monotonic()) > 300
    ):
        capture_session(session)
        text = captured_text()
        status_blockers = current_status.get("blockers")
        if isinstance(status_blockers, list):
            blockers = [str(blocker) for blocker in status_blockers]
        else:
            blockers = [str(current_status.get("reason") or "blocked status requires live diff reconciliation")]
        blockers = list(
            dict.fromkeys(
                [
                    *blockers,
                    *implementation_scope_blockers(issue, diff, current_status, task_metadata),
                    *validation_coverage_blockers(issue, diff, text, current_status, task_metadata),
                    (
                        "Blocked-status reconciliation: re-read the live files and `git diff --name-only`; "
                        "make claimed files/hunks match the actual final diff or remove stale claims before final status."
                    ),
                ]
            )
        )
        if relaunch_orchestrator_for_blockers(
            "blocked status has stale claims or stale patch evidence against a live source diff",
            diff,
            blockers,
            "",
            force_live_handoff=True,
        ):
            log("blocked-status diff reconciliation resume launched")
            time.sleep(5)
            return "continue"
    log(f"blocked marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
    progress.exit_code = 2
    progress.outcome = "blocked"
    return "break"



def finalize_solver_run(
    *,
    workdir: Path,
    start_head: str,
    issue: str,
    task_metadata: dict[str, object],
    session: str,
    progress: LifecycleProgress,
) -> int:
    """Recheck and publish the exact final diff after lifecycle cleanup."""

    restored = cleanup_patch(workdir, start_head)
    if restored:
        log(f"restored benchmark-disallowed changes: {restored}")
    final_diff = git_diff(workdir)
    if (
        progress.exit_code != 0
        and final_diff.strip()
        and progress.accepted_completed_status_snapshot is not None
        and progress.accepted_completed_status_diff_hash == final_diff_sha256(final_diff)
    ):
        final_text = captured_text()
        snapshot_blockers = [
            *completed_status_snapshot_blockers(
                issue,
                final_diff,
                final_text,
                progress.accepted_completed_status_snapshot,
                task_metadata,
            ),
            *structured_repair_gate_blockers(),
        ]
        if not status_covers_validation_commands(
            progress.accepted_completed_status_snapshot,
            coverage_probe_commands(workdir, issue, final_diff),
        ):
            snapshot_blockers.append(
                "completed status snapshot lacks adapter-selected validation command coverage for the final diff"
            )
        if not snapshot_blockers:
            STATUS_PATH.write_text(json.dumps(progress.accepted_completed_status_snapshot), encoding="utf-8")
            log(
                "nonzero wrapper exit overridden because an earlier completed status snapshot "
                "still proves the final diff and adapter validation after stale coverage follow-up state"
            )
            progress.coverage_gate_unresolved = False
            progress.exit_code = 0
            progress.outcome = "completed"
        else:
            log(
                "completed status snapshot could not override nonzero wrapper exit; blockers remain: "
                + "; ".join(snapshot_blockers)
            )
    if progress.exit_code != 0 and final_diff.strip() and completed_status_covers_adapter_validation(workdir, issue, final_diff):
        log(
            "nonzero wrapper exit overridden because status.json already records completed final-diff build verification and adapter validation accepted by the structured repair gate"
        )
        progress.coverage_gate_unresolved = False
        progress.exit_code = 0
        progress.outcome = "completed"
    if progress.exit_code == 0 and final_diff.strip():
        final_status = status()
        final_text = captured_text()
        post_cleanup_blockers = [
            *implementation_scope_blockers(issue, final_diff, final_status, task_metadata),
            *validation_coverage_blockers(issue, final_diff, final_text, final_status, task_metadata),
        ]
        status_text = json.dumps(final_status, sort_keys=True)
        if restored and not build_verification_has_evidence(status_text, final_diff):
            post_cleanup_blockers.insert(
                0,
                "benchmark cleanup changed the final submitted diff after verifier acceptance; "
                "rerun affected compile/test validation against the cleaned final diff before submission: "
                + ", ".join(restored[:8]),
            )
        if post_cleanup_blockers:
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "post-cleanup final gate rejected stale validation evidence",
                        "blockers": list(dict.fromkeys(post_cleanup_blockers)),
                        "final_diff_sha256": final_diff_sha256(final_diff),
                    }
                ),
                encoding="utf-8",
            )
            log(
                "post-cleanup final gate refused stale completion evidence; blockers remain: "
                + "; ".join(list(dict.fromkeys(post_cleanup_blockers)))
            )
            progress.exit_code = 2
            progress.outcome = "blocked"
            progress.terminal_outcome = SUBMISSION_GATE_REJECTION
    if progress.exit_code != 0 and final_diff.strip():
        final_status = status()
        final_state = str(final_status.get("status", "")).lower()
        final_text = captured_text()
        if recover_verifier_accepted_todo_closures(final_text, final_diff):
            final_status = status()
            final_state = str(final_status.get("status", "")).lower()
        original_final_validation_blockers = validation_coverage_blockers(
            issue,
            final_diff,
            final_text,
            final_status,
            task_metadata,
        )
        non_recoverable_validation_blockers = non_recoverable_final_validation_blockers(
            original_final_validation_blockers
        )
        validation_evidence = persisted_subagent_final_acceptance_evidence(final_diff)
        validation_evidence_kind = "final-verifier"
        if not validation_evidence:
            validation_evidence = persisted_subagent_visible_validation_evidence(final_diff)
            validation_evidence_kind = "visible"
        if not validation_evidence and visible_validation_passed_in_text(final_text):
            validation_evidence = "captured tmux output contains passing visible validation"
            validation_evidence_kind = "visible"
        if not validation_evidence:
            validation_evidence = persisted_stale_visible_reconciliation_evidence()
            if validation_evidence:
                validation_evidence_kind = "stale-visible"
        if (final_state != "blocked" or validation_evidence) and validation_evidence:
            final_status_for_blockers = status_with_recovered_public_evidence(
                final_status,
                validation_evidence,
                issue,
                final_text,
            )
            if validation_evidence_kind == "final-verifier":
                # The verifier owns semantic acceptance and final-diff build
                # proof. Normalize only source-map bookkeeping from the live
                # diff so harmless key-shape variation cannot discard that
                # machine-checkable acceptance.
                final_status_for_blockers = append_adapter_probe_evidence(
                    final_status_for_blockers,
                    workdir=workdir,
                    diff=final_diff,
                    compile_evidence="hash-bound-final-verifier-build",
                )
            final_probe_blockers: list[str] = []
            if validation_evidence_kind not in {"stale-visible", "final-verifier"} and coverage_probe_commands(
                workdir,
                issue,
                final_diff,
            ):
                probe_report, probe_passed = run_validation_coverage_probe(
                    workdir,
                    issue,
                    final_diff,
                    ["final cleanup recovery requires adapter public validation before accepting visible-validation text"],
                )
                if probe_passed:
                    final_status_for_blockers = append_adapter_probe_evidence(
                        final_status_for_blockers,
                        workdir=workdir,
                        diff=final_diff,
                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                    )
                else:
                    final_probe_blockers.append(
                        f"final cleanup recovery refused because adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}"
                    )
            final_blockers = [
                *implementation_scope_blockers(issue, final_diff, final_status_for_blockers, task_metadata),
                *validation_coverage_blockers(issue, final_diff, final_text, final_status_for_blockers, task_metadata),
                *non_recoverable_validation_blockers,
                *final_probe_blockers,
            ]
            final_blockers = blockers_after_passing_public_probe(final_blockers)
            if not final_blockers:
                recovered_status = append_adapter_probe_evidence(
                    {
                        "status": "completed",
                        "summary": "source diff and validation evidence recovered after missing completion marker",
                        "validation": "captured worker output contains recoverable validation evidence; status marker recovered by benchmark wrapper; "
                        + validation_evidence,
                        "risk": "completion marker was recovered by the benchmark wrapper after worker/orchestrator exit",
                    },
                    workdir=workdir,
                    diff=final_diff,
                    marker=(
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                        if validation_evidence_kind == "visible"
                        else None
                    ),
                    compile_evidence=(
                        "hash-bound-final-verifier-build"
                        if validation_evidence_kind == "final-verifier"
                        else "adapter-public-probe-passed"
                    ),
                )
                STATUS_PATH.write_text(
                    json.dumps(recovered_status),
                    encoding="utf-8",
                )
                log(f"completion marker recovered at final cleanup from source diff plus {validation_evidence_kind} validation evidence")
                progress.coverage_gate_unresolved = False
                progress.exit_code = 0
                progress.outcome = "recovered"
            else:
                log("final cleanup recovery refused; blockers remain: " + "; ".join(final_blockers))
        elif (
            final_state != "blocked" or blocked_status_recoverable_by_public_probe(final_status)
        ) and coverage_probe_commands(workdir, issue, final_diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                final_diff,
                ["final cleanup recovery found a source diff but no durable worker validation evidence"],
            )
            if probe_passed:
                final_status_for_blockers = append_adapter_probe_evidence(
                    status_with_recovered_public_evidence(
                        final_status,
                        f"adapter public helper probe passed at final cleanup ({HELPER_PROBE_PATH})",
                        issue,
                        final_text,
                    ),
                    workdir=workdir,
                    diff=final_diff,
                    marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                )
                final_blockers = [
                    *implementation_scope_blockers(issue, final_diff, final_status_for_blockers, task_metadata),
                    *validation_coverage_blockers(issue, final_diff, final_text, final_status_for_blockers, task_metadata),
                    *non_recoverable_validation_blockers,
                ]
                final_blockers = blockers_after_passing_public_probe(final_blockers)
                if not final_blockers:
                    recovered_status = append_adapter_probe_evidence(
                        {
                            "status": "completed",
                            "summary": "source diff accepted after adapter public validation probe at final cleanup",
                            "validation": "status marker recovered by benchmark wrapper",
                            "risk": "completion marker was recovered by the benchmark wrapper after missing durable worker validation evidence",
                        },
                        workdir=workdir,
                        diff=final_diff,
                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                    )
                    STATUS_PATH.write_text(
                        json.dumps(recovered_status),
                        encoding="utf-8",
                    )
                    log("completion marker recovered at final cleanup after adapter public probe passed without durable worker evidence")
                    progress.coverage_gate_unresolved = False
                    progress.exit_code = 0
                    progress.outcome = "recovered"
                else:
                    log("final cleanup adapter public probe passed, but blockers remain: " + "; ".join(final_blockers))
            else:
                log(f"final cleanup adapter public probe failed without durable worker validation evidence; inspect {HELPER_PROBE_PATH}")
    if progress.coverage_gate_unresolved:
        log("coverage gate remained unresolved; preserving current source diff for official verifier diagnostics")
    elif progress.outcome == "blocked" and not final_diff.strip():
        clear_blocked_changes(workdir, start_head, "blocked run produced no scoreable source diff")
        final_diff = git_diff(workdir)
    elif progress.outcome == "blocked":
        log("blocked run produced a scoreable source diff; preserving it for the official verifier")
    log(f"final /app diff bytes={len(final_diff.encode('utf-8'))}")
    if progress.exit_code != 0:
        emit_failure_diagnostics(session)
        if progress.terminal_outcome == SUBMISSION_GATE_REJECTION:
            final_status = status()
            raw_blockers = final_status.get("blockers")
            blockers = raw_blockers if isinstance(raw_blockers, list) else []
            publish_terminal_outcome(
                TERMINAL_OUTCOME_PATH,
                outcome=SUBMISSION_GATE_REJECTION,
                reason=str(final_status.get("reason") or "production submission gate rejected the final patch"),
                blockers=[str(blocker) for blocker in blockers],
            )
            log("terminal outcome: submission_gate_rejection")
            return SUBMISSION_GATE_REJECTION_EXIT_CODE
    return progress.exit_code
