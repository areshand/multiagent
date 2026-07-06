#!/usr/bin/env python3
"""Run multiple independent SWE Bench Pro official-order shards concurrently."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = Path("evaluation/reports")
DEFAULT_AGGREGATE_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.json"
DEFAULT_NATIVE_SOLVER_COMMAND = "/tmp/evalscope-native-multiagent-solver.sh"
DEFAULT_NATIVE_SOLVER_SOURCE = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def run_checked(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def refresh_aggregate(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_official_aggregate",
        "--json",
        str(args.aggregate_json),
        "--report",
        str(DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.md"),
        "--suggest-shard-size",
        str(args.shard_size),
        "--framework",
        args.agent_framework,
    ]
    if args.agent_framework not in {"codex", "codex-devnull"}:
        cmd.append("--allow-non-codex")
    if args.aggregate_reports:
        reports: list[str] = []
        for raw in args.aggregate_reports:
            reports.extend(part for part in raw.split(",") if part)
        cmd.extend(["--reports", *reports])
    run_checked(cmd)


def build_worker_command(args: argparse.Namespace, *, offset: int, count: int, worker_index: int) -> list[str]:
    proxy_port = args.proxy_port_base + worker_index
    prefix = args.report_prefix_template.format(
        offset=offset,
        count=count,
        worker=worker_index,
        framework=args.agent_framework,
    )
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_run_next_shard",
        "--no-refresh-before",
        "--no-refresh-after",
        "--skip-scaffold-audit",
        "--sample-offset",
        str(offset),
        "--sample-count",
        str(count),
        "--shard-size",
        str(args.shard_size),
        "--report-prefix",
        prefix,
        "--proxy-port",
        str(proxy_port),
        "--api-url",
        f"http://127.0.0.1:{proxy_port}/v1",
        "--proxy-timeout",
        str(args.proxy_timeout),
        "--proxy-ready-timeout",
        str(args.proxy_ready_timeout),
        "--agent-framework",
        args.agent_framework,
        "--agent-model-name",
        args.agent_model_name,
        "--max-steps",
        str(args.max_steps),
        "--agent-timeout",
        str(args.agent_timeout),
        "--on-demand-min-free-gb",
        str(args.on_demand_min_free_gb),
        "--swe-bench-pro-repo-path",
        str(args.swe_bench_pro_repo_path),
    ]
    if args.memory_limit:
        cmd.extend(["--memory-limit", args.memory_limit])
    if args.cpu_limit:
        cmd.extend(["--cpu-limit", args.cpu_limit])
    if args.evalscope_path:
        cmd.extend(["--evalscope-path", str(args.evalscope_path)])
    if args.agent_framework == "multiagent-native" and args.native_solver_command:
        cmd.extend(["--native-solver-command", args.native_solver_command])
    if args.native_solver_setup_command:
        cmd.extend(["--native-solver-setup-command", args.native_solver_setup_command])
    if args.agent_framework == "multiagent-native" and args.bake_native_solver:
        cmd.extend(["--bake-native-solver", "--native-solver-source", str(args.native_solver_source)])
    if args.agent_framework == "multiagent-native" and args.native_codex_auth_json:
        cmd.extend(
            [
                "--native-codex-auth-json",
                str(args.native_codex_auth_json),
                "--native-codex-auth-container-home",
                args.native_codex_auth_container_home,
            ]
        )
    if getattr(args, "score_failed_native_diff", False):
        cmd.append("--score-failed-native-diff")
    if getattr(args, "score_timed_out_native_diff", False):
        cmd.append("--score-timed-out-native-diff")
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
    if args.responses_keepalive:
        cmd.append("--responses-keepalive")
    if args.no_start_proxy:
        cmd.append("--no-start-proxy")
    if args.ignore_errors:
        cmd.append("--ignore-errors")
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate-json", type=Path, default=DEFAULT_AGGREGATE_JSON)
    parser.add_argument("--aggregate-reports", nargs="*", help="optional report patterns forwarded to the aggregator")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--shard-size", type=int, default=1)
    parser.add_argument("--sample-offset", type=int, help="first official index; default uses aggregate first missing")
    parser.add_argument("--evalscope-path", type=Path)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=Path("/private/tmp/SWE-bench_Pro-os-complete"))
    parser.add_argument("--agent-framework", default="multiagent-native", choices=["multiagent-native", "codex-devnull", "codex", "noop"])
    parser.add_argument("--agent-model-name", default="gpt-5")
    parser.add_argument("--max-steps", type=int, default=250)
    parser.add_argument("--agent-timeout", type=float, default=3600.0)
    parser.add_argument("--memory-limit", default="")
    parser.add_argument("--cpu-limit", default="")
    parser.add_argument("--on-demand-min-free-gb", type=float, default=50.0)
    parser.add_argument("--native-solver-command", default=DEFAULT_NATIVE_SOLVER_COMMAND)
    parser.add_argument("--native-solver-setup-command", default="")
    parser.add_argument("--bake-native-solver", action="store_true", default=True)
    parser.add_argument("--no-bake-native-solver", action="store_false", dest="bake_native_solver")
    parser.add_argument("--native-solver-source", type=Path, default=DEFAULT_NATIVE_SOLVER_SOURCE)
    parser.add_argument("--native-codex-auth-json", default="")
    parser.add_argument("--native-codex-auth-container-home", default="/root/.codex-multiagent-prod")
    parser.add_argument("--score-failed-native-diff", action="store_true")
    parser.add_argument("--score-timed-out-native-diff", action="store_true")
    parser.add_argument("--persistent-cache", action="store_true")
    parser.add_argument("--persistent-cache-root", type=Path, default=Path("/private/tmp/swe-bench-pro-persistent-cache"))
    parser.add_argument("--persistent-cache-mode", default="rw", choices=["rw", "ro"])
    parser.add_argument("--responses-keepalive", action="store_true")
    parser.add_argument("--ignore-errors", action="store_true")
    parser.add_argument("--no-refresh-before", action="store_true")
    parser.add_argument("--no-refresh-after", action="store_true")
    parser.add_argument("--proxy-port-base", type=int, default=8765)
    parser.add_argument("--proxy-timeout", type=int, default=1800)
    parser.add_argument("--proxy-ready-timeout", type=float, default=30.0)
    parser.add_argument("--no-start-proxy", action="store_true")
    parser.add_argument(
        "--report-prefix-template",
        default="swe-bench-pro-{framework}-offset{offset}-count{count}",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if args.shard_size < 1:
        parser.error("--shard-size must be >= 1")

    first_offset = args.sample_offset
    if not args.no_refresh_before:
        refresh_aggregate(args)
    if first_offset is None:
        aggregate = load_json(args.aggregate_json)
        suggested = aggregate.get("suggested_next_shard") or {}
        first_offset = int(suggested.get("sample_offset", aggregate.get("first_missing_index", 0)))

    commands = [
        build_worker_command(
            args,
            offset=int(first_offset) + worker_index * args.shard_size,
            count=args.shard_size,
            worker_index=worker_index,
        )
        for worker_index in range(args.workers)
    ]
    for command in commands:
        print(shlex.join(command))
    if args.dry_run:
        return 0

    procs = [subprocess.Popen(command) for command in commands]
    codes = [proc.wait() for proc in procs]
    if not args.no_refresh_after:
        refresh_aggregate(args)
    if any(code != 0 for code in codes):
        print(f"parallel shard failures: {codes}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
