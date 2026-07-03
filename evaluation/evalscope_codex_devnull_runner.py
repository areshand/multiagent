"""EvalScope external Codex runner with finite prompt stdin.

EvalScope's bundled Codex runner passes the prompt as an argv value, but
ms-enclave still attaches stdin. Codex CLI treats piped stdin plus a prompt as
additional input and can wait before issuing the first model request. This
registered runner keeps the same bridge configuration and setup path while
feeding the prompt through a temporary file via ``codex exec - < prompt.txt``.
"""

from __future__ import annotations

import base64
import shlex
from typing import Any, Dict, List

from evalscope.agent.external.runners import AgentRunResult, BridgeEndpoint, ExternalAgentTask, RunnerTimeoutError
from evalscope.agent.external.runners.codex import CodexRunner
from evalscope.api.agent import AgentEnvironment
from evalscope.api.registry import register_runner
from evalscope.utils.logger import get_logger


logger = get_logger()
_CODEX_OUTPUT_FILE = "/tmp/evalscope-codex-last.txt"
_CODEX_PROMPT_FILE = "/tmp/evalscope-codex-prompt.txt"
_CODEX_STDOUT_FILE = "/tmp/evalscope-codex-stdout.log"
_CODEX_STDERR_FILE = "/tmp/evalscope-codex-stderr.log"
_CODEX_SANDBOX_MODE = "workspace-write"


@register_runner("codex-devnull")
class CodexDevnullRunner(CodexRunner):
    """Codex external runner with stdin closed for ms-enclave."""

    framework: str = "codex-devnull"

    def __init__(self, *, working_dir: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._working_dir = working_dir or None

    async def _install_node_via_apt(self, env: AgentEnvironment) -> None:
        manager = await env.exec(["bash", "-c", "command -v apt-get || command -v apk || true"], timeout=30)
        package_manager = (manager.stdout or "").strip().splitlines()[:1]
        if package_manager and package_manager[0].endswith("/apk"):
            logger.info("CodexRunner.setup: installing Node.js via apk for Alpine-based image.")
            install = await env.exec(
                ["bash", "-c", "set -e; apk add --no-cache nodejs npm"],
                timeout=self._install_timeout_s,
            )
            if install.returncode != 0:
                raise RuntimeError(
                    "CodexRunner.setup: apk Node.js install failed "
                    f"(rc={install.returncode}). stderr={(install.stderr or '').strip()[-1000:]!r}"
                )
            return
        await super()._install_node_via_apt(env)

    async def run(
        self,
        task: ExternalAgentTask,
        env: AgentEnvironment,
        bridge: BridgeEndpoint,
    ) -> AgentRunResult:
        env_vars: Dict[str, str] = {
            "EVALSCOPE_BRIDGE_TOKEN": bridge.trial_token,
            "IS_SANDBOX": "1",
        }
        home_dir = self._resolve_home()
        if home_dir is not None:
            env_vars["HOME"] = home_dir

        wire_api = self._extra_config.get("model_providers.evalscope.wire_api", '"responses"')
        config_pairs: List[str] = [
            'model_provider="evalscope"',
            'model_providers.evalscope.name="EvalScope Bridge"',
            f'model_providers.evalscope.base_url="{bridge.base_url}/openai/v1"',
            'model_providers.evalscope.env_key="EVALSCOPE_BRIDGE_TOKEN"',
            f"model_providers.evalscope.wire_api={wire_api}",
        ]
        if self._model_name:
            config_pairs.append(f'model="{self._model_name}"')
        for key, value in self._extra_config.items():
            if key == "model_providers.evalscope.wire_api":
                continue
            config_pairs.append(f"{key}={value}")

        argv: List[str] = ["codex", "exec"]
        for pair in config_pairs:
            argv.extend(["-c", pair])
        argv.extend(
            [
                "--sandbox",
                _CODEX_SANDBOX_MODE,
                "--dangerously-bypass-approvals-and-sandbox",
                "--output-last-message",
                _CODEX_OUTPUT_FILE,
            ]
        )
        argv.extend(self._extra_args)
        encoded_prompt = base64.b64encode(task.instruction.encode("utf-8")).decode("ascii")
        prompt_write = await env.exec(
            ["bash", "-lc", f"printf %s {shlex.quote(encoded_prompt)} | base64 -d > {_CODEX_PROMPT_FILE}"],
            timeout=30,
        )
        if prompt_write.returncode != 0:
            raise RuntimeError(
                "codex-devnull failed to write prompt file: "
                f"stderr={(prompt_write.stderr or '').strip()[-1000:]!r}"
            )

        argv.append("-")

        shell_command = (
            " ".join(shlex.quote(part) for part in argv)
            + f" < {_CODEX_PROMPT_FILE} > {_CODEX_STDOUT_FILE} 2> {_CODEX_STDERR_FILE}"
        )
        sample_id = (task.metadata or {}).get("sample_id")
        env_name = getattr(env, "name", type(env).__name__)
        logger.info(
            f"codex-devnull launching: sample={sample_id} env={env_name} "
            f"model={self._model_name or '<bridge-default>'} "
            f"timeout={task.timeout}s cwd={self._working_dir or '<env-default>'} "
            f"instruction_chars={len(task.instruction)}"
        )
        result = await env.exec(
            ["bash", "-lc", shell_command],
            timeout=task.timeout,
            env=env_vars,
            cwd=self._working_dir,
        )
        logger.info(
            f"codex-devnull exited: sample={sample_id} rc={result.returncode} "
            f"wall={result.duration:.1f}s stdout={len(result.stdout or '')}B "
            f"stderr={len(result.stderr or '')}B timed_out={result.timed_out}"
        )
        if result.timed_out:
            raise RunnerTimeoutError(f"codex timed out after {task.timeout}s (returncode={result.returncode})")
        if result.returncode != 0:
            cat_stderr = await env.exec(["bash", "-c", f"tail -c 2000 {_CODEX_STDERR_FILE} 2>/dev/null || true"])
            tail_stderr = ((cat_stderr.stdout or "") + (result.stderr or "")).strip()[-2000:]
            raise RuntimeError(f"codex exited with code {result.returncode}: {tail_stderr}")

        cat = await env.exec(["bash", "-c", f"cat {_CODEX_OUTPUT_FILE} 2>/dev/null || true"])
        output = cat.stdout.strip()
        if not output:
            logger.warning(
                f"codex-devnull: --output-last-message file {_CODEX_OUTPUT_FILE!r} "
                "empty or unreadable; patch extraction still uses git diff"
            )
        return AgentRunResult(
            output=output,
            metrics={
                "wall_time": result.duration,
                "returncode": result.returncode,
            },
        )
