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
        dependency_contract_changed,
    )
except ImportError:  # pragma: no cover - direct script execution in task containers
    from swe_prod_guardrails import (
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
HELPER_PROBE_PATH = RUNTIME_ROOT / "helper-validation-probe.txt"
MULTI_VALUE_PROBE_PATH = RUNTIME_ROOT / "multi-value-probe.txt"
STALE_VISIBLE_RECONCILIATION_PATH = RUNTIME_ROOT / "stale-visible-reconciliation.txt"
CONTRACT_LEDGER_PATH = RUNTIME_ROOT / "contract-ledger.md"
SOURCE_OWNER_CANDIDATES_PATH = RUNTIME_ROOT / "source-owner-candidates.md"
FAILURE_DIAGNOSTICS_PATH = RUNTIME_ROOT / "failure-diagnostics.txt"
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






ISSUE_COVERAGE_KEYWORDS = {
    "api",
    "audit",
    "cache",
    "cached",
    "caching",
    "cluster",
    "concurrent",
    "config",
    "context",
    "credential",
    "csr",
    "directory",
    "error",
    "exec",
    "expiry",
    "fallback",
    "field",
    "fields",
    "forwarder",
    "handler",
    "initialize",
    "initialization",
    "logging",
    "namespace",
    "persist",
    "request",
    "response",
    "router",
    "session",
    "state",
    "stream",
    "ttl",
    "tunnel",
    "uploader",
}
ISSUE_COVERAGE_TRIGGER_WORDS = {
    "bug",
    "canceled",
    "cancelled",
    "cache",
    "cached",
    "caching",
    "current",
    "disconnect",
    "disconnects",
    "harder",
    "error",
    "expected",
    "fail",
    "fails",
    "failure",
    "inconsistent",
    "inconsistently",
    "missing",
    "must",
    "prevent",
    "prematurely",
    "required",
    "requires",
    "should",
    "unnecessary",
    "unnecessarily",
}

ISSUE_COVERAGE_WEAK_CLOSURE_MARKERS = {
    "source-not-touched",
    "source-not-modified",
    "source-not-changed",
    "not-touched",
    "not-modified",
    "not-changed",
    "nonblocking",
    "non-blocking",
    "verifier-reviewed",
    "not alter",
    "not changed",
    "not modify",
    "preserved-not",
    "preserved-",
}


def _clean_issue_sentence(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence.replace("**", " ")).strip(" -:*\t\r\n")


def public_issue_text_for_coverage(issue: str) -> str:
    """Return public issue text, excluding benchmark harness instructions."""

    pr_match = re.search(
        r"<pr_description>\s*(.*?)\s*</pr_description>",
        issue,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if pr_match:
        return pr_match.group(1)
    for marker in (
        "\n<instructions>",
        "\n# Task Instructions",
        "\n## Task Instructions",
        "\n## Overview\n\nYou're a software engineer",
        "\nCurrent `/app` diff excerpt",
    ):
        if marker in issue:
            return issue.split(marker, 1)[0]
    return issue


def _issue_sentences(issue: str) -> list[str]:
    lines: list[str] = []
    for raw_line in public_issue_text_for_coverage(issue).replace("\r\n", "\n").splitlines():
        line = _clean_issue_sentence(raw_line)
        if not line or line.startswith("```"):
            continue
        if len(line) > 320:
            for part in re.split(r"(?<=[.!?])\s+", line):
                cleaned = _clean_issue_sentence(part)
                if cleaned:
                    lines.append(cleaned)
        else:
            lines.append(line)
    return lines


def _issue_explicit_requirement_bullets(issue: str) -> list[str]:
    """Extract visible Requirements: bullets as first-class coverage items."""

    bullets: list[str] = []
    current: list[str] = []
    in_requirements = False
    for raw_line in public_issue_text_for_coverage(issue).replace("\r\n", "\n").splitlines():
        stripped = raw_line.strip()
        if re.match(r"^requirements?\s*:\s*$", stripped, flags=re.IGNORECASE):
            in_requirements = True
            continue
        if not in_requirements:
            continue
        if not stripped:
            continue
        if re.match(r"^(#{1,6}\s+|\w[\w -]{0,80}:\s*$)", stripped) and not re.match(
            r"^([-*]|\d+[.)])\s+", stripped
        ):
            break
        bullet_match = re.match(r"^([-*]|\d+[.)])\s+(.*)$", stripped)
        if bullet_match:
            if current:
                cleaned = _clean_issue_sentence(" ".join(current))
                if cleaned:
                    bullets.append(cleaned)
            current = [bullet_match.group(2)]
            continue
        if current:
            current.append(stripped)
    if current:
        cleaned = _clean_issue_sentence(" ".join(current))
        if cleaned:
            bullets.append(cleaned)
    return bullets


def _issue_sentence_keywords(sentence: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for code in re.findall(r"`([^`]{2,80})`", sentence):
        token = re.sub(r"[^A-Za-z0-9_./-]+", "", code).strip("./-").lower()
        if token and len(token) >= 3 and token not in seen:
            seen.add(token)
            keywords.append(token)
    for camel in re.findall(r"\b[A-Za-z]+[A-Z][A-Za-z0-9_]*\b", sentence):
        token = camel.lower()
        if token not in seen:
            seen.add(token)
            keywords.append(token)
    for word in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b", sentence.lower()):
        if word in ISSUE_COVERAGE_KEYWORDS and word not in seen:
            seen.add(word)
            keywords.append(word)
    return keywords[:8]


def _issue_requirement_id(keywords: list[str], index: int) -> str:
    parts = [re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") for keyword in keywords[:3]]
    parts = [part for part in parts if part]
    return "issue-" + "-".join(parts or [f"item-{index}"])


def _fallback_issue_keywords(sentence: str, existing: list[str]) -> list[str]:
    if existing:
        return existing
    stopwords = {
        "and",
        "are",
        "for",
        "from",
        "into",
        "only",
        "should",
        "that",
        "the",
        "their",
        "this",
        "via",
        "when",
        "with",
    }
    keywords: list[str] = []
    seen: set[str] = set()
    for word in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{3,}\b", sentence.lower()):
        if word in stopwords or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= 5:
            break
    return keywords


def issue_coverage_requirements(issue: str) -> list[dict[str, object]]:
    """Derive public issue coverage requirements without evaluator metadata."""

    requirements: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_summaries: set[str] = set()

    def add_requirement(sentence: str, *, explicit: bool = False) -> None:
        summary = _clean_issue_sentence(sentence)
        if not summary or summary.lower() in seen_summaries:
            return
        keywords = _issue_sentence_keywords(summary)
        if explicit:
            keywords = _fallback_issue_keywords(summary, keywords)
        elif len(keywords) < 2:
            return
        requirement_id = _issue_requirement_id(keywords, len(requirements) + 1)
        if requirement_id in seen_ids:
            suffix = 2
            base_id = requirement_id
            while requirement_id in seen_ids:
                requirement_id = f"{base_id}-{suffix}"
                suffix += 1
        seen_ids.add(requirement_id)
        seen_summaries.add(summary.lower())
        requirements.append(
            {
                "id": requirement_id,
                "summary": summary[:320] if explicit else summary[:220],
                "keywords": keywords,
            }
        )

    for bullet in _issue_explicit_requirement_bullets(issue):
        add_requirement(bullet, explicit=True)

    for sentence in _issue_sentences(issue):
        lower = sentence.lower()
        if not any(trigger in lower for trigger in ISSUE_COVERAGE_TRIGGER_WORDS):
            continue
        add_requirement(sentence, explicit=False)
    return requirements[:40]


def issue_coverage_blockers(issue: str, evidence_text: str) -> list[str]:
    requirements = issue_coverage_requirements(issue)
    if len(requirements) < 2:
        return []
    lower = evidence_text.lower()
    if "issue-coverage-ledger:" not in lower:
        return [
            "public issue describes multiple independent contracts, but final validation lacks `issue-coverage-ledger:` "
            "mapping each issue-stated behavior to a source change, source-level already-satisfied proof, or blocking todo"
        ]
    ledger_text = lower.split("issue-coverage-ledger:", 1)[1]
    weak_markers = sorted(marker for marker in ISSUE_COVERAGE_WEAK_CLOSURE_MARKERS if marker in ledger_text)
    if weak_markers:
        return [
            "`issue-coverage-ledger:` closes public issue coverage with weak non-evidence marker(s): "
            + ", ".join(weak_markers[:8])
            + "; use `implemented-by=PATH`, source-specific `already-satisfied-by=PATH/evidence`, or `blocking-todo=ID` instead"
        ]
    missing: list[str] = []
    for requirement in requirements:
        keywords = [str(keyword).lower() for keyword in requirement.get("keywords", [])]
        if not any(keyword in ledger_text for keyword in keywords):
            missing.append(str(requirement.get("id") or requirement.get("summary") or "issue item"))
    if missing:
        return [
            "`issue-coverage-ledger:` does not account for public issue coverage item(s): "
            + ", ".join(missing[:8])
            + "; do not accept a one-symptom patch until every issue-stated contract is implemented, proved already satisfied, or queued as a blocking todo"
        ]
    return []


def data_provenance_required(issue: str) -> bool:
    """Return whether public task text requires state-to-output tracing."""

    normalized = " ".join(issue.lower().split())
    state_terms = r"(?:initial|original|existing|input|request|configuration|config|record|object|state)"
    transfer_terms = r"(?:copy|copied|copies|preserve|preserved|retains?|retained|carry|carried|propagate|propagated|derive|derived)"
    return bool(
        re.search(rf"{transfer_terms}.{{0,100}}{state_terms}", normalized)
        or re.search(rf"{state_terms}.{{0,100}}{transfer_terms}", normalized)
    )


def data_provenance_blockers(issue: str, evidence_text: str) -> list[str]:
    """Require source-visible dataflow evidence for copied/preserved outputs."""

    if not data_provenance_required(issue):
        return []
    lower = evidence_text.lower()
    if "data-provenance-ledger:" not in lower:
        return [
            "public task requires output copied, preserved, or derived from initial/original state, but final validation lacks "
            "`data-provenance-ledger:` with `source=`, `stored-as=`, `output=`, `field=`, and `analogue=` source evidence"
        ]
    ledger = lower.split("data-provenance-ledger:", 1)[1]
    missing = [key for key in ("source=", "stored-as=", "output=", "field=", "analogue=") if key not in ledger]
    if missing:
        return [
            "`data-provenance-ledger:` is incomplete; add "
            + ", ".join(missing)
            + " and trace every claimed copied/preserved output to stored input state plus the nearest source-visible analogous type/caller"
        ]
    return []


def historical_contract_required(issue: str) -> bool:
    """Return whether the public issue describes a transition-caused regression."""

    normalized = " ".join(issue.lower().split())
    transition = re.search(r"\b(upgrad(?:e|ed|ing)|migrat(?:e|ed|ion|ing)|compatibility transition|version)\b", normalized)
    regression = re.search(
        r"\b(regression|breaks?|broke|broken|lose|loses|lost|no longer|stale|after upgrading|introduced)\b",
        normalized,
    )
    return bool(transition and regression)


def historical_contract_blockers(issue: str, evidence_text: str) -> list[str]:
    """Require complete source-history evidence for transition regressions."""

    if not historical_contract_required(issue):
        return []
    lower = evidence_text.lower()
    if "historical-contract-ledger:" not in lower:
        return [
            "public task describes an upgrade/migration regression, but final validation lacks "
            "`historical-contract-ledger:` with `baseline-source=`, `transition-path=`, "
            "`mutated-outputs=`, and `compatibility-invariant=` source evidence"
        ]
    ledger = lower.split("historical-contract-ledger:", 1)[1]
    missing = [
        key
        for key in ("baseline-source=", "transition-path=", "mutated-outputs=", "compatibility-invariant=")
        if key not in ledger
    ]
    if missing:
        return [
            "`historical-contract-ledger:` is incomplete; add "
            + ", ".join(missing)
            + " and enumerate every persisted or emitted output changed by the transition"
        ]
    return []


def contract_ledger_text(issue: str, metadata: dict[str, object] | None = None) -> str:
    solver_metadata = public_solver_metadata(metadata or {})
    coverage_issue = issue_with_public_problem_text(issue, solver_metadata)
    contract = official_test_contract(solver_metadata)
    symbols = required_public_symbols(coverage_issue, solver_metadata)
    contract_excerpt = metadata_problem_text(solver_metadata)
    issue_requirements = issue_coverage_requirements(coverage_issue)
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
    if issue_requirements:
        sections.append("- Public issue coverage items:")
        for requirement in issue_requirements:
            sections.append(
                "  - "
                + requirement["id"]
                + ": "
                + requirement["summary"]
                + " [keywords="
                + ",".join(requirement["keywords"])
                + "]"
            )
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
            "- If the public issue lists multiple behavior contracts, final validation must include `issue-coverage-ledger:` mapping every public issue coverage item to a source change, source-level proof it was already satisfied, or a blocking todo.",
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


def contract_coverage_items_excerpt(
    issue: str,
    metadata: dict[str, object] | None = None,
    limit: int = 5000,
) -> str:
    requirements = issue_coverage_requirements(issue_with_public_problem_text(issue, metadata))
    if not requirements:
        return "No public issue coverage items were auto-derived."
    lines = [
        "Public issue coverage items that must be copied into worker/verifier checklists:",
    ]
    summary_limit = max(80, min(220, (limit // max(1, len(requirements))) - 80))
    for requirement in requirements:
        summary = str(requirement["summary"])
        if len(summary) > summary_limit:
            summary = summary[:summary_limit].rstrip() + "..."
        lines.append(
            "- "
            + str(requirement["id"])
            + ": "
            + summary
            + " [keywords="
            + ",".join(str(keyword) for keyword in requirement["keywords"])
            + "]"
        )
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return "\n".join(line[: max(120, limit // max(1, len(lines)))] for line in lines)


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
