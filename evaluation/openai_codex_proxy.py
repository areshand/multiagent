#!/usr/bin/env python3
"""Minimal OpenAI-compatible chat-completions proxy backed by Codex CLI.

This is intended for benchmark harnesses that require an OpenAI-compatible
endpoint. It handles one request at a time and shells out to `codex exec` for
each chat completion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


_DUMP_COUNTER = 0
_MODEL_BACKEND_INSTRUCTIONS = """\
You are a pure OpenAI-compatible model backend for a separate benchmark agent.
The benchmark agent, not you, has access to the task repository and tools.
Do not inspect, edit, or rely on your local filesystem or shell. Do not call
your own local shell/tool functions under any circumstance, including harmless
commands like pwd, ls, cat, sed, grep, or python. Your only job is to produce
the JSON output requested by the caller.
If tools are available and repository inspection, edits, or tests are needed,
return tool calls for the provided tools. Those tool calls execute in the
benchmark environment.
For final answers, return content only.
Always emit a JSON object with both keys: content and tool_calls. Use an empty
string for content when emitting tool calls, and an empty array for tool_calls
when emitting final content. In each tool call, arguments must be a JSON string.
"""

_MODEL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "content": {"type": "string"},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "string"},
                },
                "required": ["name", "arguments"],
            },
        },
    },
    "required": ["content", "tool_calls"],
}


def maybe_dump_request(path: str, request: dict[str, Any]) -> None:
    """Optionally persist raw proxy requests for scaffold debugging."""
    dump_dir = os.environ.get("OPENAI_CODEX_PROXY_DUMP_DIR")
    if not dump_dir:
        return
    global _DUMP_COUNTER
    _DUMP_COUNTER += 1
    safe_path = path.strip("/").replace("/", "_") or "root"
    output = Path(dump_dir) / f"{_DUMP_COUNTER:04d}-{safe_path}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")


def content_text(content: Any) -> str:
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def message_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = content_text(message.get("content", ""))
        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            content = "\n".join(
                [
                    content,
                    "Tool calls:",
                    json.dumps(tool_calls, ensure_ascii=False),
                ]
            ).strip()
        tool_call_id = message.get("tool_call_id")
        if tool_call_id:
            content = f"tool_call_id={tool_call_id}\n{content}"
        parts.append(f"{role.upper()}:\n{content}")
    return "\n\n".join(parts).strip()


def tool_prompt(tools: list[dict[str, Any]], tool_choice: Any) -> str:
    if not tools:
        return ""
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "function":
            function = tool.get("function") or {}
        else:
            function = tool
        schemas.append(
            {
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {},
            }
        )
    return "\n\n".join(
        [
            "You may use tools. If you need a tool, respond with ONLY valid JSON in this exact shape:",
            '{"tool_calls":[{"name":"tool_name","arguments":{"arg":"value"}}]}',
            "If no tool is needed, respond with normal assistant text.",
            f"tool_choice={json.dumps(tool_choice, ensure_ascii=False)}",
            "Available tools:",
            json.dumps(schemas, ensure_ascii=False, indent=2),
        ]
    )


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
        if match:
            stripped = match.group(1).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and start < end:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    parsed = extract_json_object(text)
    if not parsed:
        return []
    raw_calls = parsed.get("tool_calls") or parsed.get("tools") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    calls: list[dict[str, Any]] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function") or raw_call
        name = function.get("name")
        if not name:
            continue
        arguments = function.get("arguments") or raw_call.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments_obj = json.loads(arguments)
            except json.JSONDecodeError:
                arguments_obj = {"value": arguments}
        else:
            arguments_obj = arguments
        calls.append(
            {
                "id": raw_call.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {
                    "name": str(name),
                    "arguments": json.dumps(arguments_obj, ensure_ascii=False),
                },
            }
        )
    return calls


def normalized_model_text(text: str) -> str:
    parsed = extract_json_object(text)
    if isinstance(parsed, dict) and isinstance(parsed.get("content"), str) and not parsed.get("tool_calls"):
        return parsed["content"]
    return text


def run_codex(prompt: str, codex_bin: str, timeout: int) -> tuple[str, int, str]:
    output_path = Path(tempfile.gettempdir()) / f"openai-codex-proxy-{uuid.uuid4().hex}.txt"
    scratch_dir = Path(tempfile.mkdtemp(prefix="openai-codex-proxy-empty-"))
    schema_path = Path(tempfile.gettempdir()) / f"openai-codex-proxy-schema-{uuid.uuid4().hex}.json"
    schema_path.write_text(json.dumps(_MODEL_OUTPUT_SCHEMA), encoding="utf-8")
    command = [
        codex_bin,
        "exec",
        "--sandbox",
        "read-only",
        "--cd",
        str(scratch_dir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--output-last-message",
        str(output_path),
        "--output-schema",
        str(schema_path),
        "-",
    ]
    backend_prompt = f"{_MODEL_BACKEND_INSTRUCTIONS}\n\n{prompt}".strip()
    try:
        result = subprocess.run(
            command,
            input=backend_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        final_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
        return normalized_model_text(final_text or result.stdout), result.returncode, result.stderr
    finally:
        output_path.unlink(missing_ok=True)
        schema_path.unlink(missing_ok=True)
        shutil.rmtree(scratch_dir, ignore_errors=True)


def scaffold_probe_tool_calls(request: dict[str, Any]) -> list[dict[str, Any]]:
    tools = request.get("tools") or []
    available_tool_names = {
        (tool.get("function") or tool).get("name")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("function") or tool, dict)
    }
    if "exec_command" in available_tool_names:
        tool_name = "exec_command"
    elif "bash" in available_tool_names:
        tool_name = "bash"
    else:
        return []
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "tool":
            return []
    command = r"""python3 - <<'PY'
import os
from pathlib import Path

comment_by_suffix = {
    ".js": "// evalscope scaffold probe",
    ".jsx": "// evalscope scaffold probe",
    ".ts": "// evalscope scaffold probe",
    ".tsx": "// evalscope scaffold probe",
    ".py": "# evalscope scaffold probe",
    ".go": "// evalscope scaffold probe",
    ".java": "// evalscope scaffold probe",
    ".rb": "# evalscope scaffold probe",
    ".php": "// evalscope scaffold probe",
}
skip_dirs = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "docs",
    "node_modules",
    "test",
    "tests",
    "vendor",
}
skip_files = {
    "package.json",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
}
for current_root, dirs, files in os.walk("."):
    dirs[:] = sorted(d for d in dirs if d not in skip_dirs and not d.startswith("."))
    for name in sorted(files):
        path = Path(current_root, name)
        if name in skip_files or path.suffix not in comment_by_suffix:
            continue
        if any(part.lower() in {"test", "tests"} for part in path.parts):
            continue
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n" + comment_by_suffix[path.suffix] + "\n")
        print(f"modified {path}")
        raise SystemExit(0)
raise SystemExit("no supported source file found")
PY"""
    return [
        {
            "id": f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {
                "name": tool_name,
                "arguments": json.dumps(
                    {"cmd": command, "yield_time_ms": 1000, "max_output_tokens": 2000}
                    if tool_name == "exec_command"
                    else {"command": command, "timeout": 60},
                    ensure_ascii=False,
                ),
            },
        }
    ]


def stream_error_chunk(request: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": request.get("model") or "codex-local",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": f"ERROR: {message}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenAICodexProxy/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.quiet:  # type: ignore[attr-defined]
            return
        super().log_message(fmt, *args)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_sse(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

    def write_sse(self, payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def do_GET(self) -> None:
        if self.path.rstrip("/") in {"/v1/models", "/models"}:
            self.send_json(200, {"object": "list", "data": [{"id": self.server.model, "object": "model"}]})  # type: ignore[attr-defined]
            return
        self.send_json(404, {"error": {"message": f"unknown path: {self.path}"}})

    def do_POST(self) -> None:
        if self.path.rstrip("/") not in {"/v1/chat/completions", "/chat/completions"}:
            self.send_json(404, {"error": {"message": f"unknown path: {self.path}"}})
            return
        raw = self.rfile.read(int(self.headers.get("content-length", "0") or "0"))
        try:
            request = json.loads(raw.decode("utf-8"))
            maybe_dump_request(self.path, request)
            tools = request.get("tools") or []
            prompt = message_text(request.get("messages") or [])
            tools_text = tool_prompt(tools, request.get("tool_choice"))
            if tools_text:
                prompt = f"{prompt}\n\n{tools_text}".strip()
            stream = bool(request.get("stream"))
            if stream:
                self.start_sse()
            if self.server.proxy_mode == "scaffold-probe":  # type: ignore[attr-defined]
                tool_calls = scaffold_probe_tool_calls(request)
                text, returncode, stderr = (
                    ("scaffold probe requested a source-file edit", 0, "")
                    if tool_calls
                    else ("Patch submitted successfully.", 0, "")
                )
            else:
                text, returncode, stderr = run_codex(prompt, self.server.codex_bin, self.server.timeout)  # type: ignore[attr-defined]
                tool_calls = parse_tool_calls(text) if request.get("tools") else []
        except subprocess.TimeoutExpired as exc:
            if "stream" in locals() and stream:
                self.write_sse([stream_error_chunk(request if "request" in locals() else {}, f"codex timed out after {exc.timeout}s")])
                return
            self.send_json(504, {"error": {"message": f"codex timed out after {exc.timeout}s"}})
            return
        except Exception as exc:
            if "stream" in locals() and stream:
                self.write_sse([stream_error_chunk(request if "request" in locals() else {}, str(exc))])
                return
            self.send_json(500, {"error": {"message": str(exc)}})
            return

        if returncode != 0:
            if stream:
                self.write_sse([stream_error_chunk(request, f"codex command failed: {stderr[-1000:]}")])
                return
            self.send_json(502, {"error": {"message": "codex command failed", "stderr": stderr[-4000:]}})
            return

        now = int(time.time())
        message: dict[str, Any] = {"role": "assistant", "content": None if tool_calls else text}
        finish_reason = "stop"
        if tool_calls:
            message["tool_calls"] = tool_calls
            finish_reason = "tool_calls"

        response = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": now,
            "model": request.get("model") or self.server.model,  # type: ignore[attr-defined]
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if stream:
            chunks: list[dict[str, Any]] = []
            base = {
                "id": response["id"],
                "object": "chat.completion.chunk",
                "created": now,
                "model": response["model"],
            }
            if tool_calls:
                chunks.append(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "index": index,
                                            "id": call["id"],
                                            "type": call["type"],
                                            "function": call["function"],
                                        }
                                        for index, call in enumerate(tool_calls)
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                finish_reason = "tool_calls"
            else:
                chunks.append(
                    {
                        **base,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                finish_reason = "stop"
            chunks.append(
                {
                    **base,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    "usage": response["usage"],
                }
            )
            self.write_sse(chunks)
            return
        self.send_json(200, response)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local OpenAI-compatible endpoint backed by Codex CLI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="codex-local")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--proxy-mode", choices=["codex", "scaffold-probe"], default="codex")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.model = args.model  # type: ignore[attr-defined]
    server.codex_bin = args.codex_bin  # type: ignore[attr-defined]
    server.timeout = args.timeout  # type: ignore[attr-defined]
    server.proxy_mode = args.proxy_mode  # type: ignore[attr-defined]
    server.quiet = args.quiet  # type: ignore[attr-defined]
    print(f"serving {args.model} on http://{args.host}:{args.port}/v1/chat/completions", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
