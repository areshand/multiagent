#!/usr/bin/env python3
"""Production multiagent SWE solver entrypoint for task containers.

This runs the actual multiagent launcher from a repo copied into
``/opt/multiagent`` and points it at the SWE task checkout in ``/app``. The
only eval-specific behavior is the bootstrap instruction contract: solve the
given SWE issue autonomously, consolidate the accepted patch back into /app,
and write a completion marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from evaluation.support.coding import contracts as support_contracts

from .swe_prod_guardrails import (
    changed_go_package_args,
    coverage_probe_commands,
    dependency_contract_changed,
    failed_validation_return_code,
    helper_preservation_evidence,
    helper_scope_hints,
    implementation_scope_blockers,
    required_public_symbols,
    source_symbol_changes,
)


DEFAULT_MULTIAGENT_ROOT = Path("/opt/multiagent")
DEFAULT_WORKDIR = Path("/app")
RUNTIME_ROOT = Path("/tmp/multiagent-prod-swe")
STATUS_PATH = RUNTIME_ROOT / "status.json"
TERMINAL_OUTCOME_PATH = RUNTIME_ROOT / "terminal-outcome.json"
HELPER_PROBE_PATH = RUNTIME_ROOT / "helper-validation-probe.txt"
MULTI_VALUE_PROBE_PATH = RUNTIME_ROOT / "multi-value-probe.txt"
STALE_VISIBLE_RECONCILIATION_PATH = RUNTIME_ROOT / "stale-visible-reconciliation.txt"
CONTRACT_LEDGER_PATH = RUNTIME_ROOT / "contract-ledger.md"
SOURCE_OWNER_CANDIDATES_PATH = RUNTIME_ROOT / "source-owner-candidates.md"
FAILURE_DIAGNOSTICS_PATH = RUNTIME_ROOT / "failure-diagnostics.txt"
RUNTIME_IDENTITY_PATH = RUNTIME_ROOT / "runtime-identity.json"
TASK_METADATA_PATH = Path(os.environ.get("EVAL_TASK_METADATA_FILE", "/tmp/evalscope-native-multiagent-metadata.json"))
CODEX_WRAPPER = RUNTIME_ROOT / "codex-bridge"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/root/.codex-multiagent-prod"))
APPLY_PATCH_WRAPPER = RUNTIME_ROOT / "apply_patch"
STABLE_APPLY_PATCH = Path("/usr/local/bin/apply_patch")
ACTIVE_START_HEAD: str | None = None
PUBLIC_SOLVER_METADATA_KEYS = {
    "language",
    "problem_statement",
}
PRIVATE_SOLVER_METADATA_KEYS = {
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "base_commit",
    "fail_to_pass",
    "interface",
    "pass_to_pass",
    "requirements",
    "run_script_dir",
    "selected_test_files_to_run",
    "test_patch",
}


def env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TEMPLATE_DIRS = [
    Path(__file__).resolve().with_name("templates"),
    Path(__file__).with_name("templates"),
]


def read_template(name: str) -> str:
    for template_dir in TEMPLATE_DIRS:
        path = template_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    searched = ", ".join(str(template_dir / name) for template_dir in TEMPLATE_DIRS)
    raise FileNotFoundError(f"missing native solver template {name}; searched: {searched}")


AUTONOMOUS_APPENDIX = read_template("swe_autonomous_appendix.md")
AUTONOMOUS_FINAL_OVERRIDE = read_template("swe_autonomous_final_override.md")


def log(message: str) -> None:
    print(f"[prod-multiagent-swe] {message}", flush=True)


def remove_prefix(value: str, prefix: str) -> str:
    """Python 3.8-compatible equivalent of ``str.removeprefix``."""

    return value[len(prefix) :] if value.startswith(prefix) else value


def read_prompt(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    env_path = os.environ.get("EVAL_TASK_PROMPT_FILE")
    if env_path:
        return Path(env_path).read_text(encoding="utf-8")
    return sys.stdin.read()


def read_task_metadata() -> dict[str, object]:
    if not TASK_METADATA_PATH.exists():
        return {}
    try:
        parsed = json.loads(TASK_METADATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log(f"ignoring invalid task metadata JSON at {TASK_METADATA_PATH}: {exc}")
        return {}
    if not isinstance(parsed, dict):
        return {}
    sanitized = public_solver_metadata(parsed)
    if sanitized != parsed:
        log("stripped non-public task metadata before solver prompting")
    return sanitized


def public_solver_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Return only metadata that cannot disclose the benchmark answer.

    The EvalScope runner already writes a sanitized metadata file, but the
    production solver is a trust boundary too. This keeps old task images,
    manual invocations, or future adapters from injecting expected tests, test
    patches, official requirements, row identity, repository identity, or
    row-specific hidden contracts into the multi-agent prompt path.
    """

    public: dict[str, object] = {
        key: value
        for key, value in metadata.items()
        if key in PUBLIC_SOLVER_METADATA_KEYS and key not in PRIVATE_SOLVER_METADATA_KEYS
    }
    nested = metadata.get("swe_bench_pro")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key in PUBLIC_SOLVER_METADATA_KEYS and key not in public:
                public[key] = value
    return public


def official_test_contract(metadata: dict[str, object]) -> dict[str, object]:
    """Compatibility result for callers that predate public-input sanitizing."""

    _ = metadata
    return {
        "instance_id": None,
        "fail_to_pass": [],
        "pass_to_pass": [],
        "selected_test_files_to_run": [],
        "expected_test_count": 0,
    }


def metadata_problem_text(metadata: dict[str, object] | None) -> str:
    if not metadata:
        return ""
    metadata = public_solver_metadata(metadata)
    problem_statement = metadata.get("problem_statement")
    return str(problem_statement) if problem_statement else ""


def issue_with_public_problem_text(issue: str, metadata: dict[str, object] | None = None) -> str:
    problem = metadata_problem_text(metadata)
    if not problem:
        return issue
    if problem.strip() == issue.strip():
        return issue
    if "</pr_description>" in issue and problem.strip() not in issue:
        return re.sub(
            r"\s*</pr_description>",
            "\n\n" + problem.rstrip() + "\n</pr_description>",
            issue,
            count=1,
            flags=re.IGNORECASE,
        )
    return issue.rstrip() + "\n\n" + problem






SWE_ISSUE_ENVELOPE_MARKERS = (
    "\n## Overview\n\nYou're a software engineer",
    "\nCurrent `/app` diff excerpt",
)


def public_issue_text_for_coverage(issue: str) -> str:
    return support_contracts.public_issue_text(issue, SWE_ISSUE_ENVELOPE_MARKERS)


def issue_coverage_requirements(issue: str) -> list[dict[str, object]]:
    return support_contracts.issue_coverage_requirements(public_issue_text_for_coverage(issue))


def issue_coverage_blockers(issue: str, evidence_text: str) -> list[str]:
    return support_contracts.issue_coverage_blockers(public_issue_text_for_coverage(issue), evidence_text)


data_provenance_required = support_contracts.data_provenance_required
data_provenance_blockers = support_contracts.data_provenance_blockers
historical_contract_required = support_contracts.historical_contract_required
historical_contract_blockers = support_contracts.historical_contract_blockers


def contract_ledger_text(issue: str, metadata: dict[str, object] | None = None) -> str:
    # Framework completion rules include declared type at that call site proof.
    solver_metadata = public_solver_metadata(metadata or {})
    coverage_issue = issue_with_public_problem_text(issue, solver_metadata)
    symbols = required_public_symbols(coverage_issue, solver_metadata)
    contract_excerpt = metadata_problem_text(solver_metadata)
    ledger = support_contracts.ContractLedger.from_issue(
        public_issue_text_for_coverage(coverage_issue),
        public_symbols=symbols,
        context_excerpt=contract_excerpt,
    )
    return support_contracts.render_contract_ledger(
        ledger,
        title="SWE Bench Pro Contract Ledger",
        introduction=(
            "This file is generated by the benchmark adapter from public solver inputs.",
            "Treat task/source evidence here as a durable invariant.",
            "Follow-up workers and verifiers must preserve all items, even when fixing a later verifier finding.",
            "Do not use leaked evaluator tests, hidden row names, non-public evaluator rows, or benchmark-only metadata as implementation guidance.",
        ),
        context_label="Public task requirements/interface excerpt:",
    )


def write_contract_ledger(issue: str, metadata: dict[str, object] | None = None) -> Path:
    CONTRACT_LEDGER_PATH.write_text(contract_ledger_text(issue, metadata), encoding="utf-8")
    return CONTRACT_LEDGER_PATH


def contract_ledger_excerpt(limit: int = 6000) -> str:
    if not CONTRACT_LEDGER_PATH.exists():
        return "Contract ledger has not been generated yet."
    return CONTRACT_LEDGER_PATH.read_text(encoding="utf-8", errors="replace")[-limit:]


def contract_coverage_items_excerpt(
    issue: str,
    metadata: dict[str, object] | None = None,
    limit: int = 5000,
) -> str:
    public_issue = public_issue_text_for_coverage(issue_with_public_problem_text(issue, metadata))
    return support_contracts.contract_coverage_items_excerpt(public_issue, limit=limit)


def official_expected_test_blockers(metadata: dict[str, object], current_status: dict[str, object]) -> list[str]:
    """Never gate production solving on official expected-test metadata."""

    _ = metadata, current_status
    return []


def official_expected_tests_satisfied_by_text(metadata: dict[str, object], text: str) -> bool:
    """Production no-leak mode never treats expected-test claims as evidence."""

    _ = metadata, text
    return False


def recovered_validation_text(metadata: dict[str, object], text: str, base: str) -> str:
    """Recover only public validation text; do not append official-test claims."""

    _ = metadata, text
    return base


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    safe_args = [
        arg.replace("\x00", "") if isinstance(arg, str) else arg
        for arg in args
    ]
    result = subprocess.run(safe_args, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(safe_args)}\n{tail}")
    return result
