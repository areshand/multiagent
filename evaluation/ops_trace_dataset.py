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
    r"|<codex_internal_context\b"
    r"|<subagent_notification>"
    r"|<task-notification>"
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

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)\s*[=:]\s*)"
    r"(?:\"[^\"]+\"|'[^']+'|[^\s,;}]+)"
)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{40,}(?![A-Za-z0-9])")
REQUEST_REDACTION_MARKERS = ("[TOKEN]", "[REDACTED PRIVATE KEY]")


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


def pseudonymize_replay(text: str, limit: int) -> str:
    result = pseudonymize(text, limit * 2)
    result = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        result,
        flags=re.DOTALL,
    )
    result = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----", "[REDACTED PRIVATE KEY]", result)
    result = SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", result)
    result = URL_RE.sub("[URL]", result)
    result = LONG_TOKEN_RE.sub("[TOKEN]", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if len(result) > limit:
        result = result[:limit].rstrip() + "\n[TRUNCATED]"
    return result


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    text = "\n".join(
        str(part.get("text") or part.get("input_text") or part.get("output_text") or "")
        for part in content
        if isinstance(part, dict)
    ).strip()
    marker = "## My request for Codex:"
    return text.rsplit(marker, 1)[1].strip() if marker in text else text


def _direct_request_text(text: str) -> str:
    """Remove Codex client context wrappers from an exported direct request."""
    marker = "## My request for Codex:"
    return text.rsplit(marker, 1)[1].strip() if marker in text else text.strip()


def _rollout_turns(path: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            record_type = record.get("type")
            event_type = payload.get("type")
            if record_type == "event_msg" and event_type == "task_started":
                if current is not None:
                    current["end_line"] = line_number - 1
                    turns.append(current)
                current = {
                    "start_line": line_number,
                    "end_line": line_number,
                    "user_parts": [],
                    "assistant": "",
                    "outputs": {},
                }
                continue
            if current is None:
                continue
            current["end_line"] = line_number
            if record_type == "response_item" and event_type == "message" and payload.get("role") == "user":
                message = _message_text(payload)
                if message and not message.startswith(("# AGENTS.md instructions", "<environment_context>")):
                    current["user_parts"].append(message)
            elif record_type == "event_msg" and event_type == "agent_message":
                if payload.get("phase") == "final_answer" and isinstance(payload.get("message"), str):
                    current["assistant"] = payload["message"]
            elif record_type == "response_item" and event_type in {
                "function_call_output",
                "custom_tool_call_output",
            }:
                call_id = payload.get("call_id")
                output = payload.get("output")
                if isinstance(call_id, str) and isinstance(output, str):
                    current["outputs"][call_id] = output
            elif record_type == "event_msg" and event_type == "task_complete":
                current["user"] = "\n\n".join(current.pop("user_parts"))
                turns.append(current)
                current = None
    if current is not None:
        current["user"] = "\n\n".join(current.pop("user_parts"))
        turns.append(current)
    return turns


def _source_context(request: dict[str, Any]) -> dict[str, Any] | None:
    source = request.get("source")
    source_line = request.get("source_line")
    if not isinstance(source, str) or not isinstance(source_line, int):
        return None
    path = Path(source)
    if not path.is_file():
        return None
    try:
        turns = _rollout_turns(path)
    except (OSError, UnicodeDecodeError):
        return None
    for index, turn in enumerate(turns):
        if int(turn["start_line"]) <= source_line <= int(turn["end_line"]):
            if not str(turn.get("assistant") or "").strip():
                return None
            history = []
            if index > 0:
                previous = turns[index - 1]
                previous_user = str(previous.get("user") or "").strip()
                previous_assistant = str(previous.get("assistant") or "").strip()
                if previous_user:
                    history.append(
                        {"role": "user", "content": pseudonymize_replay(previous_user, 900)}
                    )
                if previous_assistant:
                    history.append(
                        {
                            "role": "assistant",
                            "content": pseudonymize_replay(previous_assistant, 1_200),
                        }
                    )
            return {
                "history": history,
                "reference_response": pseudonymize_replay(str(turn["assistant"]), 2_500),
                "outputs": turn["outputs"],
                "rollout_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return None


def _mock_evidence(
    operations: list[dict[str, Any]],
    outputs: dict[str, str],
) -> str:
    sections = [
        "# Mock operations evidence",
        "",
        "These are pseudonymized results captured from the historical trace. Treat them as "
        "fixture data, not current production state. Do not repeat values marked redacted.",
    ]
    remaining = 6_000
    for index, operation in enumerate(operations, 1):
        operation_text = _flatten(operation.get("input"))
        actions, _risk = classify_actions(operation_text)
        services = sorted(services_in(operation_text)) or ["external"]
        call_id = str(operation.get("call_id") or "")
        raw_output = outputs.get(call_id, "")
        if "secret_access" in actions:
            rendered = "[REDACTED: secret-bearing operation output]"
        elif raw_output:
            rendered = pseudonymize_replay(raw_output, min(1_200, remaining))
        else:
            rendered = "[No captured output was available for this operation.]"
        block = (
            f"\n## Mock operation {index}\n\n"
            f"- Tool: {operation.get('tool_name') or 'external tool'}\n"
            f"- Services: {', '.join(services)}\n"
            f"- Action classes: {', '.join(actions)}\n\n"
            f"Result:\n\n```text\n{rendered}\n```\n"
        )
        if len(block) > remaining:
            break
        sections.append(block)
        remaining -= len(block)
    return "\n".join(sections).strip() + "\n"


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

    requests_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for request in requests:
        session = request.get("session_id")
        text = request.get("text")
        if (
            isinstance(session, str)
            and isinstance(text, str)
            and request.get("request_kind") == "direct_or_top_level"
            and not is_internal_agent_request(text)
            and not META_REQUEST_RE.search(text)
        ):
            requests_by_session[session].append(request)
    for session_requests in requests_by_session.values():
        session_requests.sort(key=lambda item: int(item.get("source_line") or 0))

    operations_by_request: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for operation in operations:
        if operation.get("record_type") != "tool_call":
            continue
        session = operation.get("session_id")
        source = operation.get("source")
        source_line = operation.get("source_line")
        if not isinstance(session, str) or not isinstance(source, str) or not isinstance(source_line, int):
            continue
        candidates = [
            request
            for request in requests_by_session.get(session, [])
            if request.get("source") == source
            and isinstance(request.get("source_line"), int)
            and int(request["source_line"]) < source_line
        ]
        if not candidates:
            continue
        owner = max(candidates, key=lambda item: int(item["source_line"]))
        owner_key = (session, source, int(owner["source_line"]))
        operations_by_request[owner_key].append(operation)

    correlation_services: dict[tuple[str, str], set[str]] = defaultdict(set)
    correlated_calls: set[tuple[str, str]] = set()
    for correlation in correlations:
        codex = correlation.get("codex")
        cloudtrail = correlation.get("cloudtrail")
        if not isinstance(codex, dict) or not isinstance(cloudtrail, dict):
            continue
        call_id = codex.get("call_id")
        session = codex.get("session_id")
        if not isinstance(call_id, str) or not isinstance(session, str):
            continue
        call_key = (session, call_id)
        correlated_calls.add(call_key)
        correlation_services[call_key].update(services_in(_flatten(cloudtrail)))

    cases = []
    for session, session_requests in requests_by_session.items():
        for request in session_requests:
            request_sha = request.get("text_sha256")
            if not isinstance(request_sha, str):
                continue
            request_source = request.get("source")
            request_line = request.get("source_line")
            if not isinstance(request_source, str) or not isinstance(request_line, int):
                continue
            request_operations = operations_by_request.get(
                (session, request_source, request_line), []
            )
            if not request_operations:
                continue
            context = _source_context(request)
            if context is None:
                continue
            request_text = _direct_request_text(str(request["text"]))
            safe_request = pseudonymize_replay(request_text, 1_600)
            # A redacted credential or opaque token in the actual request can
            # remove information needed to answer it. Keep those records out
            # of the benchmark instead of grading an unknowable reconstruction.
            if any(marker in safe_request for marker in REQUEST_REDACTION_MARKERS):
                continue
            operation_text = "\n".join(_flatten(item.get("input")) for item in request_operations)
            combined = request_text + "\n" + operation_text
            call_keys = {
                (session, str(item.get("call_id")))
                for item in request_operations
                if isinstance(item.get("call_id"), str)
            }
            services = services_in(combined)
            for call_key in call_keys:
                services.update(correlation_services.get(call_key, set()))
            if not services:
                services = {"aws"}
            action_classes, risk = classify_actions(operation_text or combined)
            digest = _stable_digest(salt, session, request_sha)
            case = {
                "id": f"trace-{digest[:12]}",
                "history": context["history"],
                "request": safe_request,
                "reference_response": context["reference_response"],
                "mock_evidence": _mock_evidence(request_operations, context["outputs"]),
                "services": sorted(services),
                "action_classes": list(action_classes),
                "risk": risk,
                "cloudtrail_correlated": bool(call_keys & correlated_calls),
                "split": "unassigned",
                "trace_session": f"session-{_stable_digest(salt, session)[:12]}",
                "source": {
                    "request_sha256": request_sha,
                    "rollout_sha256": context["rollout_sha256"],
                    "operation_records": len(request_operations),
                    "correlation_records": sum(
                        call_key in correlated_calls for call_key in call_keys
                    ),
                },
            }
            cases.append(case)

    if not cases:
        return []
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
            "pseudonymized_mock_outputs_included": True,
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
    source_prose = "\n".join(
        text
        for case in case_list
        for text in (
            str(case.get("request", "")),
            str(case.get("reference_response", "")),
            str(case.get("mock_evidence", "")),
            *(str(item.get("content", "")) for item in case.get("history", [])),
        )
    )
    leaked = [pattern.pattern for pattern in FORBIDDEN_OUTPUT if pattern.search(source_prose)]
    if leaked:
        raise ValueError(f"privacy validation failed; matched {len(leaked)} forbidden patterns")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.chmod(0o600)
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
