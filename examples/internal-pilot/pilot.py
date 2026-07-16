#!/usr/bin/env python3
"""Run paired baseline/orchestrated cells against pinned real tasks."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


HARNESS_ROOT = Path(__file__).resolve().parents[2]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
PLACEHOLDER = re.compile(r"REPLACE|PLACEHOLDER|<[^>]+>", re.IGNORECASE)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def git(args: Sequence[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), text=True, capture_output=True, check=check
    )


def validate_command(item: Any, where: str, errors: List[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{where} must be an object")
        return
    name = item.get("name")
    command = item.get("command")
    expected = item.get("expect_exit")
    timeout = item.get("timeout_seconds", 300)
    if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
        errors.append(f"{where}.name must contain only letters, digits, '.', '_', or '-'")
    if not isinstance(command, str) or not command.strip() or PLACEHOLDER.search(command):
        errors.append(f"{where}.command must be a non-placeholder command")
    if not isinstance(expected, int) or isinstance(expected, bool) or not 0 <= expected <= 255:
        errors.append(f"{where}.expect_exit must be an integer from 0 to 255")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout < 1:
        errors.append(f"{where}.timeout_seconds must be a positive integer")


def validate_manifest(manifest: Any, manifest_path: Path) -> List[str]:
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]
    if manifest.get("template_only") is not False and "template_only" in manifest:
        errors.append("template_only must be removed or set to false before execution")

    pilot_id = manifest.get("pilot_id")
    if (
        not isinstance(pilot_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", pilot_id)
        or PLACEHOLDER.search(pilot_id)
    ):
        errors.append("pilot_id must be a 3-64 character lowercase identifier")

    harness_commit = manifest.get("harness_commit")
    if not isinstance(harness_commit, str) or not SHA40.fullmatch(harness_commit):
        errors.append("harness_commit must be a full lowercase 40-character Git SHA")

    arms = manifest.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"baseline", "orchestrated"}:
        errors.append("arms must define exactly baseline and orchestrated")
    else:
        for arm_name in ("baseline", "orchestrated"):
            arm = arms.get(arm_name)
            driver = arm.get("driver") if isinstance(arm, dict) else None
            if (
                not isinstance(driver, list)
                or not driver
                or any(not isinstance(value, str) or not value for value in driver)
            ):
                errors.append(f"arms.{arm_name}.driver must be a nonempty argv array")

    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not 5 <= len(tasks) <= 10:
        errors.append("tasks must contain 5-10 real tasks")
        return errors

    seen_ids = set()
    for index, task in enumerate(tasks):
        where = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{where} must be an object")
            continue
        task_id = task.get("id")
        if (
            not isinstance(task_id, str)
            or not IDENTIFIER.fullmatch(task_id)
            or PLACEHOLDER.search(task_id)
        ):
            errors.append(f"{where}.id must be a non-placeholder identifier")
        elif task_id in seen_ids:
            errors.append(f"{where}.id is duplicated: {task_id}")
        else:
            seen_ids.add(task_id)

        for field in ("team", "issue_url", "issue_file", "repository"):
            value = task.get(field)
            if not isinstance(value, str) or not value.strip() or PLACEHOLDER.search(value):
                errors.append(f"{where}.{field} must be a non-placeholder string")

        issue_file = task.get("issue_file")
        if isinstance(issue_file, str) and not PLACEHOLDER.search(issue_file):
            issue_path = (manifest_path.parent / issue_file).resolve()
            if not issue_path.is_file():
                errors.append(f"{where}.issue_file does not exist: {issue_path}")

        base_commit = task.get("base_commit")
        if not isinstance(base_commit, str) or not SHA40.fullmatch(base_commit):
            errors.append(f"{where}.base_commit must be a full lowercase 40-character Git SHA")

        solver_timeout = task.get("solver_timeout_seconds")
        if not isinstance(solver_timeout, int) or isinstance(solver_timeout, bool) or solver_timeout < 60:
            errors.append(f"{where}.solver_timeout_seconds must be an integer >= 60")

        for phase in ("preflight", "validation"):
            commands = task.get(phase)
            if not isinstance(commands, list) or not commands:
                errors.append(f"{where}.{phase} must contain at least one command")
            else:
                names = set()
                for command_index, command in enumerate(commands):
                    validate_command(command, f"{where}.{phase}[{command_index}]", errors)
                    if isinstance(command, dict) and isinstance(command.get("name"), str):
                        if command["name"] in names:
                            errors.append(f"{where}.{phase} has duplicate command name {command['name']}")
                        names.add(command["name"])

        criteria = task.get("acceptance_criteria")
        if (
            not isinstance(criteria, list)
            or not criteria
            or any(
                not isinstance(value, str) or not value.strip() or PLACEHOLDER.search(value)
                for value in criteria
            )
        ):
            errors.append(f"{where}.acceptance_criteria must contain objective non-placeholder text")
    return errors


def load_and_validate(manifest_path: Path) -> Dict[str, Any]:
    try:
        manifest = read_json(manifest_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read manifest: {exc}") from exc
    errors = validate_manifest(manifest, manifest_path)
    if errors:
        raise ValueError("invalid manifest:\n- " + "\n- ".join(errors))
    return manifest


def decode_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_process(
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    started = time.monotonic()
    timed_out = False
    exit_code: Optional[int]
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = decode_timeout_output(exc.stdout)
        stderr = decode_timeout_output(exc.stderr)
        stderr += f"\npilot runner timed out after {timeout_seconds}s\n"
        exit_code = None
        timed_out = True
    duration = round(time.monotonic() - started, 3)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return {
        "argv": list(argv),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "duration_seconds": duration,
        "stdout_log": stdout_path.name,
        "stderr_log": stderr_path.name,
    }


def run_commands(
    commands: List[Dict[str, Any]], phase: str, worktree: Path, cell_dir: Path
) -> List[Dict[str, Any]]:
    results = []
    for item in commands:
        stdout_path = cell_dir / f"{phase}-{item['name']}.stdout.log"
        stderr_path = cell_dir / f"{phase}-{item['name']}.stderr.log"
        result = run_process(
            ["/bin/bash", "-lc", item["command"]],
            worktree,
            item.get("timeout_seconds", 300),
            stdout_path,
            stderr_path,
        )
        result.update(
            {
                "name": item["name"],
                "command": item["command"],
                "expected_exit": item["expect_exit"],
                "passed": not result["timed_out"] and result["exit_code"] == item["expect_exit"],
            }
        )
        results.append(result)
    return results


def resolve_driver(argv: List[str]) -> List[str]:
    resolved = list(argv)
    first = Path(resolved[0])
    if not first.is_absolute() and "/" in resolved[0]:
        resolved[0] = str((HARNESS_ROOT / first).resolve())
    return resolved


def resolve_repository(value: str, manifest_dir: Path) -> str:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        relative = (manifest_dir / candidate).resolve()
        if relative.exists():
            return str(relative)
    if candidate.is_absolute():
        return str(candidate.resolve())
    return value


def capture_patch(worktree: Path, base_commit: str, cell_dir: Path) -> Tuple[str, str, int]:
    git(["add", "-N", "--", "."], worktree)
    status = git(["status", "--porcelain=v1"], worktree).stdout
    patch = git(["diff", "--binary", base_commit, "--", "."], worktree).stdout
    (cell_dir / "git-status.txt").write_text(status, encoding="utf-8")
    (cell_dir / "change.patch").write_text(patch, encoding="utf-8")
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    changed_lines = sum(
        1 for line in patch.splitlines() if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---"))
    )
    return status, digest, changed_lines


def review_template(task: Dict[str, Any], evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "task_id": task["id"],
        "arm": evidence["arm"],
        "reviewer": "",
        "reviewed_at": "",
        "outcome": "pending",
        "failure_category": "",
        "acceptance_criteria": task["acceptance_criteria"],
        "notes": "",
    }


def run_cell(
    manifest: Dict[str, Any],
    manifest_path: Path,
    task: Dict[str, Any],
    arm: str,
    run_dir: Path,
) -> Dict[str, Any]:
    cell_dir = run_dir / "cells" / f"{task['id']}--{arm}"
    cell_dir.mkdir(parents=True)
    worktree = cell_dir / "worktree"
    started_at = utc_now()
    started = time.monotonic()

    repository = resolve_repository(task["repository"], manifest_path.parent)
    clone_result = run_process(
        ["git", "clone", "--no-hardlinks", "--quiet", repository, str(worktree)],
        HARNESS_ROOT,
        600,
        cell_dir / "clone.stdout.log",
        cell_dir / "clone.stderr.log",
    )
    if clone_result["exit_code"] != 0:
        raise RuntimeError(f"clone failed for {task['id']} {arm}; see {cell_dir / 'clone.stderr.log'}")
    checkout = run_process(
        ["git", "checkout", "--detach", "--quiet", task["base_commit"]],
        worktree,
        120,
        cell_dir / "checkout.stdout.log",
        cell_dir / "checkout.stderr.log",
    )
    if checkout["exit_code"] != 0:
        raise RuntimeError(f"checkout failed for {task['id']} {arm}; see {cell_dir / 'checkout.stderr.log'}")

    observed_base = git(["rev-parse", "HEAD"], worktree).stdout.strip()
    if observed_base != task["base_commit"]:
        raise RuntimeError(f"observed base {observed_base} does not match {task['base_commit']}")
    issue_source = (manifest_path.parent / task["issue_file"]).resolve()
    prompt_path = cell_dir / "task.md"
    shutil.copyfile(str(issue_source), str(prompt_path))

    preflight = run_commands(task["preflight"], "preflight", worktree, cell_dir)
    preflight_passed = all(result["passed"] for result in preflight)
    driver_argv = resolve_driver(manifest["arms"][arm]["driver"])
    if preflight_passed:
        driver_env = os.environ.copy()
        driver_env.update(
            {
                "PILOT_HARNESS_ROOT": str(HARNESS_ROOT),
                "PILOT_WORKTREE": str(worktree),
                "PILOT_CELL_DIR": str(cell_dir),
                "PILOT_PROMPT_FILE": str(prompt_path),
                "PILOT_TASK_ID": task["id"],
                "PILOT_ARM": arm,
                "PILOT_SOLVER_TIMEOUT_SECONDS": str(task["solver_timeout_seconds"]),
            }
        )
        driver = run_process(
            driver_argv,
            HARNESS_ROOT,
            task["solver_timeout_seconds"] + 30,
            cell_dir / "driver.stdout.log",
            cell_dir / "driver.stderr.log",
            env=driver_env,
        )
        validation = run_commands(task["validation"], "validation", worktree, cell_dir)
    else:
        (cell_dir / "driver.stdout.log").write_text("", encoding="utf-8")
        (cell_dir / "driver.stderr.log").write_text("skipped: preflight failed\n", encoding="utf-8")
        driver = {
            "argv": driver_argv,
            "exit_code": None,
            "timed_out": False,
            "duration_seconds": 0.0,
            "stdout_log": "driver.stdout.log",
            "stderr_log": "driver.stderr.log",
        }
        validation = []

    status, diff_sha256, changed_lines = capture_patch(worktree, task["base_commit"], cell_dir)
    observed_final = git(["rev-parse", "HEAD"], worktree).stdout.strip()
    has_patch = bool(status.strip())
    validations_passed = bool(validation) and all(result["passed"] for result in validation)
    if not preflight_passed:
        mechanical_status = "invalid"
        verdict = "invalid"
    elif driver["timed_out"] or driver["exit_code"] != 0 or not has_patch or not validations_passed:
        mechanical_status = "failed"
        verdict = "failed"
    else:
        mechanical_status = "passed"
        verdict = "pending-review"

    evidence = {
        "schema_version": 1,
        "pilot_id": manifest["pilot_id"],
        "task_id": task["id"],
        "team": task["team"],
        "issue_url": task["issue_url"],
        "arm": arm,
        "harness_commit": manifest["harness_commit"],
        "target_base_commit": task["base_commit"],
        "observed_base_commit": observed_base,
        "observed_final_commit": observed_final,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "preflight": preflight,
        "driver": driver,
        "validation": validation,
        "git_status": status,
        "diff_sha256": diff_sha256,
        "changed_lines": changed_lines,
        "mechanical_status": mechanical_status,
        "human_status": "pending",
        "verdict": verdict,
    }
    write_json(cell_dir / "evidence.json", evidence)
    write_json(cell_dir / "review.template.json", review_template(task, evidence))
    return evidence


def apply_review(cell_dir: Path, evidence: Dict[str, Any]) -> Dict[str, Any]:
    review_path = cell_dir / "review.json"
    if not review_path.is_file():
        return evidence
    try:
        review = read_json(review_path)
    except (OSError, json.JSONDecodeError):
        return evidence
    outcome = review.get("outcome")
    reviewer = review.get("reviewer")
    reviewed_at = review.get("reviewed_at")
    if outcome not in ("accepted", "rejected") or not reviewer or not reviewed_at:
        return evidence
    updated = dict(evidence)
    updated["human_status"] = outcome
    if evidence["mechanical_status"] == "passed":
        updated["verdict"] = "success" if outcome == "accepted" else "rejected"
    return updated


def summarize(run_dir: Path) -> Dict[str, Any]:
    rows = []
    for evidence_path in sorted((run_dir / "cells").glob("*/evidence.json")):
        evidence = read_json(evidence_path)
        rows.append(apply_review(evidence_path.parent, evidence))
    counts = {key: sum(row["verdict"] == key for row in rows) for key in (
        "success", "rejected", "failed", "invalid", "pending-review"
    )}
    payload = {"generated_at": utc_now(), "cells": len(rows), "counts": counts, "results": rows}
    write_json(run_dir / "results.json", payload)

    lines = [
        "# Internal Pilot Report",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "| Task | Team | Arm | Base commit | Diff SHA-256 | Changed lines | Mechanical | Human | Verdict | Seconds |",
        "|---|---|---|---|---|---:|---|---|---|---:|",
    ]
    for row in rows:
        lines.append(
            "| {task_id} | {team} | {arm} | `{base}` | `{diff}` | {changed_lines} | "
            "{mechanical_status} | {human_status} | {verdict} | {duration_seconds} |".format(
                base=row["target_base_commit"][:12], diff=row["diff_sha256"][:12], **row
            )
        )
    lines.extend(
        [
            "",
            "Counts: " + ", ".join(f"{key}={value}" for key, value in counts.items()),
            "",
            "`pending-review` is not success. Inspect each cell's logs, patch, and review record before drawing conclusions.",
            "",
        ]
    )
    (run_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


def harness_provenance() -> Dict[str, Any]:
    commit = git(["rev-parse", "HEAD"], HARNESS_ROOT).stdout.strip()
    status = git(["status", "--porcelain=v1"], HARNESS_ROOT).stdout
    remote_result = git(["remote", "get-url", "origin"], HARNESS_ROOT, check=False)
    return {
        "root": str(HARNESS_ROOT),
        "commit": commit,
        "remote": remote_result.stdout.strip() if remote_result.returncode == 0 else "",
        "git_status": status,
    }


def command_validate(manifest_path: Path) -> int:
    try:
        manifest = load_and_validate(manifest_path)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"valid pilot manifest: {manifest['pilot_id']} ({len(manifest['tasks'])} tasks, 2 arms)")
    return 0


def command_run(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    manifest = load_and_validate(manifest_path)
    provenance = harness_provenance()
    if provenance["commit"] != manifest["harness_commit"]:
        raise ValueError(
            f"manifest harness_commit {manifest['harness_commit']} does not match HEAD {provenance['commit']}"
        )
    if provenance["git_status"] and not args.allow_dirty_harness:
        raise ValueError("harness checkout is dirty; commit/stash changes or use --allow-dirty-harness for a non-publishable dry run")

    run_dir = args.output.resolve()
    if run_dir == HARNESS_ROOT or HARNESS_ROOT in run_dir.parents:
        raise ValueError("output must be outside the harness checkout so evidence cannot change harness provenance")
    if run_dir.exists():
        raise ValueError(f"output already exists: {run_dir}")
    (run_dir / "cells").mkdir(parents=True)
    shutil.copyfile(str(manifest_path), str(run_dir / "manifest.snapshot.json"))
    (run_dir / "harness-status.txt").write_text(provenance["git_status"], encoding="utf-8")
    run_record = {
        "schema_version": 1,
        "pilot_id": manifest["pilot_id"],
        "started_at": utc_now(),
        "finished_at": None,
        "publishable": not bool(provenance["git_status"]),
        "harness": provenance,
        "host": {"platform": platform.platform(), "python": platform.python_version()},
        "arms": [args.arm] if args.arm else ["baseline", "orchestrated"],
    }
    write_json(run_dir / "run.json", run_record)

    arms = run_record["arms"]
    total = len(manifest["tasks"]) * len(arms)
    completed = 0
    for task in manifest["tasks"]:
        for arm in arms:
            completed += 1
            print(f"[{completed}/{total}] {task['id']} {arm}", flush=True)
            run_cell(manifest, manifest_path, task, arm, run_dir)
            summarize(run_dir)
    final_provenance = harness_provenance()
    run_record["finished_at"] = utc_now()
    run_record["harness_after"] = final_provenance
    run_record["publishable"] = bool(
        run_record["publishable"]
        and final_provenance["commit"] == provenance["commit"]
        and not final_provenance["git_status"]
    )
    write_json(run_dir / "run.json", run_record)
    payload = summarize(run_dir)
    print(f"wrote {run_dir / 'results.json'} ({payload['cells']} cells)")
    print(f"wrote {run_dir / 'report.md'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a 5-10 task manifest")
    validate_parser.add_argument("manifest", type=Path)

    run_parser = subparsers.add_parser("run", help="run isolated pilot cells")
    run_parser.add_argument("manifest", type=Path)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--arm", choices=["baseline", "orchestrated"])
    run_parser.add_argument(
        "--allow-dirty-harness",
        action="store_true",
        help="allow a non-publishable dry run while recording dirty status",
    )

    summarize_parser = subparsers.add_parser("summarize", help="regenerate results from cell evidence and reviews")
    summarize_parser.add_argument("run_dir", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate":
            return command_validate(args.manifest.resolve())
        if args.command == "run":
            return command_run(args)
        payload = summarize(args.run_dir.resolve())
        print(f"wrote {args.run_dir.resolve() / 'results.json'} ({payload['cells']} cells)")
        print(f"wrote {args.run_dir.resolve() / 'report.md'}")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"pilot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
