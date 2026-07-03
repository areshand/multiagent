#!/usr/bin/env python3
"""All-Codex tmux multi-agent SWE solver for EvalScope task containers.

This entrypoint runs inside a SWE Bench Pro task image. It preserves the
production multi-agent shape by running orchestrator, worker, and verifier
agents in tmux windows, while using the EvalScope OpenAI-compatible bridge
instead of host-local interactive Codex/Claude CLIs.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path("/tmp/multiagent-swe-tmux")
DEFAULT_WORKDIR = Path("/app")
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
                    "timeout": {"type": "integer", "description": "Timeout in seconds. Defaults to 60."},
                },
                "required": ["command"],
            },
        },
    }
]


ORCHESTRATOR_SYSTEM = """\
You are the all-Codex SWE orchestrator. Read the issue and create one concise
implementation assignment for a worker plus verification guidance for a
verifier. Do not edit files. Return JSON with keys: assignment,
verification_hint, risk_notes.
"""

WORKER_SYSTEM = """\
You are the all-Codex SWE worker running in a tmux-managed multi-agent loop.
Use the bash tool for repository inspection, edits, and focused validation.
Work in /app. Fix the issue with the smallest source patch that satisfies the
requirements. Do not modify tests, generated assets, lockfiles, or unrelated
config unless the issue explicitly requires it. When complete, stop requesting
tools and summarize changed files plus validation.
"""

VERIFIER_SYSTEM = """\
You are the all-Codex SWE verifier in a tmux-managed multi-agent loop. Inspect
the worker's git diff and run focused checks when useful. Prefer read-only
inspection and tests. Do not intentionally edit files. Return JSON with keys:
needs_changes (boolean), findings (array of strings), suggested_commands
(array of strings).
"""


def log(message: str) -> None:
    print(f"[tmux-multiagent] {message}", flush=True)


def read_prompt(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    env_path = os.environ.get("EVAL_TASK_PROMPT_FILE")
    if env_path:
        return Path(env_path).read_text(encoding="utf-8")
    return sys.stdin.read()


def base_url() -> str:
    raw = os.environ.get("OPENAI_BASE_URL", "").rstrip("/")
    if not raw:
        raise RuntimeError("OPENAI_BASE_URL must be set")
    return raw


def api_key() -> str:
    token = os.environ.get("OPENAI_API_KEY", "")
    if not token:
        raise RuntimeError("OPENAI_API_KEY must be set")
    return token


def request_json(payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url()}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(f"model response had no choices: {response!r}")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise RuntimeError(f"model response message was invalid: {message!r}")
    return message


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
        return json.dumps(
            {
                "returncode": result.returncode,
                "duration_s": round(time.monotonic() - started, 3),
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


def run_model_loop(
    *,
    role: str,
    system_prompt: str,
    user_prompt: str,
    output_path: Path,
    cwd: Path,
    max_steps: int,
    tools_enabled: bool,
) -> None:
    model = os.environ.get("EVAL_NATIVE_SOLVER_MODEL", "codex-local")
    request_timeout = int(os.environ.get("EVAL_NATIVE_SOLVER_REQUEST_TIMEOUT", "900"))
    command_timeout = int(os.environ.get("EVAL_NATIVE_SOLVER_COMMAND_TIMEOUT", "60"))
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    final_content = ""
    for step in range(1, max_steps + 1):
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": 0,
        }
        if tools_enabled:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"
        log(f"{role} step={step} requesting model")
        try:
            response = request_json(payload, timeout=request_timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{role} model request failed: HTTP {exc.code}: {body[-2000:]}") from exc
        message = assistant_message(response)
        tool_calls = message.get("tool_calls") or []
        content = str(message.get("content") or "")
        final_content = content
        messages.append(
            {
                "role": "assistant",
                "content": content,
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )
        if not tool_calls:
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
                if command.strip():
                    log(f"{role} bash timeout={timeout}: {command[:220]}")
                    output = run_bash(command, timeout=timeout, cwd=cwd)
                else:
                    output = json.dumps({"returncode": 2, "error": "missing bash command"})
            messages.append({"role": "tool", "tool_call_id": call.get("id", f"call_{step}"), "content": output})
    output_path.write_text(final_content, encoding="utf-8")


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(stripped[start : end + 1])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def should_restore(path: str) -> bool:
    name = Path(path).name
    lowered = path.lower()
    if "/node_modules/" in lowered or "/dist/" in lowered or "/build/" in lowered:
        return True
    if "/public/assets/" in lowered or "/coverage/" in lowered:
        return True
    if lowered.startswith(("test/", "tests/")):
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
    return any(marker in lowered for marker in (".test.", ".spec.", "_test.", "/test/", "/tests/", "__tests__"))


def cleanup_patch(cwd: Path) -> list[str]:
    diff = subprocess.run(["git", "diff", "--name-only"], cwd=cwd, text=True, capture_output=True, timeout=30)
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    restore = [path for path in changed if should_restore(path)]
    if restore:
        subprocess.run(["git", "restore", "--", *restore], cwd=cwd, timeout=120, check=False)
    submodules = subprocess.run(
        ["git", "submodule", "status", "--recursive"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if submodules.returncode == 0:
        dirty_submodules = []
        for line in submodules.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and "-dirty" in line:
                dirty_submodules.append(parts[1])
        if dirty_submodules:
            subprocess.run(
                ["git", "submodule", "foreach", "--recursive", "git reset --hard && git clean -fdx"],
                cwd=cwd,
                timeout=300,
                check=False,
            )
            for path in dirty_submodules:
                subprocess.run(["git", "-C", path, "reset", "--hard"], cwd=cwd, timeout=120, check=False)
                subprocess.run(["git", "-C", path, "clean", "-fdx"], cwd=cwd, timeout=120, check=False)
            subprocess.run(["git", "restore", "--", *dirty_submodules], cwd=cwd, timeout=120, check=False)
            restore.extend(dirty_submodules)
    return restore


def git_diff(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "diff", "--binary", "--ignore-submodules=all"],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=60,
    )
    return result.stdout


def restore_worker_patch(cwd: Path, patch_path: Path) -> None:
    if not patch_path.exists() or not patch_path.read_text(encoding="utf-8").strip():
        return
    subprocess.run(["git", "reset", "--hard"], cwd=cwd, timeout=120, check=False)
    subprocess.run(["git", "clean", "-fd"], cwd=cwd, timeout=120, check=False)
    subprocess.run(["git", "apply", str(patch_path)], cwd=cwd, timeout=120, check=False)


def tmux_command(script: Path, role: str, prompt: Path, output: Path, cwd: Path, max_steps: int, tools: bool) -> str:
    args = [
        sys.executable,
        str(script),
        "--agent-role",
        role,
        "--prompt-file",
        str(prompt),
        "--output-file",
        str(output),
        "--workdir",
        str(cwd),
        "--max-steps",
        str(max_steps),
    ]
    if tools:
        args.append("--tools")
    quoted = " ".join(shlex.quote(arg) for arg in args)
    log_file = ROOT / f"{role}.log"
    exit_file = ROOT / f"{role}.exit"
    return f"{quoted} > {shlex.quote(str(log_file))} 2>&1; printf '%s\\n' $? > {shlex.quote(str(exit_file))}"


def wait_for(path: Path, exit_path: Path, timeout: int, role: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if exit_path.exists():
            code = exit_path.read_text(encoding="utf-8", errors="replace").strip()
            if code and code != "0":
                log_tail = (ROOT / f"{role}.log").read_text(encoding="utf-8", errors="replace")[-4000:]
                raise RuntimeError(f"{role} exited with {code} before writing {path}:\n{log_tail}")
        time.sleep(2)
    log_tail = ""
    log_path = ROOT / f"{role}.log"
    if log_path.exists():
        log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:]
    raise TimeoutError(f"timed out waiting for {role}; log tail:\n{log_tail}")


def run_tmux(prompt_path: str | None, cwd: Path) -> int:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed in this task image; cannot run tmux multi-agent solver")
    if not cwd.exists():
        raise RuntimeError(f"task workdir does not exist: {cwd}")

    ROOT.mkdir(parents=True, exist_ok=True)
    prompt = read_prompt(prompt_path)
    issue_path = ROOT / "issue.txt"
    issue_path.write_text(prompt, encoding="utf-8")
    session = f"swe-{os.getpid()}"
    script = Path(__file__)
    timeout = int(os.environ.get("EVAL_TMUX_AGENT_TIMEOUT", "2700"))
    worker_steps = int(os.environ.get("EVAL_TMUX_WORKER_STEPS", "80"))
    verifier_steps = int(os.environ.get("EVAL_TMUX_VERIFIER_STEPS", "30"))

    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "orchestrator"], check=True)

        orchestrator_prompt = ROOT / "orchestrator.prompt.txt"
        orchestrator_out = ROOT / "orchestrator.out"
        orchestrator_prompt.write_text(prompt, encoding="utf-8")
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{session}:orchestrator",
                tmux_command(script, "orchestrator", orchestrator_prompt, orchestrator_out, cwd, 1, False),
                "C-m",
            ],
            check=True,
        )
        wait_for(orchestrator_out, ROOT / "orchestrator.exit", timeout, "orchestrator")
        assignment = extract_json(orchestrator_out.read_text(encoding="utf-8"))
        assignment_text = assignment.get("assignment") or orchestrator_out.read_text(encoding="utf-8")
        verification_hint = assignment.get("verification_hint") or ""

        worker_prompt = ROOT / "worker.prompt.txt"
        worker_out = ROOT / "worker.out"
        worker_prompt.write_text(
            "\n\n".join(
                [
                    "Issue:",
                    prompt,
                    "Orchestrator assignment:",
                    str(assignment_text),
                    "Verifier hint:",
                    str(verification_hint),
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["tmux", "new-window", "-t", session, "-n", "worker"], check=True)
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{session}:worker",
                tmux_command(script, "worker", worker_prompt, worker_out, cwd, worker_steps, True),
                "C-m",
            ],
            check=True,
        )
        wait_for(worker_out, ROOT / "worker.exit", timeout, "worker")
        worker_patch = ROOT / "worker.patch"
        worker_patch.write_text(git_diff(cwd), encoding="utf-8")

        verifier_prompt = ROOT / "verifier.prompt.txt"
        verifier_out = ROOT / "verifier.out"
        verifier_prompt.write_text(
            "\n\n".join(
                [
                    "Issue:",
                    prompt,
                    "Worker summary:",
                    worker_out.read_text(encoding="utf-8")[-4000:],
                    "Worker diff:",
                    worker_patch.read_text(encoding="utf-8")[-20000:],
                    "Return JSON with needs_changes and findings.",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["tmux", "new-window", "-t", session, "-n", "verifier"], check=True)
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                f"{session}:verifier",
                tmux_command(script, "verifier", verifier_prompt, verifier_out, cwd, verifier_steps, True),
                "C-m",
            ],
            check=True,
        )
        wait_for(verifier_out, ROOT / "verifier.exit", timeout, "verifier")
        restore_worker_patch(cwd, worker_patch)
        verifier = extract_json(verifier_out.read_text(encoding="utf-8"))
        findings = verifier.get("findings") or []

        if verifier.get("needs_changes"):
            followup_prompt = ROOT / "worker-followup.prompt.txt"
            followup_out = ROOT / "worker-followup.out"
            followup_prompt.write_text(
                "\n\n".join(
                    [
                        "Issue:",
                        prompt,
                        "Verifier requested changes:",
                        json.dumps(findings, indent=2),
                        "Current diff:",
                        git_diff(cwd)[-20000:],
                    ]
                ),
                encoding="utf-8",
            )
            subprocess.run(["tmux", "new-window", "-t", session, "-n", "worker-followup"], check=True)
            subprocess.run(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    f"{session}:worker-followup",
                    tmux_command(script, "worker-followup", followup_prompt, followup_out, cwd, max(20, worker_steps // 2), True),
                    "C-m",
                ],
                check=True,
            )
            wait_for(followup_out, ROOT / "worker-followup.exit", timeout, "worker-followup")

        restored = cleanup_patch(cwd)
        if restored:
            log(f"restored non-source/generated changes: {restored}")
        final_diff = git_diff(cwd)
        log(f"final diff bytes={len(final_diff.encode('utf-8'))}")
        return 0
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_agent(args: argparse.Namespace) -> int:
    role = args.agent_role
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    output_path = Path(args.output_file)
    cwd = Path(args.workdir)
    if role == "orchestrator":
        system_prompt = ORCHESTRATOR_SYSTEM
    elif role == "verifier":
        system_prompt = VERIFIER_SYSTEM
    else:
        system_prompt = WORKER_SYSTEM
    run_model_loop(
        role=role,
        system_prompt=system_prompt,
        user_prompt=prompt,
        output_path=output_path,
        cwd=cwd,
        max_steps=args.max_steps,
        tools_enabled=args.tools,
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt", nargs="?")
    parser.add_argument("--agent-role")
    parser.add_argument("--prompt-file")
    parser.add_argument("--output-file")
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--max-steps", type=int, default=80)
    parser.add_argument("--tools", action="store_true")
    args = parser.parse_args(argv[1:])
    if args.agent_role:
        return run_agent(args)
    return run_tmux(args.prompt, Path(args.workdir))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
