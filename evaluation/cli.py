#!/usr/bin/env python3
"""CLI for multiagent evaluation adapters."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from evaluation.adapters import adapter_names, load_adapter
from evaluation.core import (
    arm_choices,
    default_arms,
    die,
    parse_csv,
    print_summary,
    reference_report,
    rescore,
    run_matrix,
    selftest_adapter,
    write_json_report,
    write_markdown_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run multiagent evaluation adapters")
    parser.add_argument("--list", action="store_true", help="list available adapters and exit")
    parser.add_argument("--adapter", default="ponytail", help="evaluation adapter name")
    parser.add_argument("--selftest", action="store_true", help="validate adapter scorers without an agent call")
    parser.add_argument("--reference-report", action="store_true", help="score built-in references and write reports without an agent call")
    parser.add_argument("--reference-kind", default="good,bad", help="comma-separated reference kinds: good,bad")
    parser.add_argument("--rescore", help="recompute metrics from a saved run directory")
    parser.add_argument("--task", help="comma-separated task IDs; default is all adapter tasks")
    parser.add_argument("--arms", help="comma-separated instruction arms; defaults depend on the adapter")
    parser.add_argument("--agent-cli", choices=["claude", "codex"], default=os.environ.get("MULTIAGENT_EVAL_AGENT", "claude"))
    parser.add_argument("--model", default=os.environ.get("MULTIAGENT_EVAL_MODEL", "claude-sonnet-4-6"))
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--run-root", help="directory for new run outputs; default evaluation/runs")
    args = parser.parse_args()

    if args.list:
        for name in adapter_names():
            adapter = load_adapter(name)
            print(f"{adapter.name}\t{adapter.description}")
        return 0

    try:
        adapter = load_adapter(args.adapter)
    except KeyError:
        die(f"unknown adapter: {args.adapter}; expected one of {', '.join(adapter_names())}")

    if (
        args.agent_cli == "codex"
        and "MULTIAGENT_EVAL_MODEL" not in os.environ
        and args.model == "claude-sonnet-4-6"
    ):
        args.model = ""

    if args.selftest:
        return 1 if selftest_adapter(adapter) else 0

    if args.rescore:
        run_dir = Path(args.rescore)
        results = rescore(adapter, run_dir)
        json_path = write_json_report(run_dir, adapter, results)
        md_path = write_markdown_report(run_dir, adapter, results)
        print_summary(results)
        print(f"\nwrote {json_path}")
        print(f"wrote {md_path}")
        return 0

    tasks = parse_csv(args.task or ",".join(adapter.tasks), adapter.tasks)
    run_root = Path(args.run_root) if args.run_root else None

    if args.reference_report:
        kinds = parse_csv(args.reference_kind, {"good", "bad"})
        run_dir, results = reference_report(
            adapter,
            tasks,
            kinds,
            **({"runs_root": run_root} if run_root else {}),
        )
        print_summary(results)
        print(f"\nwrote {run_dir / 'results.json'}")
        print(f"wrote {run_dir / 'report.md'}")
        return 0

    arms = parse_csv(args.arms or default_arms(adapter), arm_choices(adapter))
    if args.runs < 1:
        die("--runs must be positive")
    if args.workers < 1:
        die("--workers must be positive")

    if selftest_adapter(adapter):
        die("selftest failed; refusing live run")

    run_dir, results = run_matrix(
        adapter=adapter,
        tasks=tasks,
        arms=arms,
        model=args.model,
        runs=args.runs,
        workers=args.workers,
        timeout=args.timeout,
        agent_cli=args.agent_cli,
        **({"runs_root": run_root} if run_root else {}),
    )
    json_path = write_json_report(run_dir, adapter, results)
    md_path = write_markdown_report(run_dir, adapter, results)
    print_summary(results)
    print(f"\nwrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
