from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from evaluation.support.cli import multiagent_subcommand

from .swe_prod_contracts import (
    CONTRACT_LEDGER_PATH,
    HELPER_PROBE_PATH,
    RUNTIME_ROOT,
    contract_ledger_excerpt,
    run,
)
from .swe_prod_evidence import final_diff_sha256, subagent_state_roots
from .swe_prod_guardrails import helper_scope_hints
from .swe_prod_validation import source_symbol_map_resume_instructions

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
    summary: str,
    blockers: list[str],
    source_hints: list[str],
) -> str:
    """Return no-leak verifier-first routing for adapter evidence gaps.

    Adapter checks can prove that acceptance evidence is missing, but they are
    not semantic verifiers and must not manufacture source-repair todos. A fresh
    verifier first decides whether the diff is acceptable or needs source work.
    """

    blocker_text = "; ".join(blockers)[:1800] or summary
    hint_text = ", ".join(source_hints[:8]) or "derive exact paths from the live diff"
    confirmed_finding = persisted_verifier_blocking_evidence(RUNTIME_ROOT)
    if confirmed_finding:
        if verifier_evidence_is_runtime_validation_only(confirmed_finding):
            return (
                "The durable verifier evidence reports no source contract miss and blocks only on a runtime-environment "
                "test failure after hash-bound compile success. Do not create a source-repair todo and do not make the "
                "known environment-failing full test a mandatory rc=0 command. Spawn one fresh read-only behavior "
                "verifier over the unchanged final diff. It must independently recheck every public/source contract, "
                "preserve the full-test failure as runtime evidence, and either ACCEPT with explicit runtime-failure "
                "classification plus the existing build proof or emit a concrete source-level finding. "
                f"Verifier evidence: {confirmed_finding}"
            )
        return (
            "A verifier already confirmed a semantic source defect. Preserve its public/source evidence exactly; "
            "do not relabel this as verifier infrastructure and do not launch another acceptance-only verifier over the unchanged diff. "
            "Normalize the verifier evidence into finding-create, create a todo whose done criteria include the stated required resolution, "
            "and, when the handoff already records a systemic full-test runtime failure, make the exact-hash compile fallback the required "
            "rc=0 command instead of the known environment-failing full suite. Keep that failed full command as context evidence only. "
            "assign one bounded source worker, require resolution-create with validation, then launch a fresh verifier over the repaired diff. "
            f"Verifier-confirmed evidence: {confirmed_finding}"
        )
    return (
        "Treat this adapter result as a verification handoff, not a confirmed source finding. "
        "Do not create an adapter-authored finding/todo merely because acceptance evidence is missing. "
        "Spawn one fresh read-only verifier over the exact live final diff and give it these public/source blockers: "
        f"{blocker_text}. Candidate paths: {hint_text}. "
        "If the verifier returns ACCEPTED with the exact final diff hash, rerun gate-check; no worker resolution is required. "
        "If and only if the verifier confirms a semantic source defect, the verifier must record finding-create evidence, "
        "the orchestrator must create a todo from that finding, a bounded worker must call resolution-create TODO_ID "
        "--worker NAME --status resolved|blocked --validation-json JSON --why TEXT, and a later verifier must close it. "
        "Never call resolution-create for an evidence-only handoff or for a todo that no worker repaired."
    )


def verifier_evidence_is_runtime_validation_only(evidence: str) -> bool:
    """Return true for verifier blockers that explicitly clear source behavior."""

    lower = evidence.lower()
    validation_finding = "type: validation" in lower or "type=validation" in lower
    runtime_failure = any(
        marker in lower
        for marker in (
            "runtime-environment",
            "runtime fixture",
            "tls bad-record-mac",
            "tls bad record mac",
            "local error: tls: bad record mac",
            "missing runtime asset",
            "missing runtime fixture",
        )
    )
    source_cleared = any(
        marker in lower
        for marker in (
            "source review found no contract miss",
            "no source contract miss",
            "all public source-level clauses",
            "all listed source-level clauses",
            "source_contracts_satisfied=true",
        )
    )
    compile_clean = "build-verification-passed:" in lower and any(
        marker in lower for marker in ("returncode=0", "return code: 0", "rc=0")
    )
    return validation_finding and runtime_failure and source_cleared and compile_clean


def persisted_verifier_blocking_evidence(runtime_root: Path = RUNTIME_ROOT) -> str:
    """Return the newest durable verifier-confirmed semantic blocker."""

    candidates: list[tuple[int, str]] = []
    for state_dir in (runtime_root, runtime_root / "state"):
        findings_dir = state_dir / "findings"
        if not findings_dir.is_dir():
            continue
        for path in findings_dir.glob("*/finding.json"):
            try:
                finding = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                mtime = path.stat().st_mtime_ns
            except (OSError, json.JSONDecodeError):
                continue
            if str(finding.get("severity", "")).lower() != "blocking":
                continue
            affected_paths = finding.get("affected_paths")
            required_resolution = str(finding.get("required_resolution", "")).strip()
            if not isinstance(affected_paths, list) or not affected_paths or not required_resolution:
                continue
            finding_id = str(finding.get("id") or path.parent.name)
            excerpt = json.dumps(finding, sort_keys=True, separators=(",", ":"))
            candidates.append((mtime, f"structured finding {finding_id}: {excerpt}"))
    for subagents_dir in subagent_state_roots(runtime_root):
        for agent_dir in subagents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            agent_name = agent_dir.name.lower()
            if "verifier" not in agent_name and "review" not in agent_name:
                continue
            path = agent_dir / "last-message.txt"
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
                mtime = path.stat().st_mtime_ns
            except OSError:
                continue
            lower = raw.lower()
            blocking = bool(
                re.search(r"(?im)^\s*(?:blocking|verdict\s*[:=]\s*(?:blocking|rejected))\s*$", raw)
                or "blocking-finding:" in lower
            )
            if not blocking or not any(
                marker in lower
                for marker in ("required_resolution", "required resolution", "affected_paths", "affected paths")
            ):
                continue
            excerpt = " ".join(raw[-3000:].split())
            candidates.append((mtime, f"{agent_dir.name}: {excerpt}"))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: item[0])[1][:2400]


def verifier_blocking_handoff_key(
    current_status: dict[str, object],
    diff: str,
    seen_keys: set[str],
    runtime_root: Path = RUNTIME_ROOT,
) -> str:
    """Identify one unhandled durable semantic finding on a terminal diff."""

    if str(current_status.get("status", "")).lower() != "blocked" or not diff.strip():
        return ""
    evidence = persisted_verifier_blocking_evidence(runtime_root)
    if not evidence:
        return ""
    key = hashlib.sha256(
        (final_diff_sha256(diff) + "\n" + evidence).encode("utf-8", errors="replace")
    ).hexdigest()
    return "" if key in seen_keys else key


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
        + "Do not use tmux send-keys to send implementation instructions to a completed worker pane; create a fresh assignment and `multiagent subagent spawn` a new worker process. "
        + source_symbol_map_resume_instructions(blockers)
        + " "
        + structured_repair_state_instructions(
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
        "If a worker is currently running, poll or inspect it once, then force a terminal worker action: apply a narrow source patch now, "
        "emit `required-path-outside-owned: RELATIVE_PATH`, emit `validation-repair-needed:` with the exact blocker, or write blocked status with the concrete source-visible reason. "
        "Do not let a live worker continue read-only source mapping without either editing or reporting an exact blocker. "
        "If a read-only scout is still active, poll or inspect it once, persist useful findings, then finalize or kill the scout before spawning an edit-capable implementation worker. "
        "Restate the intended behavior, choose the narrowest likely source files from issue text, visible tests, docs, "
        "source callers, public APIs, data schemas, fixtures, and runtime behavior, then spawn exactly one bounded "
        "implementation worker over those paths with `replacement-no-diff-attempt=1` if this is replacing a no-diff worker. "
        "If no plausible source path can be identified from legitimate evidence, write blocked status with the concrete "
        "discovery gap. If that one same-owned-path replacement also produces no source diff and no exact outside-owned "
        "path/source blocker, write blocked status with the no-diff worker names instead of spawning worker-03/worker-04 "
        "over the same paths. Do not keep spawning read-only scouts or duplicate workers over the same package without a "
        "new source-derived finding, failed validation command, or verifier finding. Do not use leaked evaluator rows, benchmark scores, "
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

    subagent = multiagent_subcommand(repo_root, "subagent")
    if not subagent:
        raise RuntimeError(f"Rust multiagent executable not found under {repo_root}")
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
        "Do not edit tests, generated assets, bundled assets, or unrelated config. A minimal dependency checksum file may change only when the visible source API migration directly requires it and affected-package validation proves the need.\n\n"
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
            *subagent,
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
        [*subagent, "spawn", worker_name, "--instruction", instruction],
        cwd=repo_root,
        env=env,
        timeout=120,
        check=True,
    )
    return worker_name
