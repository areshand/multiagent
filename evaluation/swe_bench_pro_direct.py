#!/usr/bin/env python3
"""Run prepared SWE-style patch tasks through the task-level solver contract.

This is not the official SWE Bench Pro harness. It is the direct bridge between
prepared repository instances and ``solve_patch(...)`` so we can compare patch
solving behavior before the Docker/image/scaffold parity work is complete.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from evaluation.solver_adapter import CodexCliSolver, SolverRun


def load_instances(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, dict) and "instances" in payload:
        payload = payload["instances"]
    if not isinstance(payload, list):
        raise ValueError(f"expected a list of instances in {path}")
    return payload


def template_payload() -> list[dict[str, Any]]:
    return [
        {
            "instance_id": "example-swe-instance",
            "repo_path": "/tmp/example-repo",
            "base_commit": "optional git commit sha",
            "issue_prompt": "Fix the bug described here.",
            "test_command": ["python3", "-m", "pytest", "tests/test_example.py"],
        }
    ]


def write_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(item) for item in template_payload()) + "\n", encoding="utf-8")


def selected_records(records: list[dict[str, Any]], start: int, limit: int | None) -> list[dict[str, Any]]:
    if start < 1:
        raise ValueError("--start is 1-indexed and must be >= 1")
    selected = records[start - 1 :]
    if limit is not None:
        selected = selected[:limit]
    return selected


def instance_prompt(instance: dict[str, Any]) -> str:
    prompt = instance.get("issue_prompt") or instance.get("prompt") or instance.get("problem_statement")
    if not prompt:
        raise ValueError(f"instance {instance.get('instance_id') or instance.get('id')!r} is missing an issue prompt")
    return str(prompt)


def instance_id(instance: dict[str, Any], index: int) -> str:
    return str(instance.get("instance_id") or instance.get("id") or f"instance-{index + 1}")


def validate_instances(instances: list[dict[str, Any]], check_paths: bool = True) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, instance in enumerate(instances):
        ident = instance_id(instance, index)
        if ident in seen:
            errors.append(f"{ident}: duplicate instance_id")
        seen.add(ident)
        if not (instance.get("issue_prompt") or instance.get("prompt") or instance.get("problem_statement")):
            errors.append(f"{ident}: missing issue_prompt/prompt/problem_statement")
        raw_repo = instance.get("repo_path")
        if not raw_repo:
            errors.append(f"{ident}: missing repo_path")
        elif check_paths and not Path(str(raw_repo)).expanduser().exists():
            errors.append(f"{ident}: repo_path does not exist: {raw_repo}")
        command = instance.get("test_command")
        if command is not None and not isinstance(command, (str, list)):
            errors.append(f"{ident}: test_command must be a string or list")
    return errors


def merge_items(merge_paths: list[str], new_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for merge_path in merge_paths:
        data = json.loads(Path(merge_path).read_text(encoding="utf-8"))
        for item in data.get("items", []):
            key = item.get("instance_id")
            if key is not None:
                merged[str(key)] = item
    for item in new_items:
        key = item.get("instance_id")
        if key is not None:
            merged[str(key)] = item
    return list(merged.values())


def copy_repo(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def prepare_repo(instance: dict[str, Any], instance_dir: Path, in_place: bool) -> Path:
    raw_repo = instance.get("repo_path")
    if not raw_repo:
        raise ValueError(f"instance {instance.get('instance_id') or instance.get('id')!r} is missing repo_path")
    source = Path(str(raw_repo)).expanduser().resolve()
    if not source.exists():
        raise FileNotFoundError(f"repo_path does not exist: {source}")
    if in_place:
        repo = source
    else:
        repo = instance_dir / "repo"
        copy_repo(source, repo)

    base_commit = instance.get("base_commit")
    if base_commit:
        subprocess.run(["git", "reset", "--hard", str(base_commit)], cwd=repo, capture_output=True, text=True, check=True)
        subprocess.run(["git", "clean", "-fd"], cwd=repo, capture_output=True, text=True, check=True)
    return repo


def run_verifier(command: Any, repo: Path, timeout: int) -> dict[str, Any] | None:
    if not command:
        return None
    if isinstance(command, list):
        result = subprocess.run([str(part) for part in command], cwd=repo, capture_output=True, text=True, timeout=timeout, check=False)
        command_display = " ".join(str(part) for part in command)
    elif isinstance(command, str):
        result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=timeout, check=False, shell=True)
        command_display = command
    else:
        raise ValueError("test_command must be a string or list")
    return {
        "command": command_display,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "passed": result.returncode == 0,
    }


def item_result(
    instance: dict[str, Any],
    index: int,
    run_root: Path,
    solver: CodexCliSolver,
    timeout: int,
    verifier_timeout: int,
    dry_run: bool,
    in_place: bool,
) -> dict[str, Any]:
    ident = instance_id(instance, index)
    instance_dir = run_root / "instances" / ident
    instance_dir.mkdir(parents=True, exist_ok=True)
    repo = prepare_repo(instance, instance_dir, in_place=in_place)
    run: SolverRun = solver.solve_patch(instance_prompt(instance), repo_path=repo, timeout=timeout, dry_run=dry_run)
    diff_path = instance_dir / "patch.diff"
    diff_path.write_text(run.output, encoding="utf-8")
    verifier = None if dry_run else run_verifier(instance.get("test_command"), repo, verifier_timeout)
    correct = None
    if verifier is not None:
        correct = 1 if run.returncode == 0 and verifier["passed"] else 0
    return {
        "instance_id": ident,
        "repo_path": str(repo),
        "patch_path": str(diff_path),
        "patch_bytes": len(run.output.encode("utf-8")),
        "has_patch": bool(run.output.strip()),
        "correct": correct,
        "duration_s": run.duration_s,
        **run.to_metadata(),
        "verifier": verifier,
    }


def summarize(items: list[dict[str, Any]]) -> tuple[float | None, int, float]:
    scored = [item for item in items if item.get("correct") is not None]
    duration = round(sum(float(item.get("duration_s") or 0) for item in items), 3)
    if not scored:
        return None, 0, duration
    score = round(100 * sum(int(item["correct"]) for item in scored) / len(scored), 3)
    return score, len(scored), duration


def write_output(path: Path, args: argparse.Namespace, items: list[dict[str, Any]]) -> None:
    score, sample_size, duration_s = summarize(items)
    notes = (
        "Non-official SWE Bench Pro direct patch pilot over prepared local repos. "
        "It compares solve_patch behavior and does not include official SWE Bench Pro Docker/scaffold parity."
    )
    comparison = {
        "system": "ours-codex-swe-bench-pro-direct",
        "source": str(args.instances),
        "results": [
            {
                "benchmark": "swe-bench-pro",
                "score": score,
                "metric": "resolved_percent",
                "sample_size": sample_size,
                "official": False,
                "duration_s": duration_s,
                "notes": notes,
            }
        ],
    }
    payload = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "benchmark": "swe-bench-pro",
        "score": score,
        "sample_size": sample_size,
        "duration_s": duration_s,
        "official": False,
        "metric": "resolved_percent",
        "instances": str(args.instances),
        "start": args.start,
        "limit": args.limit,
        "notes": notes,
        "items": items,
        "comparison_result": comparison,
        "system_results": comparison,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run prepared SWE Bench Pro-style patch tasks")
    parser.add_argument("--instances", help="JSON or JSONL prepared instances")
    parser.add_argument("--output", help="output JSON path")
    parser.add_argument("--template", help="write an example JSONL instances file")
    parser.add_argument("--run-root", default="/tmp/swe-bench-pro-direct", help="directory for copied repos and patches")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--verifier-timeout", type=int, default=300)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--in-place", action="store_true", help="run in repo_path directly instead of copying it")
    parser.add_argument("--start", type=int, default=1, help="1-indexed first instance to run")
    parser.add_argument("--limit", type=int, help="maximum number of instances to run")
    parser.add_argument("--merge", action="append", default=[], help="existing result JSON to merge into output")
    parser.add_argument("--validate-only", action="store_true", help="validate selected instances without running the solver")
    parser.add_argument("--no-check-paths", action="store_true", help="skip local path existence checks during validation")
    args = parser.parse_args()

    if args.template:
        write_template(Path(args.template))
        print(f"wrote {args.template}")
        return 0
    if not args.instances:
        parser.error("--instances is required unless --template is used")
    if not args.output and not args.validate_only:
        parser.error("--output is required unless --validate-only is used")

    instances = selected_records(load_instances(Path(args.instances)), args.start, args.limit)
    errors = validate_instances(instances, check_paths=not args.no_check_paths)
    if errors:
        for error in errors:
            print(f"invalid: {error}")
        return 2
    if args.validate_only:
        print(f"valid instances={len(instances)}")
        return 0

    run_root = Path(args.run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    solver = CodexCliSolver(codex_bin=args.codex_bin)
    new_items = [
        item_result(instance, index, run_root, solver, args.timeout, args.verifier_timeout, args.dry_run, args.in_place)
        for index, instance in enumerate(instances, start=args.start - 1)
    ]
    items = merge_items(args.merge, new_items)
    write_output(Path(args.output), args, items)
    score, sample_size, _duration_s = summarize(items)
    print(f"score={score} sample_size={sample_size} wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
