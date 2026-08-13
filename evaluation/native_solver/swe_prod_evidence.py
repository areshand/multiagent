"""Runtime observation helpers for the SWE-bench adapter."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from evaluation.support.state import AtomicStatusStore

from .swe_prod_contracts import RUNTIME_ROOT, STATUS_PATH, log, run


def status() -> dict[str, object]:
    """Read solver status as a lifecycle signal, never as patch acceptance."""

    settle_seconds = float(
        os.environ.get("MULTIAGENT_STATUS_SETTLE_SECONDS", os.environ.get("EVAL_STATUS_SETTLE_SECONDS", "0.2"))
    )
    return AtomicStatusStore(STATUS_PATH, settle_seconds=settle_seconds).read()


def capture_session(session: str) -> None:
    """Persist recent tmux output for post-run diagnostics."""

    out_dir = RUNTIME_ROOT / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = run(["tmux", "list-windows", "-t", session, "-F", "#W"], timeout=20)
    if windows.returncode != 0:
        return
    for name in windows.stdout.splitlines():
        if not name.strip():
            continue
        capture = run(["tmux", "capture-pane", "-t", f"{session}:{name}", "-p", "-S", "-2000"], timeout=30)
        if capture.returncode == 0:
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
            (out_dir / f"{safe}.txt").write_text(capture.stdout, encoding="utf-8")


def tmux_has_session(session: str) -> bool:
    return run(["tmux", "has-session", "-t", session], timeout=10).returncode == 0


def find_codex_cli() -> str | None:
    found = shutil.which("codex")
    if found:
        return found
    for candidate in (
        Path("/opt/node22/bin/codex"),
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
        Path("/root/.npm-global/bin/codex"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def toolchain_path_prefixes() -> list[str]:
    prefixes: list[str] = []
    for candidate in (
        Path("/usr/local/go/bin"),
        Path("/usr/lib/go/bin"),
        Path("/opt/go/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ):
        if candidate.exists() and (candidate / "go").exists():
            prefixes.append(str(candidate))
    return prefixes


def ensure_cache_dir(path: Path) -> str:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log(f"could not create cache directory {path}: {exc}")
    return str(path)
