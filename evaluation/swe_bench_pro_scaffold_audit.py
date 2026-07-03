#!/usr/bin/env python3
"""Audit SWE Bench Pro scaffold parity from local evidence artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = Path("evaluation/reports")
DEFAULT_PREFLIGHT = DEFAULT_REPORT_DIR / "swe-bench-pro-official-preflight.json"
DEFAULT_AGGREGATE = DEFAULT_REPORT_DIR / "swe-bench-pro-official-aggregate.json"
DEFAULT_PROBE = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-probe-rootscan-offset1-count1.json"
DEFAULT_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-audit.json"
DEFAULT_MARKDOWN = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-audit.md"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "evidence": self.evidence,
        }


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def check_equal(name: str, actual: Any, expected: Any, evidence: Path) -> Check:
    return Check(
        name=name,
        passed=actual == expected,
        detail=f"expected {expected!r}, got {actual!r}",
        evidence=str(evidence),
    )


def check_true(name: str, actual: Any, evidence: Path) -> Check:
    return Check(
        name=name,
        passed=bool(actual),
        detail=f"value is {actual!r}",
        evidence=str(evidence),
    )


def first_patch(work_dir: Path) -> Path | None:
    patches = sorted(work_dir.glob("swe_bench_pro_log/*/workspace/patch.diff"))
    return patches[0] if patches else None


def first_container_log(work_dir: Path) -> Path | None:
    logs = sorted(work_dir.glob("swe_bench_pro_log/*/container.log"))
    return logs[0] if logs else None


def audit(args: argparse.Namespace) -> dict[str, Any]:
    preflight = load_json(args.preflight)
    aggregate = load_json(args.aggregate)
    probe = load_json(args.probe)

    checks: list[Check] = [
        check_equal("official split size", preflight.get("instance_count"), args.expected_full_split_size, args.preflight),
        check_equal("unique image count", preflight.get("unique_image_count"), args.expected_full_split_size, args.preflight),
        check_true("dataset complete", preflight.get("dataset_complete"), args.preflight),
        check_true("run scripts complete", preflight.get("run_scripts_complete"), args.preflight),
        check_true("official scaffold ready", preflight.get("official_scaffold_ready"), args.preflight),
        check_equal("aggregate expected count", aggregate.get("expected_count"), args.expected_full_split_size, args.aggregate),
        check_equal("aggregate duplicate count", aggregate.get("duplicate_count"), 0, args.aggregate),
        check_equal("aggregate out-of-range count", aggregate.get("out_of_range_count"), 0, args.aggregate),
        check_true("aggregate has next shard", aggregate.get("official_complete") or aggregate.get("suggested_next_shard"), args.aggregate),
        check_equal("probe status", probe.get("status"), "completed", args.probe),
        check_true("probe official verifier evidence", probe.get("official_verifier_evidence"), args.probe),
        check_equal("probe agent working dir", (probe.get("parity") or {}).get("agent_working_dir"), "/app", args.probe),
        check_equal("probe action protocol", (probe.get("parity") or {}).get("patch_source"), "git diff extracted from /app after external Codex run", args.probe),
        check_equal("probe selected count", (probe.get("sample_shard") or {}).get("selected_count"), 1, args.probe),
        check_true("on-demand image status exists", (probe.get("on_demand_image_status") or {}).get("exists"), args.probe),
    ]

    work_dir = Path(str(probe.get("work_dir") or ""))
    patch_path = first_patch(work_dir)
    if patch_path is None:
        checks.append(Check("probe patch extracted", False, "patch.diff not found", str(work_dir)))
        patch_bytes = 0
    else:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
        patch_bytes = patch_path.stat().st_size
        checks.append(
            Check(
                "probe patch extracted",
                patch_bytes > 0 and "diff --git" in patch_text,
                f"{patch_bytes} bytes",
                str(patch_path),
            )
        )

    container_log = first_container_log(work_dir)
    if container_log is None:
        checks.append(Check("probe verifier applied patch", False, "container.log not found", str(work_dir)))
    else:
        log_text = container_log.read_text(encoding="utf-8", errors="replace")
        checks.append(
            Check(
                "probe verifier applied patch",
                "Applied patch" in log_text and "No valid patches" not in log_text,
                "clean apply signal found" if "Applied patch" in log_text else "clean apply signal missing",
                str(container_log),
            )
        )

    scaffold_ready = all(check.passed for check in checks)
    official_complete = bool(aggregate.get("official_complete"))
    ready_for_official_comparison_run = scaffold_ready and (
        official_complete or bool(aggregate.get("suggested_next_shard"))
    )
    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "benchmark": "swe-bench-pro",
        "scaffold_parity_ready": scaffold_ready,
        "ready_for_official_comparison_run": ready_for_official_comparison_run,
        "official_comparison_complete": official_complete,
        "official_score": aggregate.get("official_score"),
        "official_coverage": f"{aggregate.get('covered_count')}/{aggregate.get('expected_count')}",
        "partial_weighted_score": aggregate.get("partial_weighted_score"),
        "next_shard": aggregate.get("suggested_next_shard"),
        "probe_patch_bytes": patch_bytes,
        "checks": [check.as_dict() for check in checks],
        "remaining_gap": (
            "full 731-instance run with task-solving patches"
            if scaffold_ready and not official_complete
            else "scaffold evidence incomplete"
            if not scaffold_ready
            else ""
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# SWE Bench Pro Scaffold Audit",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        f"- Scaffold parity ready: {payload['scaffold_parity_ready']}",
        f"- Ready for official comparison run: {payload['ready_for_official_comparison_run']}",
        f"- Official comparison complete: {payload['official_comparison_complete']}",
        f"- Official coverage: {payload['official_coverage']}",
        f"- Partial weighted score: {payload['partial_weighted_score']}",
        f"- Probe patch bytes: {payload['probe_patch_bytes']}",
        f"- Remaining gap: {payload['remaining_gap']}",
        "",
        "## Checks",
        "",
        "| Check | Status | Detail | Evidence |",
        "| --- | --- | --- | --- |",
    ]
    for check in payload["checks"]:
        status = "pass" if check["passed"] else "fail"
        lines.append(f"| {check['name']} | {status} | {check['detail']} | `{check['evidence']}` |")
    lines.append("")
    if payload.get("next_shard"):
        shard = payload["next_shard"]
        lines.extend(
            [
                "## Next Shard",
                "",
                f"`--sample-offset {shard['sample_offset']} --sample-count {shard['sample_count']}`",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--aggregate", type=Path, default=DEFAULT_AGGREGATE)
    parser.add_argument("--probe", type=Path, default=DEFAULT_PROBE)
    parser.add_argument("--expected-full-split-size", type=int, default=731)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--report", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--strict-official-complete", action="store_true")
    args = parser.parse_args()

    payload = audit(args)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(render_markdown(payload), encoding="utf-8")
        print(f"wrote {args.report}")
    if not payload["scaffold_parity_ready"]:
        return 1
    if args.strict_official_complete and not payload["official_comparison_complete"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
