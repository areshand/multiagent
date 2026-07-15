#!/usr/bin/env python3
"""Production multiagent SWE solver entrypoint.

The implementation is split by responsibility:

- swe_prod_contracts sanitizes public task inputs and derives contracts.
- swe_prod_bootstrap installs task-container helper tools.
- swe_prod_repository owns source discovery and final-diff handling.
- multiagent_framework owns exact-diff, verification, status, and coding guardrail primitives.
- swe_prod_state adapts those primitives to SWE runtime artifacts and probes.
- swe_prod_orchestration owns orchestrator repair and resume messages.
- swe_prod_lifecycle runs the production solver lifecycle.

Public helpers are re-exported temporarily for compatibility with existing
callers. New code should import the owning module directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import types
from pathlib import Path

try:
    from . import swe_prod_bootstrap as _bootstrap
    from . import swe_prod_checkpoints as _checkpoints
    from . import swe_prod_contracts as _contracts
    from . import swe_prod_evidence as _evidence
    from . import swe_prod_lifecycle as _lifecycle
    from . import swe_prod_orchestration as _orchestration
    from . import swe_prod_repository as _repository
    from . import swe_prod_state as _state
    from . import swe_prod_transitions as _transitions
    from . import swe_prod_validation as _validation
    from .swe_prod_contracts import *  # noqa: F403
    from .swe_prod_bootstrap import *  # noqa: F403
    from .swe_prod_repository import *  # noqa: F403
    from .swe_prod_state import *  # noqa: F403
    from .swe_prod_orchestration import *  # noqa: F403
    from .swe_prod_lifecycle import run_prod_solver
except ImportError:  # pragma: no cover - direct execution in task containers
    import swe_prod_bootstrap as _bootstrap  # type: ignore
    import swe_prod_checkpoints as _checkpoints  # type: ignore
    import swe_prod_contracts as _contracts  # type: ignore
    import swe_prod_evidence as _evidence  # type: ignore
    import swe_prod_lifecycle as _lifecycle  # type: ignore
    import swe_prod_orchestration as _orchestration  # type: ignore
    import swe_prod_repository as _repository  # type: ignore
    import swe_prod_state as _state  # type: ignore
    import swe_prod_transitions as _transitions  # type: ignore
    import swe_prod_validation as _validation  # type: ignore
    from swe_prod_contracts import *  # type: ignore  # noqa: F403
    from swe_prod_bootstrap import *  # type: ignore  # noqa: F403
    from swe_prod_repository import *  # type: ignore  # noqa: F403
    from swe_prod_state import *  # type: ignore  # noqa: F403
    from swe_prod_orchestration import *  # type: ignore  # noqa: F403
    from swe_prod_lifecycle import run_prod_solver  # type: ignore


_IMPLEMENTATION_MODULES = (
    _contracts,
    _bootstrap,
    _repository,
    _evidence,
    _validation,
    _state,
    _orchestration,
    _checkpoints,
    _transitions,
    _lifecycle,
)
_COMPATIBLE_OVERRIDES = {
    "APPLY_PATCH_WRAPPER",
    "CONTRACT_LEDGER_PATH",
    "DEFAULT_MULTIAGENT_ROOT",
    "DEFAULT_WORKDIR",
    "FAILURE_DIAGNOSTICS_PATH",
    "HELPER_PROBE_PATH",
    "MULTI_VALUE_PROBE_PATH",
    "RUNTIME_ROOT",
    "SOURCE_OWNER_CANDIDATES_PATH",
    "STABLE_APPLY_PATCH",
    "STALE_VISIBLE_RECONCILIATION_PATH",
    "STATUS_PATH",
    "coverage_probe_commands",
    "git_diff",
    "run",
}


class _CompatibilityFacade(types.ModuleType):
    """Keep legacy test/runtime overrides synchronized during module extraction."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _COMPATIBLE_OVERRIDES:
            return
        for implementation_module in _IMPLEMENTATION_MODULES:
            if hasattr(implementation_module, name):
                setattr(implementation_module, name, value)


sys.modules[__name__].__class__ = _CompatibilityFacade


def _publish_crash_status(payload: dict[str, object]) -> None:
    """Write crash state through the entrypoint configured status path."""

    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)  # noqa: F405
    temporary_path = STATUS_PATH.with_name(STATUS_PATH.name + ".tmp")  # noqa: F405
    temporary_path.write_text(json.dumps(payload), encoding="utf-8")
    temporary_path.replace(STATUS_PATH)  # noqa: F405


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--workdir", default=os.environ.get("EVAL_TASK_WORKDIR", str(DEFAULT_WORKDIR)))  # noqa: F405
    parser.add_argument(
        "--multiagent-root",
        default=os.environ.get("MULTIAGENT_REPO_ROOT", str(DEFAULT_MULTIAGENT_ROOT)),  # noqa: F405
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("EVAL_PROD_MULTIAGENT_TIMEOUT", "3300")),
    )
    args = parser.parse_args(argv[1:])
    try:
        return run_prod_solver(args.prompt, Path(args.workdir), Path(args.multiagent_root), args.timeout)
    except Exception as exc:
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)  # noqa: F405
        FAILURE_DIAGNOSTICS_PATH.write_text(traceback.format_exc(), encoding="utf-8")  # noqa: F405
        _publish_crash_status(
            {
                "status": "blocked",
                "reason": "production multiagent solver crashed before reaching a terminal state",
                "blockers": [f"{type(exc).__name__}: {exc}"],
                "failure_diagnostics": str(FAILURE_DIAGNOSTICS_PATH),  # noqa: F405
            }
        )
        log(f"production solver crashed: {type(exc).__name__}: {exc}")  # noqa: F405
        return 1


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv))
