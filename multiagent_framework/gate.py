"""Framework submission-gate integration."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable


CommandRunner = Callable[..., subprocess.CompletedProcess]


def structured_repair_gate_blockers(
    *,
    framework_root: Path,
    worktree: Path,
    state_dirs: Iterable[Path],
    runner: CommandRunner = subprocess.run,
    timeout: int = 30,
) -> list[str]:
    """Run the durable finding/todo gate for each populated state store."""

    subagent = framework_root / "bin/subagent.sh"
    if not subagent.exists():
        return []

    blockers: list[str] = []
    seen_state_dirs: set[Path] = set()
    for state_dir in state_dirs:
        state_dir = Path(state_dir)
        if state_dir in seen_state_dirs:
            continue
        seen_state_dirs.add(state_dir)
        if not any((state_dir / name).exists() for name in ("findings", "todos")):
            continue
        env = os.environ.copy()
        env.update({"MULTIAGENT_ROOT": str(worktree), "MULTIAGENT_STATE_DIR": str(state_dir)})
        result = runner(
            [str(subagent), "gate-check"],
            cwd=framework_root,
            env=env,
            timeout=timeout,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if result.returncode != 0:
            blockers.append(
                "structured repair gate rejects completed status for "
                f"{state_dir}: {output[-2000:] or 'gate-check failed without output'}"
            )
    return blockers
