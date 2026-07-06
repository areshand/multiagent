#!/usr/bin/env python3
"""Run the next missing SWE Bench Pro official-order shard.

This is a thin orchestration wrapper around:

* ``evaluation.swe_bench_pro_official_aggregate`` to find missing indices.
* ``evaluation.openai_codex_proxy`` when using the local Codex-backed model
  endpoint.
* ``evaluation.swe_bench_pro_scaffold_parity`` for the actual EvalScope run.

Use ``--dry-run`` to print the exact command without running Docker/EvalScope.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = Path("evaluation/reports")
DEFAULT_AGGREGATE_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.json"
DEFAULT_AGGREGATE_MD = DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.md"
DEFAULT_SCAFFOLD_AUDIT_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-audit.json"
DEFAULT_SCAFFOLD_AUDIT_MD = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-audit.md"
DEFAULT_WORK_ROOT = Path("/private/tmp")
DEFAULT_NATIVE_SOLVER_COMMAND = "/tmp/evalscope-native-multiagent-solver.sh"
DEFAULT_NATIVE_SOLVER_SOURCE = Path(__file__).resolve().parents[1]


def run_checked(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_aggregate(args: argparse.Namespace, *, cwd: Path) -> None:
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_official_aggregate",
        "--json",
        str(args.aggregate_json),
        "--report",
        str(args.aggregate_report),
        "--suggest-shard-size",
        str(args.shard_size),
        "--framework",
        args.agent_framework,
    ]
    if args.agent_framework not in {"codex", "codex-devnull"}:
        cmd.append("--allow-non-codex")
    if args.aggregate_reports:
        cmd.extend(["--reports", *args.aggregate_reports])
    run_checked(cmd, cwd=cwd)


def build_scaffold_audit_command(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_scaffold_audit",
        "--json",
        str(args.scaffold_audit_json),
        "--report",
        str(args.scaffold_audit_report),
    ]


def run_scaffold_audit(args: argparse.Namespace, *, cwd: Path) -> None:
    cmd = build_scaffold_audit_command(args)
    run_checked(cmd, cwd=cwd)


def wait_for_proxy(host: str, port: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    url = f"http://{host}:{port}/v1/models"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"proxy did not become ready at {url}: {last_error!r}")


def build_scaffold_command(args: argparse.Namespace, *, offset: int, count: int) -> tuple[str, list[str]]:
    if args.proxy_mode != "codex":
        default_prefix_kind = f"{args.proxy_mode}-cwd"
    elif args.agent_framework == "codex-devnull":
        default_prefix_kind = "codex-cwd"
    else:
        default_prefix_kind = f"{args.agent_framework}-cwd"
    prefix = args.report_prefix or f"swe-bench-pro-{default_prefix_kind}-offset{offset}-count{count}"
    work_dir = args.work_dir or (args.work_root / prefix)
    report_dir = args.report_dir
    api_url = args.api_url or f"http://{args.proxy_host}:{args.proxy_port}/v1"
    cmd = [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_scaffold_parity",
        "--work-dir",
        str(work_dir),
        "--output",
        str(report_dir / f"{prefix}.json"),
        "--config-json",
        str(report_dir / f"{prefix}-config.json"),
        "--config-yaml",
        str(report_dir / f"{prefix}-task-config.yaml"),
        "--preflight-output",
        str(report_dir / f"{prefix}-preflight.json"),
        "--on-demand-image-status",
        str(report_dir / f"{prefix}-on-demand-image-status.json"),
        "--report-prefix",
        prefix,
        "--swe-bench-pro-repo-path",
        str(args.swe_bench_pro_repo_path),
        "--sample-offset",
        str(offset),
        "--sample-count",
        str(count),
        "--agent-framework",
        args.agent_framework,
        "--agent-model-name",
        args.agent_model_name,
        "--max-steps",
        str(args.max_steps),
        "--agent-timeout",
        str(args.agent_timeout),
        "--agent-wire-api",
        "responses",
        "--on-demand-image-preload",
        "--on-demand-prune-after-sample",
        "--on-demand-min-free-gb",
        str(args.on_demand_min_free_gb),
        "--api-url",
        api_url,
    ]
    if args.memory_limit:
        cmd.extend(["--memory-limit", args.memory_limit])
    if args.cpu_limit:
        cmd.extend(["--cpu-limit", args.cpu_limit])
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
    if args.score_failed_native_diff:
        cmd.append("--score-failed-native-diff")
    if args.score_timed_out_native_diff:
        cmd.append("--score-timed-out-native-diff")
    if args.persistent_cache:
        cmd.extend(
            [
                "--persistent-cache",
                "--persistent-cache-root",
                str(args.persistent_cache_root),
                "--persistent-cache-mode",
                args.persistent_cache_mode,
            ]
        )
    if args.evalscope_path is not None:
        cmd.extend(["--evalscope-path", str(args.evalscope_path)])
    if args.responses_keepalive:
        cmd.extend(
            [
                "--responses-keepalive",
                "--responses-keepalive-interval",
                str(args.responses_keepalive_interval),
            ]
        )
    if args.ignore_errors:
        cmd.append("--ignore-errors")
    if args.no_auto_install:
        cmd.append("--no-auto-install")
    if args.no_docker_inspect:
        cmd.append("--no-docker-inspect")
    return prefix, cmd


def start_proxy(args: argparse.Namespace, *, cwd: Path) -> subprocess.Popen[str]:
    cmd = [
        sys.executable,
        "-m",
        "evaluation.openai_codex_proxy",
        "--host",
        args.proxy_host,
        "--port",
        str(args.proxy_port),
        "--timeout",
        str(args.proxy_timeout),
        "--codex-bin",
        args.codex_bin,
        "--proxy-mode",
        args.proxy_mode,
        "--quiet",
    ]
    return subprocess.Popen(cmd, cwd=str(cwd), text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate-json", type=Path, default=DEFAULT_AGGREGATE_JSON)
    parser.add_argument("--aggregate-report", type=Path, default=DEFAULT_AGGREGATE_MD)
    parser.add_argument("--aggregate-reports", nargs="*", help="optional report patterns forwarded to the aggregator")
    parser.add_argument("--scaffold-audit-json", type=Path, default=DEFAULT_SCAFFOLD_AUDIT_JSON)
    parser.add_argument("--scaffold-audit-report", type=Path, default=DEFAULT_SCAFFOLD_AUDIT_MD)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--report-prefix", default="")
    parser.add_argument("--shard-size", type=int, default=10)
    parser.add_argument("--sample-offset", type=int)
    parser.add_argument("--sample-count", type=int)
    parser.add_argument("--evalscope-path", type=Path)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=Path("/private/tmp/SWE-bench_Pro-os-complete"))
    parser.add_argument("--max-steps", type=int, default=250)
    parser.add_argument("--agent-timeout", type=float, default=3600.0)
    parser.add_argument("--memory-limit", default="")
    parser.add_argument("--cpu-limit", default="")
    parser.add_argument("--responses-keepalive", action="store_true")
    parser.add_argument("--responses-keepalive-interval", type=float, default=10.0)
    parser.add_argument("--on-demand-min-free-gb", type=float, default=50.0)
    parser.add_argument("--agent-framework", default="codex-devnull", choices=["codex-devnull", "codex", "noop", "multiagent-native"])
    parser.add_argument("--agent-model-name", default="gpt-5")
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
    parser.add_argument("--api-url", default="")
    parser.add_argument("--start-proxy", action="store_true", default=True)
    parser.add_argument("--no-start-proxy", action="store_false", dest="start_proxy")
    parser.add_argument("--proxy-host", default="127.0.0.1")
    parser.add_argument("--proxy-port", type=int, default=8765)
    parser.add_argument("--proxy-timeout", type=int, default=900)
    parser.add_argument("--proxy-mode", choices=["codex", "scaffold-probe"], default="codex")
    parser.add_argument("--proxy-ready-timeout", type=float, default=15.0)
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--ignore-errors", action="store_true")
    parser.add_argument("--no-auto-install", action="store_true")
    parser.add_argument("--no-docker-inspect", action="store_true")
    parser.add_argument("--no-refresh-before", action="store_true")
    parser.add_argument("--no-refresh-after", action="store_true")
    parser.add_argument(
        "--skip-scaffold-audit",
        action="store_true",
        help="skip the scaffold parity audit gate before a non-probe shard run",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cwd = Path.cwd()
    explicit_shard = args.sample_offset is not None and args.sample_count is not None
    if not args.no_refresh_before:
        refresh_aggregate(args, cwd=cwd)
    if explicit_shard:
        offset = args.sample_offset
        count = args.sample_count
    else:
        aggregate = load_json(args.aggregate_json)
        suggested = aggregate.get("suggested_next_shard") or {}
        offset = args.sample_offset if args.sample_offset is not None else suggested.get("sample_offset")
        count = args.sample_count if args.sample_count is not None else suggested.get("sample_count")
    if offset is None or count is None:
        raise SystemExit("no missing shard found; aggregate appears complete")
    offset = int(offset)
    count = int(count)
    prefix, scaffold_cmd = build_scaffold_command(args, offset=offset, count=count)

    print(f"next shard: offset={offset} count={count} prefix={prefix}")
    should_run_scaffold_audit = not args.skip_scaffold_audit and args.proxy_mode != "scaffold-probe"
    if should_run_scaffold_audit:
        print("scaffold audit command:")
        print(shlex.join(build_scaffold_audit_command(args)))
    print("scaffold command:")
    print(shlex.join(scaffold_cmd))
    if args.dry_run:
        return 0
    if should_run_scaffold_audit:
        run_scaffold_audit(args, cwd=cwd)

    proxy: subprocess.Popen[str] | None = None
    try:
        if args.start_proxy:
            proxy = start_proxy(args, cwd=cwd)
            wait_for_proxy(args.proxy_host, args.proxy_port, args.proxy_ready_timeout)
        run_checked(scaffold_cmd, cwd=cwd)
        if not args.no_refresh_after:
            refresh_aggregate(args, cwd=cwd)
    finally:
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            try:
                proxy.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
