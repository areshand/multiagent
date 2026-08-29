#!/usr/bin/env python3
"""Container entrypoint for production multiagent SWE-bench submissions."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import swe_prod_contracts as _contracts
from . import swe_prod_lifecycle as _lifecycle


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
    parser.add_argument("--prompt-profile", choices=["swe", "conversation"], default="swe")
    args = parser.parse_args(argv[1:])
    return _lifecycle.run_prod_solver(
        args.prompt,
        Path(args.workdir),
        Path(args.multiagent_root),
        args.timeout,
        args.prompt_profile,
    )


run_prod_solver = _lifecycle.run_prod_solver


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv))
