#!/usr/bin/env python3
"""Generate a private conversational workflow dataset from local Codex rollouts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from evaluation.ops_trace_dataset import FORBIDDEN_OUTPUT, pseudonymize
from evaluation.tasks.conversation_trace import CONVERSATION_TRACE_CONTRACT_VERSION


SAFE_TOOLS = {
    "list_mcp_resource_templates",
    "list_mcp_resources",
    "read_mcp_resource",
    "view_image",
}
SAFE_PROGRAMS = {
    "basename",
    "cat",
    "cut",
    "dirname",
    "file",
    "find",
    "grep",
    "head",
    "jq",
    "ls",
    "pwd",
    "realpath",
    "rg",
    "sed",
    "sort",
    "stat",
    "tail",
    "tr",
    "uniq",
    "wc",
}
SAFE_GIT = {"branch", "diff", "grep", "log", "ls-files", "rev-parse", "show", "status"}
SHELL_WRITE_RE = re.compile(
    r"(?:^|\s)(?:>|>>|2>|&>|tee\b|rm\b|mv\b|cp\b|touch\b|mkdir\b|install\b|"
    r"apply_patch\b|git\s+(?:add|commit|switch|checkout|reset|restore|clean|rebase|merge|cherry-pick)\b)"
)
EXTERNAL_OR_MUTATING_REQUEST_RE = re.compile(
    r"(?i)\b(?:approv(?:e|ed|al|ing)|apply|create\s+(?:a\s+)?pr|delet(?:e|ed|ing)|"
    r"deploy(?:ed|ing|ment)?|disabl(?:e|ed|ing)|enabl(?:e|ed|ing)|merg(?:e|ed|ing)|"
    r"push(?:ed|ing)?|remov(?:e|ed|ing)|restart(?:ed|ing)?|rotat(?:e|ed|ing)|ship(?:ped|ping)?|"
    r"start(?:ed|ing)?|stop(?:ped|ping)?|terminat(?:e|ed|ing)|updat(?:e|ed|ing)|writ(?:e|ing))\b"
)
SECRET_REQUEST_RE = re.compile(
    r"(?i)(?:password|passwd|private[_ -]?key|secret|api[_ -]?key|access[_ -]?token|"
    r"credentials?|\.env\b)"
)
DELEGATION_REQUEST_RE = re.compile(
    r"(?i)\b(?:committee|subagents?|agent team|swarm|review(?:er|ers)?\s+the\s+plan)\b"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:password|passwd|secret|token|api[_ -]?key|private[_ -]?key)\s*[=:]\s*)"
    r"(?:\"[^\"]+\"|'[^']+'|[^\s,;}]+)"
)
LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_./+=-]{40,}(?![A-Za-z0-9])")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\(https?://[^)]+\)")
URL_RE = re.compile(r"https?://[^\s)>\]]+")
REWRITE_REQUEST_RE = re.compile(r"(?i)^\s*(?:revise|rewrite|translate|polish|rephrase)\b")
CLARIFICATION_CUE_RE = re.compile(
    r"(?i)(?:\bwhich\b|\bwhat\b|\bwhere\b|\bwhen\b|\bwho\b|\bdo you mean\b|"
    r"\bcan you (?:share|provide|specify)\b|\bcould you (?:clarify|share|provide|specify)\b|"
    r"\bdo you want\b|\bshould I\b|\bwould you like\b|\bplease specify\b|"
    r"请问|哪个|哪一个|你的意思|请确认)"
)
INFRASTRUCTURE_MESSAGE_PREFIXES = (
    "# AGENTS.md instructions",
    "<environment_context>",
    "<recommended_plugins>",
)


def _stable_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8", errors="replace"))
        digest.update(b"\0")
    return digest.hexdigest()


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
    if marker in text:
        text = text.rsplit(marker, 1)[1].strip()
    return text


def pseudonymize_conversation(text: str, limit: int) -> str:
    result = pseudonymize(text, limit * 2)
    result = SENSITIVE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", result)
    result = MARKDOWN_LINK_RE.sub(r"\1 [URL]", result)
    result = URL_RE.sub("[URL]", result)
    result = LONG_TOKEN_RE.sub("[TOKEN]", result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if len(result) > limit:
        result = result[:limit].rstrip() + "\n[TRUNCATED]"
    return result


def is_read_only_command(raw: str) -> bool:
    if not raw.strip() or SHELL_WRITE_RE.search(raw):
        return False
    if any(marker in raw for marker in ("&&", "||", ";", "`", "$(", "\n")):
        return False
    for segment in (part.strip() for part in raw.split("|")):
        try:
            words = shlex.split(segment)
        except ValueError:
            return False
        while words and "=" in words[0] and not words[0].startswith("-"):
            words.pop(0)
        if not words:
            return False
        program = Path(words[0]).name
        if program == "git":
            if len(words) < 2 or words[1] not in SAFE_GIT:
                return False
        elif program not in SAFE_PROGRAMS:
            return False
        if program == "sed" and any(word.startswith("-i") for word in words[1:]):
            return False
    return True


def _call_access(name: str, arguments: Any) -> str:
    if name in SAFE_TOOLS:
        return "read"
    if name != "exec_command":
        return "write_or_external"
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return "write_or_external"
    command = parsed.get("cmd") if isinstance(parsed, dict) else None
    return "read" if isinstance(command, str) and is_read_only_command(command) else "write_or_external"


def parse_rollout(path: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
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
                if current and current["user_parts"]:
                    current["user"] = "\n\n".join(current.pop("user_parts"))
                    turns.append(current)
                current = {
                    "timestamp": str(record.get("timestamp") or ""),
                    "user_parts": [],
                    "assistant": "",
                    "calls": [],
                }
                continue
            if current is None:
                continue
            if record_type == "response_item" and event_type == "message" and payload.get("role") == "user":
                message = _message_text(payload)
                if message and not message.startswith(INFRASTRUCTURE_MESSAGE_PREFIXES):
                    current["user_parts"].append(message)
            elif record_type == "event_msg" and event_type == "agent_message":
                if payload.get("phase") == "final_answer" and isinstance(payload.get("message"), str):
                    current["assistant"] = payload["message"]
            elif record_type == "response_item" and event_type == "function_call":
                name = str(payload.get("name") or "")
                current["calls"].append((name, _call_access(name, payload.get("arguments"))))
            elif record_type == "event_msg" and event_type == "patch_apply_end":
                current["calls"].append(("apply_patch", "write_or_external"))
            elif record_type == "event_msg" and event_type == "task_complete":
                if current["user_parts"]:
                    current["user"] = "\n\n".join(current.pop("user_parts"))
                    turns.append(current)
                current = None
    if current and current["user_parts"]:
        current["user"] = "\n\n".join(current.pop("user_parts"))
        turns.append(current)
    return turns


def _response_kind(turn: dict[str, Any], has_followup: bool) -> str | None:
    calls = turn["calls"]
    assistant = str(turn["assistant"]).strip()
    if not assistant:
        return None
    if not calls:
        if (
            has_followup
            and not REWRITE_REQUEST_RE.search(str(turn["user"]))
            and assistant.rstrip().endswith(("?", "？"))
            and CLARIFICATION_CUE_RE.search(
                next((line for line in reversed(assistant.splitlines()) if line.strip()), "")
            )
        ):
            return "clarification"
        return "answer"
    if all(access == "read" for _name, access in calls):
        return "read_only"
    return None


def _eligible(turn: dict[str, Any], kind: str, has_history: bool) -> bool:
    user = str(turn["user"]).strip()
    assistant = str(turn["assistant"]).strip()
    if (
        (not has_history and kind != "clarification")
        or not (5 <= len(user) <= 1_200)
        or not (20 <= len(assistant) <= 6_000)
    ):
        return False
    if SECRET_REQUEST_RE.search(user):
        return False
    if DELEGATION_REQUEST_RE.search(user):
        return False
    if kind != "clarification" and EXTERNAL_OR_MUTATING_REQUEST_RE.search(user):
        return False
    if kind == "clarification" and len(assistant) > 400:
        return False
    question_markers = (
        "?",
        "？",
        "what",
        "why",
        "how",
        "is ",
        "can ",
        "does ",
        "哪",
        "什么",
        "为什么",
        "怎么",
    )
    if kind == "read_only" and not any(mark in user.lower() for mark in question_markers):
        return False
    return True


def _assign_splits(cases: list[dict[str, Any]]) -> None:
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_kind[str(case["response_kind"])].append(case)
    for group in by_kind.values():
        group.sort(key=lambda item: item["id"])
        for index, case in enumerate(group):
            marker = index % 10
            case["split"] = "test" if marker == 0 else "validation" if marker == 1 else "train"


def _balanced(cases: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    groups = {
        kind: sorted((case for case in cases if case["response_kind"] == kind), key=lambda item: item["id"])
        for kind in ("answer", "clarification", "read_only")
    }
    selected = []
    while len(selected) < limit and any(groups.values()):
        for kind in ("answer", "clarification", "read_only"):
            if groups[kind] and len(selected) < limit:
                selected.append(groups[kind].pop(0))
    return sorted(selected, key=lambda item: item["id"])


def build_cases(
    roots: Iterable[Path],
    max_cases: int = 12,
    salt: str = "conversation-trace-v1",
) -> list[dict[str, Any]]:
    candidates = []
    for path in sorted(file for root in roots if root.is_dir() for file in root.rglob("*.jsonl")):
        try:
            turns = parse_rollout(path)
        except (OSError, UnicodeDecodeError):
            continue
        if len(turns) < 2:
            continue
        session_digest = _stable_digest(salt, path.name)
        source_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        for index, turn in enumerate(turns):
            kind = _response_kind(turn, index + 1 < len(turns))
            if kind is None or not _eligible(turn, kind, index > 0):
                continue
            history = []
            if index > 0:
                previous = turns[index - 1]
                if str(previous.get("user") or "").strip():
                    history.append(
                        {
                            "role": "user",
                            "content": pseudonymize_conversation(str(previous["user"]), 900),
                        }
                    )
                if str(previous.get("assistant") or "").strip():
                    history.append(
                        {
                            "role": "assistant",
                            "content": pseudonymize_conversation(
                                str(previous["assistant"]), 1_200
                            ),
                        }
                    )
                if not history:
                    continue
            digest = _stable_digest(salt, path.name, str(index), str(turn["user"]))
            candidate = {
                "id": f"conversation-{digest[:12]}",
                "history": history,
                "request": pseudonymize_conversation(str(turn["user"]), 1_200),
                "reference_response": pseudonymize_conversation(
                    str(turn["assistant"]), 2_000
                ),
                "response_kind": kind,
                "split": "unassigned",
                "trace_session": f"session-{session_digest[:12]}",
                "source": {
                    "rollout_sha256": source_sha,
                    "turn_index": index + 1,
                    "had_followup": index + 1 < len(turns),
                    "read_tool_calls": sum(
                        access == "read" for _name, access in turn["calls"]
                    ),
                },
            }
            redaction_markers = ("[TOKEN]", "[REDACTED]", "[LOCAL_PATH]", "[ACTOR]", "[ARN]")
            if any(marker in candidate["request"] for marker in redaction_markers):
                continue
            candidates.append(candidate)
    deduplicated = {}
    for case in candidates:
        fingerprint = _stable_digest(
            re.sub(r"\s+", " ", str(case["request"])).strip().lower(),
            re.sub(r"\s+", " ", str(case["reference_response"])).strip().lower(),
        )
        deduplicated.setdefault(fingerprint, case)
    unique_cases = list(deduplicated.values())
    _assign_splits(unique_cases)
    return _balanced(unique_cases, max_cases)


def write_dataset(output: Path, cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    case_list = list(cases)
    payload = {
        "format_version": 1,
        "benchmark": "conversation-trace",
        "scoring_contract_version": CONVERSATION_TRACE_CONTRACT_VERSION,
        "private": True,
        "publishable": False,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "privacy": {
            "raw_tool_arguments_included": False,
            "raw_tool_outputs_included": False,
            "raw_rollout_paths_included": False,
            "note": (
                "Pseudonymized conversation prose remains private and requires "
                "explicit approval before replay."
            ),
        },
        "counts": {
            "cases": len(case_list),
            "by_kind": {
                kind: sum(case["response_kind"] == kind for case in case_list)
                for kind in ("answer", "clarification", "read_only")
            },
            "by_split": {
                split: sum(case["split"] == split for case in case_list)
                for split in ("train", "validation", "test")
            },
        },
        "cases": case_list,
    }
    prose = []
    for case in case_list:
        prose.extend([str(case["request"]), str(case["reference_response"])])
        prose.extend(str(item["content"]) for item in case["history"])
    flattened = "\n".join(prose)
    leaked = [pattern.pattern for pattern in FORBIDDEN_OUTPUT if pattern.search(flattened)]
    if leaked:
        raise ValueError(f"privacy validation failed; matched {len(leaked)} forbidden patterns")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a private dataset from local Codex sessions")
    parser.add_argument(
        "--sessions",
        action="append",
        default=[],
        help="Codex session directory; may be repeated",
    )
    parser.add_argument(
        "--output",
        default=str(Path.home() / "projects/traces/conversation-trace-cases.json"),
    )
    parser.add_argument("--max-cases", type=int, default=12)
    parser.add_argument("--salt", default="conversation-trace-v1")
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("--max-cases must be positive")
    roots = [Path(value).expanduser().resolve() for value in args.sessions]
    if not roots:
        roots = [Path.home() / ".codex/sessions", Path.home() / ".codex/archived_sessions"]
    cases = build_cases(roots, max_cases=args.max_cases, salt=args.salt)
    if not cases:
        raise SystemExit("no eligible conversational trace cases found")
    output = Path(args.output).expanduser().resolve()
    payload = write_dataset(output, cases)
    print(json.dumps({"output": str(output), **payload["counts"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
