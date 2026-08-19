from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

from evaluation.support.cli import multiagent_command

from .swe_prod_bootstrap import (
    require_path,
    write_apply_patch_helper,
    write_codex_bridge,
    write_rg_fallback,
)
from .swe_prod_contracts import (
    CODEX_HOME,
    CODEX_WRAPPER,
    ROLE_CODEX_HOME_ROOT,
    ORIGINAL_TASK_PATH,
    RUNTIME_IDENTITY_PATH,
    RUNTIME_ROOT,
    TMUX_SOCKET,
    log,
    read_prompt,
    read_task_metadata,
    run,
)
from .swe_prod_repository import (
    git_head,
    list_untracked_files,
    make_prompt,
    mark_untracked_intent_to_add,
    materialize_committed_changes,
)


ORCHESTRATOR_UID = 10001
WRITER_UID = 10002
READER_UID = 10003
SUPERVISOR_UID = 10004
ROLE_GID = 10001


def prepare_role_filesystem(workdir: Path, role_launcher: Path) -> None:
    """Give worker processes source writes without giving them to the orchestrator."""

    def prepare_tree(root: Path, uid: int, *, group_write: bool) -> None:
        paths = [root]
        paths.extend(root.rglob("*"))
        for path in paths:
            try:
                info = path.lstat()
                os.chown(path, uid, ROLE_GID, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    continue
                mode = stat.S_IMODE(info.st_mode)
                mode &= ~stat.S_IWOTH
                mode |= stat.S_IRGRP
                if stat.S_ISDIR(info.st_mode):
                    mode |= stat.S_IXGRP
                if group_write:
                    mode |= stat.S_IWGRP
                else:
                    mode &= ~stat.S_IWGRP
                # Some benchmark images expose Python builds where chmod does
                # not implement follow_symlinks=False. lstat above already
                # proves this is not a symlink, so the portable call is safe.
                os.chmod(path, mode)
            except FileNotFoundError:
                continue

    # The repository starts neutral. The privileged Rust launcher grants the
    # single active writer ownership only over its supervisor-owned paths and
    # revokes that grant when the role exits. This remains enforceable on
    # kernels where Landlock is unavailable.
    prepare_tree(workdir, 0, group_write=False)

    os.chown(role_launcher, 0, 0)
    os.chmod(role_launcher, 0o4755)

    # Codex creates private config, lock, and SQLite files at runtime. Sharing a
    # single CODEX_HOME across different role UIDs lets the orchestrator make
    # its own home unreadable to later workers. Seed an independent home for
    # each identity instead; auth is copied at runtime and is never baked into
    # the task image or trace bundle.
    ROLE_CODEX_HOME_ROOT.mkdir(parents=True, exist_ok=True)
    seed_files = [path for path in (CODEX_HOME / "auth.json", CODEX_HOME / "config.toml") if path.is_file()]
    for role, uid in (
        ("orchestrator", ORCHESTRATOR_UID),
        ("writer", WRITER_UID),
        ("reader", READER_UID),
        ("supervisor", SUPERVISOR_UID),
    ):
        home = ROLE_CODEX_HOME_ROOT / role
        home.mkdir(parents=True, exist_ok=True)
        for source in seed_files:
            shutil.copyfile(source, home / source.name)
        (home / ".gitconfig").write_text(
            f"[safe]\n\tdirectory = {workdir}\n",
            encoding="utf-8",
        )
        prepare_tree(home, uid, group_write=False)
        os.chmod(home, 0o700)

    for cache in (
        RUNTIME_ROOT / "go-build-cache",
        RUNTIME_ROOT / "go-mod-cache",
        RUNTIME_ROOT / "role-shared",
    ):
        cache.mkdir(parents=True, exist_ok=True)
        prepare_tree(cache, WRITER_UID, group_write=True)


def restore_workspace_owner(workdir: Path) -> None:
    """Return the frozen workspace to the container owner for patch transport."""

    paths = [workdir]
    paths.extend(workdir.rglob("*"))
    for path in paths:
        try:
            os.chown(path, 0, 0, follow_symlinks=False)
        except FileNotFoundError:
            continue


def find_codex_cli() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    for candidate in (
        Path("/opt/node22/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
        Path("/root/.npm-global/bin/codex"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def ensure_cache_dir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def toolchain_path_prefixes() -> list[str]:
    candidates = (
        Path("/usr/local/go/bin"),
        Path("/usr/lib/go/bin"),
        Path("/opt/go/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    )
    return [str(path) for path in candidates if (path / "go").exists()]


def tmux_has_session(session: str) -> bool:
    command = ["tmux", "-S", str(TMUX_SOCKET), "has-session", "-t", session]
    return run(command, timeout=10).returncode == 0


def tmux_has_orchestrator(session: str) -> bool:
    result = run(
        ["tmux", "-S", str(TMUX_SOCKET), "list-windows", "-t", session, "-F", "#W"],
        timeout=10,
    )
    return result.returncode == 0 and "orchestrator" in result.stdout.splitlines()


def active_workflow_phase() -> str | None:
    """Return the persisted lifecycle phase for the active production workflow."""

    state = RUNTIME_ROOT / "state"
    active_id_path = state / "runtime_state" / "active-workflow-id"
    try:
        workflow_id = active_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not workflow_id:
        return None
    lifecycle_path = state / "workflows" / workflow_id / "lifecycle" / "lifecycle.env"
    try:
        lines = lifecycle_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key == "phase":
            return value.strip() or None
    return None


def run_prod_solver(prompt_path: str | None, workdir: Path, repo_root: Path, timeout: int) -> int:
    """Run the production workflow and leave its current diff for SWE-bench.

    The adapter only starts the workflow and exposes its final workspace diff.
    EvalScope and the official SWE-bench verifier own evaluation.
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
    baseline_untracked = set(list_untracked_files(workdir))
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    baseline_untracked_path = RUNTIME_ROOT / "baseline-untracked.txt"
    baseline_untracked_path.write_text(
        "".join(f"{path}\n" for path in sorted(baseline_untracked)),
        encoding="utf-8",
    )
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

    issue = read_prompt(prompt_path)
    task_metadata = read_task_metadata()
    log("solver metadata is public-only; official expected-test metadata is not exposed to the solver")
    autonomous_prompt = make_prompt(repo_root, workdir, issue, task_metadata)
    session = f"swe-prod-{os.getpid()}"
    toolchain_prefix = ":".join(toolchain_path_prefixes())
    path_parts = [str(RUNTIME_ROOT), str(repo_root / "bin")]
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
            "MULTIAGENT_BASELINE_UNTRACKED_FILE": str(baseline_untracked_path),
            "MULTIAGENT_ORIGINAL_TASK_FILE": str(ORIGINAL_TASK_PATH),
            "ORCHESTRATOR_CLI": "codex",
            "WORKER_CLI": "codex",
            "SUBAGENT_CLI": "codex",
            "VERIFIER_CLI": "codex",
            "CODEX_BIN": str(CODEX_WRAPPER),
            "CODEX_HOME": str(CODEX_HOME),
            "MULTIAGENT_CODEX_HOME_ROOT": str(ROLE_CODEX_HOME_ROOT),
            "MULTIAGENT_CODEX_EXEC": os.environ.get("MULTIAGENT_CODEX_EXEC", "1"),
            "MULTIAGENT_EXTRA_PATH": str(RUNTIME_ROOT),
            "MULTIAGENT_ROLE_SHARED_WRITE_DIR": str(RUNTIME_ROOT / "role-shared"),
            "CARGO_TARGET_DIR": str(RUNTIME_ROOT / "role-shared" / "cargo-target"),
            "PYTHONPYCACHEPREFIX": str(RUNTIME_ROOT / "role-shared" / "pycache"),
            "MULTIAGENT_UID_SANDBOX": "1",
            "PATH": ":".join(part for part in path_parts if part),
            "GOCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-build-cache"),
            "GOMODCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache"),
            "MULTIAGENT_READY_ATTEMPTS": os.environ.get("MULTIAGENT_READY_ATTEMPTS", "80"),
            "MULTIAGENT_READY_DELAY": os.environ.get("MULTIAGENT_READY_DELAY", "1"),
        }
    )

    prepare_role_filesystem(workdir, Path(multiagent_command(repo_root)[0]))

    launch_args = [str(repo_root / "launch.sh"), "--session", session, "--root", str(workdir), "--no-attach"]
    log(f"launching production multiagent session={session} root={workdir} repo={repo_root}")
    launch = run(launch_args, env=env, timeout=120)
    launch_tail = ((launch.stderr or "") + "\n" + (launch.stdout or "")).strip()[-4000:]
    if launch.returncode != 0:
        raise RuntimeError(f"production multiagent launch failed: {launch_tail}")

    deadline = time.monotonic() + timeout
    resume_count = 0
    final_phase: str | None = None
    try:
        while time.monotonic() < deadline:
            while time.monotonic() < deadline and tmux_has_orchestrator(session):
                time.sleep(5)

            phase = active_workflow_phase()
            final_phase = phase
            if phase == "complete" or time.monotonic() >= deadline:
                break
            if phase is None:
                raise RuntimeError(
                    "production multiagent orchestrator exited without a persisted workflow lifecycle"
                )

            resume_count += 1
            log(
                "orchestrator exited before lifecycle completion; "
                f"resuming session={session} phase={phase} attempt={resume_count}"
            )
            resume_args = [
                str(repo_root / "launch.sh"),
                "--session",
                session,
                "--root",
                str(workdir),
                "--resume",
                "--no-attach",
            ]
            resumed = run(resume_args, env=env, timeout=120)
            resume_tail = ((resumed.stderr or "") + "\n" + (resumed.stdout or "")).strip()[-4000:]
            if resumed.returncode != 0:
                raise RuntimeError(f"production multiagent resume failed: {resume_tail}")
    finally:
        if tmux_has_session(session):
            run(["tmux", "-S", str(TMUX_SOCKET), "kill-session", "-t", session], timeout=30)
        restore_workspace_owner(workdir)

    if final_phase != "complete":
        rendered_phase = final_phase or "missing"
        raise RuntimeError(
            "production multiagent workflow did not reach supervisor-owned completion before "
            f"the solver deadline (phase={rendered_phase}); refusing workspace handoff"
        )

    materialize_committed_changes(workdir, start_head)
    mark_untracked_intent_to_add(workdir, baseline_untracked=baseline_untracked)
    log("workspace prepared for EvalScope submission")
    return 0
