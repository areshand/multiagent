#!/usr/bin/env python3
"""Generate private, pseudonymized multiagent-ops benchmark cases from traces."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from evaluation.tasks.ops_trace import OPS_TRACE_CONTRACT_VERSION


SERVICE_PATTERNS = {
    "cloudtrail": re.compile(r"(?i)cloudtrail"),
    "cloudwatch": re.compile(r"(?i)cloudwatch|monitoring\.amazonaws\.com"),
    "ec2": re.compile(r"(?i)\bec2\b|ec2\.amazonaws\.com"),
    "ecr": re.compile(r"(?i)\becr\b|ecr\.amazonaws\.com"),
    "eks": re.compile(r"(?i)\beks\b|eks\.amazonaws\.com|kubectl|helm"),
    "health": re.compile(r"(?i)health\.amazonaws\.com"),
    "iam": re.compile(r"(?i)\biam\b|iam\.amazonaws\.com"),
    "kms": re.compile(r"(?i)\bkms\b|kms\.amazonaws\.com"),
    "lambda": re.compile(r"(?i)\blambda\b|lambda\.amazonaws\.com"),
    "logs": re.compile(r"(?i)\blogs\b|logs\.amazonaws\.com|loki"),
    "rds": re.compile(r"(?i)\brds\b|rds\.amazonaws\.com"),
    "route53": re.compile(r"(?i)route\s*53|route53\.amazonaws\.com"),
    "s3": re.compile(r"(?i)\bs3\b|s3://|s3\.amazonaws\.com"),
    "secretsmanager": re.compile(r"(?i)secretsmanager|secrets\s+manager"),
    "ssm": re.compile(r"(?i)\bssm\b|ssm\.amazonaws\.com"),
    "sts": re.compile(r"(?i)\bsts\b|sts\.amazonaws\.com|get-caller-identity|assume-role"),
}

MUTATION_RE = re.compile(
    r"(?i)(?:\b(?:create|update|delete|put|attach|detach|terminate|modify|associate|disassociate|"
    r"start|stop|reboot|restart|rotate|enable|disable|set|apply|destroy)[-_ ]"
    r"|\b(?:terraform|tofu)\s+apply\b|\bkubectl\s+(?:apply|delete|patch|rollout|scale)\b)"
)
DESTRUCTIVE_RE = re.compile(r"(?i)\b(?:delete|terminate|destroy|detach|disable|revoke)[-_ ]")
READ_RE = re.compile(r"(?i)\b(?:get|list|describe|lookup|head|show|read|query|scan)[-_ ]")
SECRET_RE = re.compile(r"(?i)get-secret-value|decrypt|\bkms\s+sign\b|secret_access|private[_-]?key")
IDENTITY_RE = re.compile(r"(?i)get-caller-identity|assume-role|\biam\b|\bsts\b")
DEPLOYMENT_RE = re.compile(r"(?i)\b(?:kubectl|helm|eksctl|terraform|tofu)\b")
META_REQUEST_RE = re.compile(
    r"(?i)(?:find all the traces of my requests|trace export|create .*benchmark.*trace|benchmark.*using .*trace|"
    r"benchmark.*multi-agent|multi-agent.*benchmark|\bbenchmarks?\b)"
)
INTERNAL_AGENT_REQUEST_RE = re.compile(
    r"(?is)^\s*(?:"
    r"----- BEGIN (?:ORCHESTRATOR|WORKER|VERIFIER|REVIEWER|SCOUT|OPS)[^\n]* ROLE -----"
    r"|# Multiagent Role Bundle:"
    r"|<codex_delegation>"
    r"|You are Subagent\b"
    r"|You are (?:an?\s+|the\s+)?(?:[a-z-]+\s+)?"
    r"(?:worker|verifier|reviewer|scout|ops|subagent) agent (?:launched by|assigned by)"
    r"|You are (?:auditing|reviewing) PR\s+#\d+\b"
    r"|Read and follow the assignment in\b"
    r"|You are working on [^\n]+ PR\s+#\d+\b"
    r"|Follow-up for [^\n]+.*?You are still the [^\n]*worker\b"
    r")"
)

PRIVACY_REPLACEMENTS = (
    (re.compile(r"(?i)arn:aws[^\s`\"']+"), "[ARN]"),
    (re.compile(r"\b\d{12}\b"), "[ACCOUNT]"),
    (re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"), "[ACTOR]"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "[ID]"),
    (re.compile(r"(?<![\w.])(?:/Users|/home|/private/tmp|/tmp)/[^\s`\"']+"), "[LOCAL_PATH]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"(?i)(--profile(?:=|\s+))(?:'[^']+'|\"[^\"]+\"|[^\s;&|]+)"), r"\1[PROFILE]"),
)

FORBIDDEN_OUTPUT = (
    re.compile(r"(?i)arn:aws"),
    re.compile(r"\b\d{12}\b"),
    re.compile(r"(?i)\b(?:AKIA|ASIA)[A-Z0-9]{12,20}\b"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{lineno}: {exc}") from exc
            if isinstance(value, dict):
                records.append(value)
    return records


def _flatten(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_flatten(item) for item in value)
    return "" if value is None else str(value)


def pseudonymize(text: str, limit: int = 1600) -> str:
    result = text
    for pattern, replacement in PRIVACY_REPLACEMENTS:
        result = pattern.sub(replacement, result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if len(result) > limit:
        result = result[:limit].rstrip() + "\n[TRUNCATED]"
    return result


def is_internal_agent_request(text: str) -> bool:
    """Reject role prompts emitted by orchestrators rather than authenticated users."""
    return bool(INTERNAL_AGENT_REQUEST_RE.search(text))


def services_in(text: str) -> set[str]:
    return {service for service, pattern in SERVICE_PATTERNS.items() if pattern.search(text)}


def classify_actions(text: str) -> tuple[tuple[str, ...], str]:
    actions = set()
    if READ_RE.search(text) or not MUTATION_RE.search(text):
        actions.add("read")
    if MUTATION_RE.search(text):
        actions.add("mutation")
    if DESTRUCTIVE_RE.search(text):
        actions.add("destructive")
    if SECRET_RE.search(text):
        actions.add("secret_access")
    if IDENTITY_RE.search(text):
        actions.add("identity")
    if DEPLOYMENT_RE.search(text):
        actions.add("deployment")

    if "destructive" in actions or "mutation" in actions:
        risk = "high"
    elif actions & {"secret_access", "identity", "deployment"}:
        risk = "elevated"
    else:
        risk = "read_only"
    return tuple(sorted(actions)), risk


def _stable_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


def _assign_stratified_splits(cases: list[dict[str, Any]]) -> None:
    """Assign deterministic splits while retaining rare risk/evidence strata."""
    strata: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        strata[(str(case["risk"]), bool(case["cloudtrail_correlated"]))].append(case)
    for group in strata.values():
        group.sort(key=lambda item: item["id"])
        count = len(group)
        validation_count = max(1, round(count * 0.15)) if count >= 3 else 0
        test_count = max(1, round(count * 0.15)) if count >= 3 else 0
        for index, case in enumerate(group):
            if index < test_count:
                case["split"] = "test"
            elif index < test_count + validation_count:
                case["split"] = "validation"
            else:
                case["split"] = "train"


def _balanced_risks(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        groups[(str(case["risk"]), bool(case["cloudtrail_correlated"]))].append(case)
    for group in groups.values():
        group.sort(key=lambda item: item["id"])
    selected = []
    keys = [
        (risk, correlated)
        for correlated in (True, False)
        for risk in ("high", "elevated", "read_only")
        if groups[(risk, correlated)]
    ]
    while keys and len(selected) < limit:
        next_keys = []
        for key in keys:
            if groups[key] and len(selected) < limit:
                selected.append(groups[key].pop(0))
            if groups[key]:
                next_keys.append(key)
        keys = next_keys
    return selected


def _balanced(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    quotas = {
        "train": max(1, round(limit * 0.7)),
        "validation": max(1, round(limit * 0.15)),
    }
    quotas["test"] = max(1, limit - quotas["train"] - quotas["validation"])
    while sum(quotas.values()) > limit:
        quotas["train"] -= 1

    selected = []
    selected_ids = set()
    for split in ("train", "validation", "test"):
        pool = [case for case in cases if case["split"] == split]
        chosen = _balanced_risks(pool, quotas[split])
        selected.extend(chosen)
        selected_ids.update(case["id"] for case in chosen)
    if len(selected) < limit:
        remaining = [case for case in cases if case["id"] not in selected_ids]
        selected.extend(_balanced_risks(remaining, limit - len(selected)))
    return sorted(selected[:limit], key=lambda item: item["id"])


def build_cases(traces: Path, max_cases: int = 24, salt: str = "ops-trace-v1") -> list[dict[str, Any]]:
    requests = _load_jsonl(traces / "codex-requests.jsonl")
    operations = _load_jsonl(traces / "codex-aws-operations.jsonl")
    correlations = _load_jsonl(traces / "codex-cloudtrail-correlations.jsonl")

    internal_sessions = {
        str(request.get("session_id"))
        for request in requests
        if isinstance(request.get("session_id"), str)
        and isinstance(request.get("text"), str)
        and is_internal_agent_request(str(request["text"]))
    }
    requests_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for request in requests:
        session = request.get("session_id")
        text = request.get("text")
        if (
            isinstance(session, str)
            and session not in internal_sessions
            and isinstance(text, str)
            and not META_REQUEST_RE.search(text)
        ):
            requests_by_session[session].append(request)

    operation_text_by_session: dict[str, list[str]] = defaultdict(list)
    for operation in operations:
        if operation.get("record_type") != "tool_call":
            continue
        session = operation.get("session_id")
        if isinstance(session, str):
            operation_text_by_session[session].append(_flatten(operation.get("input")))

    correlation_services: dict[str, set[str]] = defaultdict(set)
    correlated_sessions = set()
    for correlation in correlations:
        codex = correlation.get("codex")
        cloudtrail = correlation.get("cloudtrail")
        if not isinstance(codex, dict) or not isinstance(cloudtrail, dict):
            continue
        session = codex.get("session_id")
        if not isinstance(session, str):
            continue
        correlated_sessions.add(session)
        correlation_services[session].update(services_in(_flatten(cloudtrail)))

    cases = []
    for session, session_requests in requests_by_session.items():
        operation_text = "\n".join(operation_text_by_session.get(session, []))
        if not operation_text and session not in correlated_sessions:
            continue
        preferred = sorted(
            session_requests,
            key=lambda item: (
                item.get("request_kind") != "direct_or_top_level",
                abs(len(str(item.get("text", ""))) - 500),
                str(item.get("timestamp_utc", "")),
            ),
        )[0]
        request_text = str(preferred["text"])
        combined = request_text + "\n" + operation_text
        services = services_in(combined) | correlation_services.get(session, set())
        if not services:
            services = {"aws"}
        action_classes, risk = classify_actions(operation_text or combined)
        digest = _stable_digest(salt, session, str(preferred.get("text_sha256", "")))
        case = {
            "id": f"trace-{digest[:12]}",
            "request": pseudonymize(request_text),
            "services": sorted(services),
            "action_classes": list(action_classes),
            "risk": risk,
            "cloudtrail_correlated": session in correlated_sessions,
            "split": "unassigned",
            "trace_session": f"session-{_stable_digest(salt, session)[:12]}",
            "source": {
                "request_sha256": preferred.get("text_sha256"),
                "operation_records": len(operation_text_by_session.get(session, [])),
                "correlation_records": sum(
                    1
                    for correlation in correlations
                    if isinstance(correlation.get("codex"), dict)
                    and correlation["codex"].get("session_id") == session
                ),
            },
        }
        cases.append(case)

    _assign_stratified_splits(cases)
    return _balanced(cases, max_cases)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_dataset(traces: Path, output: Path, cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    case_list = list(cases)
    manifest_path = traces / "manifest.json"
    payload = {
        "format_version": 1,
        "benchmark": "ops-trace",
        "scoring_contract_version": OPS_TRACE_CONTRACT_VERSION,
        "private": True,
        "publishable": False,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "trace_manifest_sha256": _sha256(manifest_path),
            "request_file_sha256": _sha256(traces / "codex-requests.jsonl"),
            "operation_file_sha256": _sha256(traces / "codex-aws-operations.jsonl"),
            "correlation_file_sha256": _sha256(traces / "codex-cloudtrail-correlations.jsonl"),
        },
        "privacy": {
            "raw_commands_included": False,
            "raw_outputs_included": False,
            "account_ids_included": False,
            "arns_included": False,
            "emails_included": False,
            "note": "Case summaries remain private because request text may contain organization-specific context.",
        },
        "counts": {
            "cases": len(case_list),
            "by_split": {
                split: sum(case["split"] == split for case in case_list)
                for split in ("train", "validation", "test")
            },
            "by_risk": {
                risk: sum(case["risk"] == risk for case in case_list)
                for risk in ("read_only", "elevated", "high")
            },
        },
        "cases": case_list,
    }
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    # Validate only source-derived prose. Stable SHA-256 fields and pseudonymous
    # case IDs may naturally contain twelve consecutive digits without being an
    # AWS account identifier.
    source_prose = "\n".join(str(case.get("request", "")) for case in case_list)
    leaked = [pattern.pattern for pattern in FORBIDDEN_OUTPUT if pattern.search(source_prose)]
    if leaked:
        raise ValueError(f"privacy validation failed; matched {len(leaked)} forbidden patterns")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a private ops-trace benchmark dataset")
    parser.add_argument("--traces", default=str(Path.home() / "projects" / "traces"))
    parser.add_argument(
        "--output",
        default=str(Path.home() / "projects" / "traces" / "ops-trace-cases.json"),
    )
    parser.add_argument("--max-cases", type=int, default=24)
    parser.add_argument("--salt", default="ops-trace-v1")
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("--max-cases must be positive")

    traces = Path(args.traces).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    cases = build_cases(traces, max_cases=args.max_cases, salt=args.salt)
    if not cases:
        raise SystemExit("no usable trace-derived cases found")
    payload = write_dataset(traces, output, cases)
    print(json.dumps({"output": str(output), **payload["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
