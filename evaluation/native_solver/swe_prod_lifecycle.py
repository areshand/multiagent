from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path

from . import swe_prod_repository as _repository
from .swe_prod_bootstrap import (
    require_path,
    write_apply_patch_helper,
    write_codex_bridge,
    write_go_singleflight_wrapper,
    write_rg_fallback,
)
from .swe_prod_checkpoints import handle_progress_checkpoint
from .swe_prod_contracts import (
    CODEX_HOME,
    CODEX_WRAPPER,
    RUNTIME_ROOT,
    STATUS_PATH,
    env_truthy,
    log,
    read_prompt,
    read_task_metadata,
    run,
)
from .swe_prod_evidence import (
    assignment_owned_paths,
    capture_session,
    ensure_cache_dir,
    find_codex_cli,
    has_live_agent_process,
    inferred_required_paths_from_worker_text,
    status,
    tmux_has_session,
    toolchain_path_prefixes,
)
from .swe_prod_guardrails import helper_scope_hints
from .swe_prod_orchestration import write_orchestrator_resume_prompt
from .swe_prod_repository import (
    cleanup_initial_environment_diff,
    git_head,
    make_prompt,
    mark_untracked_source_intent_to_add,
    materialize_committed_changes,
)
from .swe_prod_transitions import (
    finalize_solver_run,
    handle_blocked_status,
    handle_completed_status,
)
from .swe_prod_types import LifecyclePolicy, LifecycleProgress
from .swe_prod_validation import (
    source_symbol_map_blocker_present,
    status_records_selected_validation,
    structured_repair_todo_blocker_present,
)

def run_prod_solver(prompt_path: str | None, workdir: Path, repo_root: Path, timeout: int) -> int:
    global ACTIVE_START_HEAD
    require_path(repo_root / "launch.sh", "production multiagent launcher")
    require_path(repo_root / "bin" / "subagent.sh", "production subagent helper")
    require_path(workdir / ".git", "SWE task git checkout")
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is required for the production multiagent solver")
    real_codex = find_codex_cli()
    if not real_codex:
        raise RuntimeError(
            "codex CLI is required inside the task image. Bake it into the image or enable a setup command "
            "that installs @openai/codex before running the production solver."
        )
    auth_mode = os.environ.get("EVAL_CODEX_AUTH_MODE", "bridge").strip().lower()
    if auth_mode not in {"bridge", "chatgpt"}:
        raise RuntimeError(f"unsupported EVAL_CODEX_AUTH_MODE={auth_mode!r}")
    if auth_mode == "bridge" and (not os.environ.get("OPENAI_BASE_URL") or not os.environ.get("OPENAI_API_KEY")):
        raise RuntimeError("OPENAI_BASE_URL and OPENAI_API_KEY must be set for the Codex bridge")
    if auth_mode == "chatgpt" and not (CODEX_HOME / "auth.json").exists() and not os.environ.get("CODEX_ACCESS_TOKEN"):
        raise RuntimeError(
            f"ChatGPT Codex auth mode requires {CODEX_HOME / 'auth.json'} or CODEX_ACCESS_TOKEN inside the task container"
        )

    start_head = git_head(workdir)
    ACTIVE_START_HEAD = start_head
    _repository.ACTIVE_START_HEAD = start_head
    cleanup_initial_environment_diff(workdir, start_head)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    write_codex_bridge(real_codex, os.environ.get("EVAL_NATIVE_SOLVER_MODEL", "gpt-5"), auth_mode)
    write_apply_patch_helper()
    write_rg_fallback()
    write_go_singleflight_wrapper()
    issue = read_prompt(prompt_path)
    task_metadata = read_task_metadata()
    task_metadata["_solver_workdir"] = str(workdir)
    log("solver metadata is public-only; official expected-test metadata is not exposed to the solver")
    autonomous_prompt = make_prompt(repo_root, workdir, issue, task_metadata)
    session = f"swe-prod-{os.getpid()}"
    toolchain_prefix = ":".join(toolchain_path_prefixes())
    path_parts = [str(RUNTIME_ROOT)]
    if toolchain_prefix:
        path_parts.append(toolchain_prefix)
    path_parts.append(os.environ.get("PATH", ""))
    env = os.environ.copy()
    env.update(
        {
            "MULTIAGENT_SESSION": session,
            "MULTIAGENT_ROOT": str(workdir),
            "MULTIAGENT_STATE_DIR": str(RUNTIME_ROOT / "state"),
            "MULTIAGENT_RESOLUTION_AUTOCREATE_TODO": "1",
            "MULTIAGENT_WRITE_POLICY": str(RUNTIME_ROOT / "write-policy.paths"),
            "MULTIAGENT_PROMPT": str(autonomous_prompt),
            "MULTIAGENT_PROMPT_MODULE_ROOT": str(repo_root),
            "MULTIAGENT_RESUME": "0",
            "MULTIAGENT_START_HEAD": start_head,
            "ORCHESTRATOR_CLI": "codex",
            "WORKER_CLI": "codex",
            "SUBAGENT_CLI": "codex",
            "VERIFIER_CLI": "codex",
            "CODEX_BIN": str(CODEX_WRAPPER),
            "CODEX_HOME": str(CODEX_HOME),
            "MULTIAGENT_CODEX_EXEC": os.environ.get("MULTIAGENT_CODEX_EXEC", "1"),
            "MULTIAGENT_EXTRA_PATH": str(RUNTIME_ROOT),
            "PATH": ":".join(part for part in path_parts if part),
            "GOCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-build-cache"),
            "GOMODCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache"),
            "MULTIAGENT_READY_ATTEMPTS": os.environ.get("MULTIAGENT_READY_ATTEMPTS", "80"),
            "MULTIAGENT_READY_DELAY": os.environ.get("MULTIAGENT_READY_DELAY", "1"),
        }
    )

    def launch_production_session(*, resume: bool, label: str) -> tuple[bool, str]:
        launch_tail = ""
        launch_args = [str(repo_root / "launch.sh"), "--session", session, "--root", str(workdir), "--no-attach"]
        if resume:
            launch_args.append("--resume")
        for attempt in range(1, 3):
            log(
                f"launching production multiagent session={session} root={workdir} "
                f"repo={repo_root} mode={'resume' if resume else 'clean'} label={label} attempt={attempt}"
            )
            launch = run(launch_args, env=env, timeout=120)
            launch_tail = ((launch.stderr or "") + "\n" + (launch.stdout or "")).strip()[-4000:]
            if launch.returncode != 0:
                raise RuntimeError(f"production multiagent launch failed: {launch_tail}")
            time.sleep(2)
            if tmux_has_session(session):
                return True, launch_tail
            log(f"launch attempt {attempt} exited without a live tmux session")
            run(["tmux", "kill-session", "-t", session], timeout=10)
        return False, launch_tail

    launched, launch_tail = launch_production_session(resume=False, label="initial")
    if not launched:
        STATUS_PATH.write_text(
            json.dumps({"status": "blocked", "reason": f"multiagent launch exited without live tmux session: {launch_tail[-1000:]}"}),
            encoding="utf-8",
        )
        log("blocked marker: launch exited without a live tmux session")
        return 2

    lifecycle_started_at = time.monotonic()
    progress = LifecycleProgress(
        deadline=lifecycle_started_at + timeout,
        convergence_start=lifecycle_started_at,
        last_diff_changed_at=lifecycle_started_at,
    )
    policy = LifecyclePolicy.from_environment(env_truthy)
    adapter_helper_mode = os.environ.get("EVAL_ADAPTER_HELPER_MODE", "advisory").strip().lower()
    adapter_helper_source_edit_opt_in = os.environ.get("EVAL_ADAPTER_HELPER_ALLOW_SOURCE_EDITS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adapter_helper_repair_enabled = adapter_helper_mode in {"repair", "source-edit", "source_edits"} or adapter_helper_source_edit_opt_in

    def adapter_helper_repair_allowed(context: str) -> bool:
        if adapter_helper_repair_enabled:
            return True
        if context not in progress.adapter_helper_advisory_logs:
            progress.adapter_helper_advisory_logs.add(context)
            log(
                "adapter helper advisory mode: not spawning source-editing helper for "
                f"{context}; set EVAL_ADAPTER_HELPER_MODE=repair only for explicit adapter-repair experiments"
            )
        return False

    if not adapter_helper_repair_enabled and adapter_helper_mode not in {"", "advisory", "observe", "read-only", "readonly"}:
        log(f"unknown EVAL_ADAPTER_HELPER_MODE={adapter_helper_mode!r}; using advisory mode")

    def relaunch_orchestrator_for_blockers(
        reason: str,
        diff: str,
        blockers: list[str],
        probe_report: str,
        *,
        force_live_handoff: bool = False,
    ) -> bool:

        use_source_symbol_extra_resume = False
        use_verifier_infra_extra_resume = False
        use_repair_todo_extra_resume = False
        if progress.orchestrator_resume_attempts >= policy.orchestrator_resume_limit:
            if (
                source_symbol_map_blocker_present(blockers)
                and progress.source_symbol_resume_attempts < policy.source_symbol_resume_limit
            ):
                use_source_symbol_extra_resume = True
            elif (
                force_live_handoff
                and any("verifier infrastructure failed" in blocker.lower() for blocker in blockers)
                and progress.verifier_infra_resume_attempts < policy.verifier_infra_resume_limit
            ):
                use_verifier_infra_extra_resume = True
            elif (
                structured_repair_todo_blocker_present(blockers)
                and progress.repair_todo_resume_attempts < policy.repair_todo_resume_limit
            ):
                use_repair_todo_extra_resume = True
            else:
                log(
                    "production orchestrator resume skipped for "
                    f"{reason}: limit {policy.orchestrator_resume_limit} already reached"
                )
                return False
        if has_live_agent_process() and not force_live_handoff:
            log(f"production orchestrator resume skipped for {reason}: live agent process still exists")
            return False
        if force_live_handoff:
            log(f"production orchestrator forcing terminal handoff for {reason}: replacing active tmux session")
        if use_source_symbol_extra_resume:
            log(
                "production orchestrator source-symbol resume using extra bounded attempt "
                f"{progress.source_symbol_resume_attempts + 1}/{policy.source_symbol_resume_limit} for {reason}"
            )
            progress.source_symbol_resume_attempts += 1
            resume_attempt = progress.orchestrator_resume_attempts + progress.source_symbol_resume_attempts
        elif use_verifier_infra_extra_resume:
            log(
                "production orchestrator verifier-infra resume using extra bounded attempt "
                f"{progress.verifier_infra_resume_attempts + 1}/{policy.verifier_infra_resume_limit} for {reason}"
            )
            progress.verifier_infra_resume_attempts += 1
            resume_attempt = (
                progress.orchestrator_resume_attempts
                + progress.source_symbol_resume_attempts
                + progress.verifier_infra_resume_attempts
                + progress.repair_todo_resume_attempts
            )
        elif use_repair_todo_extra_resume:
            log(
                "production orchestrator repair-todo resume using extra bounded attempt "
                f"{progress.repair_todo_resume_attempts + 1}/{policy.repair_todo_resume_limit} for {reason}"
            )
            progress.repair_todo_resume_attempts += 1
            resume_attempt = (
                progress.orchestrator_resume_attempts
                + progress.source_symbol_resume_attempts
                + progress.verifier_infra_resume_attempts
                + progress.repair_todo_resume_attempts
            )
        else:
            progress.orchestrator_resume_attempts += 1
            resume_attempt = progress.orchestrator_resume_attempts
        source_hints = helper_scope_hints(workdir, issue, diff, [] if not diff.strip() else blockers)
        if not diff.strip():
            source_hints = list(
                dict.fromkeys(
                    [
                        *inferred_required_paths_from_worker_text(RUNTIME_ROOT),
                        *assignment_owned_paths(RUNTIME_ROOT),
                        *source_hints,
                    ]
                )
            )
        resume_prompt = write_orchestrator_resume_prompt(
            autonomous_prompt,
            attempt=resume_attempt,
            reason=reason,
            issue=issue,
            diff=diff,
            blockers=blockers,
            probe_report=probe_report,
            source_hints=source_hints,
        )
        try:
            STATUS_PATH.unlink(missing_ok=True)
        except OSError as exc:
            log(f"could not remove terminal marker before production orchestrator resume: {exc}")
        if tmux_has_session(session):
            capture_session(session)
            run(["tmux", "kill-session", "-t", session], timeout=30)
        env["MULTIAGENT_PROMPT"] = str(resume_prompt)
        env["MULTIAGENT_RESUME"] = "1"
        launched_resume, launch_tail = launch_production_session(
            resume=True,
            label=f"resume-{resume_attempt}",
        )
        if not launched_resume:
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "production orchestrator resume failed to create a live tmux session",
                        "blockers": blockers,
                        "launch_tail": launch_tail[-1000:],
                    }
                ),
                encoding="utf-8",
            )
            log("blocked marker: production orchestrator resume failed to create a live tmux session")
            return False
        progress.coverage_followup_at = time.monotonic()
        progress.last_capture = 0.0
        progress.missing_session_captures = 0
        progress.convergence_start = time.monotonic()
        progress.last_diff_digest = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest() if diff else ""
        progress.last_diff_changed_at = progress.convergence_start
        log(
            "production orchestrator resume launched "
            f"attempt={resume_attempt} reason={reason} prompt={resume_prompt}"
        )
        return True
    try:
        while time.monotonic() < progress.deadline:
            try:
                materialize_committed_changes(workdir, start_head)
            except Exception as exc:
                log(f"could not materialize committed worker changes during polling: {exc}")
            try:
                mark_untracked_source_intent_to_add(workdir)
            except Exception as exc:
                log(f"could not mark untracked source files intent-to-add during polling: {exc}")
            current_status = status()
            if not progress.selected_validation_claim_seen and status_records_selected_validation(current_status):
                progress.selected_validation_claim_seen = True
                log(
                    "status.json claims selected validation, but adapter will rerun its generic visible-source probe before accepting"
                )
            state = str(current_status.get("status", "")).lower()
            if state in {"completed", "complete", "done"}:
                transition = handle_completed_status(
                    current_status=current_status,
                    workdir=workdir,
                    issue=issue,
                    task_metadata=task_metadata,
                    session=session,
                    repo_root=repo_root,
                    env=env,
                    policy=policy,
                    adapter_helper_repair_allowed=adapter_helper_repair_allowed,
                    relaunch_orchestrator_for_blockers=relaunch_orchestrator_for_blockers,
                    progress=progress,
                )
                if transition == "continue":
                    continue
                break
            if state == "blocked":
                transition = handle_blocked_status(
                    current_status=current_status,
                    workdir=workdir,
                    issue=issue,
                    task_metadata=task_metadata,
                    session=session,
                    policy=policy,
                    relaunch_orchestrator_for_blockers=relaunch_orchestrator_for_blockers,
                    progress=progress,
                )
                if transition == "continue":
                    continue
                break
            if time.monotonic() - progress.last_capture > 60:
                transition = handle_progress_checkpoint(
                    current_status=current_status,
                    state=state,
                    workdir=workdir,
                    issue=issue,
                    task_metadata=task_metadata,
                    session=session,
                    repo_root=repo_root,
                    env=env,
                    adapter_helper_repair_allowed=adapter_helper_repair_allowed,
                    relaunch_orchestrator_for_blockers=relaunch_orchestrator_for_blockers,
                    policy=policy,
                    progress=progress,
                )
                if transition == "continue":
                    continue
                if transition == "break":
                    break
            time.sleep(5)
        else:
            log(f"timed out after {timeout}s; scoring current /app git diff")
            progress.exit_code = 124
            progress.outcome = "timeout"
    finally:
        capture_session(session)
        run(["tmux", "kill-session", "-t", session], timeout=30)

    materialize_committed_changes(workdir, start_head)
    return finalize_solver_run(
        workdir=workdir,
        start_head=start_head,
        issue=issue,
        task_metadata=task_metadata,
        session=session,
        progress=progress,
    )
