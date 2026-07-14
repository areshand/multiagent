"""EvalScope external runner for the native multi-agent SWE task contract.

The runner does not implement scoring. It runs a native solver command inside
the per-instance SWE Bench Pro container, then EvalScope's SWE Bench Pro
adapter extracts ``git diff`` from ``/app`` and sends that patch to the
official verifier.

By default, a nonzero native solver exit is treated as a rejected candidate and
is not forwarded to the official verifier. The production SWE adapter uses
return code 2 when its own public-contract gate rejects a patch, so scoring the
current diff in that case would turn known-bad intermediate state into noisy
benchmark evidence.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
from pathlib import Path
from typing import Any, Dict

from evalscope.agent.external.runners import AgentRunResult, AgentRunner, BridgeEndpoint, ExternalAgentTask, RunnerTimeoutError
from evalscope.api.agent import AgentEnvironment
from evalscope.api.registry import register_runner
from evalscope.utils.logger import get_logger


logger = get_logger()
_PROMPT_FILE = "/tmp/evalscope-native-multiagent-prompt.txt"
_METADATA_FILE = "/tmp/evalscope-native-multiagent-metadata.json"
_STDOUT_FILE = "/tmp/evalscope-native-multiagent-stdout.log"
_STDERR_FILE = "/tmp/evalscope-native-multiagent-stderr.log"
_DIAGNOSTICS_FILE = "/tmp/evalscope-native-multiagent-diagnostics.txt"
_DEFAULT_SOLVER_COMMAND = "/tmp/evalscope-native-multiagent-solver.sh"
_PUBLIC_METADATA_KEYS = {
    "language",
    "problem_statement",
}
_PRIVATE_SOLVER_METADATA_KEYS = {
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
_SOLVER_LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail

prompt_file="${EVAL_TASK_PROMPT_FILE:-/tmp/evalscope-native-multiagent-prompt.txt}"
workdir="${EVAL_TASK_WORKDIR:-/app}"
timeout_args=()
if [[ -n "${EVAL_PROD_MULTIAGENT_TIMEOUT:-}" ]]; then
  timeout_args=(--timeout "$EVAL_PROD_MULTIAGENT_TIMEOUT")
fi
cd "$workdir"

if [[ -x /opt/multiagent/solve_swe.sh ]]; then
  exec /opt/multiagent/solve_swe.sh "$prompt_file"
fi

if [[ -f /opt/multiagent/solve_swe.py ]]; then
  exec python3 /opt/multiagent/solve_swe.py "$prompt_file" "${timeout_args[@]}"
fi

if command -v multiagent-solve-swe >/dev/null 2>&1; then
  exec multiagent-solve-swe "$prompt_file"
fi

if command -v multiagent-swe-solver >/dev/null 2>&1; then
  exec multiagent-swe-solver "$prompt_file"
fi

cat >&2 <<'EOF'
No baked native multi-agent SWE solver was found in this task container.

Expected one of:
  /opt/multiagent/solve_swe.sh
  /opt/multiagent/solve_swe.py
  multiagent-solve-swe
  multiagent-swe-solver

The solver must read the issue prompt from $EVAL_TASK_PROMPT_FILE, edit the
repository in $EVAL_TASK_WORKDIR, and leave the final patch in git diff.
If this image uses another entrypoint, pass --native-solver-command explicitly.
EOF
exit 127
"""


def solver_internal_timeout(agent_timeout: float) -> int:
    reserve = int(os.environ.get("EVAL_NATIVE_SOLVER_TIMEOUT_RESERVE", "600"))
    reserve = max(90, min(reserve, int(agent_timeout) - 300))
    return max(300, int(agent_timeout) - reserve)


@register_runner("multiagent-native")
class MultiagentNativeRunner(AgentRunner):
    """Run a native multi-agent solver command inside the SWE task sandbox."""

    framework: str = "multiagent-native"

    def __init__(
        self,
        *,
        command: str = _DEFAULT_SOLVER_COMMAND,
        setup_command: str = "",
        working_dir: str = "/app",
        require_command: bool = False,
        model_name: str = "gpt-5",
        codex_auth_json: str = "",
        codex_auth_container_home: str = "/root/.codex-multiagent-prod",
        score_failed_diff: bool = False,
        score_timed_out_diff: bool = False,
        swe_bench_pro_repo_path: str = "",
        swe_bench_pro_sample_offset: int = 0,
        **_: Any,
    ) -> None:
        self._command = command.strip()
        self._setup_command = setup_command.strip()
        self._working_dir = working_dir or "/app"
        self._require_command = require_command
        self._model_name = model_name.strip() or "gpt-5"
        self._codex_auth_json = codex_auth_json.strip()
        self._codex_auth_container_home = codex_auth_container_home.rstrip("/") or "/root/.codex-multiagent-prod"
        self._score_failed_diff = score_failed_diff
        self._score_timed_out_diff = score_timed_out_diff
        # Accepted for backwards-compatible EvalScope configs only. Production
        # no-leak mode must not enrich solver metadata from official datasets.
        _ = swe_bench_pro_repo_path, swe_bench_pro_sample_offset

    async def setup(self, env: AgentEnvironment) -> None:
        await self._write_file(env, _DEFAULT_SOLVER_COMMAND, _SOLVER_LAUNCHER)
        chmod = await env.exec(["bash", "-lc", f"chmod +x {shlex.quote(_DEFAULT_SOLVER_COMMAND)}"], timeout=30)
        if chmod.returncode != 0:
            tail = ((chmod.stderr or "") + "\n" + (chmod.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"multiagent-native failed to install launcher: {tail}")
        if self._codex_auth_json:
            await self._install_codex_auth(env)
        if not self._setup_command:
            return None
        result = await env.exec(["bash", "-lc", self._setup_command], timeout=600, cwd=self._working_dir)
        if result.timed_out:
            raise RunnerTimeoutError("multiagent-native setup timed out")
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-2000:]
            raise RuntimeError(f"multiagent-native setup failed with code {result.returncode}: {tail}")
        return None

    async def run(
        self,
        task: ExternalAgentTask,
        env: AgentEnvironment,
        bridge: BridgeEndpoint,
    ) -> AgentRunResult:
        if self._require_command and not self._command:
            raise RuntimeError(
                "multiagent-native was configured with require_command=true but no command. "
                "The command must edit the repository in /app; EvalScope will extract git diff afterwards."
            )

        raw_metadata = dict(task.metadata or {})
        metadata = _public_solver_metadata(dict(task.metadata or {}))
        await self._write_file(env, _PROMPT_FILE, task.instruction)
        await self._write_file(env, _METADATA_FILE, json.dumps(metadata, indent=2, sort_keys=True))

        env_vars: Dict[str, str] = {
            "EVALSCOPE_BRIDGE_TOKEN": bridge.trial_token,
            "EVALSCOPE_BRIDGE_BASE_URL": bridge.base_url,
            "OPENAI_API_KEY": bridge.trial_token,
            "OPENAI_BASE_URL": f"{bridge.base_url}/openai/v1",
            "EVAL_TASK_PROMPT_FILE": _PROMPT_FILE,
            "EVAL_TASK_METADATA_FILE": _METADATA_FILE,
            "EVAL_TASK_WORKDIR": self._working_dir,
            "EVAL_NATIVE_SOLVER_MODEL": self._model_name,
            "EVAL_PROD_MULTIAGENT_TIMEOUT": str(solver_internal_timeout(task.timeout)),
            "IS_SANDBOX": "1",
        }
        if self._codex_auth_json:
            env_vars.update(
                {
                    "EVAL_CODEX_AUTH_MODE": "chatgpt",
                    "CODEX_HOME": self._codex_auth_container_home,
                }
            )
        command = self._command or _DEFAULT_SOLVER_COMMAND
        shell_command = (
            f"{command} > {shlex.quote(_STDOUT_FILE)} 2> {shlex.quote(_STDERR_FILE)}"
        )
        sample_id = raw_metadata.get("sample_id")
        logger.info(
            f"multiagent-native launching: sample={sample_id} timeout={task.timeout}s "
            f"cwd={self._working_dir} command={command!r}"
        )
        try:
            result = await env.exec(["bash", "-lc", shell_command], timeout=task.timeout, env=env_vars, cwd=self._working_dir)
        finally:
            if self._codex_auth_json:
                await self._scrub_codex_auth(env)
        logger.info(
            f"multiagent-native exited: sample={sample_id} rc={result.returncode} "
            f"wall={result.duration:.1f}s timed_out={result.timed_out}"
        )
        stdout = await env.exec(["bash", "-lc", f"tail -c 4000 {shlex.quote(_STDOUT_FILE)} 2>/dev/null || true"])
        stderr = await env.exec(["bash", "-lc", f"tail -c 4000 {shlex.quote(_STDERR_FILE)} 2>/dev/null || true"])
        stdout_tail = (stdout.stdout or "")[-4000:]
        stderr_tail = (stderr.stdout or "")[-4000:]
        diagnostics = ""
        if result.timed_out:
            diagnostics = await self._collect_rejection_diagnostics(env)
            if not self._score_timed_out_diff:
                raise RunnerTimeoutError(
                    "multiagent-native timed out after "
                    f"{task.timeout}s; refusing to score an unfinished git diff\n{diagnostics[-8000:]}"
                )
            logger.warning(f"multiagent-native timed out after {task.timeout}s; scoring current git diff by explicit config")
        elif result.returncode != 0:
            diagnostics = await self._collect_rejection_diagnostics(env)
            tail = (stderr_tail + "\n" + stdout_tail + "\n" + diagnostics).strip()[-12000:]
            if not self._score_failed_diff:
                raise RuntimeError(
                    f"multiagent-native exited with code {result.returncode}; refusing to score rejected git diff: {tail}"
                )
            logger.warning(
                f"multiagent-native exited with code {result.returncode}; scoring current git diff by explicit config: {tail}"
            )
        return AgentRunResult(
            output=stdout_tail,
            metrics={
                "wall_time": result.duration,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stderr_tail": stderr_tail,
                "diagnostics_tail": diagnostics[-4000:],
            },
        )

    async def _collect_rejection_diagnostics(self, env: AgentEnvironment) -> str:
        """Collect public/source diagnostics before EvalScope deletes the task container."""

        workdir = shlex.quote(self._working_dir)
        diagnostics_file = shlex.quote(_DIAGNOSTICS_FILE)
        script = f"""
set +e
cd {workdir} 2>/dev/null || true
out={diagnostics_file}
: > "$out"
section() {{
  printf '\\n===== %s =====\\n' "$1" >> "$out"
}}
copy_file_tail() {{
  label="$1"
  path="$2"
  bytes="$3"
  section "$label"
  if [ -f "$path" ]; then
    tail -c "$bytes" "$path" >> "$out" 2>&1
  else
    printf 'missing: %s\\n' "$path" >> "$out"
  fi
}}
copy_file_tail status.json /tmp/multiagent-prod-swe/status.json 12000
copy_file_tail source-owner-candidates /tmp/multiagent-prod-swe/source-owner-candidates.md 12000
copy_file_tail helper-validation-probe /tmp/multiagent-prod-swe/helper-validation-probe.txt 12000
copy_file_tail stale-visible-reconciliation /tmp/multiagent-prod-swe/stale-visible-reconciliation.txt 8000
copy_file_tail multi-value-probe /tmp/multiagent-prod-swe/multi-value-probe.txt 8000
copy_file_tail failure-diagnostics /tmp/multiagent-prod-swe/failure-diagnostics.txt 20000
copy_file_tail native-stdout {_STDOUT_FILE} 8000
copy_file_tail native-stderr {_STDERR_FILE} 8000
section git-status
git status --short >> "$out" 2>&1
section git-diff-name-only
git diff --name-only HEAD -- >> "$out" 2>&1
section git-diff-stat
git diff --stat HEAD -- >> "$out" 2>&1
section git-diff-check
git diff --check HEAD -- >> "$out" 2>&1
section git-diff-tail
git diff HEAD -- | tail -c 30000 >> "$out" 2>&1
copy_file_tail final-status.json /tmp/multiagent-prod-swe/status.json 12000
copy_file_tail final-failure-diagnostics /tmp/multiagent-prod-swe/failure-diagnostics.txt 20000
tail -c 60000 "$out" 2>/dev/null || true
"""
        result = await env.exec(["bash", "-lc", script], timeout=90)
        return ((result.stdout or "") + "\n" + (result.stderr or "")).strip()

    async def _write_file(self, env: AgentEnvironment, path: str, content: str) -> None:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        quoted_path = shlex.quote(path)
        if len(encoded) <= 60_000:
            result = await env.exec(
                ["bash", "-lc", f"printf %s {shlex.quote(encoded)} | base64 -d > {quoted_path}"],
                timeout=30,
            )
            if result.returncode != 0:
                tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
                raise RuntimeError(f"multiagent-native failed to write {path}: {tail}")
            return

        temp_path = f"{path}.b64"
        quoted_temp = shlex.quote(temp_path)
        result = await env.exec(["bash", "-lc", f"rm -f -- {quoted_temp}"], timeout=30)
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"multiagent-native failed to prepare {path}: {tail}")

        for start in range(0, len(encoded), 48_000):
            chunk = encoded[start:start + 48_000]
            result = await env.exec(
                ["bash", "-lc", f"printf %s {shlex.quote(chunk)} >> {quoted_temp}"],
                timeout=30,
            )
            if result.returncode != 0:
                tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
                raise RuntimeError(f"multiagent-native failed to stage {path}: {tail}")

        result = await env.exec(
            ["bash", "-lc", f"base64 -d {quoted_temp} > {quoted_path} && rm -f -- {quoted_temp}"],
            timeout=30,
        )
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"multiagent-native failed to write {path}: {tail}")

    async def _install_codex_auth(self, env: AgentEnvironment) -> None:
        auth_path = Path(self._codex_auth_json).expanduser()
        if not auth_path.exists():
            raise FileNotFoundError(f"Codex auth JSON not found: {auth_path}")
        raw = auth_path.read_bytes()
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Codex auth JSON is not valid JSON: {auth_path}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Codex auth JSON must be a JSON object: {auth_path}")

        encoded = base64.b64encode(raw).decode("ascii")
        home = shlex.quote(self._codex_auth_container_home)
        script = f"""
set -euo pipefail
mkdir -p {home}
chmod 700 {home}
python3 - <<'PY'
import base64
import os
from pathlib import Path

home = Path({self._codex_auth_container_home!r})
auth = base64.b64decode(os.environ["CODEX_AUTH_JSON_B64"])
(home / "auth.json").write_bytes(auth)
(home / "auth.json").chmod(0o600)
PY
"""
        result = await env.exec(
            ["bash", "-lc", script],
            timeout=30,
            env={"CODEX_AUTH_JSON_B64": encoded},
        )
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"multiagent-native failed to install Codex auth JSON: {tail}")

    async def _scrub_codex_auth(self, env: AgentEnvironment) -> None:
        home = shlex.quote(self._codex_auth_container_home)
        result = await env.exec(["bash", "-lc", f"rm -rf -- {home}"], timeout=30)
        if result.returncode != 0:
            tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-1000:]
            logger.warning(f"multiagent-native failed to scrub Codex auth home: {tail}")


def _public_solver_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return only non-answer metadata that may be visible to the solver.

    SWE Bench Pro rows contain verifier-side fields such as expected test names,
    selected official test files, test patches, and row identifiers. The
    production multi-agent solver must infer fixes from the issue and repository
    state, so benchmark identity and answer-shaped fields are intentionally not
    written into the task container.
    """

    public: dict[str, Any] = {
        key: value
        for key, value in metadata.items()
        if key in _PUBLIC_METADATA_KEYS and key not in _PRIVATE_SOLVER_METADATA_KEYS
    }
    nested = metadata.get("swe_bench_pro")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key in _PUBLIC_METADATA_KEYS and key not in public:
                public[key] = value
    return public
