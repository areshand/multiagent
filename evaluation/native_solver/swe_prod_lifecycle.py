from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from evaluation.support.cli import multiagent_command

from . import swe_prod_repository as _repository
from .swe_prod_bootstrap import (
    require_path,
    write_apply_patch_helper,
    write_codex_bridge,
    write_go_singleflight_wrapper,
    write_rg_fallback,
)
from .swe_prod_contracts import (
    CODEX_HOME,
    CODEX_WRAPPER,
    RUNTIME_IDENTITY_PATH,
    RUNTIME_ROOT,
    log,
    read_prompt,
    read_task_metadata,
    run,
)
from .swe_prod_evidence import (
    capture_session,
    ensure_cache_dir,
    find_codex_cli,
    status,
    tmux_has_session,
    toolchain_path_prefixes,
)
from .swe_prod_repository import (
    cleanup_initial_environment_diff,
    git_diff,
    git_head,
    make_prompt,
    mark_untracked_source_intent_to_add,
    materialize_committed_changes,
)


_TERMINAL_STATES = {"blocked", "complete", "completed", "done"}


def run_prod_solver(prompt_path: str | None, workdir: Path, repo_root: Path, timeout: int) -> int:
    """Run the production workflow and leave its current diff for SWE-bench.

    This adapter owns process setup, public-input sanitization, and workspace
    transport. It deliberately does not decide whether the produced patch is
    correct. Terminal status, internal validation, and lifecycle state are
    retained as diagnostics; the official SWE-bench verifier is the only patch
    acceptance authority.
    """

    require_path(repo_root / "launch.sh", "production multiagent launcher")
    if not multiagent_command(repo_root):
        raise RuntimeError(f"production Rust multiagent executable is missing under {repo_root}")
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
    _repository.ACTIVE_START_HEAD = start_head
    cleanup_initial_environment_diff(workdir, start_head)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    RUNTIME_IDENTITY_PATH.unlink(missing_ok=True)

    codex_version_result = run([real_codex, "--version"], timeout=30)
    if codex_version_result.returncode != 0:
        raise RuntimeError(f"could not read Codex CLI version: {codex_version_result.stderr[-1000:]}")
    node_path = str(Path(real_codex).with_name("node"))
    if not Path(node_path).is_file():
        node_path = shutil.which("node") or ""
    node_version = ""
    if node_path:
        node_result = run([node_path, "--version"], timeout=30)
        if node_result.returncode == 0:
            node_version = (node_result.stdout or "").strip()
    runtime_identity = {
        "codex_version": (codex_version_result.stdout or "").strip(),
        "node_version": node_version,
    }
    runtime_identity_tmp = RUNTIME_IDENTITY_PATH.with_name(RUNTIME_IDENTITY_PATH.name + ".tmp")
    runtime_identity_tmp.write_text(json.dumps(runtime_identity, sort_keys=True), encoding="utf-8")
    runtime_identity_tmp.replace(RUNTIME_IDENTITY_PATH)
    log("runtime identity recorded: " + json.dumps(runtime_identity, sort_keys=True))

    write_codex_bridge(real_codex, os.environ.get("EVAL_NATIVE_SOLVER_MODEL", "gpt-5"), auth_mode)
    write_apply_patch_helper()
    write_rg_fallback()
    write_go_singleflight_wrapper()

    issue = read_prompt(prompt_path)
    task_metadata = read_task_metadata()
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

    launch_args = [str(repo_root / "launch.sh"), "--session", session, "--root", str(workdir), "--no-attach"]
    log(f"launching production multiagent session={session} root={workdir} repo={repo_root}")
    launch = run(launch_args, env=env, timeout=120)
    launch_tail = ((launch.stderr or "") + "\n" + (launch.stdout or "")).strip()[-4000:]
    if launch.returncode != 0:
        raise RuntimeError(f"production multiagent launch failed: {launch_tail}")
    time.sleep(2)
    if not tmux_has_session(session):
        raise RuntimeError(f"production multiagent launch exited without a live tmux session: {launch_tail[-1000:]}")

    deadline = time.monotonic() + timeout
    last_capture = 0.0
    stop_reason = "internal timeout"
    try:
        while time.monotonic() < deadline:
            try:
                materialize_committed_changes(workdir, start_head)
                mark_untracked_source_intent_to_add(workdir)
            except Exception as exc:
                log(f"could not refresh worker changes during polling: {exc}")

            current_status = status()
            state = str(current_status.get("status", "")).lower()
            if state in _TERMINAL_STATES:
                stop_reason = f"solver status={state}"
                break
            if not tmux_has_session(session):
                stop_reason = "multiagent session exited"
                break
            if time.monotonic() - last_capture > 60:
                capture_session(session)
                last_capture = time.monotonic()
            time.sleep(5)
    finally:
        capture_session(session)
        run(["tmux", "kill-session", "-t", session], timeout=30)

    materialize_committed_changes(workdir, start_head)
    mark_untracked_source_intent_to_add(workdir)
    final_diff = git_diff(workdir)
    final_status = status()
    log(
        f"submission handoff: reason={stop_reason} status={str(final_status.get('status', '')).lower() or 'missing'} "
        f"diff_bytes={len(final_diff.encode('utf-8'))}; official SWE-bench verifier decides correctness"
    )
    return 0
