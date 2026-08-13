"""Terminal outcome contract shared by solver and benchmark evaluator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 1
SUBMISSION_GATE_REJECTION = "submission_gate_rejection"
SUBMISSION_GATE_REJECTION_EXIT_CODE = 3


def publish_terminal_outcome(
    path: Path,
    *,
    outcome: str,
    reason: str,
    blockers: Iterable[str] = (),
) -> dict[str, object]:
    """Atomically publish a production-owned terminal outcome."""

    if outcome != SUBMISSION_GATE_REJECTION:
        raise ValueError(f"unsupported terminal outcome: {outcome}")
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "reason": reason,
        "blockers": [str(blocker) for blocker in blockers],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    temporary.replace(path)
    return payload


def load_terminal_outcome(path: Path) -> dict[str, object]:
    """Load and validate a terminal outcome, returning an empty object on mismatch."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    if payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    if payload.get("outcome") != SUBMISSION_GATE_REJECTION:
        return {}
    if not isinstance(payload.get("reason"), str) or not str(payload["reason"]).strip():
        return {}
    blockers = payload.get("blockers")
    if not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
        return {}
    return payload
