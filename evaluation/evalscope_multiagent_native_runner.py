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
import ast
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
_DEFAULT_SOLVER_COMMAND = "/tmp/evalscope-native-multiagent-solver.sh"
_SOLVER_LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail

prompt_file="${EVAL_TASK_PROMPT_FILE:-/tmp/evalscope-native-multiagent-prompt.txt}"
workdir="${EVAL_TASK_WORKDIR:-/app}"
cd "$workdir"

if [[ -x /opt/multiagent/solve_swe.sh ]]; then
  exec /opt/multiagent/solve_swe.sh "$prompt_file"
fi

if [[ -f /opt/multiagent/solve_swe.py ]]; then
  exec python3 /opt/multiagent/solve_swe.py "$prompt_file"
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
        self._swe_bench_pro_repo_path = Path(swe_bench_pro_repo_path).expanduser() if swe_bench_pro_repo_path else None
        self._swe_bench_pro_sample_offset = int(swe_bench_pro_sample_offset or 0)
        self._official_contracts: dict[str, dict[str, Any]] | None = None
        self._official_contracts_by_index: dict[int, dict[str, Any]] | None = None

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

        metadata = self._enrich_metadata_with_official_contract(dict(task.metadata or {}), task.instruction)
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
            "EVAL_PROD_MULTIAGENT_TIMEOUT": str(max(300, int(task.timeout) - 90)),
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
        sample_id = metadata.get("sample_id")
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
        if result.timed_out:
            if not self._score_timed_out_diff:
                raise RunnerTimeoutError(
                    f"multiagent-native timed out after {task.timeout}s; refusing to score an unfinished git diff"
                )
            logger.warning(f"multiagent-native timed out after {task.timeout}s; scoring current git diff by explicit config")
        elif result.returncode != 0:
            tail = (stderr_tail + "\n" + stdout_tail).strip()[-2000:]
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
            },
        )

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

    def _enrich_metadata_with_official_contract(self, metadata: dict[str, Any], instruction: str = "") -> dict[str, Any]:
        contract = self._contract_for_metadata(metadata, instruction)
        if not contract:
            return metadata
        merged = dict(metadata)
        nested = dict(merged.get("swe_bench_pro") or {})
        nested.update(contract)
        merged["swe_bench_pro"] = nested
        return merged

    def _contract_for_metadata(self, metadata: dict[str, Any], instruction: str = "") -> dict[str, Any] | None:
        candidates = [
            metadata.get("instance_id"),
            metadata.get("sample_id"),
            metadata.get("id"),
            metadata.get("task_id"),
        ]
        nested = metadata.get("swe_bench_pro")
        if isinstance(nested, dict):
            candidates.extend([nested.get("instance_id"), nested.get("sample_id")])
        contracts = self._load_official_contracts()
        sample_id = metadata.get("sample_id")
        if sample_id is not None:
            try:
                official_index = self._swe_bench_pro_sample_offset + int(sample_id)
            except (TypeError, ValueError):
                official_index = None
            if official_index is not None:
                by_index = self._load_official_contracts_by_index()
                if official_index in by_index:
                    return by_index[official_index]
        for raw in candidates:
            if raw is None:
                continue
            key = str(raw)
            if key in contracts:
                return contracts[key]
            if "-v" in key:
                base = key.split("-v", 1)[0]
                if base in contracts:
                    return contracts[base]
        normalized_instruction = _normalize_problem_statement(instruction)
        if normalized_instruction:
            for contract in contracts.values():
                problem = str(contract.get("problem_statement") or "")
                if normalized_instruction == _normalize_problem_statement(problem):
                    return contract
            for contract in contracts.values():
                problem = _normalize_problem_statement(str(contract.get("problem_statement") or ""))
                if problem and (problem in normalized_instruction or normalized_instruction in problem):
                    return contract
        return None

    def _load_official_contracts(self) -> dict[str, dict[str, Any]]:
        if self._official_contracts is not None:
            return self._official_contracts
        contracts: dict[str, dict[str, Any]] = {}
        contracts_by_index: dict[int, dict[str, Any]] = {}
        if not self._swe_bench_pro_repo_path:
            self._official_contracts = contracts
            self._official_contracts_by_index = contracts_by_index
            return contracts
        dataset_path = self._swe_bench_pro_repo_path / "helper_code" / "sweap_eval_full_v2.jsonl"
        if not dataset_path.exists():
            logger.warning(f"SWE Bench Pro official JSONL not found for native metadata enrichment: {dataset_path}")
            self._official_contracts = contracts
            self._official_contracts_by_index = contracts_by_index
            return contracts
        with dataset_path.open(encoding="utf-8") as handle:
            for official_index, line in enumerate(handle):
                if not line.strip():
                    continue
                row = json.loads(line)
                instance_id = str(row.get("instance_id") or "")
                if not instance_id:
                    continue
                fail_to_pass = _parse_test_list(row.get("FAIL_TO_PASS") or row.get("fail_to_pass"))
                pass_to_pass = _parse_test_list(row.get("PASS_TO_PASS") or row.get("pass_to_pass"))
                selected_files = _parse_test_list(row.get("selected_test_files_to_run"))
                contract = {
                    "instance_id": instance_id,
                    "repo": row.get("repo"),
                    "base_commit": row.get("base_commit"),
                    "problem_statement": row.get("problem_statement"),
                    "requirements": row.get("requirements"),
                    "interface": row.get("interface"),
                    "fail_to_pass": fail_to_pass,
                    "pass_to_pass": pass_to_pass,
                    "expected_test_count": len(fail_to_pass) + len(pass_to_pass),
                    "selected_test_files_to_run": selected_files,
                    "run_script_dir": str(self._swe_bench_pro_repo_path / "run_scripts" / instance_id),
                }
                contracts[instance_id] = contract
                contracts_by_index[official_index] = contract
                if "-v" in instance_id:
                    contracts.setdefault(instance_id.split("-v", 1)[0], contract)
        self._official_contracts = contracts
        self._official_contracts_by_index = contracts_by_index
        return contracts

    def _load_official_contracts_by_index(self) -> dict[int, dict[str, Any]]:
        if self._official_contracts_by_index is not None:
            return self._official_contracts_by_index
        self._load_official_contracts()
        if self._official_contracts_by_index is None:
            self._official_contracts_by_index = {}
        return self._official_contracts_by_index

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


def _parse_test_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, tuple):
        return [str(item) for item in raw]
    if not isinstance(raw, str):
        return [str(raw)]
    text = raw.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return [text]
    if isinstance(parsed, (list, tuple)):
        return [str(item) for item in parsed]
    return [str(parsed)]


def _normalize_problem_statement(text: str) -> str:
    return " ".join(text.strip().split())
