"""EvalScope external runner for the native multi-agent SWE task contract.

The runner does not implement scoring. It runs a native solver command inside
the per-instance SWE Bench Pro container, then EvalScope's SWE Bench Pro
adapter extracts ``git diff`` from ``/app`` and sends that patch to the
official verifier.

The production SWE adapter does not inspect or score patches. It only runs the
workflow; EvalScope collects the resulting workspace diff. Task timeouts and
runner or infrastructure failures still abort the evaluation.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shlex
import uuid
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
_RUNTIME_IDENTITY_FILE = "/tmp/multiagent-prod-swe/runtime-identity.json"
_TRACE_ARCHIVE_FILE = "/tmp/evalscope-native-multiagent-trace.tar.gz"
_TRACE_CHUNK_BYTES = 256 * 1024
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

# AgentEnvironment supplies an explicit environment to this launcher. Keep the
# baked Codex runtime discoverable even when the base image's login PATH is not
# inherited (some official task images otherwise find codex but not its node
# interpreter).
export PATH="/opt/codex-node/bin:/opt/node22/bin:/usr/local/bin:${PATH:-/usr/bin:/bin}"

prompt_file="${EVAL_TASK_PROMPT_FILE:-/tmp/evalscope-native-multiagent-prompt.txt}"
timeout_args=()
if [[ -n "${EVAL_PROD_MULTIAGENT_TIMEOUT:-}" ]]; then
  timeout_args=(--timeout "$EVAL_PROD_MULTIAGENT_TIMEOUT")
fi
package=/opt/multiagent/evaluation/native_solver/__init__.py
if [[ -f "$package" && -x /opt/multiagent/launch.sh ]]; then
  cd /opt/multiagent
  exec python3 -m evaluation.native_solver.solve_swe_prod "$prompt_file" "${timeout_args[@]}"
fi

cat >&2 <<'EOF'
The production multiagent repository was not baked into this task image.
Expected /opt/multiagent/launch.sh and
/opt/multiagent/evaluation/native_solver/__init__.py.
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
        working_dir: str = "/app",
        model_name: str = "gpt-5",
        codex_auth_json: str = "",
        codex_auth_container_home: str = "/tmp/multiagent-prod-swe/codex-home",
        swe_bench_pro_repo_path: str = "",
        swe_bench_pro_sample_offset: int = 0,
        trace_output_dir: str = "",
        **_: Any,
    ) -> None:
        self._working_dir = working_dir or "/app"
        self._model_name = model_name.strip() or "gpt-5"
        self._codex_auth_json = codex_auth_json.strip()
        if not self._codex_auth_json:
            raise ValueError("multiagent-native requires runtime Codex auth JSON")
        self._codex_auth_container_home = (
            codex_auth_container_home.rstrip("/") or "/tmp/multiagent-prod-swe/codex-home"
        )
        self._swe_bench_pro_repo_path = swe_bench_pro_repo_path.strip()
        self._swe_bench_pro_sample_offset = swe_bench_pro_sample_offset
        self._trace_output_dir = Path(trace_output_dir).expanduser().resolve() if trace_output_dir.strip() else None

    async def setup(self, env: AgentEnvironment) -> None:
        await self._write_file(env, _DEFAULT_SOLVER_COMMAND, _SOLVER_LAUNCHER)
        chmod = await env.exec(["bash", "-lc", f"chmod +x {shlex.quote(_DEFAULT_SOLVER_COMMAND)}"], timeout=30)
        if chmod.returncode != 0:
            tail = ((chmod.stderr or "") + "\n" + (chmod.stdout or "")).strip()[-1000:]
            raise RuntimeError(f"multiagent-native failed to install launcher: {tail}")
        await self._install_codex_auth(env)
        return None

    async def run(
        self,
        task: ExternalAgentTask,
        env: AgentEnvironment,
        bridge: BridgeEndpoint,
    ) -> AgentRunResult:
        raw_metadata = dict(task.metadata or {})
        sample_id = raw_metadata.get("sample_id")
        sample_index = _absolute_sample_index(self._swe_bench_pro_sample_offset, sample_id)
        metadata = _public_solver_metadata(dict(task.metadata or {}))
        metadata.update(
            _public_problem_statement_metadata(
                self._swe_bench_pro_repo_path,
                sample_index,
                existing=metadata,
            )
        )
        await self._write_file(env, _PROMPT_FILE, task.instruction)
        await self._write_file(env, _METADATA_FILE, json.dumps(metadata, indent=2, sort_keys=True))

        env_vars: Dict[str, str] = {
            "EVAL_TASK_PROMPT_FILE": _PROMPT_FILE,
            "EVAL_TASK_METADATA_FILE": _METADATA_FILE,
            "EVAL_TASK_WORKDIR": self._working_dir,
            "EVAL_NATIVE_SOLVER_MODEL": self._model_name,
            "EVAL_PROD_MULTIAGENT_TIMEOUT": str(solver_internal_timeout(task.timeout)),
            "IS_SANDBOX": "1",
            "EVAL_CODEX_AUTH_MODE": "chatgpt",
            "CODEX_HOME": self._codex_auth_container_home,
        }
        _ = bridge
        command = _DEFAULT_SOLVER_COMMAND
        shell_command = (
            f"{command} > {shlex.quote(_STDOUT_FILE)} 2> {shlex.quote(_STDERR_FILE)}"
        )
        logger.info(
            f"multiagent-native launching: sample={sample_id} official_index={sample_index} "
            f"timeout={task.timeout}s "
            f"cwd={self._working_dir} command={command!r}"
        )
        runtime_identity: dict[str, Any] = {}
        trace_export: dict[str, Any] = {}
        trace_export_error: Exception | None = None
        try:
            result = await env.exec(["bash", "-lc", shell_command], timeout=task.timeout, env=env_vars, cwd=self._working_dir)
        finally:
            try:
                runtime_identity = await self._read_json_file(env, _RUNTIME_IDENTITY_FILE)
            except Exception as exc:
                logger.warning(f"multiagent-native could not read runtime identity: {exc!r}")
            if self._trace_output_dir is not None:
                try:
                    trace_export = await self._export_trace_bundle(
                        env,
                        sample_id=sample_id,
                        sample_index=sample_index,
                        instance_id=raw_metadata.get("instance_id"),
                    )
                except Exception as exc:
                    trace_export_error = exc
                    logger.error(
                        "multiagent-native could not export trace for official_index=%s: %r",
                        sample_index,
                        exc,
                    )
            await self._scrub_codex_auth(env)
        logger.info(
            f"multiagent-native exited: sample={sample_id} rc={result.returncode} "
            f"wall={result.duration:.1f}s timed_out={result.timed_out}"
        )
        logger.info(
            f"multiagent-native runtime: sample={sample_id} "
            f"identity={json.dumps(runtime_identity, sort_keys=True, separators=(',', ':'))}"
        )
        stdout = await env.exec(["bash", "-lc", f"tail -c 4000 {shlex.quote(_STDOUT_FILE)} 2>/dev/null || true"])
        stderr = await env.exec(["bash", "-lc", f"tail -c 4000 {shlex.quote(_STDERR_FILE)} 2>/dev/null || true"])
        stdout_tail = (stdout.stdout or "")[-4000:]
        stderr_tail = (stderr.stdout or "")[-4000:]
        if trace_export_error is not None:
            raise RuntimeError(
                f"multiagent-native could not export the configured trace for official_index={sample_index}: "
                f"{trace_export_error}"
            ) from trace_export_error
        if result.timed_out:
            raise RunnerTimeoutError(f"multiagent-native timed out after {task.timeout}s")
        elif result.returncode != 0:
            tail = (stderr_tail + "\n" + stdout_tail).strip()[-8000:]
            raise RuntimeError(f"multiagent-native exited with code {result.returncode}: {tail}")
        return AgentRunResult(
            output=stdout_tail,
            metrics={
                "wall_time": result.duration,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "stderr_tail": stderr_tail,
                "runtime_identity": runtime_identity,
                "trace_export": trace_export,
            },
        )

    async def _export_trace_bundle(
        self,
        env: AgentEnvironment,
        *,
        sample_id: Any,
        sample_index: int,
        instance_id: Any,
    ) -> dict[str, Any]:
        """Copy the container-local multiagent trace into a host-side row archive."""

        if self._trace_output_dir is None:
            return {}

        prepare_script = f"""
set -euo pipefail
stage=/tmp/evalscope-native-multiagent-trace-stage
archive={shlex.quote(_TRACE_ARCHIVE_FILE)}
rm -rf -- "$stage"
mkdir -p "$stage/runner"
if [[ -d /tmp/multiagent-prod-swe/state ]]; then
  cp -a /tmp/multiagent-prod-swe/state "$stage/state"
  rm -f -- "$stage/state/runtime_state/tmux.sock"
else
  printf 'multiagent state directory was not created\n' > "$stage/state-missing.txt"
fi
for source in {_STDOUT_FILE} {_STDERR_FILE} {_RUNTIME_IDENTITY_FILE}; do
  if [[ -f "$source" ]]; then
    cp -a "$source" "$stage/runner/$(basename "$source")"
  fi
done
tar -C "$stage" -czf "$archive" .
size=$(wc -c < "$archive" | tr -d '[:space:]')
digest=$(sha256sum "$archive" | awk '{{print $1}}')
printf '%s\t%s\n' "$size" "$digest"
"""
        prepared = await env.exec(["bash", "-lc", prepare_script], timeout=120)
        if prepared.returncode != 0:
            detail = ((prepared.stderr or "") + "\n" + (prepared.stdout or "")).strip()[-4000:]
            raise RuntimeError(f"could not prepare container trace archive: {detail}")
        fields = (prepared.stdout or "").strip().splitlines()[-1].split("\t")
        if len(fields) != 2:
            raise RuntimeError(f"container trace archive metadata is malformed: {prepared.stdout!r}")
        try:
            expected_size = int(fields[0])
        except ValueError as exc:
            raise RuntimeError(f"container trace archive size is invalid: {fields[0]!r}") from exc
        expected_digest = fields[1].strip().lower()
        if expected_size < 1 or len(expected_digest) != 64:
            raise RuntimeError(
                f"container trace archive metadata is invalid: size={expected_size} sha256={expected_digest!r}"
            )

        row_dir = self._trace_output_dir / f"official-row-{sample_index:06d}"
        row_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        archive_path = row_dir / "multiagent-trace.tar.gz"
        temporary_path = row_dir / f".{archive_path.name}.{uuid.uuid4().hex}.tmp"
        digest = hashlib.sha256()
        written = 0
        try:
            with temporary_path.open("wb") as handle:
                for chunk_index in range((expected_size + _TRACE_CHUNK_BYTES - 1) // _TRACE_CHUNK_BYTES):
                    chunk_script = (
                        "set -o pipefail; "
                        f"dd if={shlex.quote(_TRACE_ARCHIVE_FILE)} bs={_TRACE_CHUNK_BYTES} "
                        f"skip={chunk_index} count=1 status=none | base64"
                    )
                    chunk_result = await env.exec(["bash", "-lc", chunk_script], timeout=120)
                    if chunk_result.returncode != 0:
                        detail = ((chunk_result.stderr or "") + "\n" + (chunk_result.stdout or "")).strip()[-2000:]
                        raise RuntimeError(f"could not read trace archive chunk {chunk_index}: {detail}")
                    try:
                        chunk = base64.b64decode((chunk_result.stdout or "").encode("ascii"), validate=False)
                    except (UnicodeEncodeError, ValueError) as exc:
                        raise RuntimeError(f"trace archive chunk {chunk_index} is not valid base64") from exc
                    if not chunk:
                        raise RuntimeError(f"trace archive chunk {chunk_index} is empty")
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
            if written != expected_size:
                raise RuntimeError(f"trace archive size mismatch: expected {expected_size}, copied {written}")
            actual_digest = digest.hexdigest()
            if actual_digest != expected_digest:
                raise RuntimeError(
                    f"trace archive digest mismatch: expected {expected_digest}, copied {actual_digest}"
                )
            temporary_path.chmod(0o600)
            temporary_path.replace(archive_path)
        finally:
            temporary_path.unlink(missing_ok=True)

        manifest = {
            "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "official_index": sample_index,
            "sample_id": None if sample_id is None else str(sample_id),
            "instance_id": None if instance_id is None else str(instance_id),
            "archive": archive_path.name,
            "archive_bytes": expected_size,
            "archive_sha256": expected_digest,
            "container_state_dir": "/tmp/multiagent-prod-swe/state",
            "submission_workspace": self._working_dir,
        }
        manifest_path = row_dir / "manifest.json"
        manifest_tmp = row_dir / f".{manifest_path.name}.{uuid.uuid4().hex}.tmp"
        try:
            manifest_tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            manifest_tmp.chmod(0o600)
            manifest_tmp.replace(manifest_path)
        finally:
            manifest_tmp.unlink(missing_ok=True)

        logger.info(
            "multiagent-native trace exported: official_index=%s path=%s bytes=%s sha256=%s",
            sample_index,
            archive_path,
            expected_size,
            expected_digest,
        )
        return {
            "path": str(archive_path),
            "manifest": str(manifest_path),
            "bytes": expected_size,
            "sha256": expected_digest,
        }

    async def _read_json_file(self, env: AgentEnvironment, path: str) -> dict[str, Any]:
        result = await env.exec(["bash", "-lc", f"cat {shlex.quote(path)} 2>/dev/null || true"], timeout=30)
        raw = (result.stdout or "").strip()
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

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
        role_homes = shlex.quote("/tmp/multiagent-prod-swe/role-codex-homes")
        result = await env.exec(
            ["bash", "-lc", f"rm -rf -- {home} {role_homes}"],
            timeout=30,
        )
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


def _public_problem_statement_metadata(
    swe_bench_pro_repo_path: str,
    sample_offset: int,
    *,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load only the public problem statement from the local SWE-bench Pro JSONL."""

    if existing and existing.get("problem_statement"):
        return {}
    if not swe_bench_pro_repo_path:
        return {}
    jsonl = Path(swe_bench_pro_repo_path) / "helper_code" / "sweap_eval_full_v2.jsonl"
    if not jsonl.exists() or sample_offset < 0:
        return {}
    try:
        with jsonl.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index != sample_offset:
                    continue
                row = json.loads(line)
                statement = row.get("problem_statement")
                if isinstance(statement, str) and statement.strip():
                    return {"problem_statement": statement.strip()}
                return {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _absolute_sample_index(sample_offset: int, sample_id: Any) -> int:
    """Map EvalScope's shard-relative sample id to the official dataset row."""

    try:
        relative_index = int(sample_id)
    except (TypeError, ValueError):
        return sample_offset
    if relative_index < 0:
        return sample_offset
    return sample_offset + relative_index
