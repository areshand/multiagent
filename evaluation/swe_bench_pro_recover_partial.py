#!/usr/bin/env python3
"""Recover completed SWE Bench Pro verifier rows from an interrupted shard.

EvalScope writes review JSONL rows as samples finish, before the final
``swe_bench_pro.json`` report and scaffold-parity summary are written. This
utility turns those completed review rows into a transparent shard summary that
the official aggregate can count without pretending the interrupted parent run
completed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from evaluation.swe_bench_pro_scaffold_parity import DEFAULT_PRO_REPO, load_official_instances, with_dockerhub_username
from evaluation.swe_bench_pro_shard import build_sample_shard


DEFAULT_MODEL_ID = "codex-scaffold-parity"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
    return rows


def review_rows_path(work_dir: Path, model_id: str) -> Path:
    return work_dir / "reviews" / model_id / "swe_bench_pro_default.jsonl"


def score_acc(row: dict[str, Any]) -> float | None:
    sample_score = row.get("sample_score")
    if not isinstance(sample_score, dict):
        return None
    score = sample_score.get("score")
    if not isinstance(score, dict):
        return None
    value = score.get("value")
    if not isinstance(value, dict) or value.get("acc") is None:
        return None
    return float(value["acc"])


def sample_metadata(row: dict[str, Any]) -> dict[str, Any]:
    sample_score = row.get("sample_score")
    if not isinstance(sample_score, dict):
        return {}
    metadata = sample_score.get("sample_metadata")
    return metadata if isinstance(metadata, dict) else {}


def completed_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    for expected_index, row in enumerate(rows):
        if score_acc(row) is None:
            break
        sample_score = row.get("sample_score") or {}
        sample_id = sample_score.get("sample_id")
        if sample_id is not None and int(sample_id) != expected_index:
            break
        completed.append(row)
    return completed


def maybe_load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    reviews_path = args.reviews or review_rows_path(args.work_dir, args.model_id)
    rows = read_jsonl(reviews_path)
    completed = completed_rows(rows)
    if not completed:
        raise ValueError(f"no completed review rows found in {reviews_path}")

    instances = with_dockerhub_username(load_official_instances(args.swe_bench_pro_repo_path), args.dockerhub_username)
    shard = build_sample_shard(offset=args.sample_offset, count=len(completed), instances=instances)
    selected = shard.selected_instances
    mismatches: list[dict[str, Any]] = []
    scores: list[float] = []
    for relative_index, (row, instance) in enumerate(zip(completed, selected, strict=True)):
        scores.append(score_acc(row) or 0.0)
        metadata_instance_id = sample_metadata(row).get("instance_id")
        if metadata_instance_id is not None and str(metadata_instance_id) != str(instance["instance_id"]):
            mismatches.append(
                {
                    "relative_index": relative_index,
                    "official_index": args.sample_offset + relative_index,
                    "review_instance_id": metadata_instance_id,
                    "official_instance_id": instance["instance_id"],
                }
            )
    if mismatches and not args.allow_instance_mismatch:
        raise ValueError(f"review rows do not match official shard order: {mismatches[:3]}")

    preflight = maybe_load_json(args.preflight)
    on_demand = maybe_load_json(args.on_demand_image_status)
    now = dt.datetime.now(dt.UTC)
    started_at = args.started_at or None
    completed_at = args.completed_at or now.isoformat(timespec="seconds")
    score = sum(scores) / len(scores)
    notes = (
        "Recovered from completed EvalScope review JSONL rows after the parent "
        "SWE Bench Pro shard stopped before writing its final summary. Each "
        "included row has official verifier sample_score evidence; unfinished "
        "rows from the parent shard are not counted."
    )

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "started_at": started_at,
        "completed_at": completed_at,
        "benchmark": "swe-bench-pro",
        "status": "completed",
        "recovered_partial": True,
        "recovery_source": {
            "work_dir": str(args.work_dir),
            "reviews": str(reviews_path),
            "parent_sample_offset": args.sample_offset,
            "parent_sample_count": args.sample_count,
            "completed_review_rows": len(completed),
            "ignored_review_rows": max(0, len(rows) - len(completed)),
            "instance_mismatches": mismatches,
        },
        "score": score,
        "sample_size": len(completed),
        "official": False,
        "official_verifier_evidence": True,
        "full_official_candidate": False,
        "metric": "resolved_percent",
        "scope": f"offset-{args.sample_offset}-count-{len(completed)}-recovered",
        "work_dir": str(args.work_dir),
        "evalscope_report": None,
        "task_config_json": str(args.config_json) if args.config_json else None,
        "task_config_yaml": str(args.config_yaml) if args.config_yaml else None,
        "preflight_report": str(args.preflight) if args.preflight else None,
        "evalscope_result": {"status": "recovered-from-review-jsonl"},
        "parity": {
            "dataset": "ScaleAI/SWE-bench_Pro",
            "adapter": "evalscope swe_bench_pro",
            "agent_config": f"external {args.agent_framework}",
            "runs_inside_per_instance_docker": True,
            "patch_source": "git diff extracted from /app after external Codex run",
            "verifier": "SWE Bench Pro run_script.sh plus parser.py via EvalScope eval_instance",
            "swe_bench_pro_repo_path": str(args.swe_bench_pro_repo_path),
            "dockerhub_username": args.dockerhub_username,
            "platform": args.platform,
            "official_scaffold_ready": bool((preflight or {}).get("official_scaffold_ready", True)),
            "official_image_set_ready": bool((preflight or {}).get("official_image_set_ready", False)),
            "image_provider_ready": True,
            "image_availability_strategy": "on-demand",
            "on_demand_prune_after_sample": bool((on_demand or {}).get("prune_after_sample", False)),
        },
        "on_demand_image_status": (
            {
                "path": str(args.on_demand_image_status),
                "exists": args.on_demand_image_status.exists(),
                "status": on_demand.get("status") if on_demand else None,
            }
            if args.on_demand_image_status
            else None
        ),
        "sample_shard": shard.summary(),
        "preflight": preflight,
        "system_results": {
            "system": "ours-codex-swe-bench-pro-scaffold-parity",
            "source": str(args.output),
            "results": [
                {
                    "benchmark": "swe-bench-pro",
                    "score": score,
                    "metric": "resolved_percent",
                    "sample_size": len(completed),
                    "official": False,
                    "duration_s": None,
                    "notes": notes,
                }
            ],
        },
        "notes": notes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=DEFAULT_PRO_REPO)
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--sample-offset", type=int, required=True)
    parser.add_argument("--sample-count", type=int)
    parser.add_argument("--agent-framework", default="codex-devnull")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--config-json", type=Path)
    parser.add_argument("--config-yaml", type=Path)
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--on-demand-image-status", type=Path)
    parser.add_argument("--started-at")
    parser.add_argument("--completed-at")
    parser.add_argument("--allow-instance-mismatch", action="store_true")
    args = parser.parse_args()
    if args.sample_offset < 0:
        parser.error("--sample-offset must be >= 0")
    if args.sample_count is not None and args.sample_count < 1:
        parser.error("--sample-count must be >= 1")

    payload = build_payload(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.output}")
    print(f"recovered {payload['sample_size']} completed rows score={payload['score']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
