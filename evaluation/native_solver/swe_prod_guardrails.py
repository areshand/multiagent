from __future__ import annotations

import json
import os
import re
from pathlib import Path


def required_public_symbols(issue: str, metadata: dict[str, object] | None = None) -> list[str]:
    requirement_text = issue
    # SWE benchmark metadata can contain answer-shaped verifier fields such as
    # official requirements, interfaces, selected tests, and test patches. The
    # solver must derive symbols from the public issue text and repository state
    # only, so metadata is intentionally not used here.
    _ = metadata
    symbols: set[str] = set()
    patterns = [
        r"must\s+be\s+exposed\s+as\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
        r"\b(?:New\s+Public\s+)?(?:Class|Function|Method|Interface|Type)\s+Name:\s*`?([A-Za-z_][A-Za-z0-9_]*)\b`?(?!\.[A-Za-z0-9_])",
        r"(?<!File\s)\bName:\s*`?([A-Za-z_][A-Za-z0-9_]*)\b`?(?!\.[A-Za-z0-9_])",
        r"\b(?:class|function|method|interface|constant)\s+`([A-Za-z_][A-Za-z0-9_]*)`",
        r"\b(?:class|function|method|interface|constant)\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:is|are|must|should|that)\b",
        r"\b[Rr]ename\s+[A-Za-z_][A-Za-z0-9_]*\s+(?:queue\s+API\s+)?from\s+`?[A-Za-z_][A-Za-z0-9_]*`?\s+to\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
    ]
    for pattern in patterns:
        symbols.update(
            match
            for match in re.findall(pattern, requirement_text, flags=re.IGNORECASE)
            if _looks_like_public_symbol(match)
        )
    symbols.update(
        match
        for match in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=", requirement_text)
        if _looks_like_public_symbol(match) and match[:1].isupper()
    )
    for constants_clause in re.findall(
        r"\b(?:constants?|Add constants?):\s*([^\n.]+)",
        requirement_text,
        flags=re.IGNORECASE,
    ):
        constants_clause = re.sub(r'"[^"]*"|\'[^\']*\'', "", constants_clause)
        symbols.update(
            match
            for match in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", constants_clause)
            if _looks_like_public_symbol(match) and match[:1].isupper()
        )
    return sorted(symbols)


def _looks_like_public_symbol(symbol: str) -> bool:
    if not symbol or "." in symbol or "/" in symbol:
        return False
    lower = symbol.lower()
    if symbol.startswith("__") or lower in {"__init__", "__init_"}:
        return False
    if lower in {
        "none",
        "null",
        "true",
        "false",
        "input",
        "output",
        "path",
        "description",
        "name",
        "type",
        "file",
        "new",
        "public",
        "class",
        "function",
        "method",
        "interface",
        "constant",
        "str",
        "bool",
        "int",
        "float",
        "list",
        "dict",
        "optional",
        "callable",
        "iterable",
        "sequence",
    }:
        return False
    if lower.endswith("_env_var") or lower.endswith("_env_value"):
        return False
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", symbol))


def implementation_scope_blockers(
    issue: str,
    diff: str,
    current_status: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> list[str]:
    """Return generic source-derived blockers without benchmark answer leakage."""
    issue_lower = issue.lower()
    diff_lower = diff.lower()
    status_text = json.dumps(current_status, sort_keys=True).lower()
    blockers: list[str] = []

    changed_paths = _changed_paths(diff)
    if not diff.strip():
        blockers.append("no source diff is present; benchmark completion requires a non-empty implementation patch")
        return blockers

    test_changes = [path for path in changed_paths if _is_test_path(path)]
    non_test_changes = [path for path in changed_paths if not _is_test_path(path)]
    if test_changes and not _issue_explicitly_allows_tests(issue_lower):
        blockers.append(
            "patch changes test files without visible task evidence that tests are implementation inputs: "
            + ", ".join(test_changes[:8])
        )
    if test_changes and not non_test_changes:
        blockers.append("patch only changes tests; implement the product/source behavior instead")

    generated = [path for path in changed_paths if _is_generated_or_dependency_path(path)]
    if generated:
        blockers.append(
            "patch includes generated, lockfile, dependency, or bundled artifact changes that should not be submitted as the source fix: "
            + ", ".join(generated[:8])
        )

    if "validation-repair-needed:" in status_text:
        blockers.append(
            "reported validation explicitly requires a repair worker; resolve the failing command before completion"
        )
    if failed_validation_return_code(status_text) and not stale_visible_failure_justified(status_text):
        blockers.append(
            "reported validation includes a nonzero focused validation return code; rerun/fix it before completion "
            "or justify the stale visible expectation with replacement-probe evidence"
        )
    if any(
        marker in status_text
        for marker in (
            "undefined:",
            "undefined method",
            "undefined field",
            "has no field or method",
            "does not compile",
            "compile error",
        )
    ):
        blockers.append("reported validation contains compile-error evidence; resolve it before completion")
    elif any(marker in status_text for marker in ("failed", "failing")) and not stale_visible_failure_justified(status_text):
        blockers.append(
            "reported validation contains failing evidence; resolve it or include both "
            "`replacement-probe-passed:` and `stale-visible-failure-justified:` markers with visible/source evidence"
        )
    elif claims_stale_visible_failure(status_text) and not stale_visible_failure_justified(status_text):
        blockers.append(
            "reported validation claims a visible test/fixture expectation is stale; resolve it or include both "
            "`replacement-probe-passed:` and `stale-visible-failure-justified:` markers with visible/source evidence"
        )

    for symbol in required_public_symbols(issue, metadata):
        symbol_lower = symbol.lower()
        if symbol_lower not in diff_lower and symbol_lower not in status_text:
            blockers.append(
                f"task appears to require public symbol `{symbol}`, but the diff/status does not account for that exact symbol"
            )

    issue_mentions_data_shape = _issue_mentions_data_contract(issue)
    diff_uses_data_helper = any(
        marker in diff_lower
        for marker in (" db.", "\tdb.", "await db.", "database/", "databases/", "cache.", "redis", "mongo", "postgres")
    )
    if issue_mentions_data_shape and diff_uses_data_helper and not any(
        marker in status_text for marker in ("helper-validation-passed:", "helper-validation-skip-justified:", "bulk-helper-contract-checked:")
    ):
        blockers.append(
            "task/diff touches data helper behavior, but status does not show helper-layer validation or a source-level skip justification"
        )

    exact_helper_names = _issue_named_helpers(issue)
    for helper in exact_helper_names:
        helper_lower = helper.lower()
        if helper_lower not in diff_lower and helper_lower not in status_text:
            blockers.append(
                f"issue names helper/interface `{helper}`, but the diff/status does not preserve or implement that exact name"
            )

    symbol_changes = source_symbol_changes(diff)
    if symbol_changes and not source_owner_ledger_has_evidence(status_text):
        blockers.append(
            "source symbol contracts changed, but status does not include `source-owner-ledger:` "
            "with `selected-owner=`, at least one plausible `candidate-owner=`, rejected-owner "
            "reasoning, and `validation-package=` before source-symbol acceptance"
        )
    if symbol_changes and not source_symbol_map_has_evidence(status_text):
        blockers.append(
            "source symbol contracts changed, but status does not include `source-symbol-map-passed:` "
            "or `source-symbol-map-skip-justified:` with exact package/path placement, added/removed/renamed "
            "symbols, owner-discovery evidence, and caller or nearby-test compatibility evidence"
        )
    elif symbol_changes:
        workdir = _metadata_workdir(metadata)
        if workdir:
            blockers.extend(source_symbol_owner_candidate_blockers(workdir, issue, diff, current_status))

    if dependency_contract_changed(diff) and not dependency_contract_has_evidence(diff, status_text):
        blockers.append(
            "dependency/provider contract changed, but status does not include `constructor-dependency-checked:` "
            "with constructor/factory, production wiring, mock/fake, and caller/API compatibility evidence, or "
            "`provider-capability-checked:` for a guarded optional provider with declared receiver, method/provider, "
            "concrete provider, source declaration, and compile evidence. Do not accept bridge/store/interface changes "
            "or fallback providers without proving the owning constructor or guarded provider remains compatible."
        )

    if any(marker in issue_lower for marker in ("resend", "re-send", "retry", "throttle", "expiry", "expired", "ttl")):
        if not any(marker in status_text for marker in ("resend-gate-checked:", "throttle", "ttl", "expiry")):
            blockers.append(
                "resend/expiry behavior is in scope; verifier/status must name the resend or throttle gate inspected and the source evidence"
            )

    return blockers


def source_symbol_owner_candidate_blockers(
    workdir: Path,
    issue: str,
    diff: str,
    current_status: dict[str, object],
) -> list[str]:
    """Block source-symbol completions that ignore better issue-term owner dirs."""
    if not source_symbol_changes(diff):
        return []
    status_text = json.dumps(current_status, sort_keys=True).lower()
    if "source-symbol-map-passed:" not in status_text or "source-symbol-map-skip-justified:" in status_text:
        return []

    issue_terms = _source_owner_issue_terms(issue)
    if not issue_terms:
        return []

    changed_dirs = {
        str(Path(path).parent).replace(".", "").strip("/")
        for path in _changed_paths(diff)
        if _is_source_symbol_path(path) and not _is_test_path(path)
    }
    changed_dirs = {path for path in changed_dirs if path}
    changed_text = " ".join(changed_dirs).lower()
    symbol_text = " ".join(source_symbol_changes(diff))
    candidates = _source_owner_candidate_dirs(workdir, issue_terms)
    unaccounted: list[str] = []
    for candidate in candidates:
        candidate_lower = candidate.lower()
        if any(_same_or_nested_path(candidate_lower, changed.lower()) for changed in changed_dirs):
            continue
        if candidate_lower in status_text:
            continue
        # Only block when the issue-term directory is more specific than the
        # edited package. If the edited path already carries the term, the normal
        # source-symbol map and package validation rules are enough.
        candidate_terms = [term for term in issue_terms if _path_has_exact_term(candidate_lower, term)]
        symbol_relevant_terms = [term for term in candidate_terms if _term_appears_in_source_symbol(symbol_text, term)]
        if symbol_relevant_terms and not any(term in changed_text for term in symbol_relevant_terms):
            unaccounted.append(candidate)

    if not unaccounted:
        return []
    return [
        "source-symbol owner evidence does not account for plausible issue-term owner package(s) outside edited paths: "
        + ", ".join(unaccounted[:6])
        + "; compare these candidates in owner-evidence= or move the symbols before completion"
    ]


def dependency_contract_changed(diff: str) -> bool:
    """Detect general dependency/provider contract changes in added source lines."""

    added_lines = [
        line[1:].strip().lower()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not added_lines:
        return False
    added = "\n".join(added_lines)
    dependency_terms = (
        "store",
        "storer",
        "bridge",
        "adapter",
        "provider",
        "client",
        "repo",
        "repository",
        "service",
        "gateway",
        "factory",
    )
    if re.search(r"\btype\s+[a-z0-9_]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)[a-z0-9_]*\s+interface\b", added):
        return True
    if re.search(r"\bfunc\s+new[a-z0-9_]*\s*\([^)]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)", added):
        return True
    if re.search(r"(?<!\.)\bnew[a-z0-9_]*\s*\([^)]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)", added):
        return True
    if ".(" in added and any(term in added for term in dependency_terms):
        return True
    if any(
        re.search(r"\b" + re.escape(term) + r"\s*[:=]\s*", added)
        for term in dependency_terms
    ):
        return True
    if any(
        re.search(r"\b" + re.escape(term) + r"\.[a-z_][a-z0-9_]*\s*\(", added)
        for term in dependency_terms
    ):
        return True
    if "fallback" in added and any(term in added for term in dependency_terms):
        return True
    return False


def required_dependency_contract_changed(diff: str) -> bool:
    """Return true when the patch changes required construction/API shape."""

    added_lines = [
        line[1:].strip().lower()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not added_lines:
        return False
    added = "\n".join(added_lines)
    if re.search(r"\btype\s+[a-z0-9_]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)[a-z0-9_]*\s+interface\b", added):
        return True
    if re.search(r"\bfunc\s+new[a-z0-9_]*\s*\([^)]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)", added):
        return True
    if re.search(r"(?<!\.)\bnew[a-z0-9_]*\s*\([^)]*(store|storer|bridge|adapter|provider|client|repo|repository|service|gateway)", added):
        return True
    dependency_terms = (
        "store",
        "storer",
        "bridge",
        "adapter",
        "provider",
        "client",
        "repo",
        "repository",
        "service",
        "gateway",
        "factory",
    )
    return any(
        re.search(r"\b" + re.escape(term) + r"\s*[:=]\s*", added)
        for term in dependency_terms
    )


def optional_provider_contract_changed(diff: str) -> bool:
    added_lines = [
        line[1:].strip().lower()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    if not added_lines:
        return False
    added = "\n".join(added_lines)
    dependency_terms = ("store", "storer", "bridge", "adapter", "provider", "client", "repo", "repository", "service", "gateway")
    return ".(" in added and any(term in added for term in dependency_terms)


def dependency_contract_has_evidence(diff: str, status_text: str) -> bool:
    if constructor_dependency_has_evidence(status_text):
        return True
    if required_dependency_contract_changed(diff):
        return False
    return optional_provider_contract_changed(diff) and provider_capability_has_evidence(status_text)


def provider_capability_has_evidence(status_text: str) -> bool:
    text = status_text.lower()
    has_marker = "provider-capability-checked:" in text or (
        "dynamic_optional_interface_method=" in text
        and "call_guard=type_assertion" in text
    )
    if not has_marker:
        return False
    has_receiver = any(marker in text for marker in ("declared-receiver=", "declared_receiver=", "receiver=", "s.bridge_declared_type=", "s.store_declared_type="))
    has_method = any(marker in text for marker in ("method=", "provider-method=", "dynamic_optional_interface_method=", "listflags_declared="))
    has_provider = any(marker in text for marker in ("concrete-provider=", "concrete_provider=", "provider=", "method_exists=true"))
    has_guard = any(marker in text for marker in ("guard=", "call_guard=type_assertion", "type-assertion", "optional"))
    has_compile = any(marker in text for marker in ("compile=", "returncode=0", "go-package-validation-passed:"))
    return has_receiver and has_method and has_provider and has_guard and has_compile


def constructor_dependency_has_evidence(status_text: str) -> bool:
    text = status_text.lower()
    if "constructor-dependency-checked:" not in text:
        return False
    has_constructor = _has_evidence_key(
        text,
        (
            "constructor=",
            "constructor-path=",
            "factory=",
            "factory-path=",
            "new=",
            "new-path=",
        ),
    )
    has_wiring = _has_evidence_key(
        text,
        (
            "wiring=",
            "wiring-path=",
            "production-wiring=",
            "production-wiring-path=",
            "cmd-wiring=",
        ),
    )
    has_mock = _has_evidence_key(
        text,
        (
            "mock=",
            "mock-path=",
            "fake=",
            "fake-path=",
            "testdouble=",
            "test-double=",
        ),
    )
    has_callsite = _has_evidence_key(
        text,
        (
            "caller=",
            "callsite=",
            "api-compatible=",
            "api-shape=",
            "compile=",
            "returncode=0",
        ),
    )
    return has_constructor and has_wiring and has_mock and has_callsite


def _has_evidence_key(text: str, keys: tuple[str, ...]) -> bool:
    return any(re.search(r"(?:^|[\s{,;])" + re.escape(key), text) for key in keys)


def source_owner_ledger_has_evidence(status_text: str) -> bool:
    text = status_text.lower()
    if "source-owner-ledger-skip-justified:" in text:
        has_owner = any(marker in text for marker in ("package=", "path=", "file=", "module="))
        has_source_evidence = any(
            marker in text
            for marker in (
                "source-evidence=",
                "owner-evidence=",
                "no source symbol",
                "unchanged symbol",
                "not a symbol",
            )
        )
        return has_owner and has_source_evidence
    if "source-owner-ledger:" not in text:
        return False
    has_selected = "selected-owner=" in text
    has_candidate = "candidate-owner=" in text
    has_validation = "validation-package=" in text
    has_rejection = any(
        marker in text
        for marker in (
            "rejected-owner=",
            "rejected-candidate=",
            "rejection=",
            "not-owner=",
            "reason=",
        )
    )
    return has_selected and has_candidate and has_validation and has_rejection


def helper_preservation_evidence(issue: str, text: str) -> str:
    """Return no-leak evidence that named helper/interface contracts were preserved."""

    if not text:
        return ""
    lower = text.lower()
    if not any(marker in lower for marker in ("accepted", "no blocking finding", "no blocking findings", "contract-checked:")):
        return ""

    helpers: list[str] = []
    for helper in _issue_named_helpers(issue):
        helper_lower = helper.lower()
        if helper_lower not in lower:
            continue
        if _helper_preservation_window_has_evidence(helper_lower, lower):
            helpers.append(helper)

    if not helpers:
        return ""
    return "helper-contract-preserved: " + ", ".join(helpers)


def _metadata_workdir(metadata: dict[str, object] | None) -> Path | None:
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("_solver_workdir")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    return path if path.exists() else None


def _source_owner_issue_terms(issue: str) -> set[str]:
    terms: set[str] = set()
    stop = {
        "add",
        "adds",
        "added",
        "change",
        "changed",
        "fix",
        "test",
        "tests",
        "should",
        "would",
        "could",
        "when",
        "with",
        "from",
        "into",
        "this",
        "that",
        "have",
        "make",
        "new",
        "old",
        "public",
        "private",
        "config",
        "configuration",
        "generator",
        "linear",
    }
    for token in re.findall(r"\b[a-z][a-z0-9_-]{3,}\b", issue.lower()):
        token = token.replace("_", "-")
        if token in stop or token.endswith("ing"):
            continue
        terms.add(token)
        if token.endswith("s") and len(token) > 4:
            terms.add(token[:-1])
    return terms


def _source_owner_candidate_dirs(workdir: Path, issue_terms: set[str]) -> list[str]:
    candidates: list[str] = []
    skip_dirs = {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "vendor",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".tox",
        ".venv",
    }
    source_suffixes = {".go", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java", ".kt", ".rb", ".php"}
    for root, dirs, files in os.walk(workdir):
        root_path = Path(root)
        rel = root_path.relative_to(workdir)
        depth = len(rel.parts)
        dirs[:] = [name for name in dirs if name not in skip_dirs and not name.startswith(".") and depth < 5]
        if rel == Path(".") or depth == 0:
            continue
        rel_text = rel.as_posix().lower()
        if not any(_path_has_exact_term(rel_text, term) for term in issue_terms):
            continue
        if not any(Path(name).suffix in source_suffixes for name in files):
            continue
        candidates.append(rel.as_posix())
        if len(candidates) >= 24:
            break
    return sorted(dict.fromkeys(candidates))


def _path_has_exact_term(path_text: str, term: str) -> bool:
    parts = [part for part in re.split(r"[/_.-]+", path_text.lower()) if part]
    variants = {term}
    if term.endswith("s") and len(term) > 4:
        variants.add(term[:-1])
    else:
        variants.add(term + "s")
    return any(part in variants for part in parts)


def _term_appears_in_source_symbol(symbol_text: str, term: str) -> bool:
    if not symbol_text:
        return False
    variants = {term}
    if term.endswith("s") and len(term) > 4:
        variants.add(term[:-1])
    else:
        variants.add(term + "s")
    symbol_parts = [part for part in re.split(r"[^A-Za-z0-9]+", symbol_text) if part]
    expanded_parts: set[str] = set()
    for part in symbol_parts:
        expanded_parts.add(part)
        expanded_parts.update(split_identifier_terms(part))
    return any(variant in expanded_parts for variant in variants)


def split_identifier_terms(identifier: str) -> set[str]:
    """Split snake/kebab/camel identifiers into searchable lowercase terms."""

    terms: set[str] = set()
    for chunk in re.split(r"[_\-.]+", identifier):
        chunk = chunk.strip()
        if not chunk:
            continue
        terms.add(chunk.lower())
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", chunk):
            if part:
                terms.add(part.lower())
    return terms


def _same_or_nested_path(candidate: str, changed: str) -> bool:
    return candidate == changed or changed.startswith(candidate + "/") or candidate.startswith(changed + "/")


def source_symbol_changes(diff: str) -> list[str]:
    """Return changed source symbol definitions that need package/path proof."""
    changed_paths = _changed_paths(diff)
    source_paths = [path for path in changed_paths if _is_source_symbol_path(path)]
    if not source_paths:
        return []

    changes: list[str] = []
    current_path = ""
    for raw_line in diff.splitlines():
        if raw_line.startswith("diff --git a/") and " b/" in raw_line:
            current_path = raw_line.split(" b/", 1)[1].split("\t", 1)[0].strip()
            continue
        if current_path not in source_paths:
            continue
        if not raw_line.startswith(("+", "-")) or raw_line.startswith(("+++", "---")):
            continue
        line = raw_line[1:].strip()
        if not line or line.startswith(("//", "#", "*")):
            continue
        symbol = _changed_symbol_name(current_path, line)
        if symbol:
            changes.append(f"{raw_line[0]}{current_path}:{symbol}")
    return sorted(dict.fromkeys(changes))


def source_symbol_map_has_evidence(status_text: str) -> bool:
    text = status_text.lower()
    if "source-symbol-map-skip-justified:" in text:
        return any(marker in text for marker in ("package=", "path=", "file=")) and any(
            marker in text for marker in ("no symbol", "unchanged symbol", "not a symbol", "source evidence")
        )
    if "source-symbol-map-passed:" not in text:
        return False
    has_owner = any(marker in text for marker in ("package=", "path=", "file=", "module="))
    has_symbol = any(marker in text for marker in ("symbol=", "added-symbol=", "removed-symbol=", "renamed-symbol=", "caller="))
    has_owner_evidence = any(
        marker in text
        for marker in (
            "owner-evidence=",
            "owner-proof=",
            "source-owner=",
            "candidate-owner=",
            "owner-candidate=",
            "issue-term=",
            "package-owner=",
        )
    )
    has_compatibility = any(
        marker in text
        for marker in (
            "nearby-test=",
            "compile=",
            "caller=",
            "callsite=",
            "source-compatible",
            "same-package",
            "package-test",
        )
    )
    return has_owner and has_symbol and has_owner_evidence and has_compatibility


def _helper_preservation_window_has_evidence(helper_lower: str, text_lower: str) -> bool:
    for match in re.finditer(re.escape(helper_lower), text_lower):
        start = max(0, match.start() - 500)
        end = min(len(text_lower), match.end() + 500)
        window = text_lower[start:end]
        if any(
            marker in window
            for marker in (
                "preserv",
                "unchanged",
                "contract-checked:",
                "validated",
                "validation passed",
                "no blocking finding",
                "no blocking findings",
            )
        ):
            return True
    return False


def stale_visible_failure_justified(status_text: str) -> bool:
    """Return whether a reported visible-test failure has explicit no-leak replacement evidence."""
    text = status_text.lower()
    return "replacement-probe-passed:" in text and "stale-visible-failure-justified:" in text


def failed_validation_return_code(status_text: str) -> bool:
    text = status_text.lower()
    if not any(
        command in text
        for command in (
            "go test",
            "pytest",
            "python -m pytest",
            "npm test",
            "yarn test",
            "pnpm test",
            "jest",
            "vitest",
            "cargo test",
        )
    ):
        return False
    for match in re.finditer(r"(?:return code|exit code|rc)\s*[:=]\s*(\d+)", text):
        if int(match.group(1)) != 0:
            return True
    return False


def claims_stale_visible_failure(status_text: str) -> bool:
    text = status_text.lower()
    if "stale" not in text:
        return False
    return any(marker in text for marker in ("visible", "test", "fixture", "expectation", "golden"))


def helper_scope_hints(workdir: Path, issue: str, diff: str, blockers: list[str]) -> list[str]:
    """Return generic source ownership hints for no-leak follow-up prompts."""
    text = f"{issue.lower()}\n{diff.lower()}\n{' '.join(blockers).lower()}"
    hints: list[str] = []

    def add_existing(relative: str) -> None:
        if relative and relative not in hints and (workdir / relative).exists():
            hints.append(relative)

    for path in explicit_source_paths_from_text(workdir, "\n".join(blockers)):
        if not _is_test_path(path):
            add_existing(path)

    for path in _changed_paths(diff):
        if not path or _is_test_path(path):
            continue
        add_existing(path)
        parts = path.split("/")
        if len(parts) > 1:
            add_existing("/".join(parts[:-1]))
        if len(parts) > 2:
            add_existing("/".join(parts[:2]))

    if any(marker in text for marker in ("database", "cache", "adapter", "key", "keys", "fallback", "ttl", "expiry")):
        for relative in (
            "src/database",
            "src/databases",
            "database",
            "databases",
            "lib/database",
            "lib/databases",
            "app/database",
            "packages/database",
            "src/cache",
            "lib/cache",
        ):
            add_existing(relative)

    if any(marker in text for marker in ("parser", "parse", "serializer", "deserialize", "codec", "format")):
        for relative in ("src/parser", "src/parsers", "lib/parser", "lib/parsers", "parser", "parsers", "src/format", "lib/format"):
            add_existing(relative)

    return hints[:12]


def explicit_source_paths_from_text(workdir: Path, text: str) -> list[str]:
    """Extract existing repository source paths explicitly named in blocker text."""

    source_suffixes = ("go", "py", "pyi", "js", "jsx", "ts", "tsx", "rs", "java", "kt", "rb", "php")
    candidates: list[str] = []
    pattern = re.compile(
        r"(?<![\w./-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+"
        r"(?:\.(?:" + "|".join(source_suffixes) + r"))?)(?![\w./-])"
    )
    for match in pattern.finditer(text):
        raw = match.group(1).strip("`'\".,:;()[]{}")
        if not raw or raw.startswith(("/", "../")) or ".." in Path(raw).parts:
            continue
        path = workdir / raw
        if path.exists() and raw not in candidates:
            candidates.append(raw)
    return candidates[:16]


def deprecated_noop_probe_command() -> list[str]:
    """Deprecated compatibility hook.

    The no-leak adapter must not inject benchmark-row-specific probes. Keep the
    hook for internal compatibility, but do not return a privileged command.
    """
    return []


def coverage_probe_commands(workdir: Path, issue: str, diff: str) -> list[list[str]]:
    """Select only generic, repository-visible validation probes.

    This function intentionally avoids hidden-test-shaped commands and
    project-specific repair probes. Workers and verifiers should derive focused
    validation from visible source, tests, package scripts, and docs.
    """
    commands: list[list[str]] = []
    go_packages = changed_go_package_args(diff)
    if go_packages:
        commands.append(["go", "test", *go_packages])
    commands.extend(changed_go_related_feature_test_commands(workdir, issue, diff))
    commands.extend(changed_go_feature_test_commands(workdir, issue, diff))
    commands.extend(changed_python_test_commands(workdir, diff))
    return _dedupe_commands(commands)[:4]


def changed_go_related_feature_test_commands(workdir: Path, issue: str, diff: str) -> list[list[str]]:
    """Return same-tree Go tests for related feature packages.

    Service/init files often wire behavior that lives in sibling packages. A
    changed package can compile while a related feature package no longer does,
    so derive nearby package roots from visible path and issue tokens instead of
    relying only on the edited package.
    """

    changed_go_paths = [
        Path(path)
        for path in _changed_paths(diff)
        if path.endswith(".go") and not _is_test_path(path)
    ]
    if not changed_go_paths:
        return []

    text = f"{issue}\n{diff}".lower()
    commands: list[list[str]] = []
    for path in changed_go_paths:
        tokens = _go_feature_tokens(path, text)
        if not tokens or len(path.parts) < 2:
            continue
        search_root = workdir / path.parts[0]
        if not search_root.exists():
            continue
        for candidate in sorted(search_root.rglob("*")):
            if not candidate.is_dir() or not _has_go_tests(candidate):
                continue
            relative = candidate.relative_to(workdir)
            relative_text = relative.as_posix().lower()
            if relative == path.parent:
                continue
            if any(token in relative_text for token in tokens):
                commands.append(["go", "test", f"./{relative.as_posix()}/..."])
                break
    return commands


def _go_feature_tokens(path: Path, text: str) -> list[str]:
    raw_tokens: set[str] = set()
    for part in [*path.parts, path.stem]:
        for token in re.split(r"[^A-Za-z0-9]+", part):
            token = token.lower()
            if len(token) >= 4 and token not in {"service", "server", "client", "common", "internal", "pkg"}:
                raw_tokens.add(token)
    for token in re.findall(r"\b[a-z][a-z0-9]{3,}\b", text):
        if token in raw_tokens:
            continue
        if token in {"service", "server", "client", "common", "internal", "package", "packages", "tests"}:
            continue
        if token in path.as_posix().lower():
            raw_tokens.add(token)
    aliases = {
        "kubernetes": "kube",
        "credential": "creds",
        "credentials": "creds",
        "authentication": "auth",
        "authorization": "auth",
    }
    expanded = set(raw_tokens)
    for token in raw_tokens:
        if token in aliases:
            expanded.add(aliases[token])
    return sorted(expanded)


def changed_go_feature_test_commands(workdir: Path, issue: str, diff: str) -> list[list[str]]:
    """Return broader visible Go tests for parser/converter/data-shape changes."""

    issue_and_diff = f"{issue.lower()}\n{diff.lower()}"
    if not any(
        marker in issue_and_diff
        for marker in (
            "parser",
            "parse",
            "converter",
            "convert",
            "serializer",
            "deserialize",
            "fixture",
            "golden",
            "output",
            "json",
            "yaml",
            "record",
            "records",
            "duplicate",
            "duplicates",
        )
    ):
        return []

    commands: list[list[str]] = []
    changed_go_paths = [
        Path(path)
        for path in _changed_paths(diff)
        if path.endswith(".go") and not _is_test_path(path)
    ]
    for path in changed_go_paths:
        roots = _go_feature_roots(path)
        for root in roots:
            if _has_go_tests(workdir / root):
                commands.append(["go", "test", f"./{root.as_posix()}/..."])
                break
    return commands


def changed_go_package_args(diff: str) -> list[str]:
    packages: list[str] = []
    for path in _changed_paths(diff):
        if not path.endswith(".go") or _is_test_path(path):
            continue
        package = "./" + str(Path(path).parent)
        if package == "./.":
            package = "."
        if package not in packages:
            packages.append(package)
    return packages


def changed_python_test_commands(workdir: Path, diff: str) -> list[list[str]]:
    commands: list[list[str]] = []
    for raw_path in _changed_paths(diff):
        path = Path(raw_path)
        if path.suffix not in {".py", ".pyi", ".pyx"} or _is_test_path(raw_path):
            continue
        for test_path in _python_test_candidates(workdir, path):
            commands.append(["python", "-m", "pytest", test_path.as_posix(), "-q", "--tb=short"])
            break
    return commands


def _python_test_candidates(workdir: Path, path: Path) -> list[Path]:
    candidates: list[Path] = []
    module = path.stem
    for parent in [path.parent, *path.parents]:
        if parent == Path("."):
            break
        tests_dir = parent / "tests"
        if _has_python_tests(workdir / tests_dir):
            specific = tests_dir / f"test_{module}.py"
            if (workdir / specific).exists():
                candidates.append(specific)
            candidates.append(tests_dir)
        sibling_test = parent / f"test_{module}.py"
        if (workdir / sibling_test).exists():
            candidates.append(sibling_test)
        sibling_alt = parent / f"{module}_test.py"
        if (workdir / sibling_alt).exists():
            candidates.append(sibling_alt)
    return _dedupe_paths(candidates)


def _go_feature_roots(path: Path) -> list[Path]:
    parts = path.parts[:-1]
    roots: list[Path] = []
    if len(parts) >= 2:
        roots.append(Path(*parts[:2]))
    if len(parts) >= 3:
        roots.append(Path(*parts[:3]))
    if parts:
        roots.append(Path(*parts))
    return _dedupe_paths([root for root in roots if root != Path(".")])


def _has_go_tests(path: Path) -> bool:
    return path.exists() and any(child.name.endswith("_test.go") for child in path.rglob("*_test.go"))


def _has_python_tests(path: Path) -> bool:
    return path.exists() and any(
        child.name.startswith("test_") and child.suffix == ".py"
        for child in path.rglob("test_*.py")
    )


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = path.as_posix()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _dedupe_commands(commands: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for command in commands:
        key = tuple(command)
        if key in seen:
            continue
        seen.add(key)
        unique.append(command)
    return unique


def _changed_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for line in diff.splitlines():
        match = re.match(r"diff --git a/(.*?) b/(.*)$", line)
        if match:
            paths.append(match.group(2))
    return paths


def _is_test_path(path: str) -> bool:
    parts = Path(path).parts
    name = Path(path).name.lower()
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
    )


def _is_generated_or_dependency_path(path: str) -> bool:
    lower = path.lower()
    name = Path(lower).name
    return (
        name in {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "go.sum", "cargo.lock"}
        or "/dist/" in lower
        or "/build/" in lower
        or "/public/build/" in lower
        or lower.endswith(".min.js")
        or lower.endswith(".min.css")
        or "generated" in Path(lower).parts
        or "node_modules" in Path(lower).parts
    )


def _is_source_symbol_path(path: str) -> bool:
    lower = path.lower()
    if _is_test_path(path) or _is_generated_or_dependency_path(path):
        return False
    return lower.endswith((
        ".go",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".rb",
    ))


def _changed_symbol_name(path: str, line: str) -> str:
    lower_path = path.lower()
    patterns: list[str]
    if lower_path.endswith(".go"):
        patterns = [
            r"\bfunc\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"\btype\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface|func|map|\[|[A-Za-z_])",
            r"\bvar\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\bconst\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ]
    elif lower_path.endswith(".py"):
        patterns = [
            r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\s*[\(:]",
        ]
    elif lower_path.endswith((".js", ".jsx", ".ts", ".tsx")):
        patterns = [
            r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"\b(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\b(?:export\s+)?(?:interface|type|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\b(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)\s*=>",
        ]
    elif lower_path.endswith(".rs"):
        patterns = [
            r"\b(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"\b(?:pub\s+)?(?:struct|enum|trait|type)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        ]
    elif lower_path.endswith((".java", ".kt")):
        patterns = [
            r"\b(?:class|interface|enum|object)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            r"\b(?:public|private|protected|internal|static|final|suspend|\s)+\s*fun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            r"\b(?:public|private|protected|static|final|\s)+[A-Za-z_<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(",
        ]
    elif lower_path.endswith(".rb"):
        patterns = [
            r"\bdef\s+(?:self\.)?([A-Za-z_][A-Za-z0-9_!?=]*)",
            r"\bclass\s+([A-Za-z_][A-Za-z0-9_:]*)\b",
            r"\bmodule\s+([A-Za-z_][A-Za-z0-9_:]*)\b",
        ]
    else:
        return ""
    for pattern in patterns:
        match = re.search(pattern, line)
        if match:
            return match.group(1)
    return ""


def _issue_explicitly_allows_tests(issue_lower: str) -> bool:
    return any(
        marker in issue_lower
        for marker in ("add test", "add tests", "update test", "update tests", "fixture", "testdata", "golden", "snapshot")
    ) or _issue_mentions_output_contract_change(issue_lower)


def _issue_mentions_output_contract_change(issue_lower: str) -> bool:
    output_terms = ("expected output", "current output", "actual output", "output shape", "serialized output")
    expectation_terms = ("what did you expect", "expected to happen", "should output", "should return", "should appear")
    return any(term in issue_lower for term in output_terms) and any(term in issue_lower for term in expectation_terms)


def _issue_named_helpers(issue: str) -> list[str]:
    helpers: list[str] = []
    for match in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)`", issue):
        if _looks_like_call_symbol(match) or _looks_like_constant_symbol(match):
            helpers.append(match)
    for match in re.findall(
        r"\b(?:helper|function|method|interface|class|constant|symbol)\s+`?([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)`?",
        issue,
        flags=re.IGNORECASE,
    ):
        if "." in match or _looks_like_call_symbol(match) or _looks_like_constant_symbol(match) or match[:1].isupper():
            helpers.append(match)
    for match in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)\s*\(", issue):
        if _looks_like_call_symbol(match):
            helpers.append(match)
    return sorted(dict.fromkeys(helpers))


def _looks_like_call_symbol(symbol: str) -> bool:
    if "." in symbol:
        return all(_looks_like_public_symbol(part) for part in symbol.split("."))
    if not _looks_like_public_symbol(symbol):
        return False
    return "_" in symbol or symbol[:1].islower() and any(ch.isupper() for ch in symbol)


def _looks_like_constant_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", symbol))


def _issue_mentions_data_contract(issue: str) -> bool:
    strong_data_terms = re.search(
        r"\b(missing data|expired|expiry|ttl|cache|database|adapter|redis|mongo|postgres)\b",
        issue,
        flags=re.IGNORECASE,
    )
    data_key_terms = re.search(
        r"\b(?:keys?|fallback)\b.{0,48}\b(?:database|cache|redis|mongo|postgres|credential|secret|config|env|storage|record|field)\b"
        r"|\b(?:database|cache|redis|mongo|postgres|credential|secret|config|env|storage|record|field)\b.{0,48}\b(?:keys?|fallback)\b",
        issue,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return bool(strong_data_terms or data_key_terms)
