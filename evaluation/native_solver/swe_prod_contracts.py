"""Public task inputs and runtime paths for the SWE-bench adapter."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_MULTIAGENT_ROOT = Path("/opt/multiagent")
DEFAULT_WORKDIR = Path("/app")
RUNTIME_ROOT = Path("/tmp/multiagent-prod-swe")
RUNTIME_IDENTITY_PATH = RUNTIME_ROOT / "runtime-identity.json"
TASK_METADATA_PATH = Path(os.environ.get("EVAL_TASK_METADATA_FILE", "/tmp/evalscope-native-multiagent-metadata.json"))
CODEX_WRAPPER = RUNTIME_ROOT / "codex-bridge"
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "/tmp/multiagent-prod-swe/codex-home"))
ROLE_CODEX_HOME_ROOT = RUNTIME_ROOT / "role-codex-homes"
TMUX_SOCKET = RUNTIME_ROOT / "state" / "runtime_state" / "tmux.sock"
APPLY_PATCH_WRAPPER = RUNTIME_ROOT / "apply_patch"
STABLE_APPLY_PATCH = Path("/usr/local/bin/apply_patch")

PUBLIC_SOLVER_METADATA_KEYS = {"language", "problem_statement"}
PRIVATE_SOLVER_METADATA_KEYS = {
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

TEMPLATE_DIRS = [
    Path(__file__).resolve().with_name("templates"),
    Path(__file__).with_name("templates"),
]


def read_template(name: str) -> str:
    for template_dir in TEMPLATE_DIRS:
        path = template_dir / name
        if path.exists():
            return path.read_text(encoding="utf-8")
    searched = ", ".join(str(template_dir / name) for template_dir in TEMPLATE_DIRS)
    raise FileNotFoundError(f"missing native solver template {name}; searched: {searched}")


AUTONOMOUS_APPENDIX = read_template("swe_autonomous_appendix.md")


def log(message: str) -> None:
    print(f"[prod-multiagent-swe] {message}", flush=True)


def read_prompt(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    env_path = os.environ.get("EVAL_TASK_PROMPT_FILE")
    if env_path:
        return Path(env_path).read_text(encoding="utf-8")
    return sys.stdin.read()


def public_solver_metadata(metadata: dict[str, object]) -> dict[str, object]:
    """Strip benchmark-private fields before constructing the solver prompt."""

    public: dict[str, object] = {
        key: value
        for key, value in metadata.items()
        if key in PUBLIC_SOLVER_METADATA_KEYS and key not in PRIVATE_SOLVER_METADATA_KEYS
    }
    nested = metadata.get("swe_bench_pro")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key in PUBLIC_SOLVER_METADATA_KEYS and key not in public:
                public[key] = value
    return public


def read_task_metadata() -> dict[str, object]:
    if not TASK_METADATA_PATH.exists():
        return {}
    try:
        parsed = json.loads(TASK_METADATA_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log(f"ignoring invalid task metadata JSON at {TASK_METADATA_PATH}: {exc}")
        return {}
    if not isinstance(parsed, dict):
        return {}
    sanitized = public_solver_metadata(parsed)
    if sanitized != parsed:
        log("stripped non-public task metadata before solver prompting")
    return sanitized


def metadata_problem_text(metadata: dict[str, object] | None) -> str:
    if not metadata:
        return ""
    problem_statement = public_solver_metadata(metadata).get("problem_statement")
    return str(problem_statement) if problem_statement else ""


def issue_with_public_problem_text(issue: str, metadata: dict[str, object] | None = None) -> str:
    problem = metadata_problem_text(metadata)
    if not problem or problem.strip() == issue.strip():
        return issue
    if "</pr_description>" in issue and problem.strip() not in issue:
        return re.sub(
            r"\s*</pr_description>",
            "\n\n" + problem.rstrip() + "\n</pr_description>",
            issue,
            count=1,
            flags=re.IGNORECASE,
        )
    return issue.rstrip() + "\n\n" + problem


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 60,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    safe_args = [arg.replace("\x00", "") if isinstance(arg, str) else arg for arg in args]
    result = subprocess.run(
        safe_args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        tail = ((result.stderr or "") + "\n" + (result.stdout or "")).strip()[-4000:]
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(safe_args)}\n{tail}")
    return result
