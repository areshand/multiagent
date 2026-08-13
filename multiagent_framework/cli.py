"""Locate and invoke the Rust multiagent control-plane executable."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def multiagent_command(framework_root: Path) -> list[str]:
    """Return the executable prefix for the Rust CLI, or an empty list if absent."""

    configured = os.environ.get("MULTIAGENT_BIN", "").strip()
    candidates = [
        Path(configured) if configured else None,
        framework_root / "bin" / "multiagent",
        framework_root / "target" / "release" / "multiagent",
        framework_root / "target" / "debug" / "multiagent",
    ]
    installed = shutil.which("multiagent")
    if installed:
        candidates.append(Path(installed))
    for candidate in candidates:
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
    return []


def multiagent_subcommand(framework_root: Path, command: str, *args: str) -> list[str]:
    """Build a Rust CLI argv vector for one control-plane subcommand."""

    executable = multiagent_command(framework_root)
    return [*executable, command, *args] if executable else []
