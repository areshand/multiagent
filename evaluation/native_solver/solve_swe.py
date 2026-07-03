#!/usr/bin/env python3
"""Container-native SWE task solver for EvalScope SWE Bench Pro.

This entrypoint runs inside a per-instance SWE Bench Pro task image. It talks
to the EvalScope OpenAI-compatible bridge, executes model-requested bash tool
calls in /app, and leaves the final repository changes as git diff for
EvalScope's official verifier to extract.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SYSTEM_PROMPT = """\
You are the coding worker in a multi-agent SWE solving loop. Your job is to
inspect the repository, edit source files, and validate the fix. Use the bash
tool for every repository action. Work in /app. Prefer rg/sed/python scripts for
inspection and editing. Do not modify tests or unrelated config unless the issue
requires it. When the fix is complete, stop requesting tools and summarize the
changed files and verification.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a non-interactive bash command in the task repository.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds. Defaults to 60.",
                    },
                },
                "required": ["command"],
            },
        },
    }
]


def read_prompt(argv: list[str]) -> str:
    if len(argv) > 1:
        return Path(argv[1]).read_text(encoding="utf-8")
    prompt_file = os.environ.get("EVAL_TASK_PROMPT_FILE")
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    return sys.stdin.read()


def request_json(url: str, token: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def normalize_base_url(raw: str) -> str:
    return raw.rstrip("/")


def run_bash(command: str, timeout: int, cwd: Path) -> str:
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        duration = time.monotonic() - started
        return json.dumps(
            {
                "returncode": result.returncode,
                "duration_s": round(duration, 3),
                "stdout": result.stdout[-12000:],
                "stderr": result.stderr[-12000:],
            },
            ensure_ascii=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return json.dumps(
            {
                "returncode": -1,
                "timed_out": True,
                "timeout_s": timeout,
                "stdout": stdout[-12000:],
                "stderr": stderr[-12000:],
            },
            ensure_ascii=False,
        )


def parse_arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"command": raw}
    return parsed if isinstance(parsed, dict) else {"command": str(parsed)}


def command_from_args(args: dict[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code"):
        value = args.get(key)
        if value:
            return str(value)
    return ""


def should_restore(path: str) -> bool:
    name = Path(path).name
    lowered = path.lower()
    if "/node_modules/" in lowered or "/dist/" in lowered or "/build/" in lowered:
        return True
    if "/public/assets/" in lowered or "/coverage/" in lowered:
        return True
    if name in {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "poetry.lock",
        "go.sum",
        "go.work.sum",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    }:
        return True
    if lowered.startswith(("test/", "tests/")):
        return True
    test_markers = (".test.", ".spec.", "_test.", "/test/", "/tests/", "__tests__")
    return any(marker in lowered for marker in test_markers)


def cleanup_patch(cwd: Path) -> list[str]:
    diff = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    restore = [path for path in changed if should_restore(path)]
    if restore:
        subprocess.run(["git", "restore", "--", *restore], cwd=cwd, timeout=120, check=False)
    return restore


def assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(f"model response had no choices: {response!r}")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise RuntimeError(f"model response message was invalid: {message!r}")
    return message


def main(argv: list[str]) -> int:
    prompt = read_prompt(argv)
    cwd = Path(os.environ.get("EVAL_TASK_WORKDIR", "/app"))
    base_url = normalize_base_url(os.environ.get("OPENAI_BASE_URL", ""))
    token = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("EVAL_NATIVE_SOLVER_MODEL", "codex-local")
    max_steps = int(os.environ.get("EVAL_NATIVE_SOLVER_MAX_STEPS", "80"))
    request_timeout = int(os.environ.get("EVAL_NATIVE_SOLVER_REQUEST_TIMEOUT", "900"))
    command_timeout = int(os.environ.get("EVAL_NATIVE_SOLVER_COMMAND_TIMEOUT", "60"))

    if not base_url or not token:
        raise RuntimeError("OPENAI_BASE_URL and OPENAI_API_KEY must be set")
    if not cwd.exists():
        raise RuntimeError(f"task workdir does not exist: {cwd}")

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    url = f"{base_url}/chat/completions"

    for step in range(1, max_steps + 1):
        payload = {
            "model": model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": 0,
        }
        print(f"[native-solver] step={step} requesting model", flush=True)
        try:
            response = request_json(url, token, payload, timeout=request_timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model request failed: HTTP {exc.code}: {body[-2000:]}") from exc
        message = assistant_message(response)
        tool_calls = message.get("tool_calls") or []
        content = str(message.get("content") or "")
        messages.append(
            {
                "role": "assistant",
                "content": content,
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )
        if not tool_calls:
            print(content[-4000:], flush=True)
            break

        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name")
            args = parse_arguments(str(function.get("arguments") or "{}"))
            if name != "bash":
                output = json.dumps({"error": f"unsupported tool: {name}"})
            else:
                command = command_from_args(args)
                timeout = int(args.get("timeout") or command_timeout)
                if not command.strip():
                    output = json.dumps(
                        {
                            "returncode": 2,
                            "error": "missing bash command; call the bash tool with a non-empty command argument",
                        }
                    )
                    print("[native-solver] bash skipped: empty command", flush=True)
                else:
                    print(f"[native-solver] bash timeout={timeout}: {command[:240]}", flush=True)
                    output = run_bash(command, timeout=timeout, cwd=cwd)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", f"call_{step}"),
                    "content": output,
                }
            )
    else:
        print(f"[native-solver] reached max_steps={max_steps}", file=sys.stderr, flush=True)

    restored = cleanup_patch(cwd)
    if restored:
        print(f"[native-solver] restored non-source/generated changes: {restored}", flush=True)

    diff = subprocess.run(
        ["git", "diff", "--binary"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    print(f"[native-solver] final diff bytes={len(diff.stdout.encode('utf-8'))}", flush=True)
    if diff.stderr:
        print(diff.stderr[-2000:], file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
