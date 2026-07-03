#!/usr/bin/env python3
"""Aggregate SWE Bench Pro scaffold-parity shard summaries.

This does not run EvalScope. It validates already-written
``swe_bench_pro_scaffold_parity`` JSON summaries against the official public
JSONL order and reports whether the shard set is complete enough to serve as an
official comparison candidate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from evaluation.swe_bench_pro_scaffold_parity import DEFAULT_PRO_REPO, load_official_instances, with_dockerhub_username


DEFAULT_JSON = Path("evaluation/reports/swe-bench-pro-official-aggregate.json")
DEFAULT_REPORT = Path("evaluation/reports/swe-bench-pro-official-aggregate.md")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def discover_reports(report_dir: Path, patterns: list[str]) -> list[Path]:
    paths: list[Path] = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?[]"):
            paths.extend(sorted(report_dir.glob(pattern)))
        else:
            raw_path = Path(pattern)
            paths.append(raw_path if raw_path.exists() or raw_path.is_absolute() else report_dir / raw_path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        resolved = path if path.is_absolute() else Path(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists() and not is_sidecar_report(resolved):
            unique.append(resolved)
    return unique


def is_sidecar_report(path: Path) -> bool:
    sidecar_suffixes = (
        "-config.json",
        "-preflight.json",
        "-on-demand-image-status.json",
        "-report.json",
    )
    return any(path.name.endswith(suffix) for suffix in sidecar_suffixes)


def selected_indices(summary: dict[str, Any]) -> list[int]:
    shard = summary.get("sample_shard") or {}
    selected = shard.get("selected_instances") or []
    indices: list[int] = []
    for item in selected:
        if isinstance(item, dict) and item.get("official_index") is not None:
            indices.append(int(item["official_index"]))
    return indices


def report_matches(summary: dict[str, Any], *, framework: str, require_codex: bool) -> bool:
    if summary.get("benchmark") != "swe-bench-pro":
        return False
    if summary.get("status") != "completed":
        return False
    if not summary.get("official_verifier_evidence"):
        return False
    if not selected_indices(summary):
        return False
    agent_config = str((summary.get("parity") or {}).get("agent_config") or "")
    if framework and agent_config != f"external {framework}":
        return False
    if require_codex and agent_config not in {"external codex", "external codex-devnull"}:
        return False
    return True


def contiguous_ranges(values: list[int]) -> list[dict[str, int]]:
    if not values:
        return []
    sorted_values = sorted(values)
    ranges: list[dict[str, int]] = []
    start = prev = sorted_values[0]
    for value in sorted_values[1:]:
        if value == prev + 1:
            prev = value
            continue
        ranges.append({"start": start, "end": prev, "count": prev - start + 1})
        start = prev = value
    ranges.append({"start": start, "end": prev, "count": prev - start + 1})
    return ranges


def suggested_missing_shard(missing_indices: list[int], *, max_size: int) -> dict[str, int] | None:
    if not missing_indices:
        return None
    first = missing_indices[0]
    count = 1
    for index in missing_indices[1:]:
        if index != first + count or count >= max_size:
            break
        count += 1
    return {"sample_offset": first, "sample_count": count}


def aggregate(args: argparse.Namespace) -> dict[str, Any]:
    official_instances = with_dockerhub_username(load_official_instances(args.swe_bench_pro_repo_path), args.dockerhub_username)
    expected_count = len(official_instances)
    report_paths = discover_reports(args.report_dir, args.reports)

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    index_to_reports: dict[int, list[str]] = {}
    weighted_score_sum = 0.0
    weighted_num_sum = 0

    for path in report_paths:
        try:
            summary = load_json(path)
        except Exception as exc:  # pragma: no cover - corrupt artifact diagnostic
            excluded.append({"path": str(path), "reason": f"unreadable: {exc!r}"})
            continue
        indices = selected_indices(summary)
        if not report_matches(summary, framework=args.framework, require_codex=args.require_codex):
            excluded.append(
                {
                    "path": str(path),
                    "reason": "not a completed official-verifier shard matching filters",
                    "status": summary.get("status"),
                    "scope": summary.get("scope"),
                    "agent_config": (summary.get("parity") or {}).get("agent_config"),
                    "official_verifier_evidence": summary.get("official_verifier_evidence"),
                    "selected_count": len(indices),
                }
            )
            continue
        sample_size = int(summary.get("sample_size") or 0)
        score = float(summary.get("score") or 0.0)
        weighted_score_sum += score * sample_size
        weighted_num_sum += sample_size
        for index in indices:
            index_to_reports.setdefault(index, []).append(str(path))
        included.append(
            {
                "path": str(path),
                "scope": summary.get("scope"),
                "score": score,
                "sample_size": sample_size,
                "agent_config": (summary.get("parity") or {}).get("agent_config"),
                "selected_indices": indices,
            }
        )

    covered_indices = sorted(index_to_reports)
    duplicate_indices = sorted(index for index, owners in index_to_reports.items() if len(owners) > 1)
    all_indices = set(range(expected_count))
    missing_indices = sorted(all_indices - set(covered_indices))
    out_of_range_indices = sorted(index for index in covered_indices if index < 0 or index >= expected_count)
    official_complete = (
        expected_count == args.expected_full_split_size
        and len(covered_indices) == expected_count
        and not missing_indices
        and not duplicate_indices
        and not out_of_range_indices
        and weighted_num_sum == expected_count
    )
    first_missing = missing_indices[0] if missing_indices else None
    suggested_shard = suggested_missing_shard(missing_indices, max_size=args.suggest_shard_size)
    aggregate_score = None if weighted_num_sum == 0 else weighted_score_sum / weighted_num_sum

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "benchmark": "swe-bench-pro",
        "swe_bench_pro_repo_path": str(args.swe_bench_pro_repo_path),
        "dockerhub_username": args.dockerhub_username,
        "expected_full_split_size": args.expected_full_split_size,
        "expected_count": expected_count,
        "official_complete": official_complete,
        "official_score": aggregate_score if official_complete else None,
        "partial_weighted_score": aggregate_score,
        "partial_sample_size": weighted_num_sum,
        "covered_count": len(covered_indices),
        "missing_count": len(missing_indices),
        "duplicate_count": len(duplicate_indices),
        "out_of_range_count": len(out_of_range_indices),
        "first_missing_index": first_missing,
        "suggested_next_shard": suggested_shard,
        "covered_ranges": contiguous_ranges(covered_indices),
        "missing_ranges": contiguous_ranges(missing_indices)[: args.max_ranges],
        "duplicate_indices": duplicate_indices[: args.max_ranges],
        "out_of_range_indices": out_of_range_indices[: args.max_ranges],
        "included_reports": included,
        "excluded_reports": excluded,
    }


def fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def render_markdown(payload: dict[str, Any]) -> str:
    next_shard = payload.get("suggested_next_shard") or {}
    lines = [
        "# SWE Bench Pro Official Aggregate",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"- Official complete: {payload['official_complete']}",
        f"- Covered official indices: {payload['covered_count']}/{payload['expected_count']}",
        f"- Partial weighted score: {fmt(payload['partial_weighted_score'])}",
        f"- Official score: {fmt(payload['official_score'])}",
        f"- Missing indices: {payload['missing_count']}",
        f"- Duplicate indices: {payload['duplicate_count']}",
        f"- Out-of-range indices: {payload['out_of_range_count']}",
    ]
    if next_shard:
        lines.append(
            "- Suggested next shard: "
            f"--sample-offset {next_shard['sample_offset']} --sample-count {next_shard['sample_count']}"
        )
    lines.extend(["", "## Included Reports", ""])
    if payload["included_reports"]:
        lines.extend(["| Scope | N | Score | Agent | Path |", "| --- | ---: | ---: | --- | --- |"])
        for item in payload["included_reports"]:
            lines.append(
                f"| {item.get('scope')} | {item.get('sample_size')} | {fmt(item.get('score'))} | "
                f"{item.get('agent_config')} | {item.get('path')} |"
            )
    else:
        lines.append("No completed official-verifier shard reports matched the filters.")
    lines.extend(["", "## Missing Ranges", ""])
    if payload["missing_ranges"]:
        for item in payload["missing_ranges"]:
            lines.append(f"- {item['start']}..{item['end']} ({item['count']})")
    else:
        lines.append("None.")
    lines.extend(["", "## Excluded Reports", ""])
    if payload["excluded_reports"]:
        for item in payload["excluded_reports"]:
            lines.append(f"- {item['path']}: {item['reason']}")
    else:
        lines.append("None.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=DEFAULT_PRO_REPO)
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--expected-full-split-size", type=int, default=731)
    parser.add_argument("--report-dir", type=Path, default=Path("evaluation/reports"))
    parser.add_argument(
        "--reports",
        nargs="+",
        default=["swe-bench-pro-codex-cwd*-offset*-count*.json"],
        help="report paths or glob patterns relative to --report-dir",
    )
    parser.add_argument("--framework", default="codex-devnull")
    parser.add_argument("--require-codex", action="store_true", default=True)
    parser.add_argument("--allow-non-codex", action="store_false", dest="require_codex")
    parser.add_argument("--suggest-shard-size", type=int, default=10)
    parser.add_argument("--max-ranges", type=int, default=20)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    payload = aggregate(args)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_markdown(payload), encoding="utf-8")
    print(f"wrote {args.json}")
    print(f"wrote {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
