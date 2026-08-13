"""Atomic machine-readable lifecycle state for evaluation processes."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path


TERMINAL_STATES = frozenset({"blocked", "completed", "complete", "done"})


@dataclass(frozen=True)
class AtomicStatusStore:
    path: Path
    settle_seconds: float = 0.2

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and str(parsed.get("status", "")).lower() in TERMINAL_STATES:
                if self.settle_seconds > 0:
                    time.sleep(self.settle_seconds)
                if self.path.read_text(encoding="utf-8") != raw:
                    return {"status": "publishing"}
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {
                "status": "invalid-json",
                "raw": self.path.read_text(encoding="utf-8", errors="replace")[-1000:],
            }

    def publish(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_name(self.path.name + ".tmp")
        temporary_path.write_text(json.dumps(payload), encoding="utf-8")
        temporary_path.replace(self.path)
