"""Shared isolated Linux runner for production multiagent evaluation arms."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from evaluation.core import ROOT, git_snapshot, score_workspace


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def _runtime_evidence(state_dir: Path, workdir: Path) -> dict[str, Any]:
    workflow_id_path = state_dir / "runtime_state/active-workflow-id"
    workflow_id = workflow_id_path.read_text(encoding="utf-8").strip() if workflow_id_path.is_file() else ""
    lifecycle = _env_file(state_dir / "workflows" / workflow_id / "lifecycle/lifecycle.env")
    candidate = lifecycle.get("candidate_diff_hash", "")
    if candidate.startswith("direct-response:"):
        route = "direct-response"
    elif candidate.startswith("read-only:"):
        route = "read-only"
    elif candidate.startswith("external-only:"):
        route = "external-only"
    elif candidate:
        route = "source"
    else:
        route = "unknown"

    result_path = state_dir / "orchestrator-result.md"
    result_source = "orchestrator-result"
    if not result_path.is_file():
        result_path = state_dir / "orchestrator-last-message.txt"
        result_source = "orchestrator-last-message"
    result = result_path.read_text(encoding="utf-8")[:6_000].strip() if result_path.is_file() else ""

    roles = []
    subagents = state_dir / "subagents"
    if subagents.is_dir():
        for directory in sorted(path for path in subagents.iterdir() if path.is_dir()):
            launch = _env_file(
                state_dir / "launch-authorizations" / directory.name / "launch.env"
            )
            if not launch:
                launch = _env_file(directory / "meta.env")
            roles.append(
                {
                    "name": directory.name,
                    "role": launch.get("role", ""),
                    "access": launch.get("access", ""),
                    "state": launch.get("state", ""),
                }
            )
    operations = state_dir / "operations"
    operation_count = (
        len([path for path in operations.iterdir() if path.is_dir()]) if operations.is_dir() else 0
    )
    diff = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "phase": lifecycle.get("phase", "missing"),
        "route": route,
        "result": result,
        "result_source": result_source if result else "missing",
        "agent_count": len(roles),
        "writer_count": sum(item["access"] == "workspace-write" for item in roles),
        "external_operation_count": operation_count,
        "repo_diff_clean": diff.returncode == 0 and not diff.stdout.strip(),
        "roles": roles,
    }


def run_production_cell(
    *,
    adapter: Any,
    task_id: str,
    arm: str,
    model: str,
    run_id: int,
    run_dir: Path,
    timeout: int,
    image: str,
    runtime_prefix: str,
    prompt_profile: str = "swe",
    runtime_root: Path = Path("/tmp"),
) -> dict[str, object]:
    task = adapter.tasks[task_id]
    model_label = model or "default"
    workdir = run_dir / f"{task_id}__{arm}__{model_label}__{run_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    adapter.write_seed(workdir, task)
    original_task = workdir / "_original_task.md"
    original_task.write_text(task.prompt, encoding="utf-8")
    (workdir / "_task.json").write_text(
        json.dumps(
            {"adapter": adapter.name, "task": task_id, "arm": arm, "model": model, "run": run_id},
            indent=2,
        ),
        encoding="utf-8",
    )
    git_snapshot(workdir)
    exclude = workdir / ".git/info/exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8").rstrip("\n") + "\n_multiagent*\n",
        encoding="utf-8",
    )

    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Docker is required for a production multiagent arm")
    inspected = subprocess.run(
        [docker, "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if inspected.returncode != 0:
        raise RuntimeError(f"production multiagent image is unavailable: {image}")

    runtime_dir = runtime_root / f"{runtime_prefix}-{os.getpid()}-{task_id[-8:]}-{run_id}"
    state_dir = runtime_dir / "state"
    runtime_dir.mkdir(parents=True, exist_ok=False)
    auth_source = Path.home() / ".codex"
    auth_dir = runtime_dir / "auth-source"
    auth_dir.mkdir()
    copied_auth = False
    for name in ("auth.json", "config.toml"):
        source = auth_source / name
        if source.is_file():
            shutil.copyfile(source, auth_dir / name)
            copied_auth = copied_auth or name == "auth.json"
    if not copied_auth:
        raise RuntimeError(f"Codex authentication not found at {auth_source / 'auth.json'}")

    container_name = f"{runtime_prefix}-{os.getpid()}-{task_id[-8:]}-{run_id}"
    model_name = model or "gpt-5.6-sol"
    create_command = [
        docker,
        "create",
        "--name",
        container_name,
        "--user",
        "0:0",
        "--entrypoint",
        "python3",
        "-v",
        f"{ROOT / 'evaluation'}:/opt/multiagent/evaluation:ro",
        "-v",
        f"{auth_dir}:/auth",
        "-e",
        "CODEX_HOME=/auth",
        "-e",
        "EVAL_CODEX_AUTH_MODE=chatgpt",
        "-e",
        f"EVAL_NATIVE_SOLVER_MODEL={model_name}",
        "-e",
        "GIT_CONFIG_COUNT=1",
        "-e",
        "GIT_CONFIG_KEY_0=safe.directory",
        "-e",
        "GIT_CONFIG_VALUE_0=/app",
        image,
        "-m",
        "evaluation.native_solver.solve_swe_prod",
        "/app/_original_task.md",
        "--workdir",
        "/app",
        "--multiagent-root",
        "/opt/multiagent",
        "--timeout",
        str(timeout),
        "--prompt-profile",
        prompt_profile,
    ]

    runtime_stdout = runtime_dir / "container.stdout.txt"
    runtime_stderr = runtime_dir / "container.stderr.txt"
    started = time.monotonic()
    launch_error = ""
    with runtime_stdout.open("a", encoding="utf-8") as stdout, runtime_stderr.open(
        "a", encoding="utf-8"
    ) as stderr:
        created = subprocess.run(
            create_command,
            cwd=ROOT,
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
        returncode = created.returncode
        if returncode == 0:
            copied = subprocess.run(
                [docker, "cp", str(workdir), f"{container_name}:/app"],
                stdout=stdout,
                stderr=stderr,
                text=True,
                check=False,
            )
            returncode = copied.returncode
        if returncode == 0:
            process = subprocess.Popen(
                [docker, "start", "--attach", container_name],
                cwd=ROOT,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            try:
                returncode = process.wait(timeout=timeout + 180)
            except subprocess.TimeoutExpired:
                launch_error = f"production workflow exceeded outer timeout after {timeout + 180}s"
                subprocess.run(
                    [docker, "stop", "--time", "10", container_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                process.wait(timeout=30)
                returncode = process.returncode
        copied_workspace = subprocess.run(
            [docker, "cp", f"{container_name}:/app/.", str(workdir)],
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
        subprocess.run(
            [docker, "cp", f"{container_name}:/tmp/multiagent-prod-swe/.", str(runtime_dir)],
            stdout=stdout,
            stderr=stderr,
            text=True,
            check=False,
        )
        subprocess.run(
            [docker, "rm", "--force", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if copied_workspace.returncode != 0 and not launch_error:
            launch_error = (
                "failed to copy production workspace from container "
                f"({copied_workspace.returncode})"
            )
        elif returncode != 0 and not launch_error:
            launch_error = f"production Linux workflow exited {returncode}"

    shutil.copyfile(runtime_stdout, workdir / "_multiagent.stdout.txt")
    shutil.copyfile(runtime_stderr, workdir / "_multiagent.stderr.txt")
    evidence = _runtime_evidence(state_dir, workdir)
    (workdir / "_multiagent_evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    row = score_workspace(adapter, task_id, arm, model_label, run_id, workdir)
    row["agent_cli"] = "codex-multiagent-linux"
    row["duration_ms"] = round((time.monotonic() - started) * 1000)
    row["runtime"] = str(runtime_dir)
    (workdir / "_multiagent_runtime.txt").write_text(str(runtime_dir) + "\n", encoding="utf-8")
    if launch_error:
        row["runner_error"] = launch_error
        if row.get("correct") != 1:
            row["reason"] = f"{row.get('reason')}; {launch_error}"
    return row
