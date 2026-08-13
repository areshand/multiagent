"""Evaluation of verifier evidence bound to a final Git diff."""

from __future__ import annotations

import json
import re

from .snapshot import final_diff_sha256


def verifier_text_covers_resolution_commands(text: str, commands: list[dict[str, object]]) -> bool:
    lower = (text or "").lower().replace("\\n", "\n")
    for command in commands:
        cmd = str(command.get("cmd", "")).strip().lower()
        if not cmd:
            return False
        offset = lower.find(cmd)
        if offset >= 0:
            window = lower[max(0, offset - 250) : min(len(lower), offset + len(cmd) + 700)]
            if any(marker in window for marker in ("returncode=0", "return-code=0", "rc=0", "passed")):
                continue
        return False
    return True


def verifier_passing_commands(text: str) -> list[dict[str, object]]:
    """Extract explicit rc=0 commands from verifier protocol lines."""

    commands: list[dict[str, object]] = []
    for line in (text or "").splitlines():
        if not re.search(r"\b(?:returncode|return-code|rc)\s*=\s*0\b", line, re.IGNORECASE):
            continue
        match = re.search(r"\b(?:command|cmd)\s*=\s*([\"'])(.+?)\1", line, re.IGNORECASE)
        if not match:
            continue
        cmd = " ".join(match.group(2).split())
        if cmd and not any(item["cmd"] == cmd for item in commands):
            commands.append({"cmd": cmd, "rc": 0})
    return commands


def verifier_rechecked_todo(text: str, todo_id: str) -> bool:
    """Recognize the supported verifier recheck protocol spellings."""

    escaped_id = re.escape(todo_id.strip())
    if not escaped_id:
        return False
    return bool(
        re.search(
            rf"(?im)^\s*(?:todo|verifier)-recheck-passed:\s*(?:todo\s*=\s*)?{escaped_id}(?:\s|$)",
            text or "",
        )
    )


def _json_objects(text: str):
    decoder = json.JSONDecoder()
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def build_verification_has_evidence(text: str, diff: str) -> bool:
    """Require compile-clean rc=0 evidence bound to the exact final diff."""

    lower = text.lower().replace("\\n", "\n")
    diff_hash = final_diff_sha256(diff).lower()
    for match in re.finditer("build-verification-passed:", lower):
        window = lower[match.start() : match.start() + 800]
        if f"final-diff-sha256={diff_hash}" not in window and f'"final_diff_hash": "{diff_hash}"' not in window:
            continue
        if not any(marker in window for marker in ("compile_clean=true", '"compile_clean": true')):
            continue
        if any(marker in window for marker in ("returncode=0", "rc=0", '"rc": 0', '"returncode": 0')):
            return True
    for payload in _json_objects(text):
        build = payload.get("build_verification_passed")
        if not isinstance(build, dict):
            continue
        evidence_hash = str(
            build.get("final_diff_sha256")
            or build.get("final_diff_hash")
            or payload.get("final_diff_sha256")
            or payload.get("final_diff_hash")
            or ""
        ).lower()
        if evidence_hash != diff_hash or build.get("compile_clean") is not True:
            continue
        commands = build.get("commands")
        if isinstance(commands, list) and commands and all(
            isinstance(command, dict) and command.get("rc", command.get("returncode")) == 0
            for command in commands
        ):
            return True
        if build.get("rc", build.get("returncode")) == 0:
            return True
    return False


def behavior_verification_has_evidence(text: str, diff: str) -> bool:
    """Require semantic acceptance explicitly bound to the final diff."""

    lower = text.lower().replace("\\n", "\n")
    diff_hash = final_diff_sha256(diff).lower()
    for match in re.finditer("behavior-verification-passed:", lower):
        window = lower[match.start() : match.start() + 800]
        if f"final-diff-sha256={diff_hash}" not in window and f'"final_diff_hash": "{diff_hash}"' not in window:
            continue
        if any(
            marker in window
            for marker in (
                "public-clauses-covered=true",
                '"public_clauses_covered": true',
                "behavior_clean=true",
                '"behavior_clean": true',
            )
        ):
            return True
    return False
