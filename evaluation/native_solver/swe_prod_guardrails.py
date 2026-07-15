"""Compatibility facade for framework-owned coding guardrails."""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_framework_import_path() -> None:
    candidates = (Path(__file__).resolve().parent, Path(__file__).resolve().parents[2])
    for candidate in candidates:
        if (candidate / "multiagent_framework").is_dir() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))


_ensure_framework_import_path()

from multiagent_framework.coding.guardrails import *  # noqa: E402,F403
