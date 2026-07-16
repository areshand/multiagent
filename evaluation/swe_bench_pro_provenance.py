#!/usr/bin/env python3
"""Capture and validate a relocatable SWE Bench Pro evidence bundle."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping

from evaluation.swe_bench_pro import native_runner_summary_from_text
from evaluation.swe_bench_pro_on_demand import SOLVER_SOURCE_LABEL, native_solver_source_digest
from multiagent_framework.provenance import (
    capture_git_identity,
    copy_artifact_bundle,
    sha256_file,
    validate_artifact_bundle,
)


SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATASET_RELATIVE_PATH = Path("helper_code/sweap_eval_full_v2.jsonl")
REQUIRED_ARTIFACTS = {
    "config.json",
    "config.yaml",
    "eval-log.log",
    "evalscope-report.json",
    "image-status.json",
    "preflight.json",
    "summary.json",
}
_GIT_ID = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _artifact_path(bundle: Path, kind: str) -> Path:
    return bundle / "artifacts" / kind


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_score(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be finite")
    return float(value)


def _selected_instances(preflight: Mapping[str, Any], expected: int) -> List[Dict[str, Any]]:
    selected = ((preflight.get("sample_shard") or {}).get("selected_instances") or [])
    if not isinstance(selected, list) or len(selected) != expected:
        raise ValueError("preflight selected instance count does not match sample size")
    normalized = []
    seen = set()
    for item in selected:
        if not isinstance(item, dict):
            raise ValueError("preflight selected instance must be an object")
        instance_id = str(item.get("instance_id") or "")
        official_index = item.get("official_index")
        if not instance_id or instance_id in seen or not isinstance(official_index, int):
            raise ValueError("preflight selected instance identity is missing or duplicated")
        seen.add(instance_id)
        normalized.append({"instance_id": instance_id, "official_index": official_index})
    return normalized


def _image_evidence(
    image_status: Mapping[str, Any], selected: List[Dict[str, Any]], solver_digest: str
) -> List[Dict[str, str]]:
    if image_status.get("status") != "completed" or image_status.get("platform") != "linux/amd64":
        raise ValueError("image lifecycle did not complete on linux/amd64")
    counts = image_status.get("counts") or {}
    for name in ("failed", "stopped_low_disk", "bake_failed", "prune_failed"):
        if counts.get(name, 0) != 0:
            raise ValueError(f"image lifecycle recorded {name}")
    ready = {}
    for record in image_status.get("records") or []:
        if not isinstance(record, dict) or record.get("status") not in {"baked", "bake_reused"}:
            continue
        instance_id = str(record.get("instance_id") or "")
        if instance_id in ready:
            raise ValueError(f"duplicate ready image record: {instance_id}")
        ready[instance_id] = record
    evidence = []
    for selected_item in selected:
        instance_id = selected_item["instance_id"]
        record = ready.get(instance_id)
        if record is None:
            raise ValueError(f"missing ready image record: {instance_id}")
        identities = []
        for side in ("base_identity", "baked_identity"):
            identity = record.get(side) or {}
            image_id = str(identity.get("image_id") or "")
            if not _IMAGE_ID.fullmatch(image_id):
                raise ValueError(f"invalid {side} image ID: {instance_id}")
            if identity.get("os") != "linux" or identity.get("architecture") != "amd64":
                raise ValueError(f"invalid {side} platform: {instance_id}")
            identities.append(image_id)
        baked_reference = str(record.get("baked_image") or "")
        if not baked_reference.endswith("-" + solver_digest[:16]):
            raise ValueError(f"baked image is not bound to solver source: {instance_id}")
        if record.get("solver_source_sha256") != solver_digest:
            raise ValueError(f"image record has the wrong solver source digest: {instance_id}")
        baked_labels = (record.get("baked_identity") or {}).get("labels") or {}
        if baked_labels.get(SOLVER_SOURCE_LABEL) != solver_digest:
            raise ValueError(f"baked image label is not bound to solver source: {instance_id}")
        evidence.append(
            {
                "instance_id": instance_id,
                "base_image_id": identities[0],
                "baked_image_id": identities[1],
                "baked_reference": baked_reference,
            }
        )
    return evidence


def recompute_evidence(bundle: Path, solver_digest: str) -> Dict[str, Any]:
    """Recompute all acceptance facts from the hash-bound artifacts."""

    summary = _read_json(_artifact_path(bundle, "summary.json"))
    config = _read_json(_artifact_path(bundle, "config.json"))
    preflight = _read_json(_artifact_path(bundle, "preflight.json"))
    image_status = _read_json(_artifact_path(bundle, "image-status.json"))
    evalscope_report = _read_json(_artifact_path(bundle, "evalscope-report.json"))
    if summary.get("benchmark") != "swe-bench-pro" or summary.get("status") != "completed":
        raise ValueError("summary is not a completed SWE Bench Pro result")
    sample_size = _positive_int(summary.get("sample_size"), "summary.sample_size")
    score = _finite_score(summary.get("score"), "summary.score")
    end_to_end_score = _finite_score(summary.get("end_to_end_score"), "summary.end_to_end_score")
    if summary.get("official_verifier_evidence") is not True:
        raise ValueError("summary lacks official verifier evidence")
    native = summary.get("native_runner") or {}
    if native.get("end_to_end_scored") is not True or native.get("scored_outcome_count") != sample_size:
        raise ValueError("native outcomes do not cover the full sample size")
    eval_log = _artifact_path(bundle, "eval-log.log").read_text(encoding="utf-8", errors="replace")
    parsed_native = native_runner_summary_from_text(eval_log) or {}
    if parsed_native != native:
        raise ValueError("summary native outcomes do not match the bound EvalScope log")
    outcomes = parsed_native.get("all_exit_events") or []
    if not isinstance(outcomes, list) or len(outcomes) != sample_size:
        raise ValueError("native exit event count does not match sample size")
    runtimes = []
    for outcome in outcomes:
        identity = outcome.get("runtime_identity") if isinstance(outcome, dict) else None
        codex_version = str((identity or {}).get("codex_version") or "")
        node_version = str((identity or {}).get("node_version") or "")
        if not codex_version or not node_version:
            raise ValueError("native runtime identity is incomplete")
        runtimes.append(
            {
                "sample": str(outcome.get("sample") or ""),
                "outcome": str(outcome.get("outcome") or ""),
                "codex_version": codex_version,
                "node_version": node_version,
            }
        )
    agent_kwargs = ((config.get("agent_config") or {}).get("kwargs") or {})
    model = str(agent_kwargs.get("model_name") or "")
    if not model or not agent_kwargs.get("codex_auth_json") or config.get("ignore_errors") is not False:
        raise ValueError("effective config is not a fail-closed authenticated native run")
    platform = (((config.get("sandbox") or {}).get("default_config") or {}).get("platform"))
    if platform != "linux/amd64":
        raise ValueError("effective config platform is not linux/amd64")
    if evalscope_report.get("num") != sample_size:
        raise ValueError("EvalScope report sample size mismatch")
    if _finite_score(evalscope_report.get("score"), "EvalScope score") != score:
        raise ValueError("EvalScope report score mismatch")
    selected = _selected_instances(preflight, sample_size)
    summary_selected = ((summary.get("sample_shard") or {}).get("selected_instances") or [])
    summary_ids = [str(item.get("instance_id") or "") for item in summary_selected if isinstance(item, dict)]
    if summary_ids != [item["instance_id"] for item in selected]:
        raise ValueError("summary and preflight selected instances differ")
    images = _image_evidence(image_status, selected, solver_digest)
    return {
        "sample_size": sample_size,
        "score": score,
        "end_to_end_score": end_to_end_score,
        "model": model,
        "platform": platform,
        "selected_instances": selected,
        "images": images,
        "runtimes": runtimes,
        "solver_source_sha256": solver_digest,
    }


def validate_bundle(bundle: Path) -> Dict[str, Any]:
    """Validate artifact integrity and recompute semantic provenance."""

    manifest = _read_json(bundle / MANIFEST_NAME)
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("benchmark") != "swe-bench-pro":
        raise ValueError("unsupported provenance manifest")
    records = manifest.get("artifacts") or []
    validate_artifact_bundle(bundle, records, REQUIRED_ARTIFACTS)
    repositories = manifest.get("repositories") or {}
    for name in ("solver", "evalscope", "swe_bench_pro"):
        identity = repositories.get(name) or {}
        if not _GIT_ID.fullmatch(str(identity.get("commit") or "")):
            raise ValueError(f"invalid {name} commit")
        if not _GIT_ID.fullmatch(str(identity.get("tree") or "")) or identity.get("dirty") is not False:
            raise ValueError(f"invalid {name} tree identity")
    dataset_hash = str((manifest.get("dataset") or {}).get("sha256") or "")
    solver_digest = str(manifest.get("solver_source_sha256") or "")
    if not _SHA256.fullmatch(dataset_hash) or not _SHA256.fullmatch(solver_digest):
        raise ValueError("dataset or solver source digest is invalid")
    evidence = recompute_evidence(bundle, solver_digest)
    if manifest.get("evidence") != evidence:
        raise ValueError("manifest evidence does not match bound artifacts")
    return manifest


def capture_bundle(args: argparse.Namespace) -> Dict[str, Any]:
    """Capture a fresh bundle and validate it before returning."""

    bundle = args.bundle.resolve()
    if bundle.exists() and any(bundle.iterdir()):
        raise ValueError(f"bundle directory is not empty: {bundle}")
    bundle.mkdir(parents=True, exist_ok=True)
    repositories = {
        "solver": capture_git_identity(args.solver_repo),
        "evalscope": capture_git_identity(args.evalscope_repo),
        "swe_bench_pro": capture_git_identity(args.swe_bench_pro_repo),
    }
    if any(identity["dirty"] for identity in repositories.values()):
        raise ValueError("all provenance repositories must be clean")
    solver_digest = native_solver_source_digest(args.solver_repo)
    records = copy_artifact_bundle(
        bundle,
        {
            "summary.json": args.summary,
            "config.json": args.config_json,
            "config.yaml": args.config_yaml,
            "preflight.json": args.preflight,
            "image-status.json": args.image_status,
            "evalscope-report.json": args.evalscope_report,
            "eval-log.log": args.eval_log,
        },
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "benchmark": "swe-bench-pro",
        "repositories": repositories,
        "dataset": {"sha256": sha256_file(args.swe_bench_pro_repo / DATASET_RELATIVE_PATH)},
        "solver_source_sha256": solver_digest,
        "artifacts": records,
        "evidence": recompute_evidence(bundle, solver_digest),
    }
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return validate_bundle(bundle)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--bundle", type=Path, required=True)
    capture.add_argument("--solver-repo", type=Path, required=True)
    capture.add_argument("--evalscope-repo", type=Path, required=True)
    capture.add_argument("--swe-bench-pro-repo", type=Path, required=True)
    for name in ("summary", "config-json", "config-yaml", "preflight", "image-status", "evalscope-report", "eval-log"):
        capture.add_argument("--" + name, type=Path, required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("bundle", type=Path)
    args = parser.parse_args()
    manifest = capture_bundle(args) if args.command == "capture" else validate_bundle(args.bundle.resolve())
    print(json.dumps(manifest["evidence"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
