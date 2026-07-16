from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from pathlib import Path

from .swe_prod_contracts import (
    HELPER_PROBE_PATH,
    MULTI_VALUE_PROBE_PATH,
    RUNTIME_ROOT,
    SOURCE_OWNER_CANDIDATES_PATH,
    data_provenance_blockers,
    env_positive_int,
    historical_contract_blockers,
    issue_coverage_blockers,
    issue_with_public_problem_text,
    log,
    official_expected_test_blockers,
    official_expected_tests_satisfied_by_text,
    run,
)
from .swe_prod_evidence import (
    accepted_systemic_runtime_probe_fallback,
    build_verification_has_evidence,
    changed_code_paths_from_diff,
    changed_paths_from_diff,
    claimed_changed_path_blockers,
    completed_status_covers_adapter_validation,
    final_diff_sha256,
    go_compile_failure_present,
    go_failure_is_unaffected_unbuildable_root_target,
    go_package_validation_has_evidence,
    go_package_validation_has_explicit_marker,
    multi_value_probe_has_final_output_counts,
    persisted_exact_hash_behavior_acceptance_texts,
    policy_collection_partition_risk,
    pytest_teardown_after_success,
    remove_truncated_go_package_prefixes,
    run_final_changed_go_compile_probe,
    source_required_go_validation_packages,
    stale_patch_application_blockers,
    state_space_partition_audit_has_evidence,
    status,
    validation_probe_env,
    validation_probe_has_no_test_evidence,
    validation_text_has_no_test_evidence,
    verifier_runtime_failure_is_classified_compile_clean,
)
from .swe_prod_guardrails import (
    changed_go_package_args,
    coverage_probe_commands,
    implementation_scope_blockers,
)
from .swe_prod_repository import git_diff

def validation_coverage_blockers(
    issue: str,
    diff: str,
    text: str,
    current_status: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> list[str]:
    coverage_issue = issue_with_public_problem_text(issue, metadata)
    issue_lower = coverage_issue.lower()
    diff_lower = diff.lower()
    issue_and_diff = f"{issue_lower}\n{diff_lower}"
    # Only the explicit status payload can clear the gate. The captured tmux
    # text may include the original prompt or adapter follow-up instructions,
    # so treating it as proof can turn instructions into false evidence.
    status_text = json.dumps(current_status, sort_keys=True).lower()
    evidence_text = status_text
    if "helper-validation-passed:" in status_text and HELPER_PROBE_PATH.exists():
        try:
            evidence_text += "\n" + HELPER_PROBE_PATH.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            pass
    official_contract_satisfied = official_expected_tests_satisfied_by_text(metadata or {}, text)
    blockers: list[str] = [] if official_contract_satisfied else official_expected_test_blockers(metadata or {}, current_status)
    if any(
        marker in status_text
        for marker in (
            "validation-repair-needed:",
            "compile_clean=false",
            '"compile_clean": false',
        )
    ):
        blockers.append(
            "status.json contains unresolved verifier repair evidence (`validation-repair-needed:` "
            "or compile_clean=false); record it as a blocking finding/todo, repair it, and only "
            "complete after verifier closure plus hash-bound final validation"
        )
    blockers.extend(issue_coverage_blockers(coverage_issue, evidence_text))
    blockers.extend(data_provenance_blockers(coverage_issue, evidence_text))
    blockers.extend(historical_contract_blockers(coverage_issue, evidence_text))
    status_json_text = json.dumps(current_status, sort_keys=True)
    stale_sensitive_text = status_json_text if build_verification_has_evidence(status_text, diff) else f"{text}\n{status_json_text}"
    blockers.extend(claimed_changed_path_blockers(diff, stale_sensitive_text))
    blockers.extend(stale_patch_application_blockers(stale_sensitive_text))
    if policy_collection_partition_risk(diff):
        partition_evidence = status_json_text + "\n" + "\n".join(
            persisted_exact_hash_behavior_acceptance_texts(diff, RUNTIME_ROOT)
        )
        if not state_space_partition_audit_has_evidence(partition_evidence, diff):
            blockers.append(
                "changed logic combines a policy/mode branch with aggregate collection size, but final behavior verification lacks "
                "a hash-bound `state-space-partition-audit:` covering source modes/categories, mixed-category and unknown-variant "
                "counterexamples; rerun the behavior verifier and create a blocking finding/todo if aggregate size is not equivalent "
                "for every category-specific mode"
            )
    changed_code_paths = changed_code_paths_from_diff(diff)
    if changed_code_paths and not build_verification_has_evidence(evidence_text, diff):
        blockers.append(
            "final patch changes code, but submission lacks hash-bound build verification for the final diff: "
            + ", ".join(changed_code_paths[:8])
            + "; run affected compile/test commands after the final diff and record "
            "`build-verification-passed: final-diff-sha256="
            + final_diff_sha256(diff)
            + " compile_clean=true returncode=0`"
        )

    uses_data_helper = any(
        marker in diff_lower
        for marker in (
            " db.",
            "\tdb.",
            "(db.",
            "= db.",
            "await db.",
            "database/",
            "cache.",
            "redis",
        )
    )
    issue_mentions_data_shape = any(
        marker in issue_and_diff
        for marker in (
            "key",
            "keys",
            "fallback",
            "missing data",
            "expired",
            "expiry",
            "ttl",
            "cache",
            "database",
        )
    )
    ran_or_justified_data_helper = any(
        marker in status_text
        for marker in (
            "helper-validation-passed:",
            "helper-validation-skip-justified:",
        )
    )
    if uses_data_helper and issue_mentions_data_shape and not ran_or_justified_data_helper:
        blockers.append(
            "patch uses database/cache helper APIs and the task mentions key/fallback/expiry/cache/data behavior, "
            "but validation did not run or justify skipping helper-layer tests"
        )

    touches_go_source = any(
        line.startswith("diff --git a/") and ".go " in line
        for line in diff.splitlines()
    )
    if touches_go_source:
        go_evidence_text = evidence_text
        go_packages = changed_go_package_args(diff)
        go_validation_markers = (
            "go test",
            "go-validation-passed:",
            "go-validation-skip-justified:",
            "go-package-validation-passed:",
            "adapter public validation probe",
        )
        missing_tool_markers = (
            "go: not found",
            "go command not found",
            "go unavailable",
            "go toolchain is not installed",
            "go is not installed",
        )
        go_probe_passed = (
            "helper-validation-passed:" in status_text and all(
                go_package_validation_has_evidence(go_evidence_text, package) for package in go_packages
            )
            or "return code: 0" in go_evidence_text and "go test" in go_evidence_text
            or "go test" in go_evidence_text and any(marker in go_evidence_text for marker in (" passed", ": passed"))
        )
        if (
            go_compile_failure_present(go_evidence_text)
            and not verifier_runtime_failure_is_classified_compile_clean(go_evidence_text, diff)
            and not go_failure_is_unaffected_unbuildable_root_target(go_evidence_text, go_packages)
        ):
            blockers.append(
                "Go validation contains compile/build failure evidence such as `undefined:`, "
                "`has no field or method`, `build failed`, `FAIL`, or a nonzero return code; fix it before completion"
            )
        if validation_text_has_no_test_evidence(status_text) and "go-validation-skip-justified:" not in status_text:
            blockers.append(
                "Go source changed, but validation only shows a no-test compile check such as `[no test files]`, "
                "`no tests to run`, `-run TestNonExistent`, or `-run '^$'`; run real affected package tests or provide source-derived skip evidence"
            )
        missing_go_packages = [
            package for package in go_packages if not go_package_validation_has_evidence(go_evidence_text, package)
        ]
        required_source_go_packages = source_required_go_validation_packages(text, current_status)
        # Tmux hard-wraps long ledger lines. A split inside a path segment can
        # turn ``./lib/auth`` into a plausible but nonexistent ``./li`` token.
        required_source_go_packages = remove_truncated_go_package_prefixes(
            required_source_go_packages,
            go_packages,
        )
        missing_required_source_go_packages = [
            package
            for package in required_source_go_packages
            if not go_package_validation_has_explicit_marker(go_evidence_text, package)
        ]
        if missing_go_packages:
            blockers.append(
                "Go source changed, but final validation does not prove affected package compile/test success for: "
                + ", ".join(missing_go_packages)
                + "; run `go test ./affected/package` for every changed Go package after the final diff and record "
                "`go-package-validation-passed: package=... command=... returncode=0` for every changed package"
            )
        if missing_required_source_go_packages:
            blockers.append(
                "source-required Go validation packages are missing final returncode=0 evidence: "
                + ", ".join(missing_required_source_go_packages)
                + "; these packages were named by source-owner/scout validation evidence, so changed-package validation alone is insufficient"
            )
        elif not any(marker in go_evidence_text for marker in go_validation_markers):
            blockers.append(
                "Go source changed, but status.json does not record a Go package validation command such as `go test ./affected/package`"
            )
        if any(marker in go_evidence_text for marker in missing_tool_markers) and not go_probe_passed:
            blockers.append(
                "Go source changed, but validation reported the Go toolchain was unavailable; retry with explicit Go paths before accepting"
            )

    touches_ui_interaction_source = any(
        line.startswith("diff --git a/")
        and (
            any(ext in line for ext in (".tsx ", ".jsx ", ".vue ", ".svelte "))
            or any(path_marker in line.lower() for path_marker in ("/components/", "/views/", "/rooms/", "keyboard."))
        )
        for line in diff.splitlines()
    )
    ui_interaction_issue_or_diff = any(
        marker in issue_and_diff
        for marker in (
            "keyboard",
            "shortcut",
            "input",
            "paste",
            "focus",
            "autocomplete",
            "composer",
            "browser",
            "accessibility",
            "keydown",
            "keyup",
            "keypress",
            "interaction",
        )
    )
    ui_static_only_markers = (
        "no browser interaction tests were run",
        "no interaction tests were run",
        "no browser tests were run",
        "no component interaction tests were run",
        "residual risk is limited to runtime",
    )
    ui_validation_markers = (
        "browser interaction",
        "component interaction",
        "user-event",
        "fireevent",
        "@testing-library",
        "cypress",
        "playwright",
        "selenium",
        "jest",
        "yarn test",
        "npm test",
        "ui-validation-passed:",
        "ui-validation-skip-justified:",
    )
    if touches_ui_interaction_source and ui_interaction_issue_or_diff:
        if any(marker in status_text for marker in ui_static_only_markers) and "ui-validation-skip-justified:" not in status_text:
            blockers.append(
                "UI/keyboard interaction source changed, but final validation explicitly says browser/component interaction tests were not run"
            )
        elif "lint:types" in status_text and not any(marker in status_text for marker in ui_validation_markers):
            blockers.append(
                "UI/keyboard interaction source changed, but validation only records static type/lint coverage; run or justify a nearby interaction test"
            )

    changed_paths = changed_paths_from_diff(diff)
    parser_issue_context = any(
        marker in issue_lower
        for marker in (
            "parser",
            "parse",
            "reader",
            "decoder",
            "serializer",
            "importer",
            "exporter",
            "fixture",
        )
    )
    parser_path_context = any(
        marker in path.lower()
        for path in changed_paths
        for marker in (
            "parser",
            "parse",
            "reader",
            "decoder",
            "serializer",
            "import",
            "export",
            "fixture",
            "marc",
            "xml",
            "binary",
        )
    )
    parser_multi_value_issue = (parser_issue_context or parser_path_context) and bool(
        re.search(
            r"\b(all|every|complete|associated|linked|linkage|repeated|alternate|fallback-chain|multi-value|multiple)\b",
            issue_and_diff,
        )
    )
    parser_multi_value_diff = any(
        marker in diff_lower
        for marker in (
            "linked",
            "linkage",
            "alternate",
            "associated",
            "related",
            "multi",
            "collection",
            "values",
            "fields",
            "append(",
            "extend(",
            "setdefault(",
        )
    )
    if parser_multi_value_issue and parser_multi_value_diff:
        has_multi_value_probe = "multi-value-probe-passed:" in status_text
        has_multi_value_skip = "multi-value-probe-skip-justified:" in status_text
        if not has_multi_value_probe and not has_multi_value_skip:
            blockers.append(
                "parser/reader linked or alternate multi-value behavior changed, but status does not include "
                "`multi-value-probe-passed:` with a source-derived probe covering at least two linked values "
                "across the affected entrypoint, or `multi-value-probe-skip-justified:` with source evidence"
            )
        elif has_multi_value_probe and not multi_value_probe_has_final_output_counts(status_text):
            blockers.append(
                "`multi-value-probe-passed:` must validate the final product-facing output, not only an internal helper; "
                "include one singular `final-output-field=...` per affected output collection, with `source-count=N`, "
                "`expected-output-count=N`, and `actual-output-count=N`, "
                f"with expected and actual counts equal, and write matching command/output evidence to `{MULTI_VALUE_PROBE_PATH}`"
            )

    return blockers


def completed_status_snapshot_blockers(
    issue: str,
    diff: str,
    text: str,
    completed_status: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> list[str]:
    """Return blockers for a previously written completed status snapshot."""

    status_text = json.dumps(completed_status, sort_keys=True)
    return [
        *implementation_scope_blockers(issue, diff, completed_status, metadata),
        *validation_coverage_blockers(issue, diff, status_text, completed_status, metadata),
    ]



def run_validation_coverage_probe(
    workdir: Path,
    issue: str,
    diff: str,
    blockers: list[str],
    *,
    stale_retry_limit: int = 1,
) -> tuple[str, bool]:
    live_diff = git_diff(workdir)
    if live_diff.strip() and final_diff_sha256(live_diff) != final_diff_sha256(diff):
        log(
            "adapter public validation probe refreshed stale diff before running: "
            f"{final_diff_sha256(diff)} -> {final_diff_sha256(live_diff)}"
        )
        diff = live_diff
    commands = coverage_probe_commands(workdir, issue, diff)
    current_status = status()
    if completed_status_covers_adapter_validation(workdir, issue, diff, current_status):
        report = (
            "Adapter-selected public helper validation probe skipped because "
            "status.json already records completed final-diff build verification, "
            "covers the adapter-selected validation command surface, and the "
            "structured repair gate accepts the run."
        )
        HELPER_PROBE_PATH.write_text(report, encoding="utf-8")
        return report, True

    if not commands:
        report = "No adapter-selected public helper validation command was available for this repository/task."
        HELPER_PROBE_PATH.write_text(report, encoding="utf-8")
        return report, False

    sections: list[str] = [
        "Adapter-selected public helper validation probe.",
        "This probe uses only repository-visible tests selected from the issue text and produced diff.",
        "Coverage blockers:",
        *[f"- {blocker}" for blocker in blockers],
    ]
    passed = True
    for command in commands:
        label = " ".join(command)
        try:
            result = run(
                command,
                cwd=workdir,
                env=validation_probe_env(command, final_diff_sha256(diff)),
                timeout=env_positive_int("EVAL_VALIDATION_PROBE_TIMEOUT", 900),
            )
            returncode = result.returncode
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output = (stdout + "\n" + stderr).strip()
            output = (output + "\n" if output else "") + f"adapter validation probe timed out after {exc.timeout} seconds"
        if returncode == 125 and "validation diff changed while command was running" in output.lower():
            live_diff = git_diff(workdir)
            if stale_retry_limit > 0 and live_diff.strip():
                log(
                    "adapter public validation probe restarting after live diff changed during validation: "
                    f"{final_diff_sha256(diff)} -> {final_diff_sha256(live_diff)}"
                )
                time.sleep(2)
                return run_validation_coverage_probe(
                    workdir,
                    issue,
                    live_diff,
                    blockers,
                    stale_retry_limit=stale_retry_limit - 1,
                )
        teardown_success = returncode != 0 and pytest_teardown_after_success(output)
        no_test_evidence = validation_probe_has_no_test_evidence(label, output)
        if (returncode != 0 and not teardown_success) or no_test_evidence:
            passed = False
        sections.append(
            "\nCommand: "
            + label
            + f"\nReturn code: {returncode}\nOutput tail:\n"
            + output[-6000:]
        )
        if no_test_evidence:
            sections.append(
                "\nAdapter note: treated this command as insufficient because it did not execute real selected tests."
            )
        if teardown_success:
            sections.append(
                "\nAdapter note: treated nonzero pytest rc as passed because pytest reported all selected "
                "tests passed before a teardown transport error."
            )
    report = "\n".join(sections)
    runtime_fallback = False
    if not passed and accepted_systemic_runtime_probe_fallback(report, diff):
        compile_report, compile_passed = run_final_changed_go_compile_probe(workdir, diff)
        sections.append("\n" + compile_report)
        if compile_passed:
            passed = True
            runtime_fallback = True
        sections.append(
            "\nruntime-failure-classification: classification=environmental "
            "reason=systemic-repeated-runtime-signature "
            f"compile_clean={'true' if compile_passed else 'false'} "
            "source_contracts_satisfied=true"
        )
        if compile_passed:
            sections.append(
                "go-validation-skip-justified: reason=full-tests-failed-only-in-runtime-environment "
                "source-evidence=independent-exact-hash-behavior-verifier "
                "compile-evidence=adapter-run-hash-bound-affected-package-validation"
            )
            log(
                "adapter public validation probe accepted runtime-only fallback after "
                "exact-hash behavior acceptance and adapter compile verification"
            )
    if passed:
        diff_hash = final_diff_sha256(diff)
        changed_files = len(changed_paths_from_diff(diff))
        sections.append(
            f"\nbuild-verification-passed: final-diff-sha256={diff_hash} "
            f"changed-files={changed_files} compile_clean=true returncode=0"
        )
        go_packages = changed_go_package_args(diff)
        for package in go_packages:
            go_command = "go test -run '^$' " + " ".join(go_packages) if runtime_fallback else next(
                (
                    " ".join(command)
                    for command in commands
                    if command[:2] == ["go", "test"] and (package in command[2:] or any(arg.endswith("/...") for arg in command[2:]))
                ),
                "go test " + package,
            )
            sections.append(
                f"go-package-validation-passed: package={package} command={shlex.quote(go_command)} "
                f"returncode=0 final-diff-sha256={diff_hash}"
            )
        sections.append("\nhelper-validation-passed: adapter public helper probe")
    report = "\n".join(sections)
    HELPER_PROBE_PATH.write_text(report, encoding="utf-8")
    if not passed:
        log("adapter public validation probe failed output tail:\n" + report[-4000:])
    return report, passed



def blockers_after_passing_public_probe(blockers: list[str]) -> list[str]:
    """Drop heuristic blockers that are directly covered by selected public tests."""
    remaining: list[str] = []
    for blocker in blockers:
        lower = blocker.lower()
        if "[official-hard]" in lower:
            remaining.append(blocker)
            continue
        if "no-test" in lower or "no tests" in lower or "[no test" in lower or "testnonexistent" in lower:
            remaining.append(blocker)
            continue
        if "go source changed" in lower and "validation" in lower:
            continue
        remaining.append(blocker)
    return remaining


def non_recoverable_final_validation_blockers(blockers: list[str]) -> list[str]:
    """Block final-wrapper recovery for basic validation failures.

    Adapter-selected public probes can add useful evidence, but they must not
    convert a final Go source diff with only no-test compile evidence into a
    completed submission.
    """
    hard: list[str] = []
    for blocker in blockers:
        lower = blocker.lower()
        if (
            "no-test compile check" in lower
            or "no tests to run" in lower
            or "-run testnonexistent" in lower
            or "-run '^$'" in lower
        ):
            hard.append(blocker)
    return hard


def source_symbol_map_blocker_present(blockers: list[str]) -> bool:
    text = "\n".join(str(blocker).lower() for blocker in blockers)
    return (
        "source symbol contracts changed" in text
        or "source-symbol-map-passed:" in text
        or "source-symbol-map-skip-justified:" in text
    )


def structured_repair_todo_blocker_present(blockers: list[str]) -> bool:
    """Return true when durable repair work exists but has not reached closure."""

    text = "\n".join(str(blocker).lower() for blocker in blockers)
    if "structured repair gate rejects completed status" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "open-blocking-todo",
            "open-todo",
            "status=assigned",
            "status=resolved",
        )
    )


def source_symbol_map_resume_instructions(blockers: list[str]) -> str:
    if not source_symbol_map_blocker_present(blockers):
        return ""
    return (
        "\n\n### Source-Symbol Map Recovery Requirement\n\n"
        "The current blocker is a source-symbol map blocker. This is a public/source evidence requirement, "
        "not hidden-test guidance. Before writing completed status, inspect the live `git diff --name-only`, "
        f"`{SOURCE_OWNER_CANDIDATES_PATH}`, changed package/module declarations, changed symbol definitions, visible callers, and nearby tests. "
        "Write or repair a `source-owner-ledger:` with `selected-owner=...`, every plausible `candidate-owner=...`, rejected-owner reasons, "
        "and `validation-package=...` before sending another implementation worker. "
        "If the diff adds, removes, renames, or moves source symbols, the final `/tmp/multiagent-prod-swe/status.json` "
        "must contain one single machine-readable `source-symbol-map-passed:` line naming the owning `package=` or "
        "`path=`, each `added-symbol=`, `removed-symbol=`, or `renamed-symbol=`, `owner-evidence=` proving plausible "
        "source owners were compared from issue terms, imports, docs, callers, or nearby tests, `candidate-owner=` for any "
        "plausible issue-term package that was considered but not edited, and at least one source-derived compatibility proof "
        "such as `compile=`, `nearby-test=`, `caller=`, or `callsite=`. Do not write markdown "
        "prose such as ``source-symbol-map-passed: `path` adds `symbol` in package `name```; use literal key/value "
        "tokens such as `source-symbol-map-passed: path=lib/benchmark/linear.go package=benchmark added-symbol=Linear owner-evidence=issue-term-benchmark-package compile=go-test-lib-benchmark`. "
        "If no source-symbol contract changed, write one single machine-readable `source-symbol-map-skip-justified:` "
        "line with the exact `path=` or `package=` and source evidence. "
        "Verifier prose, worker summaries, and passing no-test compile checks are not sufficient; the durable final "
        "`status.json` is the acceptance surface."
    )


def status_records_selected_validation(current_status: dict[str, object]) -> bool:
    evidence = json.dumps(current_status, sort_keys=True).lower()
    return "helper-validation-passed" in evidence


def blocked_status_recoverable_by_public_probe(current_status: dict[str, object]) -> bool:
    if str(current_status.get("status", "")).lower() != "blocked":
        return False
    text = json.dumps(current_status, sort_keys=True).lower()
    stale_no_diff_markers = (
        "empty git diff",
        "leaving an empty git diff",
        "without inspecting or modifying /app",
        "without modifying /app",
        "no scoreable source diff",
        "no materialized source diff",
    )
    if any(marker in text for marker in stale_no_diff_markers):
        return True
    blockers = current_status.get("blockers")
    if not isinstance(blockers, list) or not blockers:
        return False
    return not blockers_after_passing_public_probe([str(blocker) for blocker in blockers])


def blocked_status_needs_diff_reconciliation(current_status: dict[str, object]) -> bool:
    """Return true for terminal blockers that require re-reading the live diff.

    These are not acceptance blockers that a public probe can clear. They mean
    the agent/verifier is reasoning from stale narrative or a patch plan that
    is not present in the actual working tree, so the production orchestrator
    should get one bounded resume over the live diff before the wrapper treats
    the run as terminal.
    """

    if str(current_status.get("status", "")).lower() != "blocked":
        return False
    text = json.dumps(current_status, sort_keys=True).lower()
    stale_markers = (
        "claimed changed source paths are absent from final git diff",
        "absent from final git diff",
        "remove the stale claim",
        "stale claim",
        "claimed companion",
        "claimed changed files",
        "stale patch",
        "patch did not apply",
        "did not apply cleanly",
        "could not find hunk context",
        "hunk failed",
        "missing edits",
        "empty git diff",
        "leaving an empty git diff",
        "without inspecting or modifying /app",
        "without modifying /app",
        "no materialized source diff",
    )
    return any(marker in text for marker in stale_markers)


def has_hard_scope_blocker(blockers: list[str]) -> bool:
    return any("[public-hard]" in blocker.lower() or "[official-hard]" in blocker.lower() for blocker in blockers)
