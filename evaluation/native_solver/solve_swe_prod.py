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
        failed_validation_return_code,
        helper_preservation_evidence,
        helper_scope_hints,
        implementation_scope_blockers,
        required_public_symbols,
        source_symbol_changes,
    )
except ImportError:  # pragma: no cover - direct script execution in task containers
    from swe_prod_guardrails import (
        changed_go_package_args,
        coverage_probe_commands,
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
HELPER_PROBE_PATH = RUNTIME_ROOT / "helper-validation-probe.txt"
MULTI_VALUE_PROBE_PATH = RUNTIME_ROOT / "multi-value-probe.txt"
STALE_VISIBLE_RECONCILIATION_PATH = RUNTIME_ROOT / "stale-visible-reconciliation.txt"
CONTRACT_LEDGER_PATH = RUNTIME_ROOT / "contract-ledger.md"
SOURCE_OWNER_CANDIDATES_PATH = RUNTIME_ROOT / "source-owner-candidates.md"
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
        "This file is generated by the benchmark adapter from public solver inputs.",
        "Treat task/source evidence here as a durable invariant.",
        "Follow-up workers and verifiers must preserve all items, even when fixing a later verifier finding.",
        "Do not use leaked evaluator tests, hidden row names, non-public evaluator rows, or benchmark-only metadata as implementation guidance.",
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
            excerpt += "\n... truncated public task context."
        sections.extend(
            [
                "- Public task requirements/interface excerpt:",
                "",
                "```text",
                excerpt,
                "```",
            ]
        )
    if not symbols:
        sections.append("- No explicit public-symbol invariants were detected from public task text.")
    sections.extend(
        [
            "",
            "Completion rules:",
            "- Do not remove, rename, or omit a required public symbol while fixing another issue.",
            "- Preserve names, arity, parameter order, return shape, and package placement for any symbol referenced by visible tests, source callers, docs, public APIs, schemas, or runtime boundaries, including package-private helpers.",
            "- For any new or changed call through a receiver, field, interface, protocol, trait, generated client/model, or adapter, prove the method exists on the declared type at that call site, not merely on a nearby concrete implementation.",
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


def structured_repair_gate_blockers() -> list[str]:
    """Return blockers if runtime structured repair state does not pass gate-check."""

    subagent = DEFAULT_MULTIAGENT_ROOT / "bin/subagent.sh"
    if not subagent.exists():
        return []

    blockers: list[str] = []
    seen_state_dirs: set[Path] = set()
    for state_dir in (RUNTIME_ROOT, RUNTIME_ROOT / "state"):
        if state_dir in seen_state_dirs:
            continue
        seen_state_dirs.add(state_dir)
        if not any((state_dir / name).exists() for name in ("findings", "todos")):
            continue
        env = os.environ.copy()
        env.update(
            {
                "MULTIAGENT_ROOT": str(DEFAULT_WORKDIR),
                "MULTIAGENT_STATE_DIR": str(state_dir),
            }
        )
        result = run(
            [str(subagent), "gate-check"],
            cwd=DEFAULT_MULTIAGENT_ROOT,
            env=env,
            timeout=30,
        )
        output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
        if result.returncode != 0:
            blockers.append(
                "structured repair gate rejects completed status for "
                f"{state_dir}: {output[-2000:] or 'gate-check failed without output'}"
            )
    return blockers


def recover_verifier_accepted_todo_closures(text: str, diff: str) -> list[str]:
    """Close resolved todos when a verifier transcript explicitly rechecked them.

    This is a terminal-state recovery, not an acceptance shortcut: it translates
    `todo-recheck-passed: TODO_ID` verifier evidence into the same `todo-close`
    primitive the orchestrator should have called, then the regular gate-check
    still decides whether the run can be accepted.
    """

    subagent = DEFAULT_MULTIAGENT_ROOT / "bin/subagent.sh"
    if not text or not subagent.exists():
        return []
    lower = text.lower()
    if "accepted" not in lower or "todo-recheck-passed:" not in lower:
        return []

    recovered: list[str] = []
    seen_state_dirs: set[Path] = set()
    for state_dir in (RUNTIME_ROOT, RUNTIME_ROOT / "state"):
        if state_dir in seen_state_dirs:
            continue
        seen_state_dirs.add(state_dir)
        todos_base = state_dir / "todos"
        if not todos_base.exists():
            continue
        for todo_dir in sorted(path for path in todos_base.iterdir() if path.is_dir()):
            todo_id = todo_dir.name
            marker = f"todo-recheck-passed: {todo_id}".lower()
            if marker not in lower:
                continue
            status_path = todo_dir / "status"
            status = status_path.read_text(encoding="utf-8", errors="replace").strip().lower() if status_path.exists() else ""
            if status != "resolved":
                continue
            try:
                todo_payload = json.loads((todo_dir / "todo.json").read_text(encoding="utf-8"))
                resolution = json.loads((todo_dir / "resolution.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log(f"verifier todo closure recovery skipped {todo_id}: invalid structured state: {exc}")
                continue
            validation = resolution.get("validation")
            if not isinstance(validation, list) or not validation:
                log(f"verifier todo closure recovery skipped {todo_id}: missing worker validation")
                continue
            commands: list[dict[str, object]] = []
            for item in validation:
                if not isinstance(item, dict):
                    commands = []
                    break
                cmd = str(item.get("cmd", "")).strip()
                try:
                    rc = int(item.get("rc", item.get("returncode", 1)))
                except (TypeError, ValueError):
                    rc = 1
                if not cmd or rc != 0:
                    commands = []
                    break
                commands.append({"cmd": cmd, "rc": rc})
            if not commands:
                log(f"verifier todo closure recovery skipped {todo_id}: worker validation is not all rc=0")
                continue
            source_finding_id = str(todo_payload.get("source_finding_id", "")).strip()
            source_finding_hash = str(todo_payload.get("source_finding_hash", "")).strip()
            if not source_finding_id:
                log(f"verifier todo closure recovery skipped {todo_id}: missing source finding id")
                continue
            recheck = {
                "accepted": True,
                "finding_rechecked": source_finding_id,
                "source_finding_id": source_finding_id,
                "source_finding_hash": source_finding_hash,
                "commands": commands,
                "evidence": f"recovered from verifier transcript marker {marker}",
                "final_diff_hash": final_diff_sha256(diff),
            }
            env = os.environ.copy()
            env.update(
                {
                    "MULTIAGENT_ROOT": str(DEFAULT_WORKDIR),
                    "MULTIAGENT_STATE_DIR": str(state_dir),
                }
            )
            result = run(
                [
                    str(subagent),
                    "todo-close",
                    todo_id,
                    "--verified-by",
                    "verifier-transcript-recovery",
                    "--recheck-json",
                    json.dumps(recheck, sort_keys=True),
                ],
                cwd=DEFAULT_MULTIAGENT_ROOT,
                env=env,
                timeout=30,
            )
            output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
            if result.returncode == 0:
                recovered.append(f"{state_dir}:{todo_id}")
                log(f"verifier todo closure recovered {todo_id}: {output}")
            else:
                log(f"verifier todo closure recovery failed {todo_id}: {output[-1000:]}")
    return recovered


def completed_status_has_final_build_evidence(diff: str) -> bool:
    """Return true when status.json already proves the final diff passed build gate."""

    if not STATUS_PATH.exists():
        return False
    try:
        current_status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(current_status, dict):
        return False
    if str(current_status.get("status", "")).lower() not in {"completed", "complete", "done"}:
        return False
    if not build_verification_has_evidence(json.dumps(current_status, sort_keys=True), diff):
        return False
    return not structured_repair_gate_blockers()


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


def write_rg_fallback() -> None:
    if shutil.which("rg"):
        return
    rg_path = RUNTIME_ROOT / "rg"
    rg_path.write_text(
        r'''#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


IGNORED_DIRS = {".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build", "coverage", "__pycache__"}


def iter_files(paths: list[str]) -> list[Path]:
    roots = [Path(path) for path in (paths or ["."])]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.exists():
            continue
        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name not in IGNORED_DIRS]
            for name in names:
                path = Path(current) / name
                if path.is_file():
                    files.append(path)
    return files


def parse_args(argv: list[str]) -> tuple[dict[str, bool], str | None, list[str]]:
    flags = {"files": False, "ignore_case": False, "files_with_matches": False}
    pattern: str | None = None
    paths: list[str] = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        if arg == "--":
            if flags["files"]:
                paths.extend(argv[idx + 1 :])
            elif idx + 1 < len(argv) and pattern is None:
                pattern = argv[idx + 1]
                paths.extend(argv[idx + 2 :])
            else:
                paths.extend(argv[idx + 1 :])
            break
        if arg == "--files":
            flags["files"] = True
            idx += 1
            continue
        if arg in {"-i", "--ignore-case"}:
            flags["ignore_case"] = True
            idx += 1
            continue
        if arg in {"-l", "--files-with-matches"}:
            flags["files_with_matches"] = True
            idx += 1
            continue
        if arg in {"-n", "-S", "--no-heading", "--hidden", "--follow", "--color=never"}:
            idx += 1
            continue
        if arg in {"-g", "--glob", "--type", "-t", "--type-not", "-T"}:
            idx += 2
            continue
        if arg.startswith("-"):
            idx += 1
            continue
        if flags["files"]:
            paths.append(arg)
            idx += 1
            continue
        if pattern is None:
            pattern = arg
        else:
            paths.append(arg)
        idx += 1
    return flags, pattern, paths


def is_binary(path: Path) -> bool:
    try:
        return b"\0" in path.read_bytes()[:4096]
    except OSError:
        return True


def main() -> int:
    flags, pattern, paths = parse_args(sys.argv[1:])
    if flags["files"]:
        for path in iter_files(paths):
            print(path)
        return 0
    if pattern is None:
        print("rg fallback: missing pattern", file=sys.stderr)
        return 2
    try:
        regex = re.compile(pattern, re.IGNORECASE if flags["ignore_case"] else 0)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE if flags["ignore_case"] else 0)
    matched = False
    for path in iter_files(paths):
        if is_binary(path):
            continue
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        file_matched = False
        for line_no, line in enumerate(lines, 1):
            if not regex.search(line):
                continue
            matched = True
            file_matched = True
            if not flags["files_with_matches"]:
                print(f"{path}:{line_no}:{line}")
        if flags["files_with_matches"] and file_matched:
            print(path)
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
''',
        encoding="utf-8",
    )
    rg_path.chmod(0o755)
    log(f"installed rg fallback at {rg_path}")


def find_go_binary() -> str | None:
    for candidate in (
        Path("/usr/local/go/bin/go-real"),
        Path("/usr/local/go/bin/go"),
        Path("/usr/bin/go-real"),
        Path("/usr/bin/go"),
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("go")
    return found


def write_go_singleflight_wrapper(real_go: str | None = None) -> None:
    real_go = real_go or find_go_binary()
    if not real_go:
        return
    system_go_path: Path | None = None
    real_go_path = Path(real_go)
    if real_go_path.name == "go" and real_go_path.exists() and os.access(real_go_path.parent, os.W_OK):
        go_real_path = real_go_path.with_name("go-real")
        if not go_real_path.exists():
            real_go_path.rename(go_real_path)
        real_go = str(go_real_path)
        system_go_path = real_go_path
    elif real_go_path.name == "go-real" and os.access(real_go_path.parent, os.W_OK):
        system_go_path = real_go_path.with_name("go")

    go_path = RUNTIME_ROOT / "go"
    wrapper_text = f'''#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


REAL_GO = {real_go!r}
LOCK_ROOT = Path(os.environ.get("MULTIAGENT_GO_TEST_LOCK_ROOT", "/tmp/multiagent-prod-swe/go-test-locks"))
WAIT_TIMEOUT = int(os.environ.get("MULTIAGENT_GO_TEST_WAIT_TIMEOUT", "3600"))
RUN_TIMEOUT = int(os.environ.get("MULTIAGENT_GO_TEST_TIMEOUT_SECONDS", os.environ.get("MULTIAGENT_VALIDATION_TIMEOUT_SECONDS", "600")))


def repo_diff_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-color"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return "nogit"
    if result.returncode != 0:
        return "nogit"
    return hashlib.sha256(result.stdout.encode()).hexdigest()


def canonical_argv(argv: list[str]) -> list[str]:
    if not argv or argv[0] != "test":
        return argv
    packages: list[str] = []
    others: list[str] = []
    for item in argv[1:]:
        if item.startswith("./"):
            packages.append(item)
        else:
            others.append(item)
    if len(packages) <= 1:
        return argv
    return [argv[0], *others, *sorted(packages)]


def key_for(argv: list[str]) -> str:
    payload = {{
        "cwd": str(Path.cwd()),
        "argv": canonical_argv(argv),
    }}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def result_key_for(argv: list[str]) -> str:
    payload = {{
        "cwd": str(Path.cwd()),
        "argv": canonical_argv(argv),
        "diff": repo_diff_hash(),
    }}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def replay(lock_dir: Path) -> int:
    stdout = lock_dir / "stdout.log"
    stderr = lock_dir / "stderr.log"
    rc_file = lock_dir / "returncode"
    if stdout.exists():
        sys.stdout.write(stdout.read_text(errors="replace"))
    if stderr.exists():
        sys.stderr.write(stderr.read_text(errors="replace"))
    try:
        return int(rc_file.read_text().strip())
    except Exception:
        return 1


def kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        proc.wait()


def run_owner(lock_dir: Path, argv: list[str]) -> int:
    (lock_dir / "pid").write_text(f"{{os.getpid()}}\\n")
    (lock_dir / "command.json").write_text(json.dumps(argv, indent=2) + "\\n")
    (lock_dir / "status").write_text("running\\n")
    started = time.time()
    start_diff_hash = repo_diff_hash()
    (lock_dir / "start_diff_hash").write_text(f"{{start_diff_hash}}\\n")
    with (lock_dir / "stdout.log").open("w") as stdout, (lock_dir / "stderr.log").open("w") as stderr:
        proc = subprocess.Popen(
            [REAL_GO, *argv],
            text=True,
            stdout=stdout,
            stderr=stderr,
            preexec_fn=child_preexec,
        )
        (lock_dir / "child_pid").write_text(f"{{proc.pid}}\\n")

        def forward_signal(signum, _frame):
            kill_process_group(proc)
            raise SystemExit(128 + signum)

        previous_handlers = {{}}
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, forward_signal)
        try:
            timed_out = False
            try:
                returncode = proc.wait(timeout=RUN_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_group(proc)
                returncode = 124
                stderr.write(f"\\ngo singleflight: go test timed out after {{RUN_TIMEOUT}} seconds\\n")
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
        finish_diff_hash = repo_diff_hash()
        stale_diff = finish_diff_hash != start_diff_hash
        if stale_diff:
            stderr.write(
                "\\ngo singleflight: validation diff changed while command was running; "
                f"start_diff_hash={{start_diff_hash}} finish_diff_hash={{finish_diff_hash}}\\n"
            )
            if returncode == 0:
                returncode = 125
    (lock_dir / "returncode").write_text(f"{{returncode}}\\n")
    (lock_dir / "finish_diff_hash").write_text(f"{{finish_diff_hash}}\\n")
    (lock_dir / "finished.json").write_text(json.dumps({{"started": started, "finished": time.time(), "returncode": returncode, "timeout_seconds": RUN_TIMEOUT, "timed_out": timed_out, "start_diff_hash": start_diff_hash, "finish_diff_hash": finish_diff_hash, "stale_diff": stale_diff}}, sort_keys=True) + "\\n")
    (lock_dir / "status").write_text(("timed-out" if timed_out else "stale-diff" if stale_diff else "done") + "\\n")
    return replay(lock_dir)


def child_preexec() -> None:
    if sys.platform.startswith("linux"):
        try:
            os.setsid()
        except Exception:
            pass
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            PR_SET_PDEATHSIG = 1
            libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
        except Exception:
            pass


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "test":
        os.execv(REAL_GO, [REAL_GO, *argv])
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    results_root = LOCK_ROOT / "results"
    results_root.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_ROOT / f"{{key_for(argv)}}.lock"
    with lock_path.open("a+") as lock_file:
        wait_started = time.time()
        announced_wait = False
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not announced_wait:
                    sys.stderr.write(f"go singleflight: waiting for duplicate validation {{lock_path.stem}}\\n")
                    announced_wait = True
                if time.time() - wait_started >= WAIT_TIMEOUT:
                    sys.stderr.write(f"go singleflight: duplicate validation wait timed out after {{WAIT_TIMEOUT}} seconds\\n")
                    return 124
                time.sleep(1)
        lock_dir = results_root / result_key_for(argv)
        status = lock_dir / "status"
        if status.exists() and status.read_text(errors="replace").strip() in {{"done", "timed-out", "stale-diff"}}:
            sys.stderr.write(f"go singleflight: replaying completed validation {{lock_dir.name}}\\n")
            return replay(lock_dir)
        lock_dir.mkdir(parents=True, exist_ok=True)
        return run_owner(lock_dir, argv)


if __name__ == "__main__":
    raise SystemExit(main())
'''
    go_path.write_text(wrapper_text, encoding="utf-8")
    go_path.chmod(0o755)
    if system_go_path is not None:
        system_go_path.write_text(wrapper_text, encoding="utf-8")
        system_go_path.chmod(0o755)
        log(f"installed go test singleflight wrapper at {system_go_path} -> {real_go}")
    log(f"installed go test singleflight wrapper at {go_path} -> {real_go}")


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


def source_owner_issue_terms(issue: str) -> list[str]:
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
        "description",
        "requirement",
        "requirements",
        "interface",
        "interfaces",
        "introduced",
        "golden",
        "patch",
        "file",
        "files",
        "path",
        "paths",
        "input",
        "inputs",
        "output",
        "outputs",
        "name",
        "type",
        "command",
        "commands",
        "status",
        "work",
        "task",
        "source",
        "code",
        "user",
        "users",
    }
    terms: set[str] = set()
    for token in re.findall(r"\b[a-z][a-z0-9_-]{3,}\b", issue.lower()):
        token = token.replace("_", "-")
        if token in stop or token.endswith("ing"):
            continue
        terms.add(token)
        if token.endswith("s") and len(token) > 4:
            terms.add(token[:-1])
        if "config" in token:
            terms.add("config")
    return sorted(terms)


def source_owner_issue_paths(issue: str) -> list[str]:
    candidates: set[str] = set()
    source_suffixes = (".go", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java", ".kt", ".rb", ".php")
    path_patterns = [
        r"\b(?:Path|New file|File):\s*`?([A-Za-z0-9_./-]+\.(?:go|pyi?|jsx?|tsx?|rs|java|kt|rb|php))`?",
        r"`([A-Za-z0-9_./-]+/[A-Za-z0-9_./-]+\.(?:go|pyi?|jsx?|tsx?|rs|java|kt|rb|php))`",
    ]
    for pattern in path_patterns:
        for match in re.findall(pattern, issue, flags=re.IGNORECASE):
            path = match.strip().strip("`.,:;")
            if not path.startswith("/") and ".." not in Path(path).parts and path.endswith(source_suffixes):
                candidates.add(path)
    return sorted(candidates)


def source_owner_term_variants(term: str) -> set[str]:
    variants = {term}
    if term.endswith("s") and len(term) > 4:
        variants.add(term[:-1])
    else:
        variants.add(term + "s")
    if term == "benchmark":
        variants.update({"bench", "benches"})
    return variants


def source_owner_path_matches(path_text: str, term: str) -> bool:
    parts = [part for part in re.split(r"[/_.-]+", path_text.lower()) if part]
    return any(part in source_owner_term_variants(term) for part in parts)


def source_owner_discovery(workdir: Path, issue: str) -> str:
    terms = source_owner_issue_terms(issue)
    issue_paths = source_owner_issue_paths(issue)
    lines = [
        "# Source Owner Candidates",
        "",
        "This file is generated from public issue text and repository source paths only.",
        "It is a pre-edit routing aid, not hidden-test guidance.",
        "",
    ]
    if not terms and not issue_paths:
        lines.append("No strong issue terms were extracted. Run read-only source owner discovery before adding new symbols.")
        SOURCE_OWNER_CANDIDATES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return "\n".join(lines)

    rows: list[tuple[int, str, str]] = []
    source_suffixes = {".go", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".rs", ".java", ".kt", ".rb", ".php"}
    ignored_parts = {".git", "vendor", "node_modules", "dist", "build", "target", "__pycache__"}

    for issue_path in issue_paths:
        rows.append((100, issue_path, "issue-explicit-source-path"))
        parent = str(Path(issue_path).parent).replace(".", "").strip("/")
        if parent:
            rows.append((95, parent, f"issue-explicit-source-path-parent={issue_path}"))

    for rel in _walk_source_dirs(workdir, max_dirs=700):
        rel_lower = rel.lower()
        reasons = [f"dir-term={term}" for term in terms if source_owner_path_matches(rel_lower, term)]
        if reasons:
            has_source = any(any((workdir / rel).glob(f"*{suffix}")) for suffix in source_suffixes)
            rows.append((30 + len(reasons), rel, ",".join(reasons) + (",source-files" if has_source else ",dir-only")))

    scanned = 0
    for path in sorted(workdir.rglob("*")):
        if scanned >= 1200:
            break
        if not path.is_file() or path.suffix not in source_suffixes:
            continue
        rel = path.relative_to(workdir).as_posix()
        if any(part in ignored_parts or part.startswith(".cache") for part in Path(rel).parts):
            continue
        scanned += 1
        rel_lower = rel.lower()
        reasons = [f"path-term={term}" for term in terms if source_owner_path_matches(rel_lower, term)]
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:6000].lower()
        except OSError:
            head = ""
        for term in terms:
            for variant in source_owner_term_variants(term):
                if re.search(rf"\bpackage\s+{re.escape(variant)}\b", head):
                    reasons.append(f"package-term={term}")
                    break
                if re.search(rf"\b(type|func|class|interface)\s+\w*{re.escape(variant)}\w*", head):
                    reasons.append(f"symbol-term={term}")
                    break
        if reasons:
            rows.append((10 + len(reasons), rel, ",".join(sorted(set(reasons)))))

    source_roots = [root for root in ("lib", "pkg", "internal", "src", "packages") if (workdir / root).is_dir()]
    for root in source_roots[:3]:
        for term in terms[:8]:
            if term in {"client", "server", "model", "metadata", "config"}:
                continue
            rows.append((5, f"{root}/{term}", f"prospective-owner-from-issue-term={term}"))

    dedup: dict[str, tuple[int, str]] = {}
    for score, path, reason in rows:
        old = dedup.get(path)
        if not old or score > old[0]:
            dedup[path] = (score, reason)
    ranked = sorted(((score, path, reason) for path, (score, reason) in dedup.items()), key=lambda item: (-item[0], item[1]))[:24]

    if issue_paths:
        lines.append("Explicit source paths from issue: " + ", ".join(issue_paths))
    lines.append("Extracted issue terms: " + ", ".join(terms))
    lines.append("")
    if ranked:
        lines.append("Candidate owners:")
        for score, path, reason in ranked:
            lines.append(f"- candidate-owner={path} score={score} reason={reason}")
    else:
        lines.append("No source owner candidates found from issue terms.")
    lines.extend(
        [
            "",
            "Pre-edit rule:",
            "- Before the first worker adds, removes, renames, or moves source symbols, write a `source-owner-ledger:` in the worker instruction.",
            "- The ledger must include `selected-owner=...`, every plausible `candidate-owner=...` considered, `rejected-owner=...` reasons, and `validation-package=...`.",
            "- If no listed owner is clearly correct, spawn a read-only contract scout instead of letting a worker choose by proximity to the first matching type.",
        ]
    )
    SOURCE_OWNER_CANDIDATES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return "\n".join(lines)


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

    sections.append("\n## Source Owner Pre-Edit Discovery\n")
    sections.append(
        f"The adapter wrote source owner candidates to `{SOURCE_OWNER_CANDIDATES_PATH}`. "
        "Before spawning any worker that may add, remove, rename, or move source symbols, paste a `source-owner-ledger:` "
        "into that worker's first instruction with `selected-owner=...`, all plausible `candidate-owner=...`, rejected-owner reasons, "
        "and `validation-package=...`. If ownership is not clear, spawn a read-only contract scout before implementation."
    )
    sections.append(source_owner_discovery(workdir, issue))
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


def subagent_state_roots(runtime_root: Path = RUNTIME_ROOT) -> list[Path]:
    roots: list[Path] = []
    for candidate in (runtime_root / "subagents", runtime_root / "state" / "subagents"):
        if candidate.exists() and candidate not in roots:
            roots.append(candidate)
    return roots


def blocked_no_diff_subagent_summaries(runtime_root: Path = RUNTIME_ROOT) -> list[str]:
    summaries: list[str] = []
    for subagents_dir in subagent_state_roots(runtime_root):
        for agent_dir in sorted(path for path in subagents_dir.iterdir() if path.is_dir()):
            status_file = agent_dir / "status"
            if not status_file.exists():
                continue
            status = status_file.read_text(encoding="utf-8", errors="replace").strip().lower()
            if status != "blocked":
                continue
            snippets: list[str] = []
            for name in ("last-message.txt", "current.txt", "transcript.log"):
                path = agent_dir / name
                if not path.exists():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    continue
                if text:
                    snippets.append(" ".join(text[-1200:].split()))
            tail = snippets[0] if snippets else "no captured blocked-worker text"
            summaries.append(f"{agent_dir.name}: {tail[:1200]}")
    return summaries


def required_path_outside_owned_reports(runtime_root: Path = RUNTIME_ROOT) -> list[str]:
    reports: list[str] = []
    pattern = re.compile(r"required-path-outside-owned:\s*([^\s`'\",;)]+)")
    for subagents_dir in subagent_state_roots(runtime_root):
        for agent_dir in sorted(path for path in subagents_dir.iterdir() if path.is_dir()):
            for name in ("last-message.txt", "current.txt", "transcript.log"):
                path = agent_dir / name
                if not path.exists():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for match in pattern.finditer(text):
                    report = match.group(1).strip()
                    if report:
                        reports.append(report)
    return list(dict.fromkeys(reports))


def emit_failure_diagnostics(session: str, *, limit: int = 24000) -> None:
    """Print compact runtime diagnostics before the sandbox is deleted."""
    sections: list[str] = ["failure diagnostics:"]
    if STATUS_PATH.exists():
        try:
            sections.append("status.json:\n" + STATUS_PATH.read_text(encoding="utf-8", errors="replace")[-4000:])
        except OSError as exc:
            sections.append(f"status.json: unreadable: {exc}")
    if SOURCE_OWNER_CANDIDATES_PATH.exists():
        try:
            sections.append("source-owner-candidates.md:\n" + SOURCE_OWNER_CANDIDATES_PATH.read_text(encoding="utf-8", errors="replace")[-6000:])
        except OSError as exc:
            sections.append(f"source-owner-candidates.md: unreadable: {exc}")

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

    for subagents_dir in subagent_state_roots(RUNTIME_ROOT):
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
    if validation_text_has_no_test_evidence(text_lower):
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


def validation_text_has_no_test_evidence(text: str) -> bool:
    text_lower = text.lower()
    return any(
        marker in text_lower
        for marker in (
            "no tests ran",
            "no tests to run",
            "0 tests",
            "0 passed",
            "[no test files]",
            "[no tests to run]",
            "warning: no tests to run",
            "-run testnonexistent",
            "-run '^$'",
        )
    )


def go_test_output_has_real_package_evidence(output: str) -> bool:
    """Return true when Go output shows at least one package ran real tests."""

    for line in output.splitlines():
        stripped = line.strip()
        if not re.match(r"^ok\s+\S+", stripped):
            continue
        lower = stripped.lower()
        if "[no tests to run]" in lower or "[no test files]" in lower:
            continue
        return True
    return False


def validation_probe_has_no_test_evidence(label: str, output: str) -> bool:
    """Classify adapter-selected probe output without rejecting mixed Go suites."""

    label_lower = label.lower()
    if "-run testnonexistent" in label_lower or "-run '^$'" in label_lower:
        return True
    if label_lower.startswith("go test") and go_test_output_has_real_package_evidence(output):
        return False
    return validation_text_has_no_test_evidence(f"{label}\n{output}")


def validation_section_offsets(text: str) -> list[int]:
    """Return likely validation-section starts from a worker report."""

    text_lower = text.lower()
    offsets: list[int] = []
    for marker in ("validation passed:", "**validation**", "## validation", "### validation"):
        start = 0
        while True:
            idx = text_lower.find(marker, start)
            if idx < 0:
                break
            offsets.append(idx)
            start = idx + len(marker)
    return sorted(set(offsets))


def validation_tail_has_required_command_and_pass(
    validation_tail: str,
    required_commands: tuple[str, ...],
    *,
    explicit_pass_marker: bool,
) -> bool:
    text = validation_tail.lower()
    if not any(command in text for command in required_commands):
        return False
    if validation_text_has_no_test_evidence(text):
        return False
    if any(
        bad in text
        for bad in (
            "validation failed",
            "tests failed",
            "go test failed",
            "pytest failed",
            "npm test failed",
            "yarn test failed",
            "traceback",
        )
    ):
        return False
    if "go test" in required_commands and "go test" not in text:
        return False
    if explicit_pass_marker:
        return True
    if re.search(r"(?m)^ok\s+\S+", validation_tail):
        return True
    if re.search(r"=+\s+[^=\n]*\bpassed\b[^=\n]*\s+=+", text):
        return True
    return bool(re.search(r"\b\d+\s+passed\b", text))


def persisted_subagent_visible_validation_evidence(
    diff: str,
    runtime_root: Path = RUNTIME_ROOT,
) -> str:
    """Return persisted worker validation evidence, if it matches the diff.

    Tmux captures can contain unrelated tool-call errors from another agent. The
    durable subagent last-message files are narrower: they contain the worker's
    final report. Use them only as a generic visible-validation recovery signal,
    never as benchmark expected-test guidance.
    """

    subagents_dir = runtime_root / "state" / "subagents"
    if not subagents_dir.exists():
        return ""

    touches_go_source = any(
        line.startswith("diff --git a/") and ".go " in line
        for line in diff.splitlines()
    )
    touches_python_source = any(
        line.startswith("diff --git a/") and any(ext in line for ext in (".py ", ".pyx ", ".pyi "))
        for line in diff.splitlines()
    )
    touches_js_source = any(
        line.startswith("diff --git a/") and any(ext in line for ext in (".js ", ".jsx ", ".ts ", ".tsx "))
        for line in diff.splitlines()
    )
    required_commands: tuple[str, ...]
    if touches_go_source:
        required_commands = ("go test",)
    elif touches_python_source:
        required_commands = ("pytest", "python -m pytest")
    elif touches_js_source:
        required_commands = ("npm test", "yarn test", "pnpm test", "jest", "vitest")
    else:
        required_commands = ("go test", "pytest", "python -m pytest", "npm test", "yarn test", "pnpm test")

    for agent_dir in sorted(path for path in subagents_dir.iterdir() if path.is_dir()):
        for name in ("last-message.txt", "current.txt"):
            path = agent_dir / name
            if not path.exists():
                continue
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = raw.lower()
            markers = validation_section_offsets(raw)
            if not markers:
                continue
            for marker in reversed(markers):
                validation_tail = raw[marker:]
                explicit_pass_marker = text[marker:].startswith("validation passed:")
                if not validation_tail_has_required_command_and_pass(
                    validation_tail,
                    required_commands,
                    explicit_pass_marker=explicit_pass_marker,
                ):
                    continue
                excerpt = raw[marker: marker + 800].strip()
                return f"persisted subagent {agent_dir.name} {name}: {excerpt}"
    return ""


def persisted_stale_visible_reconciliation_evidence(
    runtime_root: Path = RUNTIME_ROOT,
) -> str:
    """Return machine-checkable stale-visible reconciliation evidence.

    This is a no-leak recovery signal for cases where production agents decide
    a visible fixture/test expectation is stale relative to source-visible task
    evidence, but the orchestrator exits without writing ``status.json``. The
    wrapper does not infer benchmark answers here; it only requires the
    production run to have written explicit replacement/stale markers to a
    durable artifact.
    """

    path = runtime_root / STALE_VISIBLE_RECONCILIATION_PATH.name
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = raw.lower()
    if "replacement-probe-passed:" not in text or "stale-visible-failure-justified:" not in text:
        return ""
    if re.search(r"replacement-probe-passed:\s*(?:not relevant|n/a|none)\b", text):
        return ""
    if re.search(r"stale-visible-failure-justified:\s*(?:not relevant|n/a|none)\b", text):
        return ""
    if "multi-value-probe-passed:" in text and not multi_value_probe_has_final_output_counts(text):
        return ""
    excerpt = raw[-1600:].strip()
    return f"stale-visible-reconciliation-passed: {path}: {excerpt}"


def status_with_recovered_validation(
    current_status: dict[str, object],
    validation_evidence: str,
) -> dict[str, object]:
    recovered = dict(current_status)
    existing = str(recovered.get("validation", ""))
    recovered["validation"] = (
        existing + "; " if existing else ""
    ) + "captured-worker-visible-validation-passed: " + validation_evidence
    return recovered


def recovered_validation_with_helper_evidence(issue: str, text: str, validation_evidence: str) -> str:
    helper_evidence = helper_preservation_evidence(issue, text)
    if helper_evidence:
        return validation_evidence + "; " + helper_evidence
    return validation_evidence


def status_with_recovered_public_evidence(
    current_status: dict[str, object],
    validation_evidence: str,
    issue: str,
    text: str,
) -> dict[str, object]:
    return status_with_recovered_validation(
        current_status,
        recovered_validation_with_helper_evidence(issue, text, validation_evidence),
    )


def evidence_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_./:*(),+-]+", "-", value.strip())
    return token.strip("-") or "unknown"


def go_package_name_for_path(workdir: Path, path: str) -> str:
    full_path = workdir / path
    try:
        text = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    match = re.search(r"(?m)^\s*package\s+([A-Za-z_][A-Za-z0-9_]*)\b", text)
    if match:
        return match.group(1)
    parent = Path(path).parent.name
    return parent.replace("-", "_") or "unknown"


def source_symbol_adapter_evidence(workdir: Path, diff: str) -> str:
    """Return final-diff source-symbol evidence after public validation passes.

    This uses only the current diff and repository source. It deliberately does
    not account for alternate issue-term owners, so the existing owner-candidate
    guard can still reject wrong-package symbol placements.
    """

    changes = source_symbol_changes(diff)
    if not changes:
        return ""

    by_path: dict[str, list[tuple[str, str]]] = {}
    for change in changes:
        if not change or change[0] not in {"+", "-"} or ":" not in change:
            continue
        path, symbol = change[1:].rsplit(":", 1)
        if path and symbol:
            by_path.setdefault(path, []).append((change[0], symbol))
    if not by_path:
        return ""

    owner_dirs = sorted({str(Path(path).parent).replace(".", "").strip("/") or "." for path in by_path})
    validation_packages = changed_go_package_args(diff) or [f"./{owner_dirs[0]}" if owner_dirs else "./..."]
    selected_owner = owner_dirs[0] if owner_dirs else "."
    ledger_parts = [
        "source-owner-ledger:",
        f"selected-owner={evidence_token(selected_owner)}",
        *(f"candidate-owner={evidence_token(owner)}" for owner in owner_dirs),
        "rejected-owner=not-in-final-diff-without-stronger-public-source-evidence",
        f"validation-package={evidence_token(validation_packages[0])}",
    ]

    map_parts = [
        "source-symbol-map-passed:",
        "owner-evidence=adapter-final-diff-package-declaration",
        "compile=adapter-public-probe-passed",
        "caller=changed-source-paths",
        f"candidate-owner={evidence_token(selected_owner)}",
    ]
    for path in sorted(by_path):
        map_parts.append(f"path={evidence_token(path)}")
        map_parts.append(f"package={evidence_token(go_package_name_for_path(workdir, path))}")
        for sign, symbol in sorted(by_path[path]):
            key = "added-symbol" if sign == "+" else "removed-symbol"
            map_parts.append(f"{key}={evidence_token(symbol)}")
    return " ".join(ledger_parts) + "; " + " ".join(map_parts)


def append_adapter_probe_evidence(
    current_status: dict[str, object],
    *,
    workdir: Path,
    diff: str,
    marker: str | None = None,
) -> dict[str, object]:
    updated = dict(current_status)
    validation_parts = [str(updated.get("validation", "")).strip()]
    if marker:
        validation_parts.append(marker)
    source_evidence = source_symbol_adapter_evidence(workdir, diff)
    if source_evidence:
        validation_parts.append(source_evidence)
    updated["validation"] = "; ".join(part for part in validation_parts if part)
    return updated


SOURCE_CLAIM_EXTENSIONS = (
    ".go",
    ".py",
    ".pyi",
    ".pyx",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".rs",
    ".java",
    ".kt",
    ".scala",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".m",
    ".mm",
)


def changed_paths_from_diff(diff: str) -> set[str]:
    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("diff --git a/") or " b/" not in line:
            continue
        before_b, after_b = line.split(" b/", 1)
        old_path = before_b.removeprefix("diff --git a/")
        new_path = after_b.split("\t", 1)[0].strip()
        for path in (old_path, new_path):
            if path and path != "/dev/null":
                paths.add(path)
    return paths


def final_diff_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def is_test_path(path: str) -> bool:
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
        or name.endswith(".test.js")
        or name.endswith(".spec.js")
        or "__tests__" in parts
    )


def changed_code_paths_from_diff(diff: str) -> list[str]:
    return sorted(
        path
        for path in changed_paths_from_diff(diff)
        if Path(path).suffix in SOURCE_CLAIM_EXTENSIONS
        and not is_test_path(path)
        and not path.startswith((".cache/", ".gomodcache/", "node_modules/", "vendor/"))
    )


def build_verification_has_evidence(text: str, diff: str) -> bool:
    lower = text.lower().replace("\\n", "\n")
    diff_hash = final_diff_sha256(diff).lower()
    if "build-verification-passed:" not in lower:
        return False
    for match in re.finditer("build-verification-passed:", lower):
        window = lower[match.start() : match.start() + 800]
        if f"final-diff-sha256={diff_hash}" not in window and f'"final_diff_hash": "{diff_hash}"' not in window:
            continue
        if not any(marker in window for marker in ("compile_clean=true", '"compile_clean": true')):
            continue
        if not any(marker in window for marker in ("returncode=0", "rc=0", '"rc": 0', '"returncode": 0')):
            continue
        return True
    return False


def claimed_changed_source_paths(text: str) -> set[str]:
    claimed: set[str] = set()
    in_changed_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if not line:
            in_changed_section = False
            continue
        if re.match(r"^[#*_ -]*(changed|modified|updated)\s+(source\s+)?files\s*:", lower):
            in_changed_section = True
        elif re.match(r"^[#*_ -]*(changes|source changes)\s*:", lower):
            in_changed_section = True
        elif not line.startswith(("-", "*")) and not lower.startswith(("changed", "modified", "updated", "added")):
            in_changed_section = False

        if any(
            marker in lower
            for marker in (
                "inspected ",
                "reviewed ",
                "evidence:",
                "before the repair",
                "already correct",
                "already unchanged",
                "unchanged",
                "no change",
            )
        ):
            continue
        for match in re.finditer(r"`([^`\s]+)`", line):
            path = match.group(1)
            clean = path.strip().strip(".,:;")
            if clean.endswith(SOURCE_CLAIM_EXTENSIONS):
                context = lower[max(0, match.start() - 80) : match.end() + 80]
                has_nearby_change_verb = any(
                    re.search(pattern, context)
                    for pattern in (
                        r"\bchanged\b",
                        r"\bmodified\b",
                        r"\bupdated\b",
                        r"\badded\b",
                        r"\bremoved\b",
                        r"\bimplemented\b",
                        r"\bfixed\b",
                    )
                )
                if in_changed_section or has_nearby_change_verb:
                    claimed.add(clean.removeprefix("./"))
    return claimed


def claimed_changed_path_blockers(diff: str, text: str) -> list[str]:
    changed = changed_paths_from_diff(diff)
    if not changed:
        return []
    claimed = claimed_changed_source_paths(text)
    missing = sorted(path for path in claimed if path not in changed)
    if not missing:
        return []
    return [
        "agent claimed changed source paths are absent from final git diff; "
        f"make the missing edits or remove the stale claim before acceptance: {', '.join(missing[:8])}"
    ]


def stale_patch_application_blockers(text: str) -> list[str]:
    lower = (text or "").lower()
    stale_patch_markers = (
        "apply_patch: could not find hunk context",
        "apply_patch: expected hunk header",
        "patch failed",
        "hunk failed",
        "could not apply patch",
        "failed to apply patch",
    )
    if not any(marker in lower for marker in stale_patch_markers):
        return []
    return [
        "worker attempted a stale patch that did not apply cleanly; re-read the current target files, rebase the edit onto the live tree, rerun affected validation, and do not claim completion from an unapplied patch plan"
    ]


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
    blockers.extend(claimed_changed_path_blockers(diff, f"{text}\n{json.dumps(current_status, sort_keys=True)}"))
    blockers.extend(stale_patch_application_blockers(text))
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
        if go_compile_failure_present(go_evidence_text) and not go_failure_is_unaffected_unbuildable_root_target(
            go_evidence_text,
            go_packages,
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
        if missing_go_packages:
            blockers.append(
                "Go source changed, but final validation does not prove affected package compile/test success for: "
                + ", ".join(missing_go_packages)
                + "; run `go test ./affected/package` for every changed Go package after the final diff and record "
                "`go-package-validation-passed: package=... command=... returncode=0` for every changed package"
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

    return [
        *implementation_scope_blockers(issue, diff, completed_status, metadata),
        *validation_coverage_blockers(issue, diff, text, completed_status, metadata),
    ]


def go_compile_failure_present(text: str) -> bool:
    lower = text.lower()
    if failed_validation_return_code(lower):
        return True
    return any(
        marker in lower
        for marker in (
            "undefined:",
            "undefined method",
            "undefined field",
            "has no field or method",
            "build failed",
            "setup failed",
            "\\tfail\\t",
            "\tfail\t",
            " fail\t",
            " fail ",
            "fail:",
        )
    )


def go_failure_is_unaffected_unbuildable_root_target(text: str, go_packages: list[str]) -> bool:
    """Return true for mixed Go commands where only unrelated repo-root fails.

    Some Go repos intentionally have no buildable package at repository root.
    A verifier command such as ``go test ./changed/pkg .`` can therefore fail
    even when every changed package compiles. That failure should cause the
    verifier to rerun a focused command, not overwrite focused per-package
    success evidence for the final diff.
    """

    if not go_packages or "." in go_packages:
        return False
    lower = text.lower().replace("\\n", "\n")
    if not all(go_package_validation_has_evidence(lower, package) for package in go_packages):
        return False
    if not any(
        marker in lower
        for marker in (
            "build constraints exclude all go files",
            "no go files in",
            "no go files",
        )
    ):
        return False
    for line in lower.splitlines():
        if "go test" not in line:
            continue
        if re.search(r"(^|\s)\.(\s|;|$)", line):
            return True
    return False


def go_package_validation_has_evidence(text: str, package: str) -> bool:
    lower = text.lower().replace("\\n", "\n")
    package_lower = package.lower()
    package_markers = {package_lower}
    if package_lower.startswith("./"):
        package_markers.add(package_lower[2:])
    if package_lower == ".":
        package_markers.add("./...")

    if "go-package-validation-passed:" in lower:
        for match in re.finditer("go-package-validation-passed:", lower):
            window = lower[match.start() : match.start() + 500]
            if any(f"package={marker}" in window for marker in package_markers) and any(
                ok in window for ok in ("returncode=0", "return-code=0", "rc=0", "passed")
            ):
                return True

    for marker in package_markers:
        for match in re.finditer(re.escape(marker), lower):
            start = max(0, match.start() - 250)
            end = min(len(lower), match.end() + 500)
            window = lower[start:end]
            if "go test" not in window:
                continue
            if validation_text_has_no_test_evidence(window) and "go-validation-skip-justified:" not in window:
                continue
            if any(ok in window for ok in ("return code: 0", "returncode=0", "exit code: 0", "rc=0", " passed", ": passed")):
                return True
            if re.search(r"\bok\b[^\n]*" + re.escape(marker), window) or re.search(
                re.escape(marker) + r"[^\n]*\bok\b", window
            ):
                return True
    return False






def multi_value_probe_has_final_output_counts(status_text: str) -> bool:
    """Return whether a multi-value probe proves final output cardinality."""

    status_evidence = multi_value_probe_evidence(status_text)
    if not multi_value_probe_counts_match(status_evidence):
        return False
    try:
        artifact_text = MULTI_VALUE_PROBE_PATH.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return multi_value_probe_counts_match(artifact_text)


def multi_value_probe_evidence(text: str) -> str:
    marker_index = text.find("multi-value-probe-passed:")
    if marker_index < 0:
        return ""
    return text[marker_index : marker_index + 1200]


def multi_value_probe_counts_match(evidence: str) -> bool:
    field_match = re.search(r"\bfinal-output-field\s*=\s*([^\s;]+)", evidence)
    if not field_match:
        return False
    field_name = field_match.group(1).rstrip(".,")
    if re.search(r"[+,/&]|\band\b", field_name):
        return False
    if not re.search(r"\bsource-count\s*=\s*\d+", evidence):
        return False
    expected = re.search(r"\bexpected-output-count\s*=\s*(\d+)", evidence)
    actual = re.search(r"\bactual-output-count\s*=\s*(\d+)", evidence)
    return bool(expected and actual and expected.group(1) == actual.group(1))


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
    if completed_status_has_final_build_evidence(diff):
        report = (
            "Adapter-selected public helper validation probe skipped because "
            "status.json already records completed final-diff build verification "
            "and the structured repair gate accepts the run."
        )
        HELPER_PROBE_PATH.write_text(report, encoding="utf-8")
        return report, True

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
            result = run(
                command,
                cwd=workdir,
                env=validation_probe_env(command),
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
    if passed:
        diff_hash = final_diff_sha256(diff)
        changed_files = len(changed_paths_from_diff(diff))
        sections.append(
            f"\nbuild-verification-passed: final-diff-sha256={diff_hash} "
            f"changed-files={changed_files} compile_clean=true returncode=0"
        )
        go_packages = changed_go_package_args(diff)
        for package in go_packages:
            go_command = next(
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


def validation_probe_env(command: list[str]) -> dict[str, str] | None:
    if command[:2] != ["go", "test"]:
        return None
    env = os.environ.copy()
    env["GOCACHE"] = ensure_cache_dir(RUNTIME_ROOT / "go-build-cache-adapter")
    env["GOMODCACHE"] = ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache-adapter")
    return env


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


def send_tmux_literal(session: str, message: str) -> None:
    """Send literal text to tmux after stripping bytes subprocess cannot pass."""
    safe_message = message.replace("\x00", "")
    safe_message = "".join(
        char if char in "\n\t" or ord(char) >= 32 else " "
        for char in safe_message
    )
    run(["tmux", "send-keys", "-t", session, "-l", safe_message], timeout=30)
    run(["tmux", "send-keys", "-t", session, "Enter"], timeout=30)


def structured_repair_state_instructions(
    *,
    finding_id: str,
    todo_id: str,
    finding_type: str,
    summary: str,
    blockers: list[str],
    source_hints: list[str],
) -> str:
    """Return no-leak commands for recording verifier/adapter repair work."""

    affected = ",".join(source_hints[:8]) if source_hints else ""
    evidence = json.dumps(
        {
            "source": "public-source-adapter-check",
            "blockers": blockers,
            "affected_path_hints": source_hints[:8],
        },
        sort_keys=True,
    )
    required_resolution = (
        "Verifier must recheck the final diff against these public/source blockers, "
        "and close the todo only after objective validation evidence is attached."
    )
    command = (
        "cd /opt/multiagent\n"
        f"bin/subagent.sh finding-create {shlex.quote(finding_id)} "
        "--severity blocking "
        f"--type {shlex.quote(finding_type)} "
        f"--summary {shlex.quote(summary)} "
        f"--evidence-json {shlex.quote(evidence)} "
        f"--required-resolution {shlex.quote(required_resolution)}"
    )
    if affected:
        command += f" --affected {shlex.quote(affected)}"
    command += (
        "\n"
        f"bin/subagent.sh todo-create {shlex.quote(todo_id)} "
        f"--source-finding-id {shlex.quote(finding_id)} "
        f"--task {shlex.quote(summary)} "
        f"--context {shlex.quote('; '.join(blockers)[:1200])} "
        "--done-criteria 'spawn a bounded repair worker over implicated source paths' "
        "--done-criteria 'worker records resolution-create with changed paths and command return codes' "
        "--done-criteria 'verifier closes todo only after blockers are resolved'\n"
        "# After worker resolution and verifier recheck, run:\n"
        f"bin/subagent.sh todo-close {shlex.quote(todo_id)} --verified-by VERIFIER_NAME --recheck-json '<accepted verifier JSON>'\n"
        "bin/subagent.sh gate-check"
    )
    return (
        "Record the blocker as structured repair state before routing work. "
        "`resolved` is not accepted until a verifier closes the todo:\n"
        "```bash\n"
        + command
        + "\n```"
    )


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
        + source_symbol_map_resume_instructions(blockers)
        + " "
        + structured_repair_state_instructions(
            finding_id="adapter-completion-rejected-001",
            todo_id="todo-adapter-completion-rejected-001",
            finding_type="validation_gap",
            summary="Repair adapter rejected completion marker using public/source evidence.",
            blockers=blockers,
            source_hints=source_hints,
        )
        + " "
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
        "Early public-contract scope warning: the current /app diff appears to be a feature-level patch that may miss source-derived validation. "
        "Do not write completed status until these implementation-scope blockers are resolved: "
        + "; ".join(blockers)
        + "."
        + hint_text
        + f" Durable contract ledger: {CONTRACT_LEDGER_PATH}. Preserve every ledger item in all follow-up work. Ledger excerpt:\n"
        + contract_ledger_excerpt()
        + "\n"
        + " If a worker is still running, let it finish, then spawn a bounded source follow-up with the implicated source paths in --owned. "
        + "If the worker has already exited, do not send implementation text to its tmux pane; create a fresh assignment and spawn a new worker process. "
        + structured_repair_state_instructions(
            finding_id="adapter-early-scope-001",
            todo_id="todo-adapter-early-scope-001",
            finding_type="scope_gap",
            summary="Resolve early public-contract scope blockers in current source diff.",
            blockers=blockers,
            source_hints=source_hints,
        )
        + " "
        + "The follow-up must implement or prove the portable helper/resend contract, run or justify the relevant source/helper test file/package, "
        + "and the verifier/status validation must include the required helper audit markers."
    )
    send_tmux_literal(session, message)


def send_orchestrator_convergence_review(
    session: str,
    *,
    elapsed_seconds: int,
    diff: str,
    source_hints: list[str],
) -> None:
    """Ask the production orchestrator to converge without injecting answer data."""

    diff_excerpt = diff[-5000:] if diff else "No diff excerpt available."
    hint_text = (
        " Source-derived ownership candidates: " + ", ".join(source_hints) + "."
        if source_hints
        else " No specific source ownership candidates were auto-detected; use the current diff and read-only source discovery."
    )
    message = (
        f"Convergence checkpoint: the benchmark adapter has observed a non-empty /app source diff for {elapsed_seconds}s "
        "without a valid completion status. This is a churn warning, not a hidden-test hint. "
        "Do not broaden scope or keep spawning exploratory workers. Freeze the current hypothesis, inspect the current diff, "
        "and drive one of these outcomes: (1) spawn/read one verifier over the current diff, (2) if a relevant visible validation "
        "or source-derived probe failed, spawn exactly one fresh bounded repair worker over the implicated source paths, or "
        "(3) write blocked status with the unresolved source-visible contract. "
        "Before acceptance, explicitly check hidden-contract risk from legitimate evidence only: issue text, visible tests, docs, "
        "source callers, public APIs, data schemas, fixtures, and runtime behavior. Confirm API shape/package placement, nearest "
        "runnable validation or compile coverage, output/error/ordering semantics, fixture assets, and adapter/helper parity for "
        "every changed entrypoint. Do not use leaked evaluator rows, benchmark scores, hidden test names, or previous benchmark "
        "failures as guidance. "
        + hint_text
        + f" Durable contract ledger: {CONTRACT_LEDGER_PATH}. Preserve every ledger item. "
        + structured_repair_state_instructions(
            finding_id="adapter-convergence-001",
            todo_id="todo-adapter-convergence-001",
            finding_type="terminal_state_gap",
            summary="Converge non-empty source diff to verifier-checked status.",
            blockers=["non-empty source diff has no valid completion status"],
            source_hints=source_hints,
        )
        + " "
        "Current /app diff excerpt for orientation only:\n"
        + diff_excerpt
    )
    send_tmux_literal(session, message)


def send_orchestrator_no_diff_checkpoint(
    session: str,
    *,
    elapsed_seconds: int,
    issue: str,
) -> None:
    """Nudge long-running planning loops before they produce source changes."""

    issue_excerpt = issue[:2500]
    message = (
        f"No-diff planning checkpoint: {elapsed_seconds}s elapsed and /app still has no materialized source diff. "
        "This is a planning-loop warning, not a hidden-test hint. Stop broad repository exploration. "
        "Restate the intended behavior, choose the narrowest likely source files from issue text, visible tests, docs, "
        "source callers, public APIs, data schemas, fixtures, and runtime behavior, then spawn exactly one bounded "
        "implementation worker over those paths. If no plausible source path can be identified from legitimate evidence, "
        "write blocked status with the concrete discovery gap. Do not keep spawning read-only scouts or duplicate workers "
        "over the same package without a new source-derived finding. Do not use leaked evaluator rows, benchmark scores, "
        "hidden test names, or previous benchmark failures as guidance. "
        f"Durable contract ledger: {CONTRACT_LEDGER_PATH}. Preserve every ledger item. "
        "Issue excerpt for orientation only:\n"
        + issue_excerpt
    )
    send_tmux_literal(session, message)


def send_orchestrator_terminal_deadline(
    session: str,
    *,
    remaining_seconds: int,
    diff: str,
    blockers: list[str],
    probe_report: str,
    source_hints: list[str],
) -> None:
    """Force a live production orchestrator toward a terminal status before timeout."""

    blocker_text = "; ".join(blockers) if blockers else "no adapter blocker was found from public/source checks"
    probe_excerpt = probe_report[-5000:] if probe_report else "No adapter public validation probe output."
    diff_excerpt = diff[-5000:] if diff else "No current source diff."
    hint_text = (
        " Source-derived ownership candidates: " + ", ".join(source_hints) + "."
        if source_hints
        else " No specific source ownership candidates were auto-detected; use current diff and read-only source discovery only."
    )
    message = (
        f"Terminal deadline checkpoint: about {remaining_seconds}s remain before the native SWE solver times out. "
        "This is a public-source terminal discipline warning, not a hidden-test hint. Stop broad exploration now. "
        "Do not spawn new exploratory workers. Do exactly one of these terminal actions: "
        "(1) if the current diff is ready, spawn/read one final read-only verifier and write completed status with concrete "
        "visible validation evidence; (2) if a public/source blocker remains, spawn at most one bounded repair worker over "
        "the implicated paths, then one verifier; or (3) write blocked status with the concrete public/source reason. "
        "A timeout without `/tmp/multiagent-prod-swe/status.json` will be treated as a production orchestration failure. "
        "No-test compile checks are not behavioral validation for source changes. "
        "Do not use leaked evaluator rows, hidden tests, selected evaluator tests, benchmark scores, or prior evaluator outcomes. "
        f"Adapter/source blockers: {blocker_text}."
        + hint_text
        + f" Durable contract ledger: {CONTRACT_LEDGER_PATH}. Preserve every ledger item. Ledger excerpt:\n"
        + contract_ledger_excerpt()
        + "\n"
        + structured_repair_state_instructions(
            finding_id="adapter-terminal-deadline-001",
            todo_id="todo-adapter-terminal-deadline-001",
            finding_type="terminal_state_gap",
            summary="Resolve terminal deadline blockers and write trusted status.",
            blockers=blockers or ["terminal deadline requires completed or blocked status"],
            source_hints=source_hints,
        )
        + "\nAdapter public validation probe output tail:\n"
        + probe_excerpt
        + "\nCurrent /app diff excerpt for terminal review only:\n"
        + diff_excerpt
    )
    send_tmux_literal(session, message)


def write_orchestrator_resume_prompt(
    base_prompt: Path,
    *,
    attempt: int,
    reason: str,
    issue: str,
    diff: str,
    blockers: list[str],
    probe_report: str,
    source_hints: list[str],
) -> Path:
    """Write a production-orchestrator resume prompt from public/source evidence."""

    prompt_text = base_prompt.read_text(encoding="utf-8")
    blockers_text = "\n".join(f"- {blocker}" for blocker in blockers) or "- No specific blocker was generated."
    hints_text = ", ".join(source_hints) if source_hints else "none auto-detected; use read-only source discovery"
    probe_excerpt = probe_report[-5000:] if probe_report else "No adapter public validation probe output."
    diff_excerpt = diff[-7000:] if diff else "No current source diff."
    resume_prompt = RUNTIME_ROOT / f"orchestrator-autonomous-prompt-resume-{attempt:02d}.md"
    resume_prompt.write_text(
        prompt_text
        + "\n\n## Production Native Resume Handoff\n\n"
        + "The previous production multi-agent run stopped before producing a trustworthy terminal status. "
        + "This is a resume of the same task and current `/app` working tree, not a new benchmark hint. "
        + "Do not revert the current source diff merely because this is a resume. Inspect it, preserve correct work, "
        + "and repair or block based only on legitimate public/source evidence.\n\n"
        + "No-leak rule: this handoff intentionally contains no row identity, hidden tests, selected official tests, "
        + "test patch, benchmark score, or prior evaluator outcome. Do not use leaked evaluator rows or benchmark-only "
        + "metadata as implementation guidance.\n\n"
        + f"Resume attempt: {attempt}\n\n"
        + f"Resume reason: {reason}\n\n"
        + "Generic adapter/verifier blockers:\n"
        + blockers_text
        + source_symbol_map_resume_instructions(blockers)
        + "\n\n"
        + structured_repair_state_instructions(
            finding_id=f"adapter-resume-{attempt:02d}",
            todo_id=f"todo-adapter-resume-{attempt:02d}",
            finding_type="resume_repair",
            summary="Resume production run by resolving public/source blockers.",
            blockers=blockers,
            source_hints=source_hints,
        )
        + "\n\n"
        + f"Source-derived ownership candidates: {hints_text}\n\n"
        + f"Durable contract ledger: `{CONTRACT_LEDGER_PATH}`. Preserve every ledger item. Ledger excerpt:\n"
        + contract_ledger_excerpt()
        + "\n\n"
        + "Adapter public validation probe output tail:\n"
        + probe_excerpt
        + "\n\n"
        + "Current issue text excerpt:\n"
        + issue[:3500]
        + "\n\n"
        + "Current `/app` diff excerpt for orientation only:\n"
        + diff_excerpt
        + "\n\n"
        + "Resume task: run the normal orchestrator loop. Spawn one bounded source worker if the blockers require code "
        + "changes, then one verifier over the resulting diff. Run or attempt relevant visible validation from source "
        + "evidence. Write completed status only when the source-visible blockers are resolved and validation evidence is "
        + "not just a no-test compile check; otherwise write blocked status with the concrete public/source reason.\n",
        encoding="utf-8",
    )
    return resume_prompt


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
    launch_reason: str = "explicit adapter-repair experiment",
) -> str:
    """Spawn a bounded no-leak repair worker from wrapper-visible evidence.

    This must not include project-specific hidden test knowledge or memorized
    benchmark fixes; workers receive only the issue, current diff, generic
    blockers, visible contract ledger, and source-derived ownership hints.
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
        f"You are a bounded source worker launched by {launch_reason}. "
        "Work in /app only. Do not submit PRs, push, or send external messages. "
        f"Assignment ID: {assignment_id}. Branch: benchmark. Stay inside these owned source paths: {owned_csv}. "
        "Do not edit tests, lockfiles, generated assets, bundled assets, or unrelated config unless the visible task/source contract requires fixture assets.\n\n"
        "No-leak rule: do not rely on hidden tests, non-public evaluator rows, previous benchmark failures, or benchmark-only metadata as implementation guidance. "
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
            "exploitation",
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
    if verifier_infrastructure_failure_present(text):
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


def verifier_infrastructure_failure_present(text: str, workdir: Path | None = None) -> bool:
    """Return true when the verifier failed to execute its review machinery.

    This is not acceptance evidence and not a source-level rejection. The
    orchestrator should requeue a verifier or hand off to a fresh orchestrator
    instead of letting a tool/schema/path failure become the terminal semantic
    gate result.
    """

    lower = (text or "").lower()
    if not lower:
        return False
    tool_failure = any(
        marker in lower
        for marker in (
            "failed to parse function arguments",
            "missing field `cmd`",
            "missing field cmd",
            "invalid tool call",
            "tool call failed",
        )
    )
    path_failure = any(
        marker in lower
        for marker in (
            "verifier could not inspect /app",
            "could not inspect /app",
            "/app missing",
            "/app is missing",
            "working directory /app does not exist",
            "no such file or directory: '/app'",
        )
    )
    if tool_failure:
        return True
    if not path_failure:
        return False
    if workdir is None:
        workdir = DEFAULT_WORKDIR
    try:
        return Path(workdir).exists()
    except OSError:
        return True


def verifier_infrastructure_blockers(text: str, workdir: Path | None = None) -> list[str]:
    if not verifier_infrastructure_failure_present(text, workdir):
        return []
    return [
        "verifier infrastructure failed before semantic recheck; requeue a fresh verifier/orchestrator, "
        "preserve the current diff, and require structured finding/todo closure with command/source evidence "
        "before acceptance or rejection"
    ]


def orchestrator_exited_without_status(text: str) -> bool:
    if not text:
        return False
    return (
        "[multiagent codex exec exited rc=" in text
        or "[multiagent claude exited rc=" in text
        or "codex exec exited rc=" in text
        or "claude exited rc=" in text
    )


def verifier_exact_followup_available(text: str) -> bool:
    lower = (text or "").lower()
    return (
        "blocking findings with exact follow-up instructions" in lower
        or "exact follow-up instructions:" in lower
        or "blocking findings:" in lower and "rerun" in lower
        or verifier_infrastructure_failure_present(text)
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
    write_rg_fallback()
    write_go_singleflight_wrapper()
    issue = read_prompt(prompt_path)
    task_metadata = read_task_metadata()
    task_metadata["_solver_workdir"] = str(workdir)
    log("solver metadata is public-only; official expected-test metadata is not exposed to the solver")
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
            "MULTIAGENT_RESOLUTION_AUTOCREATE_TODO": "1",
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
            "GOCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-build-cache"),
            "GOMODCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache"),
            "MULTIAGENT_READY_ATTEMPTS": os.environ.get("MULTIAGENT_READY_ATTEMPTS", "80"),
            "MULTIAGENT_READY_DELAY": os.environ.get("MULTIAGENT_READY_DELAY", "1"),
        }
    )

    def launch_production_session(*, resume: bool, label: str) -> tuple[bool, str]:
        launch_tail = ""
        launch_args = [str(repo_root / "launch.sh"), "--session", session, "--root", str(workdir), "--no-attach"]
        if resume:
            launch_args.append("--resume")
        for attempt in range(1, 3):
            log(
                f"launching production multiagent session={session} root={workdir} "
                f"repo={repo_root} mode={'resume' if resume else 'clean'} label={label} attempt={attempt}"
            )
            launch = run(launch_args, env=env, timeout=120)
            launch_tail = ((launch.stderr or "") + "\n" + (launch.stdout or "")).strip()[-4000:]
            if launch.returncode != 0:
                raise RuntimeError(f"production multiagent launch failed: {launch_tail}")
            time.sleep(2)
            if tmux_has_session(session):
                return True, launch_tail
            log(f"launch attempt {attempt} exited without a live tmux session")
            run(["tmux", "kill-session", "-t", session], timeout=10)
        return False, launch_tail

    launched, launch_tail = launch_production_session(resume=False, label="initial")
    if not launched:
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
    accepted_completed_status_snapshot: dict[str, object] | None = None
    accepted_completed_status_diff_hash = ""
    selected_validation_claim_seen = False
    convergence_followup_sent = False
    no_diff_checkpoint_sent = False
    progress_repair_sent = False
    terminal_deadline_sent = False
    terminal_deadline_at: float | None = None
    no_diff_blocked_retries = 0
    convergence_start = time.monotonic()
    last_diff_digest = ""
    last_diff_changed_at = convergence_start
    coverage_followup_limit = int(os.environ.get("EVAL_COVERAGE_FOLLOWUP_LIMIT", "3"))
    early_scope_followup_limit = int(os.environ.get("EVAL_EARLY_SCOPE_FOLLOWUP_LIMIT", "3"))
    convergence_followup_after = int(os.environ.get("EVAL_CONVERGENCE_FOLLOWUP_AFTER", "900"))
    no_diff_checkpoint_after = int(os.environ.get("EVAL_NO_DIFF_CHECKPOINT_AFTER", "600"))
    progress_repair_enabled = env_truthy("EVAL_PROGRESS_REPAIR_ENABLED", True)
    progress_repair_after = int(os.environ.get("EVAL_PROGRESS_REPAIR_AFTER", "1200"))
    progress_repair_min_stall = int(os.environ.get("EVAL_PROGRESS_REPAIR_MIN_STALL", "240"))
    terminal_deadline_remaining = int(os.environ.get("EVAL_TERMINAL_DEADLINE_REMAINING", "900"))
    terminal_deadline_grace = int(os.environ.get("EVAL_TERMINAL_DEADLINE_GRACE", "300"))
    terminal_force_resume_enabled = env_truthy("EVAL_TERMINAL_FORCE_RESUME", True)
    no_diff_blocked_retry_limit = int(os.environ.get("EVAL_NO_DIFF_BLOCKED_RETRY_LIMIT", "4"))
    adapter_helper_worker_limit = int(os.environ.get("EVAL_ADAPTER_HELPER_WORKER_LIMIT", "1"))
    orchestrator_resume_limit = int(os.environ.get("EVAL_ORCHESTRATOR_RESUME_LIMIT", "1"))
    orchestrator_resume_attempts = 0
    source_symbol_resume_limit = int(os.environ.get("EVAL_SOURCE_SYMBOL_RESUME_LIMIT", "1"))
    source_symbol_resume_attempts = 0
    verifier_infra_resume_limit = int(os.environ.get("EVAL_VERIFIER_INFRA_RESUME_LIMIT", "1"))
    verifier_infra_resume_attempts = 0
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

    def relaunch_orchestrator_for_blockers(
        reason: str,
        diff: str,
        blockers: list[str],
        probe_report: str,
        *,
        force_live_handoff: bool = False,
    ) -> bool:
        nonlocal orchestrator_resume_attempts
        nonlocal source_symbol_resume_attempts
        nonlocal verifier_infra_resume_attempts
        nonlocal coverage_followup_at
        nonlocal last_capture
        nonlocal missing_session_captures
        nonlocal convergence_start
        nonlocal last_diff_digest
        nonlocal last_diff_changed_at

        use_source_symbol_extra_resume = False
        use_verifier_infra_extra_resume = False
        if orchestrator_resume_attempts >= orchestrator_resume_limit:
            if (
                source_symbol_map_blocker_present(blockers)
                and source_symbol_resume_attempts < source_symbol_resume_limit
            ):
                use_source_symbol_extra_resume = True
            elif (
                force_live_handoff
                and any("verifier infrastructure failed" in blocker.lower() for blocker in blockers)
                and verifier_infra_resume_attempts < verifier_infra_resume_limit
            ):
                use_verifier_infra_extra_resume = True
            else:
                log(
                    "production orchestrator resume skipped for "
                    f"{reason}: limit {orchestrator_resume_limit} already reached"
                )
                return False
        if has_live_agent_process() and not force_live_handoff:
            log(f"production orchestrator resume skipped for {reason}: live agent process still exists")
            return False
        if force_live_handoff:
            log(f"production orchestrator forcing terminal handoff for {reason}: replacing active tmux session")
        if use_source_symbol_extra_resume:
            log(
                "production orchestrator source-symbol resume using extra bounded attempt "
                f"{source_symbol_resume_attempts + 1}/{source_symbol_resume_limit} for {reason}"
            )
            source_symbol_resume_attempts += 1
            resume_attempt = orchestrator_resume_attempts + source_symbol_resume_attempts
        elif use_verifier_infra_extra_resume:
            log(
                "production orchestrator verifier-infra resume using extra bounded attempt "
                f"{verifier_infra_resume_attempts + 1}/{verifier_infra_resume_limit} for {reason}"
            )
            verifier_infra_resume_attempts += 1
            resume_attempt = orchestrator_resume_attempts + source_symbol_resume_attempts + verifier_infra_resume_attempts
        else:
            orchestrator_resume_attempts += 1
            resume_attempt = orchestrator_resume_attempts
        source_hints = helper_scope_hints(workdir, issue, diff, blockers)
        resume_prompt = write_orchestrator_resume_prompt(
            autonomous_prompt,
            attempt=resume_attempt,
            reason=reason,
            issue=issue,
            diff=diff,
            blockers=blockers,
            probe_report=probe_report,
            source_hints=source_hints,
        )
        try:
            STATUS_PATH.unlink(missing_ok=True)
        except OSError as exc:
            log(f"could not remove terminal marker before production orchestrator resume: {exc}")
        if tmux_has_session(session):
            capture_session(session)
            run(["tmux", "kill-session", "-t", session], timeout=30)
        env["MULTIAGENT_PROMPT"] = str(resume_prompt)
        env["MULTIAGENT_RESUME"] = "1"
        launched_resume, launch_tail = launch_production_session(
            resume=True,
            label=f"resume-{resume_attempt}",
        )
        if not launched_resume:
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "production orchestrator resume failed to create a live tmux session",
                        "blockers": blockers,
                        "launch_tail": launch_tail[-1000:],
                    }
                ),
                encoding="utf-8",
            )
            log("blocked marker: production orchestrator resume failed to create a live tmux session")
            return False
        coverage_followup_at = time.monotonic()
        last_capture = 0.0
        missing_session_captures = 0
        convergence_start = time.monotonic()
        last_diff_digest = hashlib.sha256(diff.encode("utf-8", errors="replace")).hexdigest() if diff else ""
        last_diff_changed_at = convergence_start
        log(
            "production orchestrator resume launched "
            f"attempt={resume_attempt} reason={reason} prompt={resume_prompt}"
        )
        return True
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
                if completed_status_has_final_build_evidence(diff):
                    accepted_completed_status_snapshot = dict(current_status)
                    accepted_completed_status_diff_hash = final_diff_sha256(diff)
                scope_blockers = implementation_scope_blockers(issue, diff, current_status, task_metadata)
                coverage_blockers = validation_coverage_blockers(issue, diff, text, current_status, task_metadata)
                structured_gate_blockers = structured_repair_gate_blockers()
                blockers = [*scope_blockers, *coverage_blockers, *structured_gate_blockers]
                probe_report = ""
                if coverage_probe_satisfied:
                    blockers = blockers_after_passing_public_probe(blockers)
                    scope_blockers = blockers
                    coverage_blockers = []
                if (
                    not blockers
                    and not coverage_probe_satisfied
                    and not completed_status_has_final_build_evidence(diff)
                    and coverage_probe_commands(workdir, issue, diff)
                ):
                    probe_report, probe_passed = run_validation_coverage_probe(
                        workdir,
                        issue,
                        diff,
                        ["adapter-selected public validation probe required for this issue/diff"],
                    )
                    if probe_passed:
                        coverage_probe_satisfied = True
                        current_status = append_adapter_probe_evidence(
                            current_status,
                            workdir=workdir,
                            diff=diff,
                            marker=f"helper-validation-passed: adapter public validation probe ({HELPER_PROBE_PATH})",
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
                        current_status = append_adapter_probe_evidence(
                            current_status,
                            workdir=workdir,
                            diff=diff,
                            marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
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
                if blockers and relaunch_orchestrator_for_blockers(
                    "completion marker rejected by public/source validation",
                    diff,
                    blockers,
                    probe_report,
                ):
                    time.sleep(5)
                    continue
                if blockers and has_hard_scope_blocker(blockers):
                    log(f"hard public scope blockers remain after follow-ups; refusing to submit known-bad patch: {'; '.join(blockers)}")
                    current_status = {
                        "status": "blocked",
                        "reason": "hard public scope blocker remains after adapter/verifier follow-ups",
                        "blockers": blockers,
                    }
                    STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
                    exit_code = 2
                    outcome = "blocked"
                    break
                if blockers:
                    coverage_gate_unresolved = True
                    log(f"completion marker refused because coverage blockers remain after follow-ups: {'; '.join(blockers)}")
                    current_status = {
                        "status": "blocked",
                        "reason": "coverage blockers remain after adapter/verifier follow-ups",
                        "blockers": blockers,
                    }
                    STATUS_PATH.write_text(json.dumps(current_status), encoding="utf-8")
                    exit_code = 2
                    outcome = "blocked"
                    break
                log(f"completion marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
                outcome = "completed"
                break
            if state == "blocked":
                diff = git_diff(workdir)
                reason_text = json.dumps(current_status, sort_keys=True).lower()
                no_diff_blocked = (
                    not diff.strip()
                    and (
                        "no final source diff" in reason_text
                        or "non-empty source diff" in reason_text
                        or "no materialized source diff" in reason_text
                        or "no source diff" in reason_text
                    )
                )
                if (
                    no_diff_blocked
                    and no_diff_blocked_retries < no_diff_blocked_retry_limit
                    and int(deadline - time.monotonic()) > 300
                ):
                    no_diff_blocked_retries += 1
                    ownership_paths = required_path_outside_owned_reports(RUNTIME_ROOT)
                    blockers = [
                        "production orchestrator wrote blocked status after a worker completed without a materialized source diff; restart from issue/source evidence and choose the narrowest implementation path before blocking again",
                        *[
                            f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                            for path in ownership_paths[:8]
                        ],
                    ]
                    if relaunch_orchestrator_for_blockers(
                        "blocked with no materialized source diff",
                        diff,
                        blockers,
                        "",
                        force_live_handoff=True,
                    ):
                        log(f"no-diff blocked retry launched attempt={no_diff_blocked_retries}")
                        time.sleep(5)
                        continue
                if (
                    diff.strip()
                    and blocked_status_needs_diff_reconciliation(current_status)
                    and orchestrator_resume_attempts < orchestrator_resume_limit
                    and int(deadline - time.monotonic()) > 300
                ):
                    capture_session(session)
                    text = captured_text()
                    status_blockers = current_status.get("blockers")
                    if isinstance(status_blockers, list):
                        blockers = [str(blocker) for blocker in status_blockers]
                    else:
                        blockers = [str(current_status.get("reason") or "blocked status requires live diff reconciliation")]
                    blockers = list(
                        dict.fromkeys(
                            [
                                *blockers,
                                *implementation_scope_blockers(issue, diff, current_status, task_metadata),
                                *validation_coverage_blockers(issue, diff, text, current_status, task_metadata),
                                (
                                    "Blocked-status reconciliation: re-read the live files and `git diff --name-only`; "
                                    "make claimed files/hunks match the actual final diff or remove stale claims before final status."
                                ),
                            ]
                        )
                    )
                    if relaunch_orchestrator_for_blockers(
                        "blocked status has stale claims or stale patch evidence against a live source diff",
                        diff,
                        blockers,
                        "",
                        force_live_handoff=True,
                    ):
                        log("blocked-status diff reconciliation resume launched")
                        time.sleep(5)
                        continue
                log(f"blocked marker: {json.dumps(current_status, sort_keys=True)[:2000]}")
                exit_code = 2
                outcome = "blocked"
                break
            if time.monotonic() - last_capture > 60:
                capture_session(session)
                diff_snapshot = git_diff(workdir)
                diff_bytes = len(diff_snapshot.encode("utf-8"))
                diff_digest = hashlib.sha256(diff_snapshot.encode("utf-8", errors="replace")).hexdigest() if diff_bytes else ""
                if diff_digest != last_diff_digest:
                    last_diff_digest = diff_digest
                    last_diff_changed_at = time.monotonic()
                text = captured_text()
                log(f"waiting status={state or 'none'} diff_bytes={diff_bytes}")
                remaining_seconds = int(deadline - time.monotonic())
                blocked_no_diff_subagents = blocked_no_diff_subagent_summaries(RUNTIME_ROOT)
                if (
                    not state
                    and diff_bytes == 0
                    and blocked_no_diff_subagents
                    and no_diff_blocked_retries < no_diff_blocked_retry_limit
                    and remaining_seconds > 300
                ):
                    no_diff_blocked_retries += 1
                    ownership_paths = required_path_outside_owned_reports(RUNTIME_ROOT)
                    blockers = [
                        "production subagent reached blocked status without a materialized source diff; replace the no-diff worker and implement from issue/source evidence before blocking again",
                        *[
                            f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                            for path in ownership_paths[:8]
                        ],
                        *blocked_no_diff_subagents[:3],
                    ]
                    if relaunch_orchestrator_for_blockers(
                        "blocked subagent with no materialized source diff",
                        diff_snapshot,
                        blockers,
                        "",
                        force_live_handoff=True,
                    ):
                        log(f"no-diff blocked subagent retry launched attempt={no_diff_blocked_retries}")
                        time.sleep(5)
                        continue
                if (
                    not state
                    and not terminal_deadline_sent
                    and terminal_deadline_remaining > 0
                    and remaining_seconds <= terminal_deadline_remaining
                    and tmux_has_session(session)
                ):
                    diff = diff_snapshot
                    terminal_blockers: list[str] = []
                    probe_report = ""
                    if diff_bytes > 0:
                        scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                        coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                        terminal_blockers = [*scope_blockers, *coverage_blockers]
                        if coverage_probe_satisfied:
                            terminal_blockers = blockers_after_passing_public_probe(terminal_blockers)
                        elif coverage_probe_commands(workdir, issue, diff):
                            probe_report, probe_passed = run_validation_coverage_probe(
                                workdir,
                                issue,
                                diff,
                                terminal_blockers
                                or [
                                    "terminal deadline checkpoint ran public validation before forcing final orchestrator status"
                                ],
                            )
                            if probe_passed:
                                coverage_probe_satisfied = True
                                terminal_blockers = blockers_after_passing_public_probe(scope_blockers)
                            else:
                                terminal_blockers = [
                                    *scope_blockers,
                                    f"terminal deadline adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                                ]
                    else:
                        terminal_blockers = [
                            "terminal deadline reached with no materialized source diff; write blocked status or produce the narrow source diff now"
                        ]
                    send_orchestrator_terminal_deadline(
                        session,
                        remaining_seconds=remaining_seconds,
                        diff=diff,
                        blockers=terminal_blockers,
                        probe_report=probe_report,
                        source_hints=helper_scope_hints(workdir, issue, diff, terminal_blockers),
                    )
                    terminal_deadline_sent = True
                    terminal_deadline_at = time.monotonic()
                    log(
                        "terminal deadline checkpoint sent with "
                        f"remaining={remaining_seconds}s blockers={'; '.join(terminal_blockers) if terminal_blockers else 'none'}"
                    )
                    last_capture = time.monotonic()
                    time.sleep(5)
                    continue
                if (
                    not state
                    and terminal_deadline_at is not None
                    and terminal_deadline_grace > 0
                    and time.monotonic() - terminal_deadline_at >= terminal_deadline_grace
                ):
                    diff = git_diff(workdir)
                    deadline_blockers = [
                        *implementation_scope_blockers(issue, diff, {}, task_metadata),
                        *validation_coverage_blockers(issue, diff, text, {}, task_metadata),
                    ]
                    deadline_probe_report = ""
                    if coverage_probe_satisfied:
                        deadline_blockers = blockers_after_passing_public_probe(deadline_blockers)
                    if not deadline_blockers:
                        deadline_blockers = [
                            "terminal deadline expired without completed/blocked status after orchestrator checkpoint; wrapper cannot accept an active-run diff without terminal verifier/status"
                        ]
                    remaining_after_grace = int(deadline - time.monotonic())
                    if (
                        terminal_force_resume_enabled
                        and diff.strip()
                        and orchestrator_resume_attempts < orchestrator_resume_limit
                        and remaining_after_grace > 240
                    ):
                        if coverage_probe_commands(workdir, issue, diff):
                            deadline_probe_report, deadline_probe_passed = run_validation_coverage_probe(
                                workdir,
                                issue,
                                diff,
                                deadline_blockers
                                or [
                                    "terminal handoff ran adapter-selected public validation before replacing a non-converged orchestrator"
                                ],
                            )
                            if deadline_probe_passed:
                                coverage_probe_satisfied = True
                                deadline_blockers = blockers_after_passing_public_probe(
                                    implementation_scope_blockers(issue, diff, {}, task_metadata)
                                )
                            elif not deadline_blockers:
                                deadline_blockers = [
                                    f"terminal handoff adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}"
                                ]
                        handoff_blockers = [
                            *deadline_blockers,
                            "Terminal handoff: the active production orchestrator did not write completed/blocked status after the deadline checkpoint. Continue from the current /app diff, preserve correct work, run or attempt source-visible validation, then write status.json.",
                        ]
                        if relaunch_orchestrator_for_blockers(
                            "terminal deadline expired with active no-status diff",
                            diff,
                            handoff_blockers,
                            deadline_probe_report,
                            force_live_handoff=True,
                        ):
                            terminal_deadline_sent = False
                            terminal_deadline_at = None
                            last_capture = 0.0
                            time.sleep(5)
                            continue
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "blocked",
                                "reason": "terminal deadline expired without machine-readable orchestrator status",
                                "blockers": deadline_blockers,
                            }
                        ),
                        encoding="utf-8",
                    )
                    log("blocked marker: terminal deadline expired without machine-readable orchestrator status")
                    exit_code = 2
                    outcome = "blocked"
                    break
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
                    coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                    blockers = [*scope_blockers, *coverage_blockers]
                    if coverage_probe_satisfied:
                        blockers = blockers_after_passing_public_probe(blockers)
                        scope_blockers = blockers
                        coverage_blockers = []
                    probe_report = ""
                    if blockers and coverage_followups_sent < coverage_followup_limit and tmux_has_session(session):
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
                    if blockers and relaunch_orchestrator_for_blockers(
                        "recovered completion rejected by public/source validation",
                        diff,
                        blockers,
                        probe_report,
                    ):
                        time.sleep(5)
                        continue
                    if blockers and has_hard_scope_blocker(blockers):
                        log(f"hard public scope blockers remain after follow-ups; refusing recovered accepted patch: {'; '.join(blockers)}")
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "hard public scope blocker remains after recovered acceptance",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        exit_code = 2
                        outcome = "blocked"
                        break
                    if blockers:
                        coverage_gate_unresolved = True
                        log(f"recovered completion refused because coverage blockers remain after follow-ups: {'; '.join(blockers)}")
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "coverage blockers remain after recovered acceptance",
                                    "blockers": blockers,
                                }
                            ),
                            encoding="utf-8",
                        )
                        exit_code = 2
                        outcome = "blocked"
                        break
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
                    recovered_base = (
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                        if probe_passed
                        else "final verifier accepted without status.json; adapter public helper probe did not pass"
                    )
                    recovered_validation = recovered_validation_text(
                        task_metadata,
                        text,
                        recovered_base,
                    )
                    recovered_validation = recovered_validation_with_helper_evidence(issue, text, recovered_validation)
                    recovered_status = status_with_recovered_validation({}, recovered_validation)
                    scope_blockers = implementation_scope_blockers(issue, diff, recovered_status, task_metadata)
                    if probe_passed:
                        blockers = blockers_after_passing_public_probe(scope_blockers)
                        if not blockers:
                            STATUS_PATH.write_text(
                                json.dumps(
                                    {
                                        "status": "completed",
                                        "summary": "final verifier accepted source diff; adapter recovered missing status marker",
                                        "validation": recovered_validation,
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
                    if blockers and relaunch_orchestrator_for_blockers(
                        "final verifier accepted before public/source validation passed",
                        diff,
                        blockers,
                        probe_report,
                    ):
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
                    and not convergence_followup_sent
                    and convergence_followup_after > 0
                    and time.monotonic() - convergence_start >= convergence_followup_after
                    and tmux_has_session(session)
                ):
                    diff = git_diff(workdir)
                    source_hints = helper_scope_hints(workdir, issue, diff, [])
                    send_orchestrator_convergence_review(
                        session,
                        elapsed_seconds=int(time.monotonic() - convergence_start),
                        diff=diff,
                        source_hints=source_hints,
                    )
                    convergence_followup_sent = True
                    log(
                        "convergence checkpoint sent after "
                        f"{int(time.monotonic() - convergence_start)}s with diff_bytes={diff_bytes}"
                    )
                    last_capture = time.monotonic()
                    time.sleep(5)
                    continue
                if (
                    not state
                    and diff_bytes > 0
                    and progress_repair_enabled
                    and not progress_repair_sent
                    and progress_repair_after > 0
                    and time.monotonic() - convergence_start >= progress_repair_after
                    and time.monotonic() - last_diff_changed_at >= progress_repair_min_stall
                    and tmux_has_session(session)
                ):
                    diff = diff_snapshot
                    scope_blockers = implementation_scope_blockers(issue, diff, {}, task_metadata)
                    coverage_blockers = validation_coverage_blockers(issue, diff, text, {}, task_metadata)
                    blockers = [*scope_blockers, *coverage_blockers]
                    probe_report = ""
                    probe_passed = False
                    if coverage_probe_commands(workdir, issue, diff):
                        probe_report, probe_passed = run_validation_coverage_probe(
                            workdir,
                            issue,
                            diff,
                            blockers
                            or [
                                "progress watchdog observed a stale source diff; adapter ran public validation before repair"
                            ],
                        )
                        if probe_passed:
                            coverage_probe_satisfied = True
                            blockers = blockers_after_passing_public_probe(scope_blockers)
                        elif not coverage_blockers:
                            blockers = [
                                *scope_blockers,
                                f"progress watchdog adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                            ]
                    progress_repair_sent = True
                    if blockers and adapter_helper_workers_spawned < adapter_helper_worker_limit:
                        if adapter_helper_repair_allowed("progress watchdog stale diff"):
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
                                        "Progress watchdog intervention: the same non-empty source diff has not converged to accepted validation/status. Continue from the current /app diff, fix the source-visible blockers, and do not broaden scope.",
                                    ],
                                    helper_scope_hints(workdir, issue, diff, blockers),
                                    adapter_helper_workers_spawned,
                                    probe_report,
                                    launch_reason="the production-native progress watchdog",
                                )
                                log(f"progress watchdog spawned bounded repair worker: {helper_worker}")
                                adapter_helper_last_spawn_at = time.monotonic()
                                adapter_helper_reprobe_done = False
                                adapter_helper_last_probe_digest = None
                                coverage_followup_at = time.monotonic()
                                last_capture = 0.0
                                time.sleep(5)
                                continue
                            except Exception as exc:
                                log(f"progress watchdog repair worker spawn failed: {exc}")
                    if blockers and not has_live_agent_process() and relaunch_orchestrator_for_blockers(
                        "progress watchdog found stale source diff with no live agent",
                        diff,
                        blockers,
                        probe_report,
                    ):
                        time.sleep(5)
                        continue
                    if blockers:
                        send_orchestrator_followup(session, blockers, probe_report, helper_scope_hints(workdir, issue, diff, blockers))
                        log("progress watchdog sent hard follow-up after stale diff: " + "; ".join(blockers))
                        coverage_followup_at = time.monotonic()
                    else:
                        send_orchestrator_convergence_review(
                            session,
                            elapsed_seconds=int(time.monotonic() - convergence_start),
                            diff=diff,
                            source_hints=helper_scope_hints(workdir, issue, diff, []),
                        )
                        log("progress watchdog found no adapter blockers; requested terminal verifier/status")
                    last_capture = time.monotonic()
                    time.sleep(5)
                    continue
                if (
                    not state
                    and diff_bytes == 0
                    and not no_diff_checkpoint_sent
                    and no_diff_checkpoint_after > 0
                    and time.monotonic() - convergence_start >= no_diff_checkpoint_after
                    and tmux_has_session(session)
                ):
                    send_orchestrator_no_diff_checkpoint(
                        session,
                        elapsed_seconds=int(time.monotonic() - convergence_start),
                        issue=issue,
                    )
                    no_diff_checkpoint_sent = True
                    log(
                        "no-diff planning checkpoint sent after "
                        f"{int(time.monotonic() - convergence_start)}s"
                    )
                    last_capture = time.monotonic()
                    time.sleep(5)
                    continue
                if (
                    not state
                    and diff_bytes > 0
                    and not has_live_agent_process()
                    and orchestrator_exited_without_status(text)
                    and not coverage_followup_at
                ):
                    diff = git_diff(workdir)
                    coverage_status_for_blockers = status_with_recovered_public_evidence(
                        {},
                        "captured coverage-follow-up verifier/worker text",
                        issue,
                        text,
                    )
                    scope_blockers = implementation_scope_blockers(issue, diff, coverage_status_for_blockers, task_metadata)
                    coverage_blockers = validation_coverage_blockers(
                        issue,
                        diff,
                        text,
                        coverage_status_for_blockers,
                        task_metadata,
                    )
                    infra_blockers = verifier_infrastructure_blockers(text, workdir)
                    blockers = [*scope_blockers, *coverage_blockers, *infra_blockers]
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
                    if infra_blockers and relaunch_orchestrator_for_blockers(
                        "verifier infrastructure failed before semantic recheck",
                        diff,
                        blockers,
                        probe_report,
                        force_live_handoff=True,
                    ):
                        time.sleep(5)
                        continue
                    if blockers and relaunch_orchestrator_for_blockers(
                        "orchestrator exited with unverified source diff",
                        diff,
                        blockers,
                        probe_report,
                    ):
                        time.sleep(5)
                        continue
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
                    recovered_base = (
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                        if coverage_probe_satisfied
                        else "no adapter-selected public validation command was available; implementation blockers were clean"
                    )
                    STATUS_PATH.write_text(
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "orchestrator exited with a source diff; adapter recovered missing status marker",
                                "validation": recovered_validation_with_helper_evidence(
                                    issue,
                                    text,
                                    recovered_validation_text(
                                        task_metadata,
                                        text,
                                        recovered_base,
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
                    if completed_status_has_final_build_evidence(diff):
                        log("coverage follow-up recovery yielded to completed status with accepted final build gate")
                        outcome = "completed"
                        break
                    coverage_status_for_blockers = status_with_recovered_public_evidence(
                        {},
                        "captured coverage-follow-up verifier/worker text",
                        issue,
                        text,
                    )
                    scope_blockers = implementation_scope_blockers(issue, diff, coverage_status_for_blockers, task_metadata)
                    coverage_blockers = validation_coverage_blockers(
                        issue,
                        diff,
                        text,
                        coverage_status_for_blockers,
                        task_metadata,
                    )
                    infra_blockers = verifier_infrastructure_blockers(text, workdir)
                    blockers = [*scope_blockers, *coverage_blockers, *infra_blockers]
                    if coverage_probe_satisfied:
                        blockers = blockers_after_passing_public_probe(blockers)
                        scope_blockers = blockers
                        coverage_blockers = []
                    if not blockers and not coverage_probe_satisfied and coverage_probe_commands(workdir, issue, diff):
                        probe_report, probe_passed = run_validation_coverage_probe(
                            workdir,
                            issue,
                            diff,
                            [
                                "orchestrator exited after a coverage follow-up; adapter reran selected public validation before recovery"
                            ],
                        )
                        if probe_passed:
                            coverage_probe_satisfied = True
                            latest_diff = git_diff(workdir)
                            latest_status_for_blockers = append_adapter_probe_evidence(
                                status_with_recovered_public_evidence(
                                    {},
                                    f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                    issue,
                                    text,
                                ),
                                workdir=workdir,
                                diff=latest_diff,
                                marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                            )
                            scope_blockers = implementation_scope_blockers(
                                issue,
                                latest_diff,
                                latest_status_for_blockers,
                                task_metadata,
                            )
                            blockers = blockers_after_passing_public_probe(scope_blockers)
                        else:
                            blockers = [
                                *scope_blockers,
                                f"orchestrator exited after coverage follow-up and adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}",
                            ]
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
                            latest_status_for_blockers = append_adapter_probe_evidence(
                                status_with_recovered_public_evidence(
                                    {},
                                    f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                    issue,
                                    text,
                                ),
                                workdir=workdir,
                                diff=latest_diff,
                                marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                            )
                            scope_blockers = implementation_scope_blockers(
                                issue,
                                latest_diff,
                                latest_status_for_blockers,
                                task_metadata,
                            )
                            blockers = blockers_after_passing_public_probe(scope_blockers)
                            if not blockers and latest_diff.strip():
                                recovered_status = append_adapter_probe_evidence(
                                    {
                                        "status": "completed",
                                        "summary": "orchestrator exited after adapter public validation; preserving current source diff",
                                        "validation": recovered_validation_with_helper_evidence(
                                            issue,
                                            text,
                                            recovered_validation_text(
                                                task_metadata,
                                                text,
                                                f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                            ),
                                        ),
                                        "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                                    },
                                    workdir=workdir,
                                    diff=latest_diff,
                                    marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                )
                                STATUS_PATH.write_text(
                                    json.dumps(recovered_status),
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
                                    latest_status_for_blockers = append_adapter_probe_evidence(
                                        status_with_recovered_public_evidence(
                                            {},
                                            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                            issue,
                                            text,
                                        ),
                                        workdir=workdir,
                                        diff=latest_diff,
                                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                    )
                                    latest_blockers = implementation_scope_blockers(
                                        issue,
                                        latest_diff,
                                        latest_status_for_blockers,
                                        task_metadata,
                                    )
                                    latest_blockers = blockers_after_passing_public_probe(latest_blockers)
                                    if not latest_blockers and latest_diff.strip():
                                        recovered_status = append_adapter_probe_evidence(
                                            {
                                                "status": "completed",
                                                "summary": "adapter recovery worker fixed public contract; preserving current source diff",
                                                "validation": recovered_validation_with_helper_evidence(
                                                    issue,
                                                    text,
                                                    recovered_validation_text(
                                                        task_metadata,
                                                        text,
                                                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                                    ),
                                                ),
                                                "risk": "completion marker recovered by benchmark wrapper after adapter helper fix",
                                            },
                                            workdir=workdir,
                                            diff=latest_diff,
                                            marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                        )
                                        STATUS_PATH.write_text(
                                            json.dumps(recovered_status),
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
                        force_verifier_handoff = (
                            terminal_force_resume_enabled
                            and (verifier_exact_followup_available(text) or bool(infra_blockers))
                            and int(deadline - time.monotonic()) > 240
                        )
                        if blockers and relaunch_orchestrator_for_blockers(
                            (
                                "verifier infrastructure failed before semantic recheck"
                                if infra_blockers
                                else "orchestrator exited after unresolved verifier follow-up"
                                if force_verifier_handoff
                                else "orchestrator exited after unresolved coverage follow-up"
                            ),
                            diff,
                            [
                                *blockers,
                                *(
                                    [
                                        (
                                            "Verifier infrastructure handoff: the verifier did not complete a semantic recheck because its tool/path execution failed. "
                                            "Preserve the current /app diff, spawn a fresh read-only verifier, require structured findings/todos for any semantic blockers, "
                                            "and do not write completed status until gate-check plus final build/provider evidence pass."
                                            if infra_blockers
                                            else "Verifier exact-follow-up handoff: a verifier produced concrete public/source repair instructions, but the active run did not apply them before exiting. Continue from the current /app diff, apply or disprove those verifier findings from source, rerun the implicated visible validation, then write status.json."
                                        )
                                    ]
                                    if force_verifier_handoff
                                    else []
                                ),
                            ],
                            probe_report,
                            force_live_handoff=force_verifier_handoff,
                        ):
                            time.sleep(5)
                            continue
                        if completed_status_has_final_build_evidence(git_diff(workdir)):
                            log("coverage follow-up blocker path yielded to completed status with accepted final build gate")
                            outcome = "completed"
                            break
                        ownership_paths = required_path_outside_owned_reports(RUNTIME_ROOT)
                        if (
                            not diff.strip()
                            and ownership_paths
                            and orchestrator_resume_attempts < orchestrator_resume_limit
                            and int(deadline - time.monotonic()) > 240
                            and relaunch_orchestrator_for_blockers(
                                "orchestrator exited after ownership-boundary no-diff worker",
                                diff,
                                [
                                    *blockers,
                                    *[
                                        f"worker reported required-path-outside-owned:{path}; include this source path in the next bounded worker owned set"
                                        for path in ownership_paths[:8]
                                    ],
                                    "The previous worker correctly stopped at an ownership boundary without producing a diff. Spawn a fresh bounded worker whose owned paths include the requested outside-owned path plus the original endpoint owner paths.",
                                ],
                                probe_report,
                                force_live_handoff=True,
                            )
                        ):
                            time.sleep(5)
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
                    if diff.strip() and (coverage_probe_satisfied or not coverage_probe_commands(workdir, issue, diff)):
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "completed",
                                    "summary": "orchestrator exited after adapter helper validation; preserving current source diff",
                                    "validation": recovered_validation_with_helper_evidence(
                                        issue,
                                        text,
                                        recovered_validation_text(
                                            task_metadata,
                                            text,
                                            f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                                        ),
                                    ),
                                    "risk": "completion marker recovered by benchmark wrapper after orchestrator exit",
                                }
                            ),
                            encoding="utf-8",
                        )
                        log("completion marker recovered after adapter helper probe and orchestrator exit")
                        outcome = "recovered"
                        break
                    if diff.strip():
                        coverage_gate_unresolved = True
                        STATUS_PATH.write_text(
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "reason": "adapter public validation was not proven after coverage follow-up",
                                    "blockers": [
                                        f"adapter-selected public validation did not pass; inspect {HELPER_PROBE_PATH}"
                                    ],
                                }
                            ),
                            encoding="utf-8",
                        )
                        log("blocked marker: adapter public validation was not proven after coverage follow-up")
                        exit_code = 2
                        outcome = "blocked"
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
    if (
        exit_code != 0
        and final_diff.strip()
        and accepted_completed_status_snapshot is not None
        and accepted_completed_status_diff_hash == final_diff_sha256(final_diff)
    ):
        final_text = captured_text()
        snapshot_blockers = [
            *completed_status_snapshot_blockers(
                issue,
                final_diff,
                final_text,
                accepted_completed_status_snapshot,
                task_metadata,
            ),
            *structured_repair_gate_blockers(),
        ]
        if not snapshot_blockers:
            STATUS_PATH.write_text(json.dumps(accepted_completed_status_snapshot), encoding="utf-8")
            log(
                "nonzero wrapper exit overridden because an earlier completed status snapshot "
                "still proves the final diff after stale coverage follow-up state"
            )
            coverage_gate_unresolved = False
            exit_code = 0
            outcome = "completed"
        else:
            log(
                "completed status snapshot could not override nonzero wrapper exit; blockers remain: "
                + "; ".join(snapshot_blockers)
            )
    if exit_code != 0 and final_diff.strip() and completed_status_has_final_build_evidence(final_diff):
        log(
            "nonzero wrapper exit overridden because status.json already records completed final-diff build verification accepted by the structured repair gate"
        )
        coverage_gate_unresolved = False
        exit_code = 0
        outcome = "completed"
    if exit_code == 0 and final_diff.strip():
        final_status = status()
        final_text = captured_text()
        post_cleanup_blockers = [
            *implementation_scope_blockers(issue, final_diff, final_status, task_metadata),
            *validation_coverage_blockers(issue, final_diff, final_text, final_status, task_metadata),
        ]
        status_text = json.dumps(final_status, sort_keys=True)
        if restored and not build_verification_has_evidence(status_text, final_diff):
            post_cleanup_blockers.insert(
                0,
                "benchmark cleanup changed the final submitted diff after verifier acceptance; "
                "rerun affected compile/test validation against the cleaned final diff before submission: "
                + ", ".join(restored[:8]),
            )
        if post_cleanup_blockers:
            STATUS_PATH.write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "post-cleanup final gate rejected stale validation evidence",
                        "blockers": list(dict.fromkeys(post_cleanup_blockers)),
                        "final_diff_sha256": final_diff_sha256(final_diff),
                    }
                ),
                encoding="utf-8",
            )
            log(
                "post-cleanup final gate refused stale completion evidence; blockers remain: "
                + "; ".join(list(dict.fromkeys(post_cleanup_blockers)))
            )
            exit_code = 2
            outcome = "blocked"
    if exit_code != 0 and final_diff.strip():
        final_status = status()
        final_state = str(final_status.get("status", "")).lower()
        final_text = captured_text()
        if recover_verifier_accepted_todo_closures(final_text, final_diff):
            final_status = status()
            final_state = str(final_status.get("status", "")).lower()
        original_final_validation_blockers = validation_coverage_blockers(
            issue,
            final_diff,
            final_text,
            final_status,
            task_metadata,
        )
        non_recoverable_validation_blockers = non_recoverable_final_validation_blockers(
            original_final_validation_blockers
        )
        validation_evidence = persisted_subagent_visible_validation_evidence(final_diff)
        validation_evidence_kind = "visible"
        if not validation_evidence and visible_validation_passed_in_text(final_text):
            validation_evidence = "captured tmux output contains passing visible validation"
            validation_evidence_kind = "visible"
        if not validation_evidence:
            validation_evidence = persisted_stale_visible_reconciliation_evidence()
            if validation_evidence:
                validation_evidence_kind = "stale-visible"
        if (final_state != "blocked" or validation_evidence) and validation_evidence:
            final_status_for_blockers = status_with_recovered_public_evidence(
                final_status,
                validation_evidence,
                issue,
                final_text,
            )
            final_probe_blockers: list[str] = []
            if validation_evidence_kind != "stale-visible" and coverage_probe_commands(workdir, issue, final_diff):
                probe_report, probe_passed = run_validation_coverage_probe(
                    workdir,
                    issue,
                    final_diff,
                    ["final cleanup recovery requires adapter public validation before accepting visible-validation text"],
                )
                if probe_passed:
                    final_status_for_blockers = append_adapter_probe_evidence(
                        final_status_for_blockers,
                        workdir=workdir,
                        diff=final_diff,
                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                    )
                else:
                    final_probe_blockers.append(
                        f"final cleanup recovery refused because adapter-selected public validation failed; inspect {HELPER_PROBE_PATH}"
                    )
            final_blockers = [
                *implementation_scope_blockers(issue, final_diff, final_status_for_blockers, task_metadata),
                *validation_coverage_blockers(issue, final_diff, final_text, final_status_for_blockers, task_metadata),
                *non_recoverable_validation_blockers,
                *final_probe_blockers,
            ]
            final_blockers = blockers_after_passing_public_probe(final_blockers)
            if not final_blockers:
                recovered_status = append_adapter_probe_evidence(
                    {
                        "status": "completed",
                        "summary": "source diff and validation evidence recovered after missing completion marker",
                        "validation": "captured worker output contains recoverable validation evidence; status marker recovered by benchmark wrapper; "
                        + validation_evidence,
                        "risk": "completion marker was recovered by the benchmark wrapper after worker/orchestrator exit",
                    },
                    workdir=workdir,
                    diff=final_diff,
                    marker=(
                        f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})"
                        if validation_evidence_kind != "stale-visible"
                        else None
                    ),
                )
                STATUS_PATH.write_text(
                    json.dumps(recovered_status),
                    encoding="utf-8",
                )
                log(f"completion marker recovered at final cleanup from source diff plus {validation_evidence_kind} validation evidence")
                coverage_gate_unresolved = False
                exit_code = 0
                outcome = "recovered"
            else:
                log("final cleanup recovery refused; blockers remain: " + "; ".join(final_blockers))
        elif (
            final_state != "blocked" or blocked_status_recoverable_by_public_probe(final_status)
        ) and coverage_probe_commands(workdir, issue, final_diff):
            probe_report, probe_passed = run_validation_coverage_probe(
                workdir,
                issue,
                final_diff,
                ["final cleanup recovery found a source diff but no durable worker validation evidence"],
            )
            if probe_passed:
                final_status_for_blockers = append_adapter_probe_evidence(
                    status_with_recovered_public_evidence(
                        final_status,
                        f"adapter public helper probe passed at final cleanup ({HELPER_PROBE_PATH})",
                        issue,
                        final_text,
                    ),
                    workdir=workdir,
                    diff=final_diff,
                    marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                )
                final_blockers = [
                    *implementation_scope_blockers(issue, final_diff, final_status_for_blockers, task_metadata),
                    *validation_coverage_blockers(issue, final_diff, final_text, final_status_for_blockers, task_metadata),
                    *non_recoverable_validation_blockers,
                ]
                final_blockers = blockers_after_passing_public_probe(final_blockers)
                if not final_blockers:
                    recovered_status = append_adapter_probe_evidence(
                        {
                            "status": "completed",
                            "summary": "source diff accepted after adapter public validation probe at final cleanup",
                            "validation": "status marker recovered by benchmark wrapper",
                            "risk": "completion marker was recovered by the benchmark wrapper after missing durable worker validation evidence",
                        },
                        workdir=workdir,
                        diff=final_diff,
                        marker=f"helper-validation-passed: adapter public helper probe ({HELPER_PROBE_PATH})",
                    )
                    STATUS_PATH.write_text(
                        json.dumps(recovered_status),
                        encoding="utf-8",
                    )
                    log("completion marker recovered at final cleanup after adapter public probe passed without durable worker evidence")
                    coverage_gate_unresolved = False
                    exit_code = 0
                    outcome = "recovered"
                else:
                    log("final cleanup adapter public probe passed, but blockers remain: " + "; ".join(final_blockers))
            else:
                log(f"final cleanup adapter public probe failed without durable worker validation evidence; inspect {HELPER_PROBE_PATH}")
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
