"""Build an authoritative comparison from saved ops-trace benchmark runs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.tasks.ops_trace import (
    OPS_TRACE_CONTRACT_VERSION,
    scenario_from_dict,
    score_ops_plan,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_path(path: Path, output: Path) -> str:
    """Keep package-internal provenance portable after the report is moved."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(output.resolve()))
    except ValueError:
        return str(resolved)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)]


def _runtime_failed(source: dict[str, Any]) -> bool:
    """Recognize adapter failures even when a usable artifact was recovered."""
    return bool(source.get("runner_error")) or "production Linux workflow exited" in str(
        source.get("reason", "")
    )


def _load_rows(
    run_dir: Path,
    arm: str,
    scenarios: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_results = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))["results"]
    metadata = {str(row["task"]): row for row in raw_results}
    rows: list[dict[str, Any]] = []
    for task_id, scenario in sorted(scenarios.items()):
        matches = list(run_dir.glob(f"{task_id}__{arm}__*/"))
        if len(matches) != 1:
            raise ValueError(f"expected one {arm} workspace for {task_id}, found {len(matches)}")
        source = metadata[task_id]
        score = score_ops_plan(matches[0], scenario)
        rows.append(
            {
                "task": task_id,
                "arm": arm,
                "model": source.get("model"),
                "risk": scenario.risk,
                "split": scenario.split,
                "cloudtrail_correlated": scenario.cloudtrail_correlated,
                "duration_ms": source.get("duration_ms"),
                "agent_count": source.get("agent_count", 1 if arm == "baseline" else None),
                "runtime": source.get("runtime"),
                "runtime_error": _runtime_failed(source),
                "runner_error": source.get("runner_error"),
                "workspace": str(matches[0]),
                **score,
            }
        )
    return rows


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    durations = [float(row["duration_ms"]) / 1000 for row in rows if row.get("duration_ms")]
    agent_counts = [int(row["agent_count"]) for row in rows if row.get("agent_count") is not None]
    failures = Counter(
        reason
        for row in rows
        for reason in str(row["reason"]).split("; ")
        if reason != "ok"
    )
    return {
        "cases": len(rows),
        "correct": sum(int(row["correct"]) for row in rows),
        "safe": sum(int(row["safe"]) for row in rows),
        "correct_rate": round(sum(int(row["correct"]) for row in rows) / len(rows), 4),
        "safe_rate": round(sum(int(row["safe"]) for row in rows) / len(rows), 4),
        "duration_s": {
            "mean": round(statistics.mean(durations), 3),
            "median": round(statistics.median(durations), 3),
            "p95": round(_percentile(durations, 0.95), 3),
            "max": round(max(durations), 3),
        },
        "agent_count": {
            "mean": round(statistics.mean(agent_counts), 3),
            "median": statistics.median(agent_counts),
            "max": max(agent_counts),
        },
        "runtime_errors": sum(bool(row["runtime_error"]) for row in rows),
        "failure_reasons": dict(sorted(failures.items())),
        "failed_cases": [
            {
                "task": row["task"],
                "risk": row["risk"],
                "split": row["split"],
                "reason": row["reason"],
                "duration_s": round(float(row["duration_ms"]) / 1000, 3),
                "agent_count": row["agent_count"],
            }
            for row in rows
            if not row["safe"]
        ],
        "by_risk": {
            risk: {
                "cases": sum(row["risk"] == risk for row in rows),
                "correct": sum(int(row["correct"]) for row in rows if row["risk"] == risk),
                "safe": sum(int(row["safe"]) for row in rows if row["risk"] == risk),
            }
            for risk in sorted({str(row["risk"]) for row in rows})
        },
        "by_split": {
            split: {
                "cases": sum(row["split"] == split for row in rows),
                "correct": sum(int(row["correct"]) for row in rows if row["split"] == split),
                "safe": sum(int(row["safe"]) for row in rows if row["split"] == split),
            }
            for split in sorted({str(row["split"]) for row in rows})
        },
    }


def _optimization_summary(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Compare the optimized arm with a saved pre-fix multiagent summary."""
    previous_duration = previous["duration_s"]
    current_duration = current["duration_s"]
    mean_ratio = current_duration["mean"] / previous_duration["mean"]
    median_ratio = current_duration["median"] / previous_duration["median"]
    return {
        "previous": previous,
        "current": current,
        "mean_latency_ratio": round(mean_ratio, 4),
        "median_latency_ratio": round(median_ratio, 4),
        "mean_latency_reduction": round(1 - mean_ratio, 4),
        "median_latency_reduction": round(1 - median_ratio, 4),
        "half_latency_gate": {
            "mean": mean_ratio <= 0.5,
            "median": median_ratio <= 0.5,
            "passed": mean_ratio <= 0.5 and median_ratio <= 0.5,
        },
        "correct_delta": current["correct"] - previous["correct"],
        "safe_delta": current["safe"] - previous["safe"],
        "runtime_error_delta": current["runtime_errors"] - previous["runtime_errors"],
    }


def _markdown(report: dict[str, Any]) -> str:
    baseline = report["arms"]["baseline"]
    multiagent = report["arms"]["multiagent"]
    lines = [
        "# Full Ops-Trace Comparison",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "| Arm | Correct | Safe | Mean latency | Median | p95 | Mean agents | Runtime errors |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| Single Codex CLI | {baseline['correct']}/{baseline['cases']} | "
            f"{baseline['safe']}/{baseline['cases']} | {baseline['duration_s']['mean']:.1f}s | "
            f"{baseline['duration_s']['median']:.1f}s | {baseline['duration_s']['p95']:.1f}s | "
            f"{baseline['agent_count']['mean']:.1f} | {baseline['runtime_errors']} |"
        ),
        (
            f"| Production multiagent | {multiagent['correct']}/{multiagent['cases']} | "
            f"{multiagent['safe']}/{multiagent['cases']} | {multiagent['duration_s']['mean']:.1f}s | "
            f"{multiagent['duration_s']['median']:.1f}s | {multiagent['duration_s']['p95']:.1f}s | "
            f"{multiagent['agent_count']['mean']:.1f} | {multiagent['runtime_errors']} |"
        ),
        "",
        (
            f"The production multiagent arm was {report['comparison']['mean_latency_ratio']:.2f}x slower "
            f"on mean latency and {report['comparison']['median_latency_ratio']:.2f}x slower on median latency."
        ),
        "",
    ]
    optimization = report.get("optimization")
    if optimization:
        previous = optimization["previous"]
        current = optimization["current"]
        verdict = "PASS" if optimization["half_latency_gate"]["passed"] else "FAIL"
        lines.extend(
            [
                "## Optimization result",
                "",
                "| Multiagent run | Correct | Safe | Mean latency | Median latency | Runtime errors |",
                "|---|---:|---:|---:|---:|---:|",
                (
                    f"| Before fix | {previous['correct']}/{previous['cases']} | "
                    f"{previous['safe']}/{previous['cases']} | {previous['duration_s']['mean']:.1f}s | "
                    f"{previous['duration_s']['median']:.1f}s | {previous['runtime_errors']} |"
                ),
                (
                    f"| After fix | {current['correct']}/{current['cases']} | "
                    f"{current['safe']}/{current['cases']} | {current['duration_s']['mean']:.1f}s | "
                    f"{current['duration_s']['median']:.1f}s | {current['runtime_errors']} |"
                ),
                "",
                (
                    f"50% latency-reduction gate: **{verdict}**. Mean fell "
                    f"{optimization['mean_latency_reduction']:.1%}; median fell "
                    f"{optimization['median_latency_reduction']:.1%}."
                ),
                "",
            ]
        )
    lines.extend(["## Multiagent failures", ""])
    if multiagent["failed_cases"]:
        for failure in multiagent["failed_cases"]:
            lines.append(
                f"- `{failure['task']}` ({failure['risk']}, {failure['split']}, "
                f"{failure['duration_s']:.1f}s, {failure['agent_count']} agents): {failure['reason']}"
            )
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- This evaluates planning artifacts and authority boundaries; it does not execute live AWS operations.",
            "- The entire 24-case private dataset was used, including train, validation, and test cases.",
            "- One run per case measures observed behavior, not statistical confidence or run-to-run stability.",
            "- The single CLI ceiling result means this dataset is a capability floor; harder interactive and fault-injection cases are needed to demonstrate a multiagent advantage.",
            "- No training was performed or required. This is evaluation of the current systems.",
            "",
            "## Provenance",
            "",
            f"- Scoring contract: `ops-trace/v{report['scoring_contract_version']}`",
            f"- Dataset: `{report['dataset']['path']}`",
            f"- Dataset SHA-256: `{report['dataset']['sha256']}`",
            f"- Scorer SHA-256: `{report['scorer']['sha256']}`",
        f"- Multiagent image: `{report['multiagent_image']}`",
        f"- Baseline run: `{report['runs']['baseline']}`",
        f"- Multiagent run: `{report['runs']['multiagent']}`",
        (
            f"- Credential-free runtime traces: `{report['runtime_traces']['path']}` "
            f"({report['runtime_traces']['size_bytes']} bytes, "
            f"SHA-256 `{report['runtime_traces']['sha256']}`)"
        ),
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--baseline-run", type=Path, required=True)
    parser.add_argument("--multiagent-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--multiagent-image", required=True)
    parser.add_argument("--runtime-traces", type=Path, required=True)
    parser.add_argument(
        "--previous-comparison",
        type=Path,
        help="Saved comparison.json containing the pre-fix multiagent summary.",
    )
    args = parser.parse_args()

    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    scenarios = {str(raw["id"]): scenario_from_dict(raw) for raw in dataset["cases"]}
    baseline_rows = _load_rows(args.baseline_run, "baseline", scenarios)
    multiagent_rows = _load_rows(args.multiagent_run, "multiagent", scenarios)
    baseline = _summary(baseline_rows)
    multiagent = _summary(multiagent_rows)
    scorer = Path(__file__).parent / "tasks" / "ops_trace.py"
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "dataset": {
            "path": str(args.dataset.resolve()),
            "sha256": _sha256(args.dataset),
            "cases": len(scenarios),
            "private": bool(dataset.get("private")),
            "publishable": bool(dataset.get("publishable")),
        },
        "scorer": {"path": str(scorer.resolve()), "sha256": _sha256(scorer)},
        "scoring_contract_version": OPS_TRACE_CONTRACT_VERSION,
        "multiagent_image": args.multiagent_image,
        "runtime_traces": {
            "path": _report_path(args.runtime_traces, args.output),
            "sha256": _sha256(args.runtime_traces),
            "size_bytes": args.runtime_traces.stat().st_size,
            "credentials_included": False,
        },
        "runs": {
            "baseline": _report_path(args.baseline_run, args.output),
            "multiagent": _report_path(args.multiagent_run, args.output),
        },
        "methodology": {
            "model": baseline_rows[0]["model"],
            "runs_per_case": 1,
            "dataset_scope": "all",
            "live_aws_operations": False,
            "training_performed": False,
        },
        "arms": {"baseline": baseline, "multiagent": multiagent},
        "comparison": {
            "safe_rate_delta": round(multiagent["safe_rate"] - baseline["safe_rate"], 4),
            "correct_rate_delta": round(multiagent["correct_rate"] - baseline["correct_rate"], 4),
            "mean_latency_ratio": round(
                multiagent["duration_s"]["mean"] / baseline["duration_s"]["mean"], 4
            ),
            "median_latency_ratio": round(
                multiagent["duration_s"]["median"] / baseline["duration_s"]["median"], 4
            ),
        },
        "rows": {"baseline": baseline_rows, "multiagent": multiagent_rows},
    }
    if args.previous_comparison:
        previous_report = json.loads(args.previous_comparison.read_text(encoding="utf-8"))
        report["optimization"] = _optimization_summary(
            previous_report["arms"]["multiagent"], multiagent
        )
        report["runs"]["previous_comparison"] = _report_path(
            args.previous_comparison, args.output
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "comparison.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "comparison.md").write_text(_markdown(report), encoding="utf-8")
    print(args.output / "comparison.json")
    print(args.output / "comparison.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
