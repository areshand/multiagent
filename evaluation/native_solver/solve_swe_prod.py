#!/usr/bin/env python3
"""Container entrypoint for production multiagent SWE-bench submissions."""

from __future__ import annotations

import argparse
import json
import os
import traceback
from pathlib import Path

from . import swe_prod_contracts as _contracts
from . import swe_prod_lifecycle as _lifecycle


def _publish_crash_status(payload: dict[str, object]) -> None:
    _contracts.RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = _contracts.STATUS_PATH.with_name(_contracts.STATUS_PATH.name + ".tmp")
    temporary_path.write_text(json.dumps(payload), encoding="utf-8")
    temporary_path.replace(_contracts.STATUS_PATH)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--workdir", default=os.environ.get("EVAL_TASK_WORKDIR", str(_contracts.DEFAULT_WORKDIR)))
    parser.add_argument(
        "--multiagent-root",
        default=os.environ.get("MULTIAGENT_REPO_ROOT", str(_contracts.DEFAULT_MULTIAGENT_ROOT)),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("EVAL_PROD_MULTIAGENT_TIMEOUT", "3300")),
    )
    args = parser.parse_args(argv[1:])
    try:
        return _lifecycle.run_prod_solver(args.prompt, Path(args.workdir), Path(args.multiagent_root), args.timeout)
    except Exception as exc:
        _contracts.RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        _contracts.FAILURE_DIAGNOSTICS_PATH.write_text(traceback.format_exc(), encoding="utf-8")
        _publish_crash_status(
            {
                "status": "blocked",
                "reason": "production multiagent solver crashed before submission handoff",
                "blockers": [f"{type(exc).__name__}: {exc}"],
                "failure_diagnostics": str(_contracts.FAILURE_DIAGNOSTICS_PATH),
            }
        )
        _contracts.log(f"production solver crashed: {type(exc).__name__}: {exc}")
        return 1


run_prod_solver = _lifecycle.run_prod_solver


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv))
