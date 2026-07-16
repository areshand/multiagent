"""Compatibility facade with explicit framework-owned coding guardrails."""

from __future__ import annotations

from multiagent_framework.coding.guardrails import (
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

__all__ = [
    "changed_go_package_args",
    "coverage_probe_commands",
    "dependency_contract_changed",
    "failed_validation_return_code",
    "helper_preservation_evidence",
    "helper_scope_hints",
    "implementation_scope_blockers",
    "required_public_symbols",
    "source_symbol_changes",
]
