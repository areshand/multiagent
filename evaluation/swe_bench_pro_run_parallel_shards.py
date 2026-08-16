#!/usr/bin/env python3
"""Run independent production-multiagent SWE Bench Pro shards concurrently."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from evaluation.swe_bench_pro_official_aggregate import DEFAULT_REPORT_PATTERNS


DEFAULT_REPORT_DIR = Path("evaluation/reports")
DEFAULT_AGGREGATE_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.json"
DEFAULT_NATIVE_SOLVER_SOURCE = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_checked(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def parse_sample_offsets(raw: str) -> list[int]:
    offsets = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if any(offset < 0 for offset in offsets):
        raise ValueError("--sample-offsets entries must be >= 0")
    return offsets


def refresh_aggregate(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_official_aggregate",
        "--json",
        str(args.aggregate_json),
        "--report",
        str(args.report_dir / "swe-bench-pro-official-aggregate.md"),
        "--report-dir",
        str(args.report_dir),
        "--suggest-shard-size",
        str(args.shard_size),
        "--swe-bench-pro-repo-path",
        str(args.swe_bench_pro_repo_path),
    ]
    if args.aggregate_reports:
        reports = [part for raw in args.aggregate_reports for part in raw.split(",") if part]
        cmd.extend(["--reports", *reports])
    else:
        # Custom report-prefix templates are common for named or commit-specific
        # runs. Keep aggregation independent of the prefix while sidecar JSON is
        # filtered by the aggregate command.
        cmd.extend(["--reports", *DEFAULT_REPORT_PATTERNS])
    run_checked(cmd)


def build_worker_command(
    args: argparse.Namespace,
    *,
    offset: int,
    count: int,
    worker_index: int,
) -> list[str]:
    prefix = args.report_prefix_template.format(offset=offset, count=count, worker=worker_index)
    work_dir = args.work_root / prefix
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro",
        "--work-dir",
        str(work_dir),
        "--output",
        str(args.report_dir / f"{prefix}.json"),
        "--config-json",
        str(args.report_dir / f"{prefix}-config.json"),
        "--config-yaml",
        str(args.report_dir / f"{prefix}-task-config.yaml"),
        "--preflight-output",
        str(args.report_dir / f"{prefix}-preflight.json"),
        "--on-demand-image-status",
        str(args.report_dir / f"{prefix}-on-demand-image-status.json"),
        "--report-prefix",
        prefix,
        "--sample-offset",
        str(offset),
        "--sample-count",
        str(count),
        "--swe-bench-pro-repo-path",
        str(args.swe_bench_pro_repo_path),
        "--agent-model-name",
        args.agent_model_name,
        "--max-steps",
        str(args.max_steps),
        "--agent-timeout",
        str(args.agent_timeout),
        "--native-solver-source",
        str(args.native_solver_source),
        "--native-codex-auth-json",
        str(args.native_codex_auth_json),
        "--native-codex-auth-container-home",
        args.native_codex_auth_container_home,
        "--native-trace-dir",
        str(args.native_trace_dir),
        "--on-demand-min-free-gb",
        str(args.on_demand_min_free_gb),
        "--no-docker-inspect",
    ]
    if args.evalscope_path:
        cmd.extend(["--evalscope-path", str(args.evalscope_path)])
    if args.memory_limit:
        cmd.extend(["--memory-limit", args.memory_limit])
    if args.cpu_limit:
        cmd.extend(["--cpu-limit", args.cpu_limit])
    if args.persistent_cache:
        cache_root = args.persistent_cache_root
        if args.persistent_cache_mode == "rw" and args.workers > 1:
            cache_root = cache_root / f"worker-{worker_index}"
        cmd.extend(
            [
                "--persistent-cache",
                "--persistent-cache-root",
                str(cache_root),
                "--persistent-cache-mode",
                args.persistent_cache_mode,
            ]
        )
    if args.ignore_errors:
        cmd.append("--ignore-errors")
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate-json", type=Path, default=DEFAULT_AGGREGATE_JSON)
    parser.add_argument("--aggregate-reports", nargs="*")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--work-root", type=Path, default=Path("/private/tmp"))
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--shard-size", type=int, default=1)
    parser.add_argument("--sample-offset", type=int)
    parser.add_argument("--sample-offsets")
    parser.add_argument("--evalscope-path", type=Path)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=Path("/private/tmp/SWE-bench_Pro-os-complete"))
    parser.add_argument("--agent-model-name", default="gpt-5")
    parser.add_argument("--max-steps", type=int, default=250)
    parser.add_argument("--agent-timeout", type=float, default=3600.0)
    parser.add_argument("--memory-limit", default="")
    parser.add_argument("--cpu-limit", default="")
    parser.add_argument("--on-demand-min-free-gb", type=float, default=50.0)
    parser.add_argument("--native-solver-source", type=Path, default=DEFAULT_NATIVE_SOLVER_SOURCE)
    parser.add_argument("--native-codex-auth-json", type=Path, required=True)
    parser.add_argument(
        "--native-codex-auth-container-home",
        default="/tmp/multiagent-prod-swe/codex-home",
    )
    parser.add_argument(
        "--native-trace-dir",
        type=Path,
        help="shared host directory for per-official-row multiagent trace archives; defaults to REPORT_DIR/traces",
    )
    parser.add_argument("--persistent-cache", action="store_true")
    parser.add_argument("--persistent-cache-root", type=Path, default=Path("/private/tmp/swe-bench-pro-persistent-cache"))
    parser.add_argument("--persistent-cache-mode", default="rw", choices=["rw", "ro"])
    parser.add_argument("--ignore-errors", action="store_true")
    parser.add_argument("--no-refresh-before", action="store_true")
    parser.add_argument("--no-refresh-after", action="store_true")
    parser.add_argument("--report-prefix-template", default="swe-bench-pro-production-w{worker}-offset{offset}-count{count}")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.native_trace_dir is None:
        args.native_trace_dir = args.report_dir / "traces"

    if args.workers < 1 or args.shard_size < 1:
        parser.error("--workers and --shard-size must be >= 1")
    explicit_offsets = parse_sample_offsets(args.sample_offsets or "")
    if explicit_offsets and args.sample_offset is not None:
        parser.error("--sample-offset and --sample-offsets are mutually exclusive")
    if explicit_offsets and len(explicit_offsets) > args.workers:
        parser.error("--sample-offsets cannot contain more entries than --workers")
    if not args.no_refresh_before:
        refresh_aggregate(args)

    first_offset = args.sample_offset
    if explicit_offsets:
        worker_offsets = explicit_offsets
    else:
        if first_offset is None:
            aggregate = load_json(args.aggregate_json)
            suggested = aggregate.get("suggested_next_shard") or {}
            first_offset = int(suggested.get("sample_offset", aggregate.get("first_missing_index", 0)))
        worker_offsets = [int(first_offset) + index * args.shard_size for index in range(args.workers)]

    commands = [
        build_worker_command(args, offset=offset, count=args.shard_size, worker_index=index)
        for index, offset in enumerate(worker_offsets)
    ]
    for command in commands:
        print(shlex.join(command))
    if args.dry_run:
        return 0

    processes = [subprocess.Popen(command) for command in commands]
    codes = [process.wait() for process in processes]
    if any(code != 0 for code in codes):
        print(f"parallel shard failures: {codes}", file=sys.stderr)
        return 1
    if not args.no_refresh_after:
        refresh_aggregate(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
