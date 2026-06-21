"""Generic evaluator for multiagent coding evaluation adapters."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


ROOT = Path(__file__).resolve().parents[1]
RUNS_ROOT = ROOT / "evaluation" / "runs"
CODE_EXT = {".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".java", ".rb", ".sh"}

Score = dict[str, Any]


@dataclass(frozen=True)
class EvalTask:
    id: str
    prompt: str
    seed: dict[str, str]
    score: Callable[[Path], Score]
    file: str | None = None
    good: str | None = None
    bad: str | None = None
    axis: str = "safe"


class Adapter(Protocol):
    name: str
    description: str
    tasks: dict[str, EvalTask]

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        ...

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        ...


BASELINE_FALLBACK = """\
You are a worker agent launched by the multiagent orchestrator.

Rules:
- Work only in the current workspace.
- Do not submit PRs, push, or send external messages.
- If blocked, stop and state what you need.
- Stay focused on the assigned files and task.
- Make the smallest clear change that satisfies the task and leave a concise final status.
"""

PONYTAIL_FULL = """\
Ponytail mode: full.

Before writing code, climb this ladder and stop at the first rung that works:
1. Does this need to be built at all?
2. Does the standard library solve it?
3. Does a native platform feature solve it?
4. Does an already-installed dependency solve it?
5. Can the change be one small edit?
6. Only then write the minimum code that works.

No unrequested abstractions, dependencies, configuration, factories, wrappers, or boilerplate.
Prefer deletion over addition and boring code over clever code. If you intentionally take a
shortcut, mark it with `ponytail:` and name the ceiling plus the trigger to revisit it.

Do not simplify away trust-boundary validation, data-loss handling, security, accessibility,
real-world calibration, or explicit user scope. Non-trivial logic should leave one minimal
runnable check when practical.
"""

PONYTAIL_LITE = """\
Ponytail mode: lite.

Prefer existing code, the standard library, and native platform behavior before adding code.
Name any simpler alternative you considered, but still satisfy the requested task.
"""

PONYTAIL_ULTRA = """\
Ponytail mode: ultra.

Deletion-first YAGNI. Build nothing unless it is necessary for the exact task. Use stdlib/native
features aggressively, avoid every new abstraction or dependency, and keep only safety checks that
protect correctness, data, or trust boundaries.
"""

NO_RUN = """\
Write the implementation and stop. Do not run a dev server, install dependencies, open a browser,
or call external services. You may edit files in this workspace. Only the code left on disk is
measured.
"""

ARMS = {
    "baseline": "",
    "ponytail-lite": PONYTAIL_LITE,
    "ponytail-full": PONYTAIL_FULL,
    "ponytail-ultra": PONYTAIL_ULTRA,
}


def die(message: str) -> None:
    print(f"evaluation: {message}", file=sys.stderr)
    raise SystemExit(2)


def parse_csv(value: str, choices: dict[str, Any] | set[str] | list[str] | tuple[str, ...]) -> list[str]:
    allowed = set(choices)
    items = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in items if item not in allowed]
    if unknown:
        die(f"unknown value(s): {', '.join(unknown)}; expected one of {', '.join(sorted(allowed))}")
    return items


def arm_choices(adapter: Adapter) -> dict[str, str] | set[str] | list[str] | tuple[str, ...]:
    choices = getattr(adapter, "arms", None)
    return choices if choices is not None else ARMS


def default_arms(adapter: Adapter) -> str:
    value = getattr(adapter, "default_arms", None)
    return str(value) if value else "baseline,ponytail-full"


def run_git(workdir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git") or "git"
    return subprocess.run([git, *args], cwd=workdir, capture_output=True, text=True, check=False)


def git_snapshot(workdir: Path) -> None:
    run_git(workdir, "init", "-q")
    run_git(workdir, "config", "user.email", "eval@local")
    run_git(workdir, "config", "user.name", "multiagent-eval")
    run_git(workdir, "config", "commit.gpgsign", "false")
    run_git(workdir, "add", "-A")
    result = run_git(workdir, "commit", "-q", "-m", "base", "--no-verify")
    if result.returncode != 0:
        die(f"git snapshot failed in {workdir}: {result.stderr.strip()}")
    base = run_git(workdir, "rev-parse", "HEAD")
    if base.returncode == 0:
        (workdir / "_base_commit").write_text(base.stdout.strip() + "\n", encoding="utf-8")


def is_test(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    name = path.name.lower()
    return name.startswith("test_") or name.endswith("_test.py") or "test" in parts[:-1] or "tests" in parts[:-1]


def base_commit(workdir: Path) -> str:
    marker = workdir / "_base_commit"
    if marker.exists():
        return marker.read_text(encoding="utf-8").strip()
    result = run_git(workdir, "rev-list", "--max-parents=0", "HEAD")
    if result.returncode == 0:
        commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if commits:
            return commits[-1]
    return "HEAD"


def add_numstat(stats: dict[str, int], output: str) -> None:
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _deleted, rel = parts
        if added == "-":
            continue
        path = Path(rel)
        if path.suffix not in CODE_EXT or "node_modules" in path.parts or "__pycache__" in path.parts:
            continue
        if is_test(path):
            stats["test_loc"] += int(added)
            stats["test_files"] += 1
        else:
            stats["src_loc"] += int(added)
            stats["src_files"] += 1


def git_diff_stats(workdir: Path) -> dict[str, int]:
    stats = {
        "src_loc": 0,
        "src_files": 0,
        "test_loc": 0,
        "test_files": 0,
    }
    base = base_commit(workdir)
    committed = run_git(workdir, "diff", "--numstat", f"{base}..HEAD")
    add_numstat(stats, committed.stdout)

    run_git(workdir, "add", "-A")
    result = run_git(workdir, "diff", "--cached", "--numstat", "HEAD")
    add_numstat(stats, result.stdout)
    return stats


def current_worker_system() -> str:
    prompt_path = ROOT / "orchestrator_prompt.md"
    try:
        text = prompt_path.read_text(encoding="utf-8")
        start = text.index("## Required Worker First Instruction")
        end = text.index("## Worker Spawn Skill", start)
        section = text[start:end].strip()
        return (
            "You are a worker agent launched by the multiagent orchestrator.\n\n"
            "Use the current repository worker rules below. They are extracted from "
            "`orchestrator_prompt.md`, so evaluation tracks changes to the multiagent system.\n\n"
            f"{section}"
        )
    except Exception:
        return BASELINE_FALLBACK


def system_for_arm(arm: str) -> str:
    if arm not in ARMS:
        die(f"unknown arm: {arm}; expected one of {', '.join(sorted(ARMS))}")
    suffix = ARMS[arm]
    base = current_worker_system()
    return base if not suffix else base + "\n\n" + suffix


def system_for_adapter_arm(adapter: Adapter, arm: str) -> str:
    adapter_system = getattr(adapter, "system_for_arm", None)
    if callable(adapter_system):
        return adapter_system(arm)
    return system_for_arm(arm)


def score_workspace(adapter: Adapter, task_id: str, arm: str, model: str, run_id: int, workdir: Path) -> dict[str, Any]:
    task = adapter.tasks[task_id]
    score = task.score(workdir)
    stats = git_diff_stats(workdir)
    meta: dict[str, Any] = {}
    agent_json = workdir / "_agent.json"
    if agent_json.exists():
        try:
            data = json.loads(agent_json.read_text(encoding="utf-8"))
            usage = data.get("usage") or {}
            meta = {
                "cost": data.get("total_cost_usd"),
                "duration_ms": data.get("duration_ms"),
                "turns": data.get("num_turns"),
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_tokens": (usage.get("cache_read_input_tokens") or 0)
                + (usage.get("cache_creation_input_tokens") or 0),
            }
        except Exception as exc:
            meta = {"agent_json_error": str(exc)}
    return {
        "adapter": adapter.name,
        "task": task_id,
        "arm": arm,
        "model": model,
        "run": run_id,
        "workspace": str(workdir),
        **score,
        **stats,
        **meta,
    }


def selftest_adapter(adapter: Adapter) -> int:
    failures = 0
    for task_id, task in adapter.tasks.items():
        if task.good is None or task.bad is None:
            print(f"skip {task_id:12} no references")
            continue
        for kind in ("good", "bad"):
            with tempfile.TemporaryDirectory() as tmp:
                workdir = Path(tmp)
                adapter.write_reference(workdir, task, kind)
                result = task.score(workdir)
            ok = (
                result["correct"] == 1 and result["safe"] == 1
                if kind == "good"
                else result[task.axis] == 0
            )
            label = "ok " if ok else "XX "
            print(
                f"{label}{task_id:12} {kind:4} "
                f"correct={result['correct']} safe={result['safe']} axis={task.axis} {result['reason']}"
            )
            failures += 0 if ok else 1
    print(f"\nselftest[{adapter.name}]: {'all scorers valid' if failures == 0 else str(failures) + ' failures'}")
    return failures


def build_claude_command(prompt: str, system: str, model: str) -> list[str]:
    claude = shutil.which("claude")
    if not claude:
        die("claude CLI not found on PATH")
    return [
        claude,
        "-p",
        prompt,
        "--model",
        model,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--setting-sources",
        "project,local",
        "--strict-mcp-config",
        "--disallowedTools",
        "Bash",
        "--append-system-prompt",
        system + "\n" + NO_RUN,
    ]


def build_codex_command(prompt: str, system: str, model: str, workdir: Path) -> list[str]:
    codex = shutil.which("codex")
    if not codex:
        die("codex CLI not found on PATH")
    combined = system + "\n" + NO_RUN + "\n\nTask:\n" + prompt
    cmd = [
        codex,
        "exec",
        "--cd",
        str(workdir),
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "--output-last-message",
        str(workdir / "_agent.final.txt"),
    ]
    if model:
        cmd += ["--model", model]
    cmd.append(combined)
    return cmd


def build_agent_command(agent_cli: str, prompt: str, system: str, model: str, workdir: Path) -> list[str]:
    if agent_cli == "claude":
        return build_claude_command(prompt, system, model)
    if agent_cli == "codex":
        return build_codex_command(prompt, system, model, workdir)
    die(f"unknown agent CLI: {agent_cli}; expected claude or codex")


def kill_process_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        proc.terminate()


def run_agent_cell(
    adapter: Adapter,
    task_id: str,
    arm: str,
    model: str,
    run_id: int,
    run_dir: Path,
    timeout: int,
    agent_cli: str,
) -> dict[str, Any]:
    task = adapter.tasks[task_id]
    workdir = run_dir / f"{task_id}__{arm}__{model}__{run_id}"
    workdir.mkdir(parents=True, exist_ok=False)
    adapter.write_seed(workdir, task)
    (workdir / "_task.json").write_text(
        json.dumps(
            {
                "adapter": adapter.name,
                "task": task_id,
                "arm": arm,
                "model": model,
                "run": run_id,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    git_snapshot(workdir)

    cmd = build_agent_command(agent_cli, task.prompt, system_for_adapter_arm(adapter, arm), model, workdir)
    stderr_path = workdir / "_agent.stderr.txt"
    stdout_path = workdir / ("_agent.json" if agent_cli == "claude" else "_agent.stdout.jsonl")
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = subprocess.Popen(
            cmd,
            cwd=workdir,
            stdout=stdout,
            stderr=stderr,
            start_new_session=(os.name == "posix"),
        )
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            stderr.write(f"\n[KILLED after {timeout}s timeout]\n".encode())

    row = score_workspace(adapter, task_id, arm, model, run_id, workdir)
    row["agent_cli"] = agent_cli
    return row


def rescore(adapter: Adapter, run_dir: Path) -> list[dict[str, Any]]:
    if not run_dir.exists():
        die(f"run dir does not exist: {run_dir}")
    results: list[dict[str, Any]] = []
    for workdir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
        parts = workdir.name.split("__")
        if len(parts) != 4:
            continue
        task_id, arm, model, run_text = parts
        if task_id not in adapter.tasks or arm not in set(arm_choices(adapter)):
            continue
        try:
            run_id = int(run_text)
        except ValueError:
            continue
        results.append(score_workspace(adapter, task_id, arm, model, run_id, workdir))
    return results


def mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def aggregate(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    for row in results:
        grouped.setdefault((row["adapter"], row["task"], row["arm"], row["model"]), []).append(row)

    base_metric_keys = {
        "correct",
        "safe",
        "src_loc",
        "src_files",
        "test_loc",
        "test_files",
        "duration_ms",
        "cost",
        "turns",
        "input_tokens",
        "output_tokens",
        "cache_tokens",
        "run",
    }
    metadata_keys = {"adapter", "agent_cli", "task", "arm", "model", "workspace", "reason"}

    aggregate_rows = []
    for (adapter, task, arm, model), rows in sorted(grouped.items()):
        token_totals = [
            (row.get("input_tokens") or 0) + (row.get("output_tokens") or 0) + (row.get("cache_tokens") or 0)
            for row in rows
            if row.get("output_tokens") is not None
        ]
        aggregate_row = {
            "adapter": adapter,
            "task": task,
            "arm": arm,
            "model": model,
            "runs": len(rows),
            "correct_rate": mean([row["correct"] for row in rows]),
            "safe_rate": mean([row["safe"] for row in rows]),
            "src_loc_mean": mean([row["src_loc"] for row in rows]),
            "src_files_mean": mean([row["src_files"] for row in rows]),
            "test_loc_mean": mean([row["test_loc"] for row in rows]),
            "duration_s_mean": mean([row["duration_ms"] / 1000 for row in rows if row.get("duration_ms")]),
            "tokens_mean": mean(token_totals),
            "cost_mean": mean([row["cost"] for row in rows if row.get("cost") is not None]),
        }
        custom_keys = sorted({
            key
            for row in rows
            for key, value in row.items()
            if key not in base_metric_keys
            and key not in metadata_keys
            and isinstance(value, (int, float))
        })
        for key in custom_keys:
            aggregate_row[f"{key}_mean"] = mean(
                [row[key] for row in rows if isinstance(row.get(key), (int, float))]
            )
        aggregate_rows.append(aggregate_row)
    return aggregate_rows


def write_json_report(run_dir: Path, adapter: Adapter, results: list[dict[str, Any]]) -> Path:
    payload = {
        "adapter": adapter.name,
        "description": adapter.description,
        "results": results,
        "aggregate": aggregate(results),
    }
    path = run_dir / "results.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def markdown_report(adapter: Adapter, results: list[dict[str, Any]]) -> str:
    rows = aggregate(results)
    extra_columns = [
        ("First Wave", "first_wave_agents_mean"),
        ("Max Agents", "max_concurrent_agents_mean"),
        ("Avg Agents", "avg_concurrent_agents_mean"),
        ("Concurrency", "concurrency_ratio_mean"),
    ]
    visible_extra_columns = [
        column for column in extra_columns if any(row.get(column[1]) is not None for row in rows)
    ]
    lines = [
        f"# Evaluation Report: {adapter.name}",
        "",
        adapter.description,
        "",
        "| Task | Arm | Runs | Correct | Safe | Source LOC | Source Files | Time s | Tokens | Cost |"
        + "".join(f" {label} |" for label, _key in visible_extra_columns),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
        + "---:|" * len(visible_extra_columns),
    ]
    for row in rows:
        lines.append(
            "| {task} | {arm} | {runs} | {correct_rate} | {safe_rate} | {src_loc_mean} | "
            "{src_files_mean} | {duration_s_mean} | {tokens_mean} | {cost_mean} |".format(**row)
            + "".join(f" {row.get(key)} |" for _label, key in visible_extra_columns)
        )
    lines.append("")
    return "\n".join(lines)


def write_markdown_report(run_dir: Path, adapter: Adapter, results: list[dict[str, Any]]) -> Path:
    path = run_dir / "report.md"
    path.write_text(markdown_report(adapter, results), encoding="utf-8")
    return path


def print_summary(results: list[dict[str, Any]]) -> None:
    rows = aggregate(results)
    print("\n=== aggregate ===")
    has_concurrency = any(row.get("max_concurrent_agents_mean") is not None for row in rows)
    suffix_header = " max_agents avg_agents" if has_concurrency else ""
    print(
        f"{'adapter':16} {'task':12} {'arm':15} {'runs':>4} {'correct':>7} {'safe':>7} "
        f"{'loc':>7} {'files':>7} {'time_s':>8}{suffix_header}"
    )
    for row in rows:
        suffix = (
            f" {row.get('max_concurrent_agents_mean')!s:>10} {row.get('avg_concurrent_agents_mean')!s:>10}"
            if has_concurrency
            else ""
        )
        print(
            f"{row['adapter']:16} {row['task']:12} {row['arm']:15} {row['runs']:>4} "
            f"{row['correct_rate']!s:>7} {row['safe_rate']!s:>7} "
            f"{row['src_loc_mean']!s:>7} {row['src_files_mean']!s:>7} "
            f"{row['duration_s_mean']!s:>8}{suffix}"
        )


def run_matrix(
    adapter: Adapter,
    tasks: list[str],
    arms: list[str],
    model: str,
    runs: int,
    workers: int,
    timeout: int,
    runs_root: Path = RUNS_ROOT,
    agent_cli: str = "claude",
) -> tuple[Path, list[dict[str, Any]]]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = runs_root / adapter.name / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    matrix = [
        (adapter, task, arm, model, run_id, run_dir, timeout, agent_cli)
        for task in tasks
        for arm in arms
        for run_id in range(1, runs + 1)
    ]
    print(f"\nrunning {len(matrix)} cells in {run_dir} with {workers} worker(s)")

    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {pool.submit(run_agent_cell, *cell): cell for cell in matrix}
        for future in concurrent.futures.as_completed(future_map):
            _adapter, task, arm, _model, run_id, _run_dir, _timeout, _agent_cli = future_map[future]
            try:
                row = future.result()
            except Exception as exc:
                row = {
                    "adapter": adapter.name,
                    "agent_cli": agent_cli,
                    "task": task,
                    "arm": arm,
                    "model": model,
                    "run": run_id,
                    "correct": 0,
                    "safe": 0,
                    "src_loc": 0,
                    "src_files": 0,
                    "test_loc": 0,
                    "test_files": 0,
                    "reason": f"runner error: {exc}",
                }
            results.append(row)
            print(
                f"[{len(results)}/{len(matrix)}] {row['adapter']} {row['task']} {row['arm']} "
                f"run={row['run']} correct={row['correct']} safe={row['safe']} "
                f"loc={row['src_loc']} reason={row['reason']}"
            )
            write_json_report(run_dir, adapter, results)
            write_markdown_report(run_dir, adapter, results)
    return run_dir, results


def reference_report(
    adapter: Adapter,
    tasks: list[str],
    kinds: list[str],
    runs_root: Path = RUNS_ROOT,
) -> tuple[Path, list[dict[str, Any]]]:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-reference")
    run_dir = runs_root / adapter.name / stamp
    run_dir.mkdir(parents=True, exist_ok=False)

    results: list[dict[str, Any]] = []
    for task_id in tasks:
        task = adapter.tasks[task_id]
        for kind in kinds:
            if kind not in ("good", "bad"):
                die(f"unknown reference kind: {kind}")
            if task.good is None or task.bad is None:
                continue
            arm = f"reference-{kind}"
            workdir = run_dir / f"{task_id}__{arm}__reference__1"
            workdir.mkdir(parents=True, exist_ok=False)
            adapter.write_seed(workdir, task)
            (workdir / "_task.json").write_text(
                json.dumps(
                    {
                        "adapter": adapter.name,
                        "task": task_id,
                        "arm": arm,
                        "model": "reference",
                        "run": 1,
                        "reference": kind,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            git_snapshot(workdir)
            adapter.write_reference(workdir, task, kind)
            results.append(score_workspace(adapter, task_id, arm, "reference", 1, workdir))

    write_json_report(run_dir, adapter, results)
    write_markdown_report(run_dir, adapter, results)
    return run_dir, results
