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
import traceback
import types
from pathlib import Path

from . import swe_prod_bootstrap as _bootstrap
from . import swe_prod_checkpoints as _checkpoints
from . import swe_prod_contracts as _contracts
from . import swe_prod_evidence as _evidence
from . import swe_prod_lifecycle as _lifecycle
from . import swe_prod_orchestration as _orchestration
from . import swe_prod_repository as _repository
from . import swe_prod_transitions as _transitions
from . import swe_prod_validation as _validation


_IMPLEMENTATION_MODULES = (
    _contracts,
    _bootstrap,
    _repository,
    _evidence,
    _validation,
    _orchestration,
    _checkpoints,
    _transitions,
    _lifecycle,
)
_LEGACY_EXPORT_MODULES = (
    _contracts,
    _bootstrap,
    _repository,
    _evidence,
    _validation,
    _orchestration,
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
    "RUNTIME_IDENTITY_PATH",
    "SOURCE_OWNER_CANDIDATES_PATH",
    "STABLE_APPLY_PATCH",
    "STALE_VISIBLE_RECONCILIATION_PATH",
    "STATUS_PATH",
    "TERMINAL_OUTCOME_PATH",
    "coverage_probe_commands",
    "git_diff",
    "run",
    "run_prod_solver",
}


def _publish_legacy_exports() -> None:
    """Materialize the helper surface formerly produced by wildcard imports."""

    namespace = globals()
    for implementation_module in _LEGACY_EXPORT_MODULES:
        for name, value in vars(implementation_module).items():
            if not name.startswith("_"):
                namespace[name] = value
    namespace["run_prod_solver"] = _lifecycle.run_prod_solver


_publish_legacy_exports()
del _publish_legacy_exports


class _CompatibilityFacade(types.ModuleType):
    """Keep legacy test/runtime overrides synchronized during module extraction."""

    def __getattr__(self, name: str) -> object:
        for implementation_module in _IMPLEMENTATION_MODULES:
            if hasattr(implementation_module, name):
                return getattr(implementation_module, name)
        raise AttributeError(f"module {self.__name__!r} has no attribute {name!r}")

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name not in _COMPATIBLE_OVERRIDES:
            return
        for implementation_module in _IMPLEMENTATION_MODULES:
            if hasattr(implementation_module, name):
                setattr(implementation_module, name, value)


os.sys.modules[__name__].__class__ = _CompatibilityFacade


def _publish_crash_status(payload: dict[str, object]) -> None:
    """Write crash state through the entrypoint configured status path."""

    _contracts.STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
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
                "reason": "production multiagent solver crashed before reaching a terminal state",
                "blockers": [f"{type(exc).__name__}: {exc}"],
                "failure_diagnostics": str(_contracts.FAILURE_DIAGNOSTICS_PATH),
            }
        )
        _contracts.log(f"production solver crashed: {type(exc).__name__}: {exc}")
        return 1


__all__ = sorted(name for name in globals() if not name.startswith("_"))


if __name__ == "__main__":
    raise SystemExit(main(os.sys.argv))
