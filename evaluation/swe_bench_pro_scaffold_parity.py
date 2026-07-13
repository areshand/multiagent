#!/usr/bin/env python3
"""Run SWE Bench Pro through EvalScope's scaffold-parity path.

This runner is for official-comparison work, not the older direct
``solve_patch`` pilot. It drives EvalScope's ``swe_bench_pro`` adapter with an
external Codex agent running inside the per-instance Docker image. EvalScope
then extracts ``git diff`` from ``/app`` and scores it with the benchmark's
Docker-side ``run_script.sh`` / ``parser.py`` verifier.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any


DEFAULT_REPORT_DIR = Path("evaluation/reports")
DEFAULT_EVALSCOPE_PATH = Path("/private/tmp/evalscope_tmp")
DEFAULT_PRO_REPO = Path("/private/tmp/SWE-bench_Pro-os-complete")
DEFAULT_WORK_DIR = Path("/private/tmp/evalscope-swe-bench-pro-scaffold-parity-public-nodebb")
DEFAULT_OUTPUT = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-parity-public-nodebb.json"
DEFAULT_CONFIG_JSON = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-parity-public-nodebb-config.json"
DEFAULT_CONFIG_YAML = DEFAULT_REPORT_DIR / "swe-bench-pro-scaffold-parity-public-nodebb-task-config.yaml"
DEFAULT_PREFLIGHT_OUTPUT = DEFAULT_REPORT_DIR / "swe-bench-pro-official-preflight.json"
DEFAULT_ON_DEMAND_IMAGE_STATUS = DEFAULT_REPORT_DIR / "swe-bench-pro-on-demand-image-status.json"
DEFAULT_IMAGE_ARCHIVE_DIR = Path("/private/tmp/swe-bench-pro-image-preload")
DEFAULT_PERSISTENT_CACHE_ROOT = Path("/private/tmp/swe-bench-pro-persistent-cache")
DEFAULT_NATIVE_SOLVER_COMMAND = "/tmp/evalscope-native-multiagent-solver.sh"
DEFAULT_NATIVE_SOLVER_SOURCE = Path(__file__).resolve().parents[1]
DEFAULT_FULL_SPLIT_SIZE = 731

COMPILE_FAILURE_PATTERNS = (
    "undefined:",
    "undefined method",
    "undefined field",
    "has no field or method",
    "build failed",
    "compile failed",
    "compilation failed",
)

SUBMISSION_GATE_REJECTION_PATTERNS = (
    "refusing to score rejected git diff",
    "coverage blockers remain",
    "validation coverage gate remained unresolved",
    "final patch changes code, but submission lacks hash-bound build verification",
)


def parse_limit(raw: str) -> int | None:
    if raw.lower() in {"none", "full", "all", "0"}:
        return None
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("--limit must be >= 1, or one of none/full/all/0")
    return value


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(ch in text for ch in ":#{}[],&*?|\n\r\t") or text.lower() in {"true", "false", "null"}:
        return json.dumps(text)
    return text


def to_yaml(value: Any, indent: int = 0) -> str:
    prefix = " " * indent
    if isinstance(value, dict):
        if not value:
            return f"{prefix}{{}}"
        lines: list[str] = []
        for key, item in value.items():
            if item == {}:
                lines.append(f"{prefix}{key}: {{}}")
                continue
            elif item == []:
                lines.append(f"{prefix}{key}: []")
                continue
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}{key}:")
                lines.append(to_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{prefix}[]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(to_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{yaml_scalar(value)}"


def scaffold_config(args: argparse.Namespace) -> dict[str, Any]:
    sandbox_default: dict[str, Any] = {
        "platform": args.platform,
    }
    if args.memory_limit:
        sandbox_default["memory_limit"] = args.memory_limit
    if args.cpu_limit:
        sandbox_default["cpu_limit"] = args.cpu_limit

    generation_config: dict[str, Any] = {
        "temperature": args.temperature,
    }
    if args.max_tokens is not None:
        generation_config["max_tokens"] = args.max_tokens

    effective_limit = None if sample_shard_enabled(args) else args.limit
    config: dict[str, Any] = {
        "model": args.model,
        "model_id": args.model_id,
        "eval_type": args.eval_type,
        "datasets": ["swe_bench_pro"],
        "dataset_args": {
            "swe_bench_pro": {
                "extra_params": {
                    "swe_bench_pro_repo_path": str(args.swe_bench_pro_repo_path),
                    "dockerhub_username": args.dockerhub_username,
                    "action_protocol": "toolcall",
                    "max_steps": args.max_steps,
                    "command_timeout": args.command_timeout,
                    "eval_timeout": args.eval_timeout,
                }
            }
        },
        "limit": effective_limit,
        "eval_batch_size": args.eval_batch_size,
        "generation_config": generation_config,
        "sandbox": {
            "enabled": True,
            "engine": "docker",
            "default_config": sandbox_default,
            "manager_config": {},
            "pool_size": None,
        },
        "agent_config": {
            "mode": "external",
            "framework": args.agent_framework,
            "timeout": args.agent_timeout,
            "kwargs": {
                "auto_install": not args.no_auto_install,
                "install_timeout_s": args.install_timeout,
                "model_name": args.agent_model_name,
                "working_dir": args.agent_working_dir,
            },
        },
        "work_dir": str(args.work_dir),
        "no_timestamp": True,
        "analysis_report": False,
        "collect_perf": True,
        "ignore_errors": args.ignore_errors,
        "seed": args.seed,
    }
    if args.api_url:
        config["api_url"] = args.api_url
    if args.api_key is not None:
        config["api_key"] = args.api_key
    if args.codex_home:
        config["agent_config"]["kwargs"]["home_override"] = args.codex_home
    if args.codex_npm_package:
        config["agent_config"]["kwargs"]["npm_package"] = args.codex_npm_package
    if args.agent_wire_api != "responses":
        config["agent_config"]["kwargs"].setdefault("extra_config", {})[
            "model_providers.evalscope.wire_api"
        ] = json.dumps(args.agent_wire_api)
    if args.agent_framework == "multiagent-native":
        config["agent_config"]["kwargs"]["command"] = args.native_solver_command
        config["agent_config"]["kwargs"]["setup_command"] = args.native_solver_setup_command
        config["agent_config"]["kwargs"]["working_dir"] = args.agent_working_dir
        config["agent_config"]["kwargs"]["swe_bench_pro_repo_path"] = str(args.swe_bench_pro_repo_path)
        config["agent_config"]["kwargs"]["swe_bench_pro_sample_offset"] = args.sample_offset
        config["agent_config"]["kwargs"]["score_failed_diff"] = args.score_failed_native_diff
        config["agent_config"]["kwargs"]["score_timed_out_diff"] = args.score_timed_out_native_diff
        if args.native_codex_auth_json:
            config["agent_config"]["kwargs"]["codex_auth_json"] = str(args.native_codex_auth_json)
            config["agent_config"]["kwargs"]["codex_auth_container_home"] = args.native_codex_auth_container_home
    if args.persistent_cache:
        config["sandbox"]["default_config"].setdefault("env_vars", {})["SWE_BENCH_PRO_PERSISTENT_CACHE"] = "1"
    return config


def sample_shard_enabled(args: argparse.Namespace) -> bool:
    return args.sample_offset > 0 or args.sample_count is not None


def scope_for_args(args: argparse.Namespace) -> str:
    if sample_shard_enabled(args):
        count = "to-end" if args.sample_count is None else str(args.sample_count)
        return f"offset-{args.sample_offset}-count-{count}"
    return "full" if args.limit is None else f"limit-{args.limit}"


def write_config(config: dict[str, Any], json_path: Path, yaml_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    yaml_path.write_text(to_yaml(config) + "\n", encoding="utf-8")


def dockerhub_image_uri(instance_id: str, dockerhub_username: str, repo_name: str) -> str:
    repo_base, repo_name_only = repo_name.lower().split("/")
    hsh = instance_id.replace("instance_", "")

    if instance_id == "instance_element-hq__element-web-ec0f940ef0e8e3b61078f145f34dc40d1938e6c5-vnan":
        repo_name_only = "element-web"
    elif "element-hq" in repo_name.lower() and "element-web" in repo_name.lower():
        repo_name_only = "element"
        if hsh.endswith("-vnan"):
            hsh = hsh[:-5]
    elif hsh.endswith("-vnan"):
        hsh = hsh[:-5]

    tag = f"{repo_base}.{repo_name_only}-{hsh}"
    if len(tag) > 128:
        tag = tag[:128]
    return f"{dockerhub_username}/sweap-images:{tag}"


def load_official_instances(repo_path: Path) -> list[dict[str, Any]]:
    dataset_path = repo_path / "helper_code" / "sweap_eval_full_v2.jsonl"
    if not dataset_path.exists():
        raise FileNotFoundError(f"SWE Bench Pro public JSONL is missing: {dataset_path}")
    instances: list[dict[str, Any]] = []
    with dataset_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            instance_id = str(row["instance_id"])
            repo_name = str(row.get("repo") or "")
            instances.append(
                {
                    "line": line_no,
                    "instance_id": instance_id,
                    "repo": repo_name,
                    "base_commit": row.get("base_commit"),
                    "image": dockerhub_image_uri(instance_id, "jefzda", repo_name),
                    "run_script_dir": str(repo_path / "run_scripts" / instance_id),
                }
            )
    return instances


def with_dockerhub_username(instances: list[dict[str, Any]], dockerhub_username: str) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for item in instances:
        copied = dict(item)
        copied["image"] = dockerhub_image_uri(str(item["instance_id"]), dockerhub_username, str(item["repo"]))
        updated.append(copied)
    return updated


def inspect_local_image(image: str) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker command not found"
    except subprocess.TimeoutExpired:
        return False, "docker image inspect timed out"
    if result.returncode == 0:
        return True, None
    return False, (result.stderr or "").strip().splitlines()[-1] if result.stderr else "docker image inspect failed"


def build_preflight_report(args: argparse.Namespace, *, inspect_docker: bool) -> dict[str, Any]:
    raw_instances = load_official_instances(args.swe_bench_pro_repo_path)
    instances = with_dockerhub_username(raw_instances, args.dockerhub_username)
    sample_shard = None
    if sample_shard_enabled(args):
        from evaluation.swe_bench_pro_shard import build_sample_shard

        sample_shard = build_sample_shard(offset=args.sample_offset, count=args.sample_count, instances=instances)
    missing_run_scripts: list[str] = []
    missing_parsers: list[str] = []
    missing_instance_info: list[str] = []
    for item in instances:
        run_dir = Path(str(item["run_script_dir"]))
        if not (run_dir / "run_script.sh").exists():
            missing_run_scripts.append(str(item["instance_id"]))
        if not (run_dir / "parser.py").exists():
            missing_parsers.append(str(item["instance_id"]))
        if not (run_dir / "instance_info.txt").exists():
            missing_instance_info.append(str(item["instance_id"]))

    local_present: list[str] = []
    local_missing: list[dict[str, str]] = []
    unique_images = sorted({str(item["image"]) for item in instances})
    if inspect_docker:
        for image in unique_images:
            present, error = inspect_local_image(image)
            if present:
                local_present.append(image)
            else:
                local_missing.append({"image": image, "error": error or ""})

    dataset_complete = len(instances) >= args.expected_full_split_size
    run_scripts_complete = not missing_run_scripts and not missing_parsers
    docker_checked = inspect_docker
    image_set_ready = docker_checked and not local_missing and len(local_present) == len(unique_images)
    official_scaffold_ready = dataset_complete and run_scripts_complete

    return {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "benchmark": "swe-bench-pro",
        "swe_bench_pro_repo_path": str(args.swe_bench_pro_repo_path),
        "dataset_jsonl": str(args.swe_bench_pro_repo_path / "helper_code" / "sweap_eval_full_v2.jsonl"),
        "dockerhub_username": args.dockerhub_username,
        "expected_full_split_size": args.expected_full_split_size,
        "instance_count": len(instances),
        "unique_image_count": len(unique_images),
        "dataset_complete": dataset_complete,
        "run_scripts_complete": run_scripts_complete,
        "official_scaffold_ready": official_scaffold_ready,
        "docker_local_checked": docker_checked,
        "official_image_set_ready": image_set_ready,
        "local_image_count": len(local_present),
        "missing_local_image_count": len(local_missing),
        "missing_run_script_count": len(missing_run_scripts),
        "missing_parser_count": len(missing_parsers),
        "missing_instance_info_count": len(missing_instance_info),
        "missing_run_scripts": missing_run_scripts[:50],
        "missing_parsers": missing_parsers[:50],
        "missing_instance_info": missing_instance_info[:50],
        "missing_local_images": local_missing[:50],
        "sample_shard": sample_shard.summary() if sample_shard else None,
        "instances": instances,
    }


def json_safe(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return str(value)


def ensure_evalscope_path(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"EvalScope path does not exist: {path}")
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def find_evalscope_report(work_dir: Path, model_id: str) -> Path | None:
    candidates = [
        work_dir / "reports" / model_id / "swe_bench_pro.json",
        work_dir / "reports" / "swe_bench_pro.json",
    ]
    candidates.extend(sorted((work_dir / "reports").glob("*/swe_bench_pro.json")) if (work_dir / "reports").exists() else [])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def native_runner_summary(work_dir: Path) -> dict[str, Any] | None:
    log_path = work_dir / "logs" / "eval_log.log"
    if not log_path.exists():
        return None

    exit_events: list[dict[str, Any]] = []
    scored_failed_diff = False
    scored_timed_out_diff = False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(
            r"multiagent-native exited: sample=(?P<sample>\S+) rc=(?P<rc>-?\d+) "
            r"wall=(?P<wall>[0-9.]+)s timed_out=(?P<timed_out>True|False)",
            line,
        )
        if match:
            exit_events.append(
                {
                    "sample": match.group("sample"),
                    "returncode": int(match.group("rc")),
                    "wall_time_s": float(match.group("wall")),
                    "timed_out": match.group("timed_out") == "True",
                }
            )
        if "multiagent-native exited with code" in line and "scoring current git diff by explicit config" in line:
            scored_failed_diff = True
        if "multiagent-native timed out" in line and "scoring current git diff by explicit config" in line:
            scored_timed_out_diff = True

    if not exit_events and not scored_failed_diff and not scored_timed_out_diff:
        return None

    latest = exit_events[-1] if exit_events else None
    clean = bool(latest and latest["returncode"] == 0 and not latest["timed_out"])
    return {
        "latest": latest,
        "all_exit_events": exit_events,
        "clean_native_completion": clean,
        "scored_failed_native_diff": scored_failed_diff,
        "scored_timed_out_native_diff": scored_timed_out_diff,
        "diagnostic_scored_diff": scored_failed_diff or scored_timed_out_diff,
    }


def read_failure_artifact_text(work_dir: Path, run_result: dict[str, Any] | None, evalscope_report: dict[str, Any] | None) -> str:
    chunks: list[str] = []
    if run_result:
        chunks.append(json.dumps(json_safe(run_result), sort_keys=True))
    if evalscope_report:
        chunks.append(json.dumps(json_safe(evalscope_report), sort_keys=True))
    artifact_paths = [work_dir / "logs" / "eval_log.log"]
    reports_dir = work_dir / "reports"
    if reports_dir.exists():
        artifact_paths.extend(sorted(reports_dir.glob("**/*.json"))[:8])
    for path in artifact_paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace")[-200_000:])
        except OSError:
            continue
    return "\n".join(chunks).lower()


def failure_postmortem(
    *,
    work_dir: Path,
    run_result: dict[str, Any] | None,
    evalscope_report: dict[str, Any] | None,
    score: float | None,
    native_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    text = read_failure_artifact_text(work_dir, run_result, evalscope_report)
    if not text:
        return None

    compile_markers = [marker for marker in COMPILE_FAILURE_PATTERNS if marker in text]
    submission_gate_markers = [marker for marker in SUBMISSION_GATE_REJECTION_PATTERNS if marker in text]
    native_clean = bool(native_summary and native_summary.get("clean_native_completion"))
    latest_native = native_summary.get("latest") if isinstance(native_summary, dict) else None
    native_returncode = latest_native.get("returncode") if isinstance(latest_native, dict) else None
    native_rejected = bool(native_summary and not native_clean and submission_gate_markers)

    if compile_markers and score == 0 and native_clean:
        return {
            "category": "official_compile_failure",
            "root_cause": "submission_invariant_gap",
            "markers": compile_markers,
            "required_response": (
                "Stop prompt/adapter recovery work and strengthen the build verifier/submission gate. "
                "A patch that fails compile/build must not reach the official verifier."
            ),
        }
    if native_returncode == 124:
        return {
            "category": "native_timeout_without_submission",
            "root_cause": "terminal_state_gap",
            "markers": submission_gate_markers[:4],
            "required_response": (
                "Treat this as a production orchestration terminal-state failure. If repository-visible "
                "validation passed, the native wrapper must either recover a machine-readable completed "
                "status before timeout or write an explicit blocked status with remaining blockers."
            ),
        }
    if native_rejected:
        return {
            "category": "native_submission_gate_rejection",
            "root_cause": "pre_official_acceptance_invariant_blocked_submission",
            "markers": submission_gate_markers[:4],
            "required_response": (
                "Do not count this as an official solver miss. Inspect the blocked invariants, then fix the "
                "orchestrator/verifier structured evidence or source patch before rerunning."
            ),
        }
    if compile_markers and score == 0:
        return {
            "category": "compile_failure_detected",
            "root_cause": "build_correctness_failure",
            "markers": compile_markers,
            "required_response": (
                "Route analysis to the build verifier and changed-package compile/test gate before hidden-contract work."
            ),
        }
    return None


def summarize_result(
    *,
    args: argparse.Namespace,
    config: dict[str, Any],
    run_result: dict[str, Any] | None,
    evalscope_report_path: Path | None,
    preflight: dict[str, Any] | None,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    status: str,
) -> dict[str, Any]:
    evalscope_report = None
    if evalscope_report_path is not None and evalscope_report_path.exists():
        evalscope_report = json.loads(evalscope_report_path.read_text(encoding="utf-8"))

    score = None
    sample_size = None
    if evalscope_report is not None:
        score = evalscope_report.get("score")
        sample_size = evalscope_report.get("num")
    native_summary = (
        native_runner_summary(args.work_dir)
        if config.get("agent_config", {}).get("framework") == "multiagent-native"
        else None
    )
    clean_native_score = score
    diagnostic_score = None
    if native_summary and native_summary.get("diagnostic_scored_diff"):
        diagnostic_score = score
        clean_native_score = None
    elif native_summary and not native_summary.get("clean_native_completion"):
        clean_native_score = None

    postmortem = failure_postmortem(
        work_dir=args.work_dir,
        run_result=run_result,
        evalscope_report=evalscope_report,
        score=score,
        native_summary=native_summary,
    )

    scaffold_parity = (
        status == "completed"
        and config["agent_config"]["mode"] == "external"
        and config["agent_config"]["framework"] in {"codex", "codex-devnull", "multiagent-native"}
        and config["dataset_args"]["swe_bench_pro"]["extra_params"]["command_timeout"] >= 60
        and config["dataset_args"]["swe_bench_pro"]["extra_params"]["eval_timeout"] >= 3600
    )
    official_scaffold_ready = bool(preflight.get("official_scaffold_ready")) if preflight else False
    official_image_set_ready = bool(preflight.get("official_image_set_ready")) if preflight else False
    image_provider_ready = official_image_set_ready or args.on_demand_image_preload
    selected_official_verifier_ready = official_scaffold_ready
    if preflight and sample_shard_enabled(args):
        selected = (preflight.get("sample_shard") or {}).get("selected_instances") or []
        selected_ids = {str(item.get("instance_id")) for item in selected if isinstance(item, dict)}
        missing_run_scripts = set(preflight.get("missing_run_scripts") or [])
        missing_parsers = set(preflight.get("missing_parsers") or [])
        selected_official_verifier_ready = bool(selected_ids) and not (
            selected_ids & (missing_run_scripts | missing_parsers)
        )
    official_ready = (
        status == "completed"
        and sample_size is not None
        and sample_size > 0
        and (sample_shard_enabled(args) or (args.limit is not None and args.limit >= 1))
        and selected_official_verifier_ready
        and image_provider_ready
    )
    full_official = (
        scaffold_parity
        and args.limit is None
        and not sample_shard_enabled(args)
        and official_scaffold_ready
        and image_provider_ready
    )

    notes = (
        "SWE Bench Pro scaffold-parity run using EvalScope external Codex runner "
        "inside the per-instance Docker image, with official run_script/parser scoring. "
        "A limited run is official-verifier evidence but not a full benchmark score."
    )

    return {
        "generated_at": completed_at.isoformat(timespec="seconds"),
        "started_at": started_at.isoformat(timespec="seconds"),
        "benchmark": "swe-bench-pro",
        "status": status,
        "score": score,
        "clean_native_score": clean_native_score,
        "diagnostic_score": diagnostic_score,
        "sample_size": sample_size,
        "official": full_official,
        "official_verifier_evidence": official_ready,
        "full_official_candidate": full_official,
        "metric": "resolved_percent",
        "scope": scope_for_args(args),
        "work_dir": str(args.work_dir),
        "evalscope_report": str(evalscope_report_path) if evalscope_report_path else None,
        "task_config_json": str(args.config_json),
        "task_config_yaml": str(args.config_yaml),
        "preflight_report": str(args.preflight_output),
        "evalscope_result": json_safe(run_result),
        "native_runner": native_summary,
        "failure_postmortem": postmortem,
        "parity": {
            "dataset": "ScaleAI/SWE-bench_Pro",
            "adapter": "evalscope swe_bench_pro",
            "agent_config": f"external {config['agent_config']['framework']}",
            "runs_inside_per_instance_docker": True,
            "patch_source": "git diff extracted from /app after external runner",
            "verifier": "SWE Bench Pro run_script.sh plus parser.py via EvalScope eval_instance",
            "swe_bench_pro_repo_path": str(args.swe_bench_pro_repo_path),
            "dockerhub_username": args.dockerhub_username,
            "platform": args.platform,
            "command_timeout": args.command_timeout,
            "agent_timeout": args.agent_timeout,
            "eval_timeout": args.eval_timeout,
            "auto_install_codex_in_container": not args.no_auto_install,
            "agent_model_name": args.agent_model_name,
            "agent_working_dir": args.agent_working_dir,
            "official_scaffold_ready": official_scaffold_ready,
            "selected_official_verifier_ready": selected_official_verifier_ready,
            "official_image_set_ready": official_image_set_ready,
            "image_provider_ready": image_provider_ready,
            "image_availability_strategy": "on-demand" if args.on_demand_image_preload else "preloaded",
            "on_demand_prune_after_sample": args.on_demand_prune_after_sample,
            "persistent_cache": args.persistent_cache,
            "persistent_cache_root": str(args.persistent_cache_root) if args.persistent_cache else None,
            "persistent_cache_mode": args.persistent_cache_mode if args.persistent_cache else None,
            "bake_native_solver": getattr(args, "bake_native_solver", False),
            "native_solver_source": (
                str(args.native_solver_source) if getattr(args, "bake_native_solver", False) else None
            ),
            "native_codex_auth_mode": "chatgpt-auth-json" if args.native_codex_auth_json else "bridge",
            "native_codex_auth_container_home": (
                args.native_codex_auth_container_home if args.native_codex_auth_json else None
            ),
            "score_failed_native_diff": getattr(args, "score_failed_native_diff", False),
            "score_timed_out_native_diff": getattr(args, "score_timed_out_native_diff", False),
        },
        "on_demand_image_status": (
            {
                "path": str(args.on_demand_image_status),
                "exists": args.on_demand_image_status.exists(),
            }
            if args.on_demand_image_preload
            else None
        ),
        "sample_shard": preflight.get("sample_shard") if preflight else None,
        "preflight": json_safe(preflight),
        "system_results": {
            "system": (
                "ours-multiagent-swe-bench-pro-scaffold-parity"
                if config["agent_config"]["framework"] == "multiagent-native"
                else "ours-codex-swe-bench-pro-scaffold-parity"
            ),
            "source": str(args.output),
            "results": [
                {
                    "benchmark": "swe-bench-pro",
                    "score": score,
                    "metric": "resolved_percent",
                    "sample_size": sample_size,
                    "official": full_official,
                    "duration_s": round((completed_at - started_at).total_seconds(), 3),
                    "notes": notes,
                }
            ],
        },
        "notes": notes,
    }


def copy_evalscope_artifacts(work_dir: Path, report_dir: Path, prefix: str, model_id: str) -> dict[str, str]:
    copied: dict[str, str] = {}
    mappings = {
        "log": work_dir / "logs" / "eval_log.log",
        "task_config": work_dir / "configs" / "task_config.yaml",
        "report": find_evalscope_report(work_dir, model_id),
    }
    for name, source in mappings.items():
        if source is None or not source.exists():
            continue
        suffix = source.suffix or ".txt"
        dest = report_dir / f"{prefix}-{name}{suffix}"
        shutil.copyfile(source, dest)
        copied[name] = str(dest)
    return copied


def run_evalscope(config: dict[str, Any], evalscope_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    ensure_evalscope_path(evalscope_path)
    if config.get("agent_config", {}).get("framework") == "codex-devnull":
        import evaluation.evalscope_codex_devnull_runner  # noqa: F401
    if config.get("agent_config", {}).get("framework") == "multiagent-native":
        import evaluation.evalscope_multiagent_native_runner  # noqa: F401
    if config.get("agent_config", {}).get("framework") == "noop":
        import evaluation.evalscope_noop_runner  # noqa: F401
    if (
        config.get("agent_config", {}).get("framework") in {"codex", "codex-devnull"}
        and args.agent_wire_api == "responses"
        and args.responses_keepalive
    ):
        from evaluation.evalscope_responses_keepalive import install_responses_keepalive_patch

        install_responses_keepalive_patch(ping_interval_s=args.responses_keepalive_interval)
    if sample_shard_enabled(args):
        from evaluation.swe_bench_pro_shard import build_sample_shard, install_sample_shard_hooks

        instances = with_dockerhub_username(load_official_instances(args.swe_bench_pro_repo_path), args.dockerhub_username)
        shard = build_sample_shard(offset=args.sample_offset, count=args.sample_count, instances=instances)
        install_sample_shard_hooks(shard)
    if args.persistent_cache:
        from evaluation.swe_bench_pro_cache import PersistentCacheManager, install_persistent_cache_hooks

        install_persistent_cache_hooks(
            PersistentCacheManager(
                cache_root=args.persistent_cache_root,
                platform=args.platform,
                mode=args.persistent_cache_mode,
            )
        )
    image_manager = None
    if args.on_demand_image_preload:
        from evaluation.swe_bench_pro_on_demand import OnDemandImageManager, install_on_demand_image_hooks

        image_manager = OnDemandImageManager(
            archive_dir=args.on_demand_archive_dir,
            status_path=args.on_demand_image_status,
            platform=args.platform,
            image_timeout=args.on_demand_image_timeout,
            retries=args.on_demand_retry_rate_limit,
            backoff_s=args.on_demand_retry_backoff,
            min_free_gb=args.on_demand_min_free_gb,
            prune_after_sample=args.on_demand_prune_after_sample,
            bake_native_solver=args.bake_native_solver,
            native_solver_source=args.native_solver_source,
        )
        install_on_demand_image_hooks(image_manager)
    from evalscope.run import run_task

    try:
        result = run_task(config)
    except Exception:
        if image_manager is not None:
            image_manager.finalize("failed")
        raise
    if image_manager is not None:
        image_manager.finalize("completed")
    if isinstance(result, dict):
        return result
    return {"result": result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evalscope-path", type=Path, default=DEFAULT_EVALSCOPE_PATH)
    parser.add_argument("--swe-bench-pro-repo-path", type=Path, default=DEFAULT_PRO_REPO)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config-json", type=Path, default=DEFAULT_CONFIG_JSON)
    parser.add_argument("--config-yaml", type=Path, default=DEFAULT_CONFIG_YAML)
    parser.add_argument("--preflight-output", type=Path, default=DEFAULT_PREFLIGHT_OUTPUT)
    parser.add_argument("--on-demand-image-status", type=Path, default=DEFAULT_ON_DEMAND_IMAGE_STATUS)
    parser.add_argument("--report-prefix", default="swe-bench-pro-scaffold-parity-public-nodebb")
    parser.add_argument("--model", default="codex-local")
    parser.add_argument("--model-id", default="codex-scaffold-parity")
    parser.add_argument("--eval-type", default="openai_api")
    parser.add_argument("--agent-framework", default="codex-devnull", choices=["codex-devnull", "codex", "noop", "multiagent-native"])
    parser.add_argument("--agent-model-name", default="gpt-5")
    parser.add_argument("--agent-working-dir", default="/app")
    parser.add_argument("--native-solver-command", default=DEFAULT_NATIVE_SOLVER_COMMAND)
    parser.add_argument("--native-solver-setup-command", default="")
    parser.add_argument("--bake-native-solver", action="store_true")
    parser.add_argument("--native-solver-source", type=Path, default=DEFAULT_NATIVE_SOLVER_SOURCE)
    parser.add_argument(
        "--native-codex-auth-json",
        default=os.environ.get("NATIVE_CODEX_AUTH_JSON", ""),
        help="host path to Codex auth.json copied into each live task container at runtime; never baked into images",
    )
    parser.add_argument("--native-codex-auth-container-home", default="/root/.codex-multiagent-prod")
    parser.add_argument(
        "--score-failed-native-diff",
        action="store_true",
        help="opt in to official scoring of git diff after a nonzero native solver exit",
    )
    parser.add_argument(
        "--score-timed-out-native-diff",
        action="store_true",
        help="opt in to official scoring of git diff after the native solver times out",
    )
    parser.add_argument("--agent-wire-api", default="responses", choices=["responses", "chat"])
    parser.add_argument("--responses-keepalive-interval", type=float, default=10.0)
    parser.add_argument(
        "--responses-keepalive",
        action="store_true",
        help="enable the experimental Responses SSE keepalive monkeypatch",
    )
    parser.add_argument(
        "--no-responses-keepalive",
        action="store_false",
        dest="responses_keepalive",
        help="use EvalScope's native Responses stream path (default)",
    )
    parser.add_argument("--api-url", default=os.environ.get("EVALSCOPE_MODEL_API_URL", "http://127.0.0.1:8765/v1"))
    parser.add_argument("--api-key", default=os.environ.get("EVALSCOPE_MODEL_API_KEY", "EMPTY"))
    parser.add_argument("--limit", type=parse_limit, default=1)
    parser.add_argument("--sample-offset", type=int, default=0, help="official JSONL row offset for sharded runs")
    parser.add_argument("--sample-count", type=int, help="number of official JSONL rows to run from --sample-offset")
    parser.add_argument("--eval-batch-size", type=int, default=1)
    parser.add_argument("--dockerhub-username", default="jefzda")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--memory-limit", default="")
    parser.add_argument("--cpu-limit", default="")
    parser.add_argument("--max-steps", type=int, default=250)
    parser.add_argument("--command-timeout", type=float, default=60.0)
    parser.add_argument("--agent-timeout", type=float, default=3600.0)
    parser.add_argument("--eval-timeout", type=int, default=3600)
    parser.add_argument("--install-timeout", type=float, default=600.0)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--expected-full-split-size", type=int, default=DEFAULT_FULL_SPLIT_SIZE)
    parser.add_argument("--codex-home", default="")
    parser.add_argument("--codex-npm-package", default="")
    parser.add_argument("--no-auto-install", action="store_true")
    parser.add_argument("--ignore-errors", action="store_true")
    parser.add_argument("--on-demand-image-preload", action="store_true")
    parser.add_argument("--on-demand-archive-dir", type=Path, default=DEFAULT_IMAGE_ARCHIVE_DIR)
    parser.add_argument("--on-demand-image-timeout", type=int, default=600)
    parser.add_argument("--on-demand-retry-rate-limit", type=int, default=3)
    parser.add_argument("--on-demand-retry-backoff", type=int, default=180)
    parser.add_argument("--on-demand-min-free-gb", type=float, default=50.0)
    parser.add_argument("--on-demand-prune-after-sample", action="store_true")
    parser.add_argument("--persistent-cache", action="store_true")
    parser.add_argument("--persistent-cache-root", type=Path, default=DEFAULT_PERSISTENT_CACHE_ROOT)
    parser.add_argument("--persistent-cache-mode", default="rw", choices=["rw", "ro"])
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--no-docker-inspect", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--write-config-only", action="store_true")
    parser.add_argument("--summarize-only", action="store_true", help="write summary JSON from an existing work_dir")
    args = parser.parse_args()
    if args.sample_offset < 0:
        parser.error("--sample-offset must be >= 0")
    if args.sample_count is not None and args.sample_count < 1:
        parser.error("--sample-count must be >= 1")
    if args.agent_framework == "multiagent-native" and args.bake_native_solver and args.native_solver_source.is_file():
        parser.error(
            "--bake-native-solver for multiagent-native must use the multiagent repo root, not a single solver file. "
            "A file source bakes the eval scaffold only and does not evaluate the production orchestrator/worker/verifier workflow."
        )

    config = scaffold_config(args)
    write_config(config, args.config_json, args.config_yaml)
    preflight: dict[str, Any] | None = None
    should_preflight = not args.no_preflight and (args.preflight_only or not args.write_config_only)
    if should_preflight:
        preflight = build_preflight_report(args, inspect_docker=not args.no_docker_inspect)
        args.preflight_output.parent.mkdir(parents=True, exist_ok=True)
        args.preflight_output.write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if args.write_config_only:
        print(f"wrote {args.config_json}")
        print(f"wrote {args.config_yaml}")
        if preflight is not None:
            print(f"wrote {args.preflight_output}")
        return 0
    if args.preflight_only:
        if preflight is None:
            preflight = build_preflight_report(args, inspect_docker=not args.no_docker_inspect)
            args.preflight_output.parent.mkdir(parents=True, exist_ok=True)
            args.preflight_output.write_text(json.dumps(preflight, indent=2), encoding="utf-8")
        print(f"wrote {args.preflight_output}")
        return 0

    started_at = dt.datetime.now(dt.UTC)
    status = "completed"
    run_result: dict[str, Any] | None = None
    if args.summarize_only:
        run_result = {"status": "summarized-existing-work-dir"}
    else:
        try:
            run_result = run_evalscope(config, args.evalscope_path, args)
        except Exception as exc:
            status = "failed"
            run_result = {"error": repr(exc), "traceback": traceback.format_exc()}
    completed_at = dt.datetime.now(dt.UTC)

    evalscope_report_path = find_evalscope_report(args.work_dir, args.model_id)
    payload = summarize_result(
        args=args,
        config=config,
        run_result=run_result,
        evalscope_report_path=evalscope_report_path,
        preflight=preflight,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
    )
    artifacts = copy_evalscope_artifacts(args.work_dir, args.output.parent, args.report_prefix, args.model_id)
    if artifacts:
        payload["copied_artifacts"] = artifacts

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.output}")
    if status != "completed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
