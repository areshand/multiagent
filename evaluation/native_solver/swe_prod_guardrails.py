from __future__ import annotations

import json
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

    if any(marker in status_text for marker in ("undefined:", "does not compile", "compile error")):
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

    if any(marker in issue_lower for marker in ("resend", "re-send", "retry", "throttle", "expiry", "expired", "ttl")):
        if not any(marker in status_text for marker in ("resend-gate-checked:", "throttle", "ttl", "expiry")):
            blockers.append(
                "resend/expiry behavior is in scope; verifier/status must name the resend or throttle gate inspected and the source evidence"
            )

    return blockers


def stale_visible_failure_justified(status_text: str) -> bool:
    """Return whether a reported visible-test failure has explicit no-leak replacement evidence."""
    text = status_text.lower()
    return "replacement-probe-passed:" in text and "stale-visible-failure-justified:" in text


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
    commands.extend(changed_go_feature_test_commands(workdir, issue, diff))
    commands.extend(changed_python_test_commands(workdir, diff))
    return _dedupe_commands(commands)[:4]


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
