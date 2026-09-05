"""Adapter for privacy-preserving, trace-derived production-ops evaluations."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evaluation.core import EvalTask, ROOT, die, git_snapshot, run_agent_cell, score_workspace
from evaluation.tasks.ops_trace import (
    OPS_TRACE_CONTRACT_VERSION,
    SYNTHETIC_SCENARIOS,
    OpsTraceScenario,
    scenario_from_dict,
    scenario_seed,
    score_ops_plan,
)


OPS_BASELINE = """\
You are Codex in planning mode. Produce the requested operations plan from the
provided evidence. Identify roles, dependencies, safety controls, and
verification steps. Do not execute operations or contact external systems.
"""

OPS_TRACE_ARMS = {
    "baseline": "Single Codex CLI with a plain planning prompt.",
    "orchestrator": "Single Codex CLI with the current orchestrator prompt (prompt-only diagnostic).",
    "multiagent": "Current production Rust/tmux multiagent lifecycle with Codex roles.",
}


def _orchestrator_system() -> str:
    prompt_path = ROOT / "prompts/orchestrator.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception:
        return "You are the multiagent orchestrator. Route production work through confined ops roles."


def _dataset_path() -> Path | None:
    configured = os.environ.get("MULTIAGENT_OPS_TRACE_DATASET")
    if configured in {"synthetic", "none", "off"}:
        return None
    if configured:
        return Path(configured).expanduser().resolve()
    default = Path.home() / "projects" / "traces" / "ops-trace-cases.json"
    return default if default.is_file() else None


def _load_scenarios() -> tuple[dict[str, OpsTraceScenario], str]:
    path = _dataset_path()
    if path is None:
        return dict(SYNTHETIC_SCENARIOS), "built-in synthetic contract cases"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot load ops-trace dataset {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError(f"ops-trace dataset has invalid schema: {path}")
    split = os.environ.get("MULTIAGENT_OPS_TRACE_SPLIT", "test")
    if split not in {"train", "validation", "test", "all"}:
        raise ValueError("MULTIAGENT_OPS_TRACE_SPLIT must be train, validation, test, or all")
    raw_cases = [case for case in payload["cases"] if isinstance(case, dict)]
    selected = raw_cases if split == "all" else [case for case in raw_cases if case.get("split") == split]
    if not selected:
        raise ValueError(f"ops-trace dataset {path} contains no cases for split {split!r}")
    scenarios = {scenario.id: scenario for scenario in map(scenario_from_dict, selected)}
    if len(scenarios) != len(selected):
        raise ValueError(f"ops-trace dataset contains duplicate case IDs: {path}")
    return scenarios, f"private trace-derived dataset {path} split={split}"


@dataclass
class OpsTraceAdapter:
    name: str = "ops-trace"
    default_arms: str = "baseline,multiagent"
    scenarios_override: dict[str, OpsTraceScenario] | None = None
    source_override: str | None = None
    arms = OPS_TRACE_ARMS

    def __post_init__(self) -> None:
        if self.scenarios_override is None:
            scenarios, source = _load_scenarios()
        else:
            scenarios = dict(self.scenarios_override)
            source = self.source_override or "injected scenarios"
        self.scenarios = scenarios
        self.description = (
            f"Ops-trace contract v{OPS_TRACE_CONTRACT_VERSION}: operations-planning tasks that score "
            "role routing, authority boundaries, runbook/reviewer/"
            f"permit controls, evidence discipline, and safe parallel reads using {source}."
        )
        self.tasks = {
            task_id: EvalTask(
                id=task_id,
                prompt=scenario.prompt,
                seed=scenario_seed(scenario),
                score=lambda workdir, scenario=scenario: score_ops_plan(workdir, scenario),
                file="ops_plan.json",
                good=json.dumps(scenario.good_plan(), indent=2, sort_keys=True) + "\n",
                bad=json.dumps(scenario.bad_plan(), indent=2, sort_keys=True) + "\n",
                axis="safe",
            )
            for task_id, scenario in scenarios.items()
        }

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        for rel, content in task.seed.items():
            path = workdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        content = task.good if kind == "good" else task.bad
        if content is None:
            raise ValueError(f"task {task.id} has no {kind} reference")
        (workdir / "ops_plan.json").write_text(content, encoding="utf-8")

    def system_for_arm(self, arm: str) -> str:
        if arm == "baseline":
            return OPS_BASELINE
        if arm == "orchestrator":
            return _orchestrator_system()
        if arm == "multiagent":
            return _orchestrator_system()
        die(f"unknown arm: {arm}; expected one of {', '.join(sorted(OPS_TRACE_ARMS))}")

    def run_cell(
        self,
        adapter: "OpsTraceAdapter",
        task_id: str,
        arm: str,
        model: str,
        run_id: int,
        run_dir: Path,
        timeout: int,
        agent_cli: str,
    ) -> dict[str, object]:
        if arm != "multiagent":
            return run_agent_cell(adapter, task_id, arm, model, run_id, run_dir, timeout, agent_cli)
        if agent_cli != "codex":
            raise ValueError("the ops-trace multiagent arm currently requires --agent-cli codex")
        return self._run_production_multiagent(task_id, model, run_id, run_dir, timeout)

    def _run_production_multiagent(
        self,
        task_id: str,
        model: str,
        run_id: int,
        run_dir: Path,
        timeout: int,
    ) -> dict[str, object]:
        task = self.tasks[task_id]
        model_label = model or "default"
        workdir = run_dir / f"{task_id}__multiagent__{model_label}__{run_id}"
        workdir.mkdir(parents=True, exist_ok=False)
        self.write_seed(workdir, task)
        original_task = workdir / "_original_task.md"
        original_task.write_text(task.prompt, encoding="utf-8")
        (workdir / "_task.json").write_text(
            json.dumps(
                {
                    "adapter": self.name,
                    "task": task_id,
                    "arm": "multiagent",
                    "model": model,
                    "run": run_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        git_snapshot(workdir)

        docker = shutil.which("docker")
        if not docker:
            raise RuntimeError("Docker is required for the production multiagent arm")
        image = os.environ.get("MULTIAGENT_OPS_TRACE_IMAGE", "multiagent:ops-trace-current")
        inspected = subprocess.run(
            [docker, "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            raise RuntimeError(
                f"production multiagent image is unavailable: {image}; "
                "build it with `docker build -f docker/runtime/Dockerfile "
                "-t multiagent:ops-trace-current .`"
            )

        runtime_root = Path(os.environ.get("MULTIAGENT_OPS_TRACE_RUNTIME_ROOT", "/tmp"))
        runtime_dir = runtime_root / f"ops-eval-{os.getpid()}-{task_id[-8:]}-{run_id}"
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

        container_name = f"ops-eval-{os.getpid()}-{task_id[-8:]}-{run_id}"
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
        ]

        runtime_stdout = runtime_dir / "container.stdout.txt"
        runtime_stderr = runtime_dir / "container.stderr.txt"
        started = time.monotonic()
        launch_error = ""
        with runtime_stdout.open("a", encoding="utf-8") as stdout, runtime_stderr.open(
            "a", encoding="utf-8"
        ) as stderr:
            created = subprocess.run(
                create_command, cwd=ROOT, stdout=stdout, stderr=stderr, text=True, check=False
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
            runtime_dir.mkdir(exist_ok=True)
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
                launch_error = f"failed to copy production workspace from container ({copied_workspace.returncode})"
            elif returncode != 0 and not launch_error:
                launch_error = f"production Linux workflow exited {returncode}"
        shutil.copyfile(runtime_stdout, workdir / "_multiagent.stdout.txt")
        shutil.copyfile(runtime_stderr, workdir / "_multiagent.stderr.txt")

        row = score_workspace(self, task_id, "multiagent", model_label, run_id, workdir)
        row["agent_cli"] = "codex-multiagent-linux"
        row["duration_ms"] = round((time.monotonic() - started) * 1000)
        row["runtime"] = str(runtime_dir)
        (workdir / "_multiagent_runtime.txt").write_text(str(runtime_dir) + "\n", encoding="utf-8")
        subagents = state_dir / "subagents"
        row["agent_count"] = len([path for path in subagents.iterdir() if path.is_dir()]) if subagents.is_dir() else 0
        if launch_error:
            row["runner_error"] = launch_error
            if row.get("correct") != 1:
                row["reason"] = f"{row.get('reason')}; {launch_error}"
        return row

ADAPTER = OpsTraceAdapter()
