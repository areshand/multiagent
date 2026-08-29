#!/usr/bin/env python3
"""Combine private ops and conversation trace datasets into one manifest."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from evaluation.tasks.conversation_trace import CONVERSATION_TRACE_CONTRACT_VERSION
from evaluation.tasks.ops_trace import OPS_TRACE_CONTRACT_VERSION


SUITE_NAMES = ("ops-trace", "conversation-trace")
CURRENT_CONTRACT_VERSIONS = {
    "ops-trace": OPS_TRACE_CONTRACT_VERSION,
    "conversation-trace": CONVERSATION_TRACE_CONTRACT_VERSION,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_suite(path: Path, expected_benchmark: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot load {expected_benchmark} dataset {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_benchmark} dataset must be a JSON object: {path}")
    if payload.get("benchmark") != expected_benchmark:
        raise ValueError(
            f"expected benchmark {expected_benchmark!r} in {path}, got {payload.get('benchmark')!r}"
        )
    if payload.get("format_version") != 1 or not isinstance(payload.get("cases"), list):
        raise ValueError(f"{expected_benchmark} dataset has an unsupported schema: {path}")
    if payload.get("private") is not True or payload.get("publishable") is not False:
        raise ValueError(f"{expected_benchmark} dataset must remain private and non-publishable")
    return payload


def combine_datasets(
    ops_payload: dict[str, Any],
    conversation_payload: dict[str, Any],
    source_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    suites = {
        "ops-trace": ops_payload,
        "conversation-trace": conversation_payload,
    }
    seen: set[str] = set()
    by_split = {split: 0 for split in ("train", "validation", "test")}
    for suite_name, payload in suites.items():
        if payload.get("benchmark") != suite_name or not isinstance(payload.get("cases"), list):
            raise ValueError(f"invalid nested suite payload: {suite_name}")
        for case in payload["cases"]:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                raise ValueError(f"{suite_name} contains a case without a string ID")
            case_id = case["id"]
            if case_id in seen:
                raise ValueError(f"duplicate case ID across trace suites: {case_id}")
            seen.add(case_id)
            split = case.get("split")
            if split in by_split:
                by_split[str(split)] += 1

    return {
        "format_version": 1,
        "benchmark": "trace",
        "private": True,
        "publishable": False,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "scoring_contract_versions": {
            name: suites[name].get("scoring_contract_version", CURRENT_CONTRACT_VERSIONS[name])
            for name in SUITE_NAMES
        },
        "source": {"dataset_sha256": source_hashes or {}},
        "privacy": {
            "raw_traces_included": False,
            "note": (
                "Nested pseudonymized suites remain private. Combining their manifests does "
                "not authorize model replay or publication."
            ),
        },
        "counts": {
            "cases": len(seen),
            "by_suite": {name: len(suites[name]["cases"]) for name in SUITE_NAMES},
            "by_split": by_split,
        },
        "suites": suites,
    }


def write_dataset(
    output: Path,
    ops_path: Path,
    conversation_path: Path,
) -> dict[str, Any]:
    ops_payload = load_suite(ops_path, "ops-trace")
    conversation_payload = load_suite(conversation_path, "conversation-trace")
    payload = combine_datasets(
        ops_payload,
        conversation_payload,
        {
            "ops-trace": _sha256(ops_path),
            "conversation-trace": _sha256(conversation_path),
        },
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(output)
    return payload


def main() -> int:
    trace_root = Path.home() / "projects" / "traces"
    parser = argparse.ArgumentParser(description="Combine private trace benchmark manifests")
    parser.add_argument("--ops", default=str(trace_root / "ops-trace-cases.json"))
    parser.add_argument(
        "--conversation", default=str(trace_root / "conversation-trace-cases.json")
    )
    parser.add_argument("--output", default=str(trace_root / "trace-cases.json"))
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    payload = write_dataset(
        output,
        Path(args.ops).expanduser().resolve(),
        Path(args.conversation).expanduser().resolve(),
    )
    print(json.dumps({"output": str(output), **payload["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
