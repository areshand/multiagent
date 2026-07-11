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
from pathlib import Path

try:
    from .swe_prod_guardrails import (
        changed_go_package_args,
        coverage_probe_commands,
        helper_scope_hints,
        implementation_scope_blockers,
        required_public_symbols,
    )
except ImportError:  # pragma: no cover - direct script execution in task containers
    from swe_prod_guardrails import (
        changed_go_package_args,
        coverage_probe_commands,
        helper_scope_hints,
        implementation_scope_blockers,
        required_public_symbols,
    )


DEFAULT_MULTIAGENT_ROOT = Path("/opt/multiagent")
DEFAULT_WORKDIR = Path("/app")
RUNTIME_ROOT = Path("/tmp/multiagent-prod-swe")
STATUS_PATH = RUNTIME_ROOT / "status.json"
HELPER_PROBE_PATH = RUNTIME_ROOT / "helper-validation-probe.txt"
CONTRACT_LEDGER_PATH = RUNTIME_ROOT / "contract-ledger.md"
TASK_METADATA_PATH = Path(os.environ.get("EVAL_TASK_METADATA_FILE", "/tmp/evalscope-native-multiagent-metadata.json"))
CODEX_WRAPPER = RUNTIME_ROOT / "codex-bridge"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/root/.codex-multiagent-prod"))
APPLY_PATCH_WRAPPER = RUNTIME_ROOT / "apply_patch"
STABLE_APPLY_PATCH = Path("/usr/local/bin/apply_patch")
ACTIVE_START_HEAD: str | None = None
PUBLIC_SOLVER_METADATA_KEYS = {
    "language",
}
PRIVATE_SOLVER_METADATA_KEYS = {
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "base_commit",
    "fail_to_pass",
    "interface",
    "pass_to_pass",
    "problem_statement",
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


def _list_from_metadata(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return [text]
        return _list_from_metadata(parsed)
    return [str(value)]


def official_test_contract(metadata: dict[str, object]) -> dict[str, object]:
    metadata = public_solver_metadata(metadata or {})
    nested = metadata.get("swe_bench_pro")
    if isinstance(nested, dict):
        source: dict[str, object] = nested
    else:
        source = metadata
    fail_to_pass = _list_from_metadata(source.get("fail_to_pass") or source.get("FAIL_TO_PASS"))
    pass_to_pass = _list_from_metadata(source.get("pass_to_pass") or source.get("PASS_TO_PASS"))
    selected_files = _list_from_metadata(source.get("selected_test_files_to_run"))
    return {
        "instance_id": source.get("instance_id") or metadata.get("instance_id") or metadata.get("sample_id"),
        "fail_to_pass": fail_to_pass,
        "pass_to_pass": pass_to_pass,
        "selected_test_files_to_run": selected_files,
        "expected_test_count": len(fail_to_pass) + len(pass_to_pass),
    }


def metadata_problem_text(metadata: dict[str, object] | None) -> str:
    if not metadata:
        return ""
    metadata = public_solver_metadata(metadata)
    nested = metadata.get("swe_bench_pro")
    if isinstance(nested, dict):
        source: dict[str, object] = nested
    else:
        source = metadata
    parts = [
        source.get("problem_statement"),
        source.get("requirements"),
        source.get("interface"),
    ]
    return "\n".join(str(part) for part in parts if part)






def contract_ledger_text(issue: str, metadata: dict[str, object] | None = None) -> str:
    solver_metadata = public_solver_metadata(metadata or {})
    contract = official_test_contract(solver_metadata)
    symbols = required_public_symbols(issue, solver_metadata)
    contract_excerpt = metadata_problem_text(solver_metadata)
    sections = [
        "# SWE Bench Pro Contract Ledger",
        "",
        "This file is generated by the benchmark adapter. Treat task/source evidence here as a durable invariant.",
        "Follow-up workers and verifiers must preserve all items, even when fixing a later verifier finding.",
        "Do not use leaked evaluator tests, hidden row names, official expected rows, or benchmark-only metadata as implementation guidance.",
        "",
    ]
    if contract.get("instance_id"):
        sections.append(f"- Instance: `{contract['instance_id']}`")
    if symbols:
        sections.append("- Required public source symbols/interfaces:")
        sections.extend(f"  - `{symbol}`" for symbol in symbols)
    if contract_excerpt:
        excerpt = contract_excerpt[:6000]
        if len(contract_excerpt) > len(excerpt):
            excerpt += "\n... truncated; see task metadata for the full official contract."
        sections.extend(
            [
                "- Official requirements/interface excerpt:",
                "",
                "```text",
                excerpt,
                "```",
            ]
        )
    if not symbols:
        sections.append("- No explicit expected tests or public-symbol invariants were provided by the adapter.")
    sections.extend(
        [
            "",
            "Completion rules:",
            "- Do not remove, rename, or omit a required public symbol while fixing another issue.",
            "- Preserve names, arity, parameter order, return shape, and package placement for any symbol referenced by visible tests, source callers, docs, public APIs, schemas, or runtime boundaries, including package-private helpers.",
            "- Visible-test success does not override this ledger; workers must preserve these invariants and verifiers must reject contradictions.",
            "- Literal expected values, command argv, serialized outputs, error text, and ordered lists from legitimate task/source evidence are normative; workers and verifiers must probe that exact shape when practical.",
            "- Hidden contracts must be inferred from user intent, issue text, visible tests, docs, source compatibility behavior, public APIs, data schemas, and runtime behavior.",
            "- Verifier reports must explicitly say whether every listed invariant is preserved.",
            "",
        ]
    )
    return "\n".join(sections)


def write_contract_ledger(issue: str, metadata: dict[str, object] | None = None) -> Path:
    CONTRACT_LEDGER_PATH.write_text(contract_ledger_text(issue, metadata), encoding="utf-8")
    return CONTRACT_LEDGER_PATH


def contract_ledger_excerpt(limit: int = 6000) -> str:
    if not CONTRACT_LEDGER_PATH.exists():
        return "Contract ledger has not been generated yet."
    return CONTRACT_LEDGER_PATH.read_text(encoding="utf-8", errors="replace")[-limit:]


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


def require_path(path: Path, description: str) -> None:
    if not path.exists():
        raise RuntimeError(f"missing {description}: {path}")


def write_codex_bridge(real_codex: str, model: str, auth_mode: str) -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    node_bin = str(Path(real_codex).parent / "node")
    codex_exec = (
        f"exec {node_bin!r} {real_codex!r} \\"
        if Path(node_bin).exists() and os.access(node_bin, os.X_OK)
        else f"exec {real_codex!r} \\"
    )
    (CODEX_HOME / "config.toml").write_text(
        """[projects."/app"]
trust_level = "trusted"

[projects."/opt/multiagent"]
trust_level = "trusted"
""",
        encoding="utf-8",
    )
    if auth_mode == "chatgpt":
        CODEX_WRAPPER.write_text(
            f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_HOME={str(CODEX_HOME)!r}
{codex_exec}
  -c 'model_provider="openai"' \\
  -c 'model="{model}"' \\
  "$@"
""",
            encoding="utf-8",
        )
        CODEX_WRAPPER.chmod(0o755)
        return

    CODEX_WRAPPER.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
export CODEX_HOME={str(CODEX_HOME)!r}
{codex_exec}
  -c 'model_provider="evalscope"' \\
  -c 'model_providers.evalscope.name="EvalScope Bridge"' \\
  -c "model_providers.evalscope.base_url=\\"${{OPENAI_BASE_URL}}\\"" \\
  -c 'model_providers.evalscope.env_key="OPENAI_API_KEY"' \\
  -c 'model_providers.evalscope.wire_api="responses"' \\
  -c 'model="{model}"' \\
  "$@"
""",
        encoding="utf-8",
    )
    CODEX_WRAPPER.chmod(0o755)


def write_apply_patch_helper() -> None:
    APPLY_PATCH_WRAPPER.parent.mkdir(parents=True, exist_ok=True)
    APPLY_PATCH_WRAPPER.write_text(
        r'''#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def die(message: str) -> None:
    print(f"apply_patch: {message}", file=sys.stderr)
    raise SystemExit(1)


def strip_prefix(line: str) -> str:
    if not line:
        die("malformed empty patch line")
    return line[1:]


def find_sequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    limit = len(lines) - len(needle) + 1
    for idx in range(max(0, start), max(0, limit)):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    for idx in range(0, max(0, limit)):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def apply_update(path: Path, hunks: list[list[str]]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    cursor = 0
    for hunk in hunks:
        old: list[str] = []
        new: list[str] = []
        for line in hunk:
            if line.startswith(" "):
                old.append(strip_prefix(line))
                new.append(strip_prefix(line))
            elif line.startswith("-"):
                old.append(strip_prefix(line))
            elif line.startswith("+"):
                new.append(strip_prefix(line))
            elif line.startswith("\\"):
                continue
            else:
                die(f"unsupported hunk line in {path}: {line!r}")
        idx = find_sequence(lines, old, cursor)
        if idx < 0:
            die(f"could not find hunk context in {path}")
        lines[idx : idx + len(old)] = new
        cursor = idx + len(new)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def main() -> int:
    text = sys.stdin.read().splitlines()
    if not text or text[0] != "*** Begin Patch":
        die("expected *** Begin Patch")
    idx = 1
    changed: list[Path] = []
    while idx < len(text):
        line = text[idx]
        if line == "*** End Patch":
            break
        if line.startswith("*** Update File: "):
            path = Path(line.removeprefix("*** Update File: "))
            idx += 1
            hunks: list[list[str]] = []
            current: list[str] | None = None
            while idx < len(text) and not text[idx].startswith("*** "):
                if text[idx].startswith("@@"):
                    if current is not None:
                        hunks.append(current)
                    current = []
                else:
                    if current is None:
                        die(f"expected hunk header for {path}")
                    current.append(text[idx])
                idx += 1
            if current is not None:
                hunks.append(current)
            apply_update(path, hunks)
            changed.append(path)
            continue
        if line.startswith("*** Add File: "):
            path = Path(line.removeprefix("*** Add File: "))
            idx += 1
            new_lines: list[str] = []
            while idx < len(text) and not text[idx].startswith("*** "):
                if not text[idx].startswith("+"):
                    die(f"expected add line for {path}")
                new_lines.append(strip_prefix(text[idx]))
                idx += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
            changed.append(path)
            continue
        if line.startswith("*** Delete File: "):
            path = Path(line.removeprefix("*** Delete File: "))
            path.unlink()
            changed.append(path)
            idx += 1
            continue
        die(f"unsupported patch directive: {line!r}")
    if idx >= len(text) or text[idx] != "*** End Patch":
        die("missing *** End Patch")
    for path in changed:
        print(f"patched {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    APPLY_PATCH_WRAPPER.chmod(0o755)
    try:
        if not STABLE_APPLY_PATCH.exists():
            shutil.copy2(APPLY_PATCH_WRAPPER, STABLE_APPLY_PATCH)
            STABLE_APPLY_PATCH.chmod(0o755)
    except OSError as exc:
        log(f"could not install stable apply_patch helper at {STABLE_APPLY_PATCH}: {exc}")


def _walk_source_dirs(workdir: Path, *, max_dirs: int = 500) -> list[str]:
    ignored = {".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "coverage", "__pycache__"}
    dirs: list[str] = []
    for root, names, _files in os.walk(workdir):
        names[:] = [name for name in names if name not in ignored and not name.startswith(".cache")]
        rel = Path(root).relative_to(workdir)
        if rel == Path("."):
            continue
        if len(rel.parts) > 4:
            names[:] = []
            continue
        dirs.append(str(rel))
        if len(dirs) >= max_dirs:
            break
    return dirs


def repo_discovery_snapshot(workdir: Path, issue: str) -> str:
    """Build a compact, public-source-only orientation note for the orchestrator."""
    sections: list[str] = ["\n## Repository Discovery Snapshot\n"]
    top_level = [path.name + ("/" if path.is_dir() else "") for path in sorted(workdir.iterdir(), key=lambda p: p.name)[:60]]
    if top_level:
        sections.append("Top-level entries visible in /app: " + ", ".join(top_level[:40]))

    go_mod = workdir / "go.mod"
    if go_mod.exists():
        module = ""
        for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("module "):
                module = line.removeprefix("module ").strip()
                break
        issue_lower = issue.lower()
        issue_terms = {
            term
            for term in re.findall(r"[a-zA-Z][a-zA-Z0-9_/-]{2,}", issue_lower)
            if len(term) >= 4
        }
        priority_terms = {
            "auth",
            "user",
            "api",
            "server",
            "cache",
            "database",
            "config",
            "policy",
            "session",
            "parser",
            "serializer",
            "adapter",
            "client",
            "model",
            "metadata",
        }
        candidates: list[tuple[int, str, str]] = []
        for rel in _walk_source_dirs(workdir):
            rel_lower = rel.lower()
            score = 0
            for term in issue_terms | priority_terms:
                normalized = term.replace("_", "-")
                if normalized in rel_lower or normalized.replace("-", "") in rel_lower.replace("-", ""):
                    score += 1
            if score:
                has_go = any(path.suffix == ".go" for path in (workdir / rel).glob("*.go"))
                candidates.append((score, rel, "go-files" if has_go else "dir-only"))
        candidates = sorted(candidates, key=lambda item: (-item[0], item[1]))[:18]
        go_note = f"Go module: {module or '(module line not found)'}."
        if candidates:
            go_note += " Public-source candidate package directories from issue terms: " + ", ".join(
                f"{rel} ({kind})" for _score, rel, kind in candidates
            )
        else:
            go_note += " No obvious package directory matched issue terms; run read-only package discovery before editing."
        sections.append(go_note)
        sections.append(
            "Go placement rule: when the issue asks for new exported structs/functions, choose the package whose import path matches "
            "the domain named in the issue, even if that directory currently has no non-test Go files. Do not default to a generic "
            "`utils` package when a domain-specific package or API package exists."
        )
        sections.append(
            "Go public API contract rule: before finalizing a new exported API, infer exact names, package placement, return "
            "shape, and injectable seams from the issue text, visible source callers, docs, and nearby tests. If multiple "
            "spellings are plausible from visible evidence, prefer tiny compatibility wrappers over a broad rewrite."
        )
        sections.append(
            "Go parser/reader rule: when an issue asks for parsing or filesystem/input readers, derive malformed-input, "
            "partial-data, and injected-error behavior from visible docs, callers, and existing tests. Keep data structures "
            "minimal unless public source evidence requires broader fields."
        )

    package_json = workdir / "package.json"
    if package_json.exists():
        sections.append(
            "JavaScript/TypeScript repo detected. Prefer repository-visible package scripts and nearby Jest/Mocha/Vitest test files; "
            "do not edit built assets or lockfiles unless the issue explicitly asks for them."
        )

    if (workdir / "pyproject.toml").exists() or (workdir / "setup.py").exists() or (workdir / "pytest.ini").exists():
        sections.append(
            "Python repo detected. Prefer the nearest pytest module/package and inspect import paths before adding new public APIs."
        )

    return "\n".join(sections) + "\n"


def make_prompt(repo_root: Path, workdir: Path, issue: str, metadata: dict[str, object] | None = None) -> Path:
    base_prompt = repo_root / "orchestrator_prompt.md"
    require_path(base_prompt, "production orchestrator prompt")
    solver_metadata = public_solver_metadata(metadata or {})
    ledger_path = write_contract_ledger(issue, solver_metadata)
    prompt = (
        base_prompt.read_text(encoding="utf-8")
        + AUTONOMOUS_APPENDIX
        + issue
        + "\n\n## Durable Contract Ledger\n\n"
        + f"The adapter wrote the durable contract ledger to `{ledger_path}`. "
        + "Every worker and verifier instruction must preserve every invariant in that file. "
        + "When spawning follow-up workers, copy the relevant ledger items into the worker prompt.\n\n"
        + contract_ledger_excerpt()
        + repo_discovery_snapshot(workdir, issue)
        + AUTONOMOUS_FINAL_OVERRIDE
    )
    prompt_path = RUNTIME_ROOT / "orchestrator-autonomous-prompt.md"
    prompt_path.write_text(prompt, encoding="utf-8")
    return prompt_path


def git_diff(cwd: Path) -> str:
    args = ["git", "diff", "--binary", "--ignore-submodules=all"]
    if ACTIVE_START_HEAD:
        args.append(ACTIVE_START_HEAD)
    result = run(args, cwd=cwd, timeout=60)
    return result.stdout


def git_head(cwd: Path) -> str:
    result = run(["git", "rev-parse", "HEAD"], cwd=cwd, timeout=30, check=True)
    return result.stdout.strip()


def materialize_committed_changes(cwd: Path, start_head: str) -> None:
    current_head = git_head(cwd)
    if current_head == start_head:
        return
    log(f"materializing committed changes as working diff: {start_head[:12]}..{current_head[:12]}")
    result = run(["git", "reset", "--mixed", start_head], cwd=cwd, timeout=120)
    if result.returncode != 0:
        tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
        raise RuntimeError(f"failed to materialize committed changes with git reset --mixed: {tail}")


def clear_blocked_changes(cwd: Path, start_head: str, reason: str) -> None:
    log(f"clearing /app git state: {reason}")
    result = run(["git", "reset", "--hard", start_head], cwd=cwd, timeout=120)
    if result.returncode != 0:
        tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
        raise RuntimeError(f"failed to clear blocked changes with git reset --hard: {tail}")


def is_disallowed_patch_path(path: str) -> bool:
    name = Path(path).name
    lowered = path.lower()
    return (
        name in {"dump.rdb", "appendonly.aof", "appendonly.aof.manifest", "patch.txt", "patch.diff", "changes.diff"}
        or name.startswith(("patch-", "patch_"))
        or lowered.endswith((".patch", ".diff"))
        or lowered.startswith("appendonlydir/")
        or "/appendonlydir/" in lowered
        or lowered.startswith((".cache/", ".gocache/", ".gomodcache/", ".npm/", ".pnpm-store/", ".yarn/cache/"))
        or any(marker in lowered for marker in ("/.cache/", "/.gocache/", "/.gomodcache/", "/.npm/", "/.pnpm-store/", "/.yarn/cache/"))
        or lowered.startswith(("test/", "tests/"))
        or any(marker in lowered for marker in (".test.", ".spec.", "_test.", "/test/", "/tests/", "__tests__"))
        or "/node_modules/" in lowered
        or "/dist/" in lowered
        or "/build/" in lowered
        or "/coverage/" in lowered
        or lowered.startswith("doc/help/")
        or "/doc/help/" in lowered
        or "/public/assets/" in lowered
        or "/public/build/" in lowered
        or "/public/dist/" in lowered
        or lowered.endswith((".bundle.js", ".bundle.css", ".min.js", ".min.css"))
        or (name.endswith("_mock.go") or name.startswith("mock_"))
        or name
        in {
            "package-lock.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "poetry.lock",
            "go.sum",
            "go.work.sum",
        }
    )


def is_dependency_manifest_path(path: str) -> bool:
    name = Path(path).name
    lowered = path.lower()
    return (
        name
        in {
            "package.json",
            "package-lock.json",
            "npm-shrinkwrap.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "requirements.txt",
            "requirements-dev.txt",
            "pyproject.toml",
            "poetry.lock",
            "pipfile",
            "pipfile.lock",
            "go.mod",
            "go.sum",
            "go.work",
            "go.work.sum",
            "cargo.toml",
            "cargo.lock",
        }
        or lowered.endswith(("/requirements.txt", "/requirements-dev.txt"))
        or "/requirements/" in lowered
    )


def cleanup_initial_environment_diff(cwd: Path, start_head: str) -> list[str]:
    """Remove dependency/install churn that exists before workers start.

    EvalScope auto-install and image setup can mutate tracked manifests before
    the production orchestrator has done any task work. If left in place, those
    files pollute ownership detection and can become the only final diff. This
    cleanup runs only at solver startup, before any worker can make a legitimate
    source edit.
    """

    result = run(["git", "diff", "--name-only", "HEAD", "--"], cwd=cwd, timeout=30)
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    restore = [
        path
        for path in changed
        if is_disallowed_patch_path(path) or is_dependency_manifest_path(path) or is_gitlink_path(cwd, path)
    ]
    if restore:
        result = run(["git", "restore", "--source", start_head, "--staged", "--worktree", "--", *restore], cwd=cwd, timeout=120)
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
            raise RuntimeError(f"failed to restore pre-worker environment diffs from task HEAD: {tail}")
        log(f"restored pre-worker environment diffs before orchestration: {restore}")
    return restore


def is_gitlink_path(cwd: Path, path: str) -> bool:
    result = run(["git", "ls-files", "-s", "--", path], cwd=cwd, timeout=30)
    return any(line.startswith("160000 ") for line in result.stdout.splitlines())


def mark_untracked_source_intent_to_add(cwd: Path) -> list[str]:
    """Make new source files visible to live adapter diff checks.

    The official scorer reads ``git diff``. Workers sometimes create a source
    file and report its contents before running ``git add -N``. Waiting until
    final cleanup hides required public symbols from the live coverage gate, so
    mark safe untracked source files as intent-to-add during polling too.
    """

    others = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd, timeout=30)
    untracked = [line.strip() for line in others.stdout.splitlines() if line.strip()]
    intent_to_add = [
        path
        for path in untracked
        if not is_disallowed_patch_path(path) and (cwd / path).is_file()
    ]
    if intent_to_add:
        run(["git", "add", "-N", "--", *intent_to_add], cwd=cwd, timeout=120)
        log(f"marked untracked source files intent-to-add for live diff checks: {intent_to_add}")
    return intent_to_add


def cleanup_patch(cwd: Path, start_head: str) -> list[str]:
    result = run(["git", "diff", "--name-only", "HEAD", "--"], cwd=cwd, timeout=30)
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    restore: list[str] = []
    for path in changed:
        if is_disallowed_patch_path(path) or is_gitlink_path(cwd, path):
            restore.append(path)
    if restore:
        result = run(["git", "restore", "--source", start_head, "--staged", "--worktree", "--", *restore], cwd=cwd, timeout=120)
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
            raise RuntimeError(f"failed to restore benchmark-disallowed paths from task HEAD: {tail}")

    others = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd, timeout=30)
    untracked = [line.strip() for line in others.stdout.splitlines() if line.strip()]
    intent_to_add: list[str] = []
    removed_untracked: list[str] = []
    for path in untracked:
        full_path = cwd / path
        if is_disallowed_patch_path(path):
            try:
                if full_path.is_dir():
                    shutil.rmtree(full_path)
                else:
                    full_path.unlink(missing_ok=True)
                removed_untracked.append(path)
            except OSError as exc:
                log(f"could not remove untracked disallowed path {path}: {exc}")
        elif full_path.is_file():
            intent_to_add.append(path)
    for cache_root in (".cache", ".gocache", ".gomodcache", ".npm", ".pnpm-store"):
        full_path = cwd / cache_root
        if not full_path.exists():
            continue
        try:
            if full_path.is_dir():
                shutil.rmtree(full_path)
            else:
                full_path.unlink(missing_ok=True)
            removed_untracked.append(cache_root)
        except OSError as exc:
            log(f"could not remove untracked tool cache root {cache_root}: {exc}")
    if intent_to_add:
        mark_untracked_source_intent_to_add(cwd)
    if removed_untracked:
        log(f"removed untracked benchmark-disallowed paths: {removed_untracked}")
    remaining = run(["git", "diff", "--name-only", "HEAD", "--"], cwd=cwd, timeout=30)
    remaining_disallowed = [
        line.strip()
        for line in remaining.stdout.splitlines()
        if line.strip() and is_disallowed_patch_path(line.strip())
    ]
    if remaining_disallowed:
        raise RuntimeError(f"benchmark-disallowed paths remain in final diff after cleanup: {remaining_disallowed}")
    return restore


def status() -> dict[str, object]:
    if not STATUS_PATH.exists():
        return {}
    try:
        parsed = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {"status": "invalid-json", "raw": STATUS_PATH.read_text(encoding="utf-8", errors="replace")[-1000:]}


def capture_session(session: str) -> None:
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


def captured_text() -> str:
    out_dir = RUNTIME_ROOT / "captures"
    if not out_dir.exists():
        return ""
    chunks: list[str] = []
    for path in sorted(out_dir.glob("*.txt")):
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace")[-12000:])
        except OSError:
            continue
    return "\n".join(chunks).lower()


def emit_failure_diagnostics(session: str, *, limit: int = 24000) -> None:
    """Print compact runtime diagnostics before the sandbox is deleted."""
    sections: list[str] = ["failure diagnostics:"]
    if STATUS_PATH.exists():
        try:
            sections.append("status.json:\n" + STATUS_PATH.read_text(encoding="utf-8", errors="replace")[-4000:])
        except OSError as exc:
            sections.append(f"status.json: unreadable: {exc}")

    windows = run(["tmux", "list-windows", "-t", session, "-F", "#W"], timeout=10)
    if windows.returncode == 0 and windows.stdout.strip():
        sections.append("tmux windows:\n" + windows.stdout.strip())

    captures_dir = RUNTIME_ROOT / "captures"
    if captures_dir.exists():
        for path in sorted(captures_dir.glob("*.txt"))[:12]:
            try:
                tail = path.read_text(encoding="utf-8", errors="replace")[-3000:]
            except OSError as exc:
                tail = f"unreadable: {exc}"
            sections.append(f"capture {path.name}:\n{tail}")

    subagents_dir = RUNTIME_ROOT / "state" / "subagents"
    if subagents_dir.exists():
        for agent_dir in sorted(path for path in subagents_dir.iterdir() if path.is_dir())[:12]:
            status_file = agent_dir / "status"
            status_text = ""
            if status_file.exists():
                status_text = status_file.read_text(encoding="utf-8", errors="replace").strip()
            sections.append(f"subagent {agent_dir.name} status: {status_text or 'unknown'}")
            for name in ("current.txt", "last-message.txt", "last-error.txt"):
                path = agent_dir / name
                if not path.exists():
                    continue
                try:
                    sections.append(f"subagent {agent_dir.name} {name}:\n" + path.read_text(encoding="utf-8", errors="replace")[-2500:])
                except OSError as exc:
                    sections.append(f"subagent {agent_dir.name} {name}: unreadable: {exc}")

    text = "\n\n".join(sections)
    log(text[-limit:])


def accepted_without_status_marker(text: str, diff_bytes: int) -> bool:
    if not text:
        return False
    status_write_failed = (
        ("cannot write" in text and "status.json" in text)
        or ("no longer available" in text and "status.json" in text)
        or ("failed to write" in text and "status.json" in text)
        or ("write /tmp/multiagent-prod-swe/status.json" in text and "status.json" in text)
        or ("writing /tmp/multiagent-prod-swe/status.json" in text and "status.json" in text)
    )
    if not status_write_failed:
        return False
    if "reject:" in text or "blocking finding" in text and "none" not in text:
        return False
    worker_commit_done = (
        "final status: complete" in text
        and "commit:" in text
        and ("worker-" in text or "assignment" in text)
    )
    if diff_bytes <= 0 and not worker_commit_done:
        return False
    accepted = (
        "blocking findings\n\n  - none" in text
        or "blocking findings\n\n  none" in text
        or "blocking findings: none" in text
        or "no blocking" in text
        or "recommendation\n  accept" in text
        or "recommendation: accept" in text
        or "accept with follow-up" in text
    )
    return accepted


def final_verifier_accepted_without_status(text: str, diff_bytes: int) -> bool:
    if diff_bytes <= 0 or not text:
        return False
    if not orchestrator_exited_without_status(text):
        return False
    rejected = (
        "recommendation: reject" in text
        or "blocking finding" in text and "none" not in text
        or "blockers remain" in text
    )
    if rejected:
        return False
    accepted = (
        "blockers: none\n\nrecommendation: accept" in text
        or "blockers: none\r\n\r\nrecommendation: accept" in text
        or "verifier accepted the patch" in text
        or "accepted the patch" in text and "verifier" in text
        or "completed via the multiagent workflow" in text
        or "ponytail pass: no blockers found" in text
    )
    return accepted


def visible_validation_passed_in_text(text: str) -> bool:
    """Return whether captured agent output contains a passing visible validation.

    This is a generic recovery signal for cases where a bounded worker fixed the
    source diff and reported a local visible test command, but the orchestrator
    exited before writing ``status.json``. It must not encode benchmark expected
    tests or row-specific knowledge.
    """

    text_lower = text.lower()
    if not text_lower:
        return False
    if any(marker in text_lower for marker in ("no tests ran", "0 tests", "0 passed")):
        return False
    summary_matches = list(
        re.finditer(
            r"=+\s+(?P<summary>[^=\n]*(?:passed|xfailed|deselected)[^=\n]*)\s+=+",
            text_lower,
        )
    )
    for match in reversed(summary_matches):
        summary = match.group("summary")
        if "passed" in summary and " failed" not in summary and " error" not in summary and " errors" not in summary:
            return True
    validation_markers = (
        "validation passed:",
        "result:",
        "tests passed",
        "go test",
        "pytest",
        "npm test",
        "yarn test",
    )
    if not any(marker in text_lower for marker in validation_markers):
        return False
    tail = text_lower[-5000:]
    return (
        (" passed" in tail or ": passed" in tail)
        and "failed" not in tail
        and "error:" not in tail
        and "traceback" not in tail
    )


def validation_coverage_blockers(
    issue: str,
    diff: str,
    text: str,
    current_status: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> list[str]:
    issue_lower = issue.lower()
    diff_lower = diff.lower()
    issue_and_diff = f"{issue_lower}\n{diff_lower}"
    # Only the explicit status payload can clear the gate. The captured tmux
    # text may include the original prompt or adapter follow-up instructions,
    # so treating it as proof can turn instructions into false evidence.
    status_text = json.dumps(current_status, sort_keys=True).lower()
    official_contract_satisfied = official_expected_tests_satisfied_by_text(metadata or {}, text)
    blockers: list[str] = [] if official_contract_satisfied else official_expected_test_blockers(metadata or {}, current_status)

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
        go_validation_markers = (
            "go test",
            "go-validation-passed:",
            "go-validation-skip-justified:",
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
            "helper-validation-passed:" in status_text
            or "return code: 0" in status_text and "go test" in status_text
            or "go test" in status_text and any(marker in status_text for marker in (" passed", ": passed", "[no test files]"))
        )
        if not any(marker in status_text for marker in go_validation_markers):
            blockers.append(
                "Go source changed, but status.json does not record a Go package validation command such as `go test ./affected/package`"
            )
        if any(marker in status_text for marker in missing_tool_markers) and not go_probe_passed:
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

    return blockers






def pytest_teardown_after_success(output: str) -> bool:
    """Treat a post-summary teardown transport error as success from output evidence."""

    output_lower = output.lower()
    if "the x11 connection broke" not in output_lower and "fatal io error" not in output_lower:
        return False
    summary_matches = list(
        re.finditer(
            r"=+\s+(?P<summary>[^=\n]*(?:passed|xfailed|deselected)[^=\n]*)\s+=+",
            output_lower,
        )
    )
    if not summary_matches:
        return False
    summary = summary_matches[-1].group("summary")
    return (
        "passed" in summary
        and " failed" not in summary
        and " error" not in summary
        and " errors" not in summary
        and " no tests ran" not in summary
    )








def run_validation_coverage_probe(workdir: Path, issue: str, diff: str, blockers: list[str]) -> tuple[str, bool]:
    commands = coverage_probe_commands(workdir, issue, diff)
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
            result = run(command, cwd=workdir, timeout=env_positive_int("EVAL_VALIDATION_PROBE_TIMEOUT", 300))
            returncode = result.returncode
            output = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            output = (stdout + "\n" + stderr).strip()
            output = (output + "\n" if output else "") + f"adapter validation probe timed out after {exc.timeout} seconds"
        teardown_success = returncode != 0 and pytest_teardown_after_success(output)
        if returncode != 0 and not teardown_success:
            passed = False
        sections.append(
            "\nCommand: "
            + label
            + f"\nReturn code: {returncode}\nOutput tail:\n"
            + output[-6000:]
        )
        if teardown_success:
            sections.append(
                "\nAdapter note: treated nonzero pytest rc as passed because pytest reported all selected "
                "tests passed before a teardown transport error."
            )
    if passed:
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
        if "go source changed" in lower and "validation" in lower:
            continue
        remaining.append(blocker)
    return remaining


def status_records_selected_validation(current_status: dict[str, object]) -> bool:
    evidence = json.dumps(current_status, sort_keys=True).lower()
    return "helper-validation-passed" in evidence


def has_hard_scope_blocker(blockers: list[str]) -> bool:
    return any("[official-hard]" in blocker.lower() for blocker in blockers)


def send_tmux_literal(session: str, message: str) -> None:
    """Send literal text to tmux after stripping bytes subprocess cannot pass."""
    safe_message = message.replace("\x00", "")
    safe_message = "".join(
        char if char in "\n\t" or ord(char) >= 32 else " "
        for char in safe_message
    )
    run(["tmux", "send-keys", "-t", session, "-l", safe_message], timeout=30)
    run(["tmux", "send-keys", "-t", session, "Enter"], timeout=30)


def send_orchestrator_followup(session: str, blockers: list[str], probe_report: str, source_hints: list[str]) -> None:
    probe_excerpt = probe_report[-5000:] if probe_report else "No adapter helper probe output."
    hint_text = (
        " Source-derived helper ownership candidates: " + ", ".join(source_hints) + "."
        if source_hints
        else " No specific ownership candidates were auto-detected; run read-only discovery for helper/resend APIs, then spawn the narrowest source worker."
    )
    message = (
        "Benchmark adapter rejected the completion marker. "
        "Do not write completed status yet. Blocking findings: "
        + "; ".join(blockers)
        + "."
        + hint_text
        + f" Durable contract ledger: {CONTRACT_LEDGER_PATH}. Every follow-up worker/verifier must preserve every ledger item. Ledger excerpt:\n"
        + contract_ledger_excerpt()
        + "\n"
        + " If any finding is an implementation-scope blocker, spawn a new bounded source worker with these implicated source paths in --owned; do not only rerun the original feature worker. "
        + "Do not use tmux send-keys to send implementation instructions to a completed worker pane; create a fresh assignment and `bin/subagent.sh spawn` a new worker process. "
        + f"The adapter ran public helper validation and wrote details to {HELPER_PROBE_PATH}. "
        + "Probe output tail:\n"
        + probe_excerpt
        + "\nContinue the orchestration loop: remove or ignore the prior status marker, spawn a bounded follow-up "
        "worker/verifier if needed, inspect the implicated helper/resend APIs and nearby tests, run the relevant source or helper-layer "
        "test file/package when practical. The verifier final report must include the helper validation pass marker "
        "from the initial benchmark instructions plus the exact passing helper command, or the helper validation skip "
        "marker from the initial benchmark instructions plus the concrete source-level reason no helper test is relevant. "
        "Do not use leaked evaluator rows or benchmark-only expected-test metadata as implementation guidance. "
        "Choose validation from legitimate task/source/product evidence: issue text, visible tests, docs, source callers, public APIs, schemas, fixtures, and runtime behavior. "
        "If the ledger lists required public symbols, the follow-up worker must keep or add those exact source symbols while fixing the latest blocker. "
        "Only write completed status after this is addressed."
    )
    send_tmux_literal(session, message)


def send_orchestrator_scope_warning(session: str, blockers: list[str], source_hints: list[str]) -> None:
    hint_text = (
        " Source-derived helper ownership candidates: " + ", ".join(source_hints) + "."
        if source_hints
        else " No specific ownership candidates were auto-detected; run read-only discovery for helper/resend APIs, then spawn the narrowest source worker."
    )
    message = (
        "Early benchmark scope warning: the current /app diff appears to be a feature-level patch that may fail official tests. "
        "Do not write completed status until these implementation-scope blockers are resolved: "
        + "; ".join(blockers)
        + "."
        + hint_text
        + f" Durable contract ledger: {CONTRACT_LEDGER_PATH}. Preserve every ledger item in all follow-up work. Ledger excerpt:\n"
        + contract_ledger_excerpt()
        + "\n"
        + " If a worker is still running, let it finish, then spawn a bounded source follow-up with the implicated source paths in --owned. "
        + "If the worker has already exited, do not send implementation text to its tmux pane; create a fresh assignment and spawn a new worker process. "
        + "The follow-up must implement or prove the portable helper/resend contract, run or justify the relevant source/helper test file/package, "
        + "and the verifier/status validation must include the required helper audit markers."
    )
    send_tmux_literal(session, message)


def benchmark_specific_recovery_enabled(issue: str, blockers: list[str], diff: str) -> bool:
    """Deprecated compatibility hook.

    PR4's production eval path must not activate row-specific repair flows from
    benchmark memory. Never route source edits through a benchmark-row-specific
    adapter worker.
    """

    return False


def spawn_adapter_helper_worker(
    repo_root: Path,
    workdir: Path,
    env: dict[str, str],
    issue: str,
    diff: str,
    blockers: list[str],
    source_owned: list[str],
    index: int,
    probe_report: str = "",
) -> str:
    """Spawn an opt-in no-leak adapter helper worker.

    This path is disabled by default and is only for explicit adapter-repair
    experiments. It must not include project-specific hidden test knowledge or
    memorized benchmark fixes; workers receive only the issue, current diff,
    generic blockers, visible contract ledger, and source-derived ownership
    hints.
    """

    owned = list(dict.fromkeys(source_owned or helper_scope_hints(workdir, issue, diff, blockers)))
    if not owned:
        owned = [path for path in ("src", "lib", "app", "pkg", "internal") if (workdir / path).exists()]
    if not owned:
        owned = ["."]
    owned_csv = ",".join(owned[:8])
    worker_name = f"worker-adapter-helper-{index:02d}"
    assignment_id = f"SWE-ADAPTER-HELPER-{index:03d}"
    diff_excerpt = diff[-5000:]
    probe_excerpt = probe_report[-4000:] if probe_report else ""
    ledger_excerpt = contract_ledger_excerpt()
    instruction = (
        "You are a bounded source worker launched by an explicit adapter-repair experiment. "
        "Work in /app only. Do not submit PRs, push, or send external messages. "
        f"Assignment ID: {assignment_id}. Branch: benchmark. Stay inside these owned source paths: {owned_csv}. "
        "Do not edit tests, lockfiles, generated assets, bundled assets, or unrelated config unless the visible task/source contract requires fixture assets.\n\n"
        "No-leak rule: do not rely on hidden tests, official expected rows, previous benchmark failures, or benchmark-only metadata as implementation guidance. "
        "Use only the issue text, visible source/tests/docs, public APIs, runtime behavior, and the current diff.\n\n"
        f"Durable contract ledger from `{CONTRACT_LEDGER_PATH}`:\n{ledger_excerpt}\n\n"
        "Generic blocking findings from the adapter/verifier:\n- "
        + "\n- ".join(blockers)
        + "\n\nTask: inspect the implicated source/helper layer and implement or prove the missing source-derived contract. "
        "If a blocker lacks visible source evidence, report it as unresolved risk instead of coding to it. "
        "Run or attempt the relevant visible test file/package or a temporary source-level probe derived from visible evidence.\n\n"
        "Current issue text excerpt:\n"
        + issue[:3500]
        + ("\n\nAdapter public validation probe output excerpt:\n" + probe_excerpt if probe_excerpt else "")
        + "\n\nCurrent /app diff excerpt to integrate with, without reverting unrelated feature work:\n"
        + diff_excerpt
    )
    run(
        [
            str(repo_root / "bin/subagent.sh"),
            "assignment-create",
            worker_name,
            "--assignment-id",
            assignment_id,
            "--branch",
            "benchmark",
            "--owned",
            owned_csv,
            "--role",
            "worker",
        ],
        cwd=repo_root,
        env=env,
        timeout=60,
        check=True,
    )
    run(
        [str(repo_root / "bin/subagent.sh"), "spawn", worker_name, "--instruction", instruction],
        cwd=repo_root,
        env=env,
        timeout=120,
        check=True,
    )
    return worker_name


def blocked_without_status_marker(text: str) -> bool:
    if not text or "status.json" not in text:
        return False
    blocker_phrases = (
        "caller explicitly instructed",
        "benchmark environment is not mounted",
        "environment is not mounted",
        "benchmark environment is unavailable",
        "/app and /opt/multiagent are unavailable",
        "cannot continue the orchestrator workflow",
        "cannot write",
        "failed to write",
        "cannot proceed",
        "unable to continue",
    )
    return "blocked:" in text and any(phrase in text for phrase in blocker_phrases)


def orchestrator_exited_without_status(text: str) -> bool:
    if not text:
        return False
    return (
        "[multiagent codex exec exited rc=" in text
        or "[multiagent claude exited rc=" in text
        or "codex exec exited rc=" in text
        or "claude exited rc=" in text
    )


def has_live_agent_process() -> bool:
    result = run(
        ["ps", "-ef"],
        timeout=10,
    )
    for line in (result.stdout or "").splitlines():
        lower = line.lower()
        if "grep" in lower or "sleep infinity" in lower or "codex exec exited" in lower:
            continue
        if "codex-bridge" in lower and "bash -c" in lower:
            continue
        if (
            "/bin/codex" in lower
            or "node_modules/@openai/codex" in lower
            or " claude" in lower
            or "/claude" in lower
        ):
            return True
    return False


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


def run_prod_solver(prompt_path: str | None, workdir: Path, repo_root: Path, timeout: int) -> int:
    global ACTIVE_START_HEAD
    require_path(repo_root / "launch.sh", "production multiagent launcher")
    require_path(repo_root / "bin" / "subagent.sh", "production subagent helper")
    require_path(workdir / ".git", "SWE task git checkout")
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is required for the production multiagent solver")
    real_codex = find_codex_cli()
    if not real_codex:
        raise RuntimeError(
            "codex CLI is required inside the task image. Bake it into the image or enable a setup command "
            "that installs @openai/codex before running the production solver."
        )
    auth_mode = os.environ.get("EVAL_CODEX_AUTH_MODE", "bridge").strip().lower()
    if auth_mode not in {"bridge", "chatgpt"}:
        raise RuntimeError(f"unsupported EVAL_CODEX_AUTH_MODE={auth_mode!r}")
    if auth_mode == "bridge" and (not os.environ.get("OPENAI_BASE_URL") or not os.environ.get("OPENAI_API_KEY")):
        raise RuntimeError("OPENAI_BASE_URL and OPENAI_API_KEY must be set for the Codex bridge")
    if auth_mode == "chatgpt" and not (CODEX_HOME / "auth.json").exists() and not os.environ.get("CODEX_ACCESS_TOKEN"):
        raise RuntimeError(
            f"ChatGPT Codex auth mode requires {CODEX_HOME / 'auth.json'} or CODEX_ACCESS_TOKEN inside the task container"
        )

    start_head = git_head(workdir)
    ACTIVE_START_HEAD = start_head
    cleanup_initial_environment_diff(workdir, start_head)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    write_codex_bridge(real_codex, os.environ.get("EVAL_NATIVE_SOLVER_MODEL", "gpt-5"), auth_mode)
    write_apply_patch_helper()
    issue = read_prompt(prompt_path)
    task_metadata = read_task_metadata()
    contract = official_test_contract(task_metadata)
    if contract["expected_test_count"]:
        log(
            "stripped official expected-test metadata before solver prompting: "
            f"instance={contract.get('instance_id')} fail_to_pass={len(contract['fail_to_pass'])} "
            f"pass_to_pass={len(contract['pass_to_pass'])}"
        )
    else:
        log("no official expected-test metadata found in task metadata")
    autonomous_prompt = make_prompt(repo_root, workdir, issue, task_metadata)
    session = f"swe-prod-{os.getpid()}"
    toolchain_prefix = ":".join(toolchain_path_prefixes())
    path_parts = [str(RUNTIME_ROOT)]
    if toolchain_prefix:
        path_parts.append(toolchain_prefix)
    path_parts.append(os.environ.get("PATH", ""))
    env = os.environ.copy()
    env.update(
        {
            "MULTIAGENT_SESSION": session,
            "MULTIAGENT_ROOT": str(workdir),
            "MULTIAGENT_STATE_DIR": str(RUNTIME_ROOT / "state"),
            "MULTIAGENT_WRITE_POLICY": str(RUNTIME_ROOT / "write-policy.paths"),
            "MULTIAGENT_PROMPT": str(autonomous_prompt),
            "MULTIAGENT_RESUME": "0",
            "MULTIAGENT_START_HEAD": start_head,
            "ORCHESTRATOR_CLI": "codex",
            "WORKER_CLI": "codex",
            "SUBAGENT_CLI": "codex",
            "VERIFIER_CLI": "codex",
            "CODEX_BIN": str(CODEX_WRAPPER),
            "CODEX_HOME": str(CODEX_HOME),
            "MULTIAGENT_CODEX_EXEC": os.environ.get("MULTIAGENT_CODEX_EXEC", "1"),
            "MULTIAGENT_EXTRA_PATH": str(RUNTIME_ROOT),
            "PATH": ":".join(part for part in path_parts if part),
            "GOCACHE": os.environ.get("GOCACHE", ensure_cache_dir(RUNTIME_ROOT / "go-build-cache")),
            "GOMODCACHE": os.environ.get("GOMODCACHE", ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache")),
            "MULTIAGENT_READY_ATTEMPTS": os.environ.get("MULTIAGENT_READY_ATTEMPTS", "80"),
            "MULTIAGENT_READY_DELAY": os.environ.get("MULTIAGENT_READY_DELAY", "1"),
        }
    )

    launch_tail = ""
    for attempt in range(1, 3):
        log(f"launching production multiagent session={session} root={workdir} repo={repo_root} attempt={attempt}")
        launch = run([str(repo_root / "launch.sh"), "--session", session, "--root", str(workdir), "--no-attach"], env=env, timeout=120)
        launch_tail = ((launch.stderr or "") + "\n" + (launch.stdout or "")).strip()[-4000:]
        if launch.returncode != 0:
            raise RuntimeError(f"production multiagent launch failed: {launch_tail}")
        time.sleep(2)
        if tmux_has_session(session):
            break
        log(f"launch attempt {attempt} exited without a live tmux session")
        run(["tmux", "kill-session", "-t", session], timeout=10)
    else:
        STATUS_PATH.write_text(
            json.dumps({"status": "blocked", "reason": f"multiagent launch exited without live tmux session: {launch_tail[-1000:]}"}),
            encoding="utf-8",
        )
        log("blocked marker: launch exited without a live tmux session")
        return 2

    deadline = time.monotonic() + timeout
    last_capture = 0.0
    missing_session_captures = 0
    coverage_followups_sent = 0
    coverage_followup_at: float | None = None
    early_scope_followups_sent = 0
    early_scope_signature = ""
    early_scope_seen_count = 0
    adapter_helper_workers_spawned = 0
    adapter_helper_last_spawn_at: float | None = None
    adapter_helper_reprobe_done = False
    adapter_helper_last_probe_digest: str | None = None
    coverage_gate_unresolved = False
    coverage_probe_satisfied = False
    selected_validation_claim_seen = False
    coverage_followup_limit = int(os.environ.get("EVAL_COVERAGE_FOLLOWUP_LIMIT", "3"))
    early_scope_followup_limit = int(os.environ.get("EVAL_EARLY_SCOPE_FOLLOWUP_LIMIT", "3"))
    adapter_helper_worker_limit = int(os.environ.get("EVAL_ADAPTER_HELPER_WORKER_LIMIT", "1"))
    adapter_helper_mode = os.environ.get("EVAL_ADAPTER_HELPER_MODE", "advisory").strip().lower()
    adapter_helper_source_edit_opt_in = os.environ.get("EVAL_ADAPTER_HELPER_ALLOW_SOURCE_EDITS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    adapter_helper_repair_enabled = adapter_helper_mode in {"repair", "source-edit", "source_edits"} or adapter_helper_source_edit_opt_in
    adapter_helper_advisory_logs: set[str] = set()

    def adapter_helper_repair_allowed(context: str) -> bool:
        if adapter_helper_repair_enabled:
            return True
        if context not in adapter_helper_advisory_logs:
            adapter_helper_advisory_logs.add(context)
            log(
                "adapter helper advisory mode: not spawning source-editing helper for "
                f"{context}; set EVAL_ADAPTER_HELPER_MODE=repair only for explicit adapter-repair experiments"
            )
        return False

    if not adapter_helper_repair_enabled and adapter_helper_mode not in {"", "advisory", "observe", "read-only", "readonly"}:
        log(f"unknown EVAL_ADAPTER_HELPER_MODE={adapter_helper_mode!r}; using advisory mode")
    early_adapter_helper_spawn_enabled = os.environ.get("EVAL_ADAPTER_HELPER_EARLY_SPAWN", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    coverage_followup_timeout = int(os.environ.get("EVAL_COVERAGE_FOLLOWUP_TIMEOUT", "900"))
    adapter_helper_grace_seconds = int(os.environ.get("EVAL_ADAPTER_HELPER_GRACE_SECONDS", "600"))
    exit_code = 0
    outcome = "timeout"
    try:
        while time.monotonic() < deadline:
            try:
                materialize_committed_changes(workdir, start_head)
            except Exception as exc:
                log(f"could not materialize committed worker changes during polling: {exc}")
            try:
                mark_untracked_source_intent_to_add(workdir)
            except Exception as exc:
                log(f"could not mark untracked source files intent-to-add during polling: {exc}")
            current_status = status()
            if not selected_validation_claim_seen and status_records_selected_validation(current_status):
                selected_validation_claim_seen = True
                log(
                    "status.json claims selected validation, but adapter will rerun its generic visible-source probe before accepting"
                )
            state = str(current_status.get("status", "")).lower()
            if state in {"completed", "complete", "done"}:
                capture_session(session)
                diff = git_diff(workdir)
                text = captured_text()
                scope_blockers = implementation_scope_blockers(issue, diff, current_status, task_metadata)
                coverage_blockers = [] if coverage_probe_satisfied else validation_coverage_blockers(issue, diff, text, current_status, task_metadata)
                blockers = [*scope_blockers, *coverage_blockers]
                if coverage_probe_satisfied:
                    blockers = blockers_after_passing_public_probe(blockers)
                    scope_blockers = blockers
                    coverage_blockers = []
                if not blockers and not coverage_probe_satisfied and coverage_probe_commands(workdir, issue, diff):
                    probe_report, probe_passed = run_validation_coverage_probe(
                        workdir,
                        issue,
                        diff,
                        ["adapter-selected public validation probe required for this issue/diff"],
                    )
                    if probe_passed:
                        coverage_probe_satisfied = True
                        current_status["validation"] = (
                            str(current_status.get("validation", ""))
                            + f"; helper-validation-passed: adapter public validation probe ({HELPER_PROBE_PATH})"
                        )
                        STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
                        log("completion marker verified by adapter public validation probe")
                    else:
                        coverage_blockers = [
                            f"adapter-selected public validation probe failed; inspect {HELPER_PROBE_PATH} and fix the final diff"
                        ]
                        blockers = [*scope_blockers, *coverage_blockers]
                if blockers and coverage_followups_sent < coverage_followup_limit and tmux_has_session(session):
                    probe_report = ""
                    if coverage_blockers or coverage_probe_commands(workdir, issue, diff):
                        probe_report, probe_passed = run_validation_coverage_probe(workdir, issue, diff, coverage_blockers)
                    else:
                        probe_passed = False
                    if probe_passed:
                        coverage_probe_satisfied = True
                        current_status["validation"] = (
                            str(current_status.get("validation", ""))
                            + f"; helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                        )
                        STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
                        log("coverage gate satisfied by adapter public helper probe")
                        blockers = blockers_after_passing_public_probe([*scope_blockers, *coverage_blockers])
                        scope_blockers = blockers
                        coverage_blockers = []
                    if not blockers:
                        log("completion marker accepted after adapter public helper probe")
                    else:
                        coverage_followups_sent += 1
                        try:
                            STATUS_PATH.unlink(missing_ok=True)
                        except OSError as exc:
                            log(f"could not remove weak completion marker before follow-up: {exc}")
                        if (
                            not has_live_agent_process()
                            and adapter_helper_workers_spawned < adapter_helper_worker_limit
                            and adapter_helper_repair_allowed("weak completion")
                        ):
                            adapter_helper_workers_spawned += 1
                            try:
                                helper_worker = spawn_adapter_helper_worker(
                                    repo_root,
                                    workdir,
                                    env,
                                    issue,
                                    diff,
                                    [
                                        *blockers,
                                        "The orchestrator/verifier accepted a weak completion marker but no live agent remains to handle the follow-up; continue from the current /app diff and resolve these adapter blockers.",
                                    ],
                                    helper_scope_hints(workdir, issue, diff, blockers),
                                    adapter_helper_workers_spawned,
                                    probe_report,
                                )
                                log(f"adapter recovery worker spawned immediately after weak completion: {helper_worker}")
                                adapter_helper_last_spawn_at = time.monotonic()
                                adapter_helper_reprobe_done = False
                                adapter_helper_last_probe_digest = None
                                coverage_followup_at = time.monotonic()
                                last_capture = 0.0
                                time.sleep(5)
                                continue
                            except Exception as exc:
                                log(f"adapter recovery worker spawn failed after weak completion: {exc}")
                        send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
                        log(f"coverage gate follow-up {coverage_followups_sent}: {'; '.join(blockers)}")
                        coverage_followup_at = time.monotonic()
                        if (
                            orchestrator_exited_without_status(text)
                            and not has_live_agent_process()
                            and adapter_helper_workers_spawned < adapter_helper_worker_limit
                            and adapter_helper_repair_allowed("rejected completion")
                        ):
                            adapter_helper_workers_spawned += 1
                            try:
                                helper_worker = spawn_adapter_helper_worker(
                                    repo_root,
                                    workdir,
                                    env,
                                    issue,
                                    diff,
                                    [
                                        *blockers,
                                        "The orchestrator already exited after a rejected completion marker; continue from the current /app diff and do not wait for the orchestrator to spawn this follow-up.",
                                    ],
                                    helper_scope_hints(workdir, issue, diff, blockers),
                                    adapter_helper_workers_spawned,
                                    probe_report,
                                )
                                log(f"adapter recovery worker spawned immediately after rejected completion: {helper_worker}")
                                adapter_helper_last_spawn_at = time.monotonic()
                                adapter_helper_reprobe_done = False
                                adapter_helper_last_probe_digest = None
                            except Exception as exc:
                                log(f"adapter recovery worker spawn failed after rejected completion: {exc}")
                        last_capture = 0.0
                        time.sleep(5)
                        continue
                if blockers and has_hard_scope_blocker(blockers):
                    log(f"hard official scope blockers remain after follow-ups; refusing to submit known-bad patch: {'; '.join(blockers)}")
                    current_status = {
                        "status": "blocked",
                        "reason": "hard official scope blocker remains after adapter/verifier follow-ups",
                        "blockers": blockers,
                    }
                    STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
                    exit_code = 2
                    outcome = "blocked"
                    break
                if blockers:
                    log(f"coverage gate still has blockers after follow-ups; preserving patch for scoring: {'; '.join(blockers)}")
                log(f"completion marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
                outcome = "completed"
                break
            if state == "blocked":
                log(f"blocked marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
                exit_code = 2
                outcome = "blocked"
                break
            if time.monotonic() - last_capture > 60:
                capture_session(session)
                diff_bytes = len(git_diff(workdir).encode("utf-8"))
                text = captured_text()
                log(f"waiting status={state or 'none'} diff_bytes={diff_bytes}")
                if (
                    not state
                    and diff_bytes > 0
                    and early_scope_followups_sent < early_scope_followup_limit
                    and tmux_has_session(session)
                ):
                    diff = git_diff(workdir)
                    early_scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    if early_scope_blockers:
                        signature = "; ".join(early_scope_blockers)
                        if signature == early_scope_signature:
                            early_scope_seen_count += 1
                        else:
                            early_scope_signature = signature
                            early_scope_seen_count = 1
                        if early_scope_seen_count >= 2:
                            source_hints = helper_scope_hints(workdir, issue, diff, early_scope_blockers)
                            send_orchestrator_scope_warning(
                                session,
                                early_scope_blockers,
                                source_hints,
                            )
                            early_scope_followups_sent += 1
                            log(f"early scope warning {early_scope_followups_sent}: {signature}")
                            if (
                                early_adapter_helper_spawn_enabled
                                and not has_live_agent_process()
                                and adapter_helper_workers_spawned < adapter_helper_worker_limit
                                and adapter_helper_repair_allowed("early scope warning")
                            ):
                                adapter_helper_workers_spawned += 1
                                try:
                                    helper_worker = spawn_adapter_helper_worker(
                                        repo_root,
                                        workdir,
                                        env,
                                        issue,
                                        diff,
                                        early_scope_blockers,
                                        source_hints,
                                        adapter_helper_workers_spawned,
                                    )
                                    log(f"adapter helper worker spawned: {helper_worker}")
                                    adapter_helper_last_spawn_at = time.monotonic()
                                    adapter_helper_reprobe_done = False
                                except Exception as exc:
                                    log(f"adapter helper worker spawn failed: {exc}")
                            elif not early_adapter_helper_spawn_enabled:
                                log(
                                    "adapter helper worker early spawn skipped; preserving orchestrator ownership of active source edits"
                                )
                            last_capture = time.monotonic()
                            time.sleep(5)
                            continue
                    else:
                        early_scope_signature = ""
                        early_scope_seen_count = 0
                if not state and accepted_without_status_marker(text, diff_bytes):
                    diff = git_diff(workdir)
                    scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    coverage_blockers = [] if coverage_probe_satisfied else validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                    blockers = [*scope_blockers, *coverage_blockers]
                    if coverage_probe_satisfied:
                        blockers = blockers_after_passing_public_probe(blockers)
                        scope_blockers = blockers
                        coverage_blockers = []
                    if blockers and coverage_followups_sent < coverage_followup_limit and tmux_has_session(session):
                        probe_report = ""
                        if coverage_blockers or coverage_probe_commands(workdir, issue, diff):
                            probe_report, probe_passed = run_validation_coverage_probe(workdir, issue, diff, coverage_blockers)
                        else:
                            probe_passed = False
                        if probe_passed:
                            coverage_probe_satisfied = True
                            blockers = blockers_after_passing_public_probe([*scope_blockers, *coverage_blockers])
                            scope_blockers = blockers
                            coverage_blockers = []
                            log("coverage gate satisfied by adapter public helper probe")
                        if blockers:
                            coverage_followups_sent += 1
                            send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
                            log(f"coverage gate follow-up {coverage_followups_sent}: {'; '.join(blockers)}")
                            coverage_followup_at = time.monotonic()
                            if (
                                orchestrator_exited_without_status(text)
                                and not has_live_agent_process()
                                and adapter_helper_workers_spawned < adapter_helper_worker_limit
                                and adapter_helper_repair_allowed("rejected recovered completion")
                            ):
                                adapter_helper_workers_spawned += 1
                                try:
                                    helper_worker = spawn_adapter_helper_worker(
                                        repo_root,
                                        workdir,
                                        env,
                                        issue,
                                        diff,
                                        [
                                            *blockers,
                                            "The orchestrator already exited after a rejected completion marker; continue from the current /app diff and do not wait for the orchestrator to spawn this follow-up.",
                                        ],
                                        helper_scope_hints(workdir, issue, diff, blockers),
                                        adapter_helper_workers_spawned,
                                        probe_report,
                                    )
                                    log(f"adapter recovery worker spawned immediately after rejected recovered completion: {helper_worker}")
                                    adapter_helper_last_spawn_at = time.monotonic()
                                    adapter_helper_reprobe_done = False
                                    adapter_helper_last_probe_digest = None
                                except Exception as exc:
                                    log(f"adapter recovery worker spawn failed after rejected recovered completion: {exc}")
                            last_capture = 0.0
                            time.sleep(5)
                            continue
                    if blockers and has_hard_scope_blocker(blockers):
                        log(f"hard official scope blockers remain after follow-ups; refusing recovered accepted patch: {'; '.join(blockers)}")
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "hard official scope blocker remains after recovered acceptance",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        exit_code = 2
                        outcome = "blocked"
                        break
                    if blockers:
                        log(f"coverage gate still has blockers after follow-ups; recovering accepted patch anyway: {'; '.join(blockers)}")
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "accepted source diff found; orchestrator failed to write status marker",
                                "validation": recovered_validation_text(
                                    task_metadata,
                                    text,
                                    (
                                        f"see captured verifier output; helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                                        if coverage_probe_satisfied
                                        else "see captured verifier output"
                                    ),
                                ),
                                "risk": "status marker was recovered by the benchmark wrapper",
                            }
                        ),
                        encoding="utf-8",
                    )
                    log("completion marker recovered from accepted diff plus verifier output")
                    outcome = "recovered"
                    break
                if not state and final_verifier_accepted_without_status(text, diff_bytes):
                    diff = git_diff(workdir)
                    probe_report = ""
                    probe_passed = coverage_probe_satisfied
                    if not probe_passed and coverage_probe_commands(workdir, issue, diff):
                        probe_report, probe_passed = run_validation_coverage_probe(
                            workdir,
                            issue,
                            diff,
                            ["final verifier accepted without status.json; adapter reran selected public validation before recovery"],
                        )
                    scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    if probe_passed:
                        blockers = blockers_after_passing_public_probe(scope_blockers)
                        if not blockers:
                            STATUS_PATH.write_text(
                                json.dumps(
                                    {
                                        "status": "completed",
                                        "summary": "final verifier accepted source diff; adapter recovered missing status marker",
                                        "validation": recovered_validation_text(
                                            task_metadata,
                                            text,
                                            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                        ),
                                        "risk": "status marker was recovered by the benchmark wrapper",
                                    }
                                ),
                                encoding="utf-8",
                            )
                            log("completion marker recovered from final verifier accept plus passing adapter probe")
                            outcome = "recovered"
                            break
                        coverage_blockers = []
                        log(
                            "final verifier accepted and adapter probe passed, but hard implementation blockers remain: "
                            + "; ".join(blockers)
                        )
                    else:
                        coverage_blockers = [
                            f"final verifier accepted without status.json, but adapter-selected public validation probe failed; inspect {HELPER_PROBE_PATH}"
                        ]
                        blockers = [*scope_blockers, *coverage_blockers]
                    if (
                        tmux_has_session(session)
                        and not has_live_agent_process()
                        and adapter_helper_workers_spawned < adapter_helper_worker_limit
                        and adapter_helper_repair_allowed("final verifier/probe mismatch")
                    ):
                        adapter_helper_workers_spawned += 1
                        try:
                            helper_worker = spawn_adapter_helper_worker(
                                repo_root,
                                workdir,
                                env,
                                issue,
                                diff,
                                [
                                    *blockers,
                                    "The final verifier accepted too early, but the adapter public probe caught a required source-derived public API mismatch. Continue from the current /app diff, add only the missing public contract, and make the adapter probe pass before any completion marker.",
                                ],
                                helper_scope_hints(workdir, issue, diff, blockers),
                                adapter_helper_workers_spawned,
                                probe_report,
                            )
                            log(f"adapter recovery worker spawned after final verifier/probe mismatch: {helper_worker}")
                            adapter_helper_last_spawn_at = time.monotonic()
                            adapter_helper_reprobe_done = False
                            adapter_helper_last_probe_digest = None
                            coverage_followup_at = time.monotonic()
                            last_capture = 0.0
                            time.sleep(5)
                            continue
                        except Exception as exc:
                            log(f"adapter recovery worker spawn failed after final verifier/probe mismatch: {exc}")
                    if coverage_followups_sent < coverage_followup_limit and tmux_has_session(session):
                        coverage_followups_sent += 1
                        send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
                        log(f"coverage gate follow-up {coverage_followups_sent}: {'; '.join(blockers)}")
                        coverage_followup_at = time.monotonic()
                        last_capture = 0.0
                        time.sleep(5)
                        continue
                    coverage_gate_unresolved = True
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "blocked",
                                "reason": "final verifier accepted but adapter public validation probe failed",
                                "blockers": blockers,
                            }
                        ),
                        encoding="utf-8",
                    )
                    log("blocked marker: final verifier accepted but adapter public validation probe failed")
                    exit_code = 2
                    outcome = "blocked"
                    break
                if not state and blocked_without_status_marker(text):
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "blocked",
                                "reason": "orchestrator reported a terminal blocker without writing status.json",
                            }
                        ),
                        encoding="utf-8",
                    )
                    log("blocked marker recovered from orchestrator terminal blocker text")
                    exit_code = 2
                    outcome = "blocked"
                    break
                if (
                    not state
                    and diff_bytes > 0
                    and not has_live_agent_process()
                    and orchestrator_exited_without_status(text)
                ):
                    diff = git_diff(workdir)
                    scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    coverage_blockers = [] if coverage_probe_satisfied else validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                    blockers = [*scope_blockers, *coverage_blockers]
                    probe_report = ""
                    if coverage_probe_commands(workdir, issue, diff):
                        probe_report, probe_passed = run_validation_coverage_probe(
                            workdir,
                            issue,
                            diff,
                            blockers or ["orchestrator exited with a source diff but no status marker; adapter ran public validation before recovery"],
                        )
                        if probe_passed:
                            coverage_probe_satisfied = True
                            blockers = blockers_after_passing_public_probe(scope_blockers)
                        else:
                            blockers = [
                                *scope_blockers,
                                f"orchestrator exited without status and adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                            ]
                    if blockers and adapter_helper_workers_spawned < adapter_helper_worker_limit and adapter_helper_repair_allowed("orchestrator exited with unverified diff"):
                        adapter_helper_workers_spawned += 1
                        try:
                            helper_worker = spawn_adapter_helper_worker(
                                repo_root,
                                workdir,
                                env,
                                issue,
                                diff,
                                [
                                    *blockers,
                                    "The orchestrator exited after producing a source diff but without a completion status; continue from the current /app diff and resolve these adapter blockers.",
                                ],
                                helper_scope_hints(workdir, issue, diff, blockers),
                                adapter_helper_workers_spawned,
                                probe_report,
                            )
                            log(f"adapter recovery worker spawned after unverified orchestrator-exit diff: {helper_worker}")
                            adapter_helper_last_spawn_at = time.monotonic()
                            adapter_helper_reprobe_done = False
                            adapter_helper_last_probe_digest = None
                            coverage_followup_at = time.monotonic()
                            last_capture = 0.0
                            time.sleep(5)
                            continue
                        except Exception as exc:
                            log(f"adapter recovery worker spawn failed after unverified orchestrator-exit diff: {exc}")
                    if blockers:
                        coverage_gate_unresolved = True
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "orchestrator exited with unverified source diff",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        log("blocked marker: orchestrator exited with unverified source diff")
                        exit_code = 2
                        outcome = "blocked"
                        break
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "orchestrator exited with a source diff; adapter recovered missing status marker",
                                "validation": recovered_validation_text(
                                    task_metadata,
                                    text,
                                    (
                                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                                        if coverage_probe_satisfied
                                        else "no adapter-selected public validation command was available; implementation blockers were clean"
                                    ),
                                ),
                                "risk": "completion marker recovered by benchmark wrapper after orchestrator exit without status.json",
                            }
                        ),
                        encoding="utf-8",
                    )
                    log("completion marker recovered from orchestrator-exit source diff")
                    outcome = "recovered"
                    break
                if not state and coverage_followup_at and (
                    orchestrator_exited_without_status(text)
                    or (diff_bytes > 0 and not has_live_agent_process())
                ):
                    diff = git_diff(workdir)
                    scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    coverage_blockers = [] if coverage_probe_satisfied else validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                    blockers = [*scope_blockers, *coverage_blockers]
                    if coverage_probe_satisfied:
                        blockers = blockers_after_passing_public_probe(blockers)
                        scope_blockers = blockers
                        coverage_blockers = []
                    if blockers:
                        probe_report = ""
                        probe_passed = False
                        if coverage_probe_commands(workdir, issue, diff):
                            probe_report, probe_passed = run_validation_coverage_probe(
                                workdir,
                                issue,
                                diff,
                                blockers,
                            )
                        if probe_passed:
                            coverage_probe_satisfied = True
                            latest_diff = git_diff(workdir)
                            scope_blockers = implementation_scope_blockers(issue, latest_diff, {}, task_metadata)
                            blockers = blockers_after_passing_public_probe(scope_blockers)
                            if not blockers and latest_diff.strip():
                                STATUS_PATH.write_text(
                                    json.dumps(
                                        {
                                            "status": "completed",
                                            "summary": "orchestrator exited after adapter public validation; preserving current source diff",
                                            "validation": recovered_validation_text(
                                                task_metadata,
                                                text,
                                                f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                            ),
                                            "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                                        }
                                    ),
                                    encoding="utf-8",
                                )
                                log("completion marker recovered after adapter public probe passed following orchestrator exit")
                                outcome = "recovered"
                                break
                            log(
                                "adapter public probe passed after orchestrator exit, but implementation blockers remain: "
                                + "; ".join(blockers)
                            )
                        if (
                            tmux_has_session(session)
                            and adapter_helper_workers_spawned < adapter_helper_worker_limit
                            and adapter_helper_repair_allowed("orchestrator exit coverage blockers")
                        ):
                            adapter_helper_workers_spawned += 1
                            try:
                                helper_worker = spawn_adapter_helper_worker(
                                    repo_root,
                                    workdir,
                                    env,
                                    issue,
                                    diff,
                                    [
                                        *blockers,
                                        "The orchestrator/verifier exited without resolving these blockers; continue from the current /app diff and make the adapter-selected public validation probe pass before any completion marker.",
                                    ],
                                    helper_scope_hints(workdir, issue, diff, blockers),
                                    adapter_helper_workers_spawned,
                                    probe_report,
                                )
                                log(f"adapter recovery worker spawned after orchestrator exit: {helper_worker}")
                                adapter_helper_last_spawn_at = time.monotonic()
                                adapter_helper_reprobe_done = False
                                adapter_helper_last_probe_digest = None
                                coverage_followup_at = time.monotonic()
                                last_capture = 0.0
                                time.sleep(5)
                                continue
                            except Exception as exc:
                                log(f"adapter recovery worker spawn failed after orchestrator exit: {exc}")
                        if (
                            adapter_helper_last_spawn_at is not None
                            and time.monotonic() - adapter_helper_last_spawn_at >= 30
                            and coverage_probe_commands(workdir, issue, diff)
                        ):
                            probe_digest = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest()
                            if adapter_helper_reprobe_done and adapter_helper_last_probe_digest == probe_digest:
                                pass
                            else:
                                adapter_helper_reprobe_done = True
                                adapter_helper_last_probe_digest = probe_digest
                                probe_report, probe_passed = run_validation_coverage_probe(
                                    workdir,
                                    issue,
                                    diff,
                                    blockers,
                                )
                                if probe_passed:
                                    coverage_probe_satisfied = True
                                    latest_diff = git_diff(workdir)
                                    latest_blockers = implementation_scope_blockers(issue, latest_diff, {}, task_metadata)
                                    latest_blockers = blockers_after_passing_public_probe(latest_blockers)
                                    if not latest_blockers and latest_diff.strip():
                                        STATUS_PATH.write_text(
                                            json.dumps(
                                                {
                                                    "status": "completed",
                                                    "summary": "adapter recovery worker fixed public contract; preserving current source diff",
                                                    "validation": recovered_validation_text(
                                                        task_metadata,
                                                        text,
                                                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                                    ),
                                                    "risk": "completion marker recovered by benchmark wrapper after adapter helper fix",
                                                }
                                            ),
                                            encoding="utf-8",
                                        )
                                        log("completion marker recovered after adapter helper re-probe passed")
                                        outcome = "recovered"
                                        break
                                    blockers = latest_blockers or blockers_after_passing_public_probe(blockers)
                                    log(
                                        "adapter helper re-probe passed but remaining implementation blockers persist: "
                                        + "; ".join(blockers)
                                    )
                                else:
                                    log(f"adapter helper re-probe still failed; see {HELPER_PROBE_PATH}")
                        if (
                            adapter_helper_last_spawn_at is not None
                            and time.monotonic() - adapter_helper_last_spawn_at < adapter_helper_grace_seconds
                        ):
                            elapsed = int(time.monotonic() - adapter_helper_last_spawn_at)
                            log(
                                "waiting for recently spawned adapter recovery worker before terminal blocker "
                                f"elapsed={elapsed}s grace={adapter_helper_grace_seconds}s"
                            )
                            last_capture = 0.0
                            time.sleep(10)
                            continue
                        coverage_gate_unresolved = True
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "orchestrator exited after coverage follow-up without writing valid completion status",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        log("blocked marker: orchestrator exited after unresolved coverage follow-up")
                        exit_code = 2
                        outcome = "blocked"
                        break
                    if diff.strip():
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "completed",
                                    "summary": "orchestrator exited after adapter helper validation; preserving current source diff",
                                    "validation": recovered_validation_text(
                                        task_metadata,
                                        text,
                                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                    ),
                                    "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                                }
                            ),
                            encoding="utf-8",
                        )
                        log("completion marker recovered after adapter helper probe and orchestrator exit")
                        outcome = "recovered"
                        break
                if not tmux_has_session(session) and diff_bytes == 0 and not state:
                    missing_session_captures += 1
                    if missing_session_captures >= 3:
                        STATUS_PATH.write_text(
                            json.dumps({"status": "blocked", "reason": "tmux session disappeared before producing status or diff"}),
                            encoding="utf-8",
                        )
                        log("blocked marker: tmux session disappeared before producing status or diff")
                        exit_code = 2
                        outcome = "blocked"
                        break
                else:
                    missing_session_captures = 0
                if coverage_followup_at and time.monotonic() - coverage_followup_at > coverage_followup_timeout:
                    diff = git_diff(workdir)
                    blockers = validation_coverage_blockers(issue, diff, text, current_status, task_metadata)
                    if blockers:
                        coverage_gate_unresolved = True
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "validation coverage gate remained unresolved after helper probe follow-up",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        log(f"blocked marker: coverage gate unresolved after {coverage_followup_timeout}s")
                        exit_code = 2
                        outcome = "blocked"
                        break
                    coverage_followup_at = None
                last_capture = time.monotonic()
            time.sleep(5)
        else:
            log(f"timed out after {timeout}s; scoring current /app git diff")
            exit_code = 124
            outcome = "timeout"
    finally:
        capture_session(session)
        run(["tmux", "kill-session", "-t", session], timeout=30)

    materialize_committed_changes(workdir, start_head)
    restored = cleanup_patch(workdir, start_head)
    if restored:
        log(f"restored benchmark-disallowed changes: {restored}")
    final_diff = git_diff(workdir)
    if exit_code != 0 and final_diff.strip() and not coverage_gate_unresolved:
        final_status = status()
        final_state = str(final_status.get("status", "")).lower()
        final_text = captured_text()
        if final_state != "blocked" and visible_validation_passed_in_text(final_text):
            final_blockers = [
                *implementation_scope_blockers(issue, final_diff, final_status, task_metadata),
                *validation_coverage_blockers(issue, final_diff, final_text, final_status, task_metadata),
            ]
            final_blockers = blockers_after_passing_public_probe(final_blockers)
            if not final_blockers:
                STATUS_PATH.write_text(
                    json.dumps(
                        {
                            "status": "completed",
                            "summary": "source diff and visible validation recovered after missing completion marker",
                            "validation": "captured worker output contains passing visible validation; status marker recovered by benchmark wrapper",
                            "risk": "completion marker was recovered by the benchmark wrapper after worker/orchestrator exit",
                        }
                    ),
                    encoding="utf-8",
                )
                log("completion marker recovered at final cleanup from source diff plus passing visible validation")
                exit_code = 0
                outcome = "recovered"
            else:
                log("final cleanup recovery refused; blockers remain: " + "; ".join(final_blockers))
    if coverage_gate_unresolved:
        log("coverage gate remained unresolved; preserving current source diff for official verifier diagnostics")
    elif outcome == "blocked" and not final_diff.strip():
        clear_blocked_changes(workdir, start_head, "blocked run produced no scoreable source diff")
        final_diff = git_diff(workdir)
    elif outcome == "blocked":
        log("blocked run produced a scoreable source diff; preserving it for the official verifier")
    log(f"final /app diff bytes={len(final_diff.encode('utf-8'))}")
    if exit_code != 0:
        emit_failure_diagnostics(session)
    return exit_code


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--workdir", default=os.environ.get("EVAL_TASK_WORKDIR", str(DEFAULT_WORKDIR)))
    parser.add_argument("--multiagent-root", default=os.environ.get("MULTIAGENT_REPO_ROOT", str(DEFAULT_MULTIAGENT_ROOT)))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("EVAL_PROD_MULTIAGENT_TIMEOUT", "3300")))
    args = parser.parse_args(argv[1:])
    return run_prod_solver(args.prompt, Path(args.workdir), Path(args.multiagent_root), args.timeout)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
