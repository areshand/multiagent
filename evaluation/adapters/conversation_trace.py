"""Adapter comparing legacy and shortcut production flows on conversational traces."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from evaluation.core import EvalTask
from evaluation.production_multiagent import run_production_cell
from evaluation.tasks.conversation_trace import (
    CONVERSATION_TRACE_CONTRACT_VERSION,
    SYNTHETIC_SCENARIOS,
    ConversationTraceScenario,
    scenario_from_dict,
    scenario_seed,
    score_conversation_result,
)


CONVERSATION_TRACE_ARMS = {
    "legacy": "Production runtime image built from the pre-shortcut main revision.",
    "shortcut": "Production runtime image containing direct-response and read-only routes.",
}


def _dataset_path() -> Path | None:
    configured = os.environ.get("MULTIAGENT_CONVERSATION_TRACE_DATASET")
    if configured in {"synthetic", "none", "off"}:
        return None
    if configured:
        return Path(configured).expanduser().resolve()
    default = Path.home() / "projects/traces/benchmark/conversation-trace-cases.json"
    return default if default.is_file() else None


def _load_scenarios() -> tuple[dict[str, ConversationTraceScenario], str]:
    path = _dataset_path()
    if path is None:
        return dict(SYNTHETIC_SCENARIOS), "built-in synthetic contract cases"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot load conversation-trace dataset {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError(f"conversation-trace dataset has invalid schema: {path}")
    split = os.environ.get("MULTIAGENT_CONVERSATION_TRACE_SPLIT", "test")
    if split not in {"train", "validation", "test", "all"}:
        raise ValueError(
            "MULTIAGENT_CONVERSATION_TRACE_SPLIT must be train, validation, test, or all"
        )
    raw_cases = [case for case in payload["cases"] if isinstance(case, dict)]
    selected = raw_cases if split == "all" else [case for case in raw_cases if case.get("split") == split]
    if not selected:
        raise ValueError(f"conversation-trace dataset {path} contains no cases for split {split!r}")
    scenarios = {scenario.id: scenario for scenario in map(scenario_from_dict, selected)}
    if len(scenarios) != len(selected):
        raise ValueError(f"conversation-trace dataset contains duplicate case IDs: {path}")
    return scenarios, f"private trace-derived dataset {path} split={split}"


@dataclass
class ConversationTraceAdapter:
    name: str = "conversation-trace"
    default_arms: str = "legacy,shortcut"
    scenarios_override: dict[str, ConversationTraceScenario] | None = None
    source_override: str | None = None
    arms = CONVERSATION_TRACE_ARMS

    def __post_init__(self) -> None:
        if self.scenarios_override is None:
            scenarios, source = _load_scenarios()
        else:
            scenarios = dict(self.scenarios_override)
            source = self.source_override or "injected scenarios"
        self.scenarios = scenarios
        self.description = (
            f"Conversation-trace contract v{CONVERSATION_TRACE_CONTRACT_VERSION}: compares "
            "production workflow route, role fanout, write safety, and latency on bounded "
            f"multi-turn replays using {source}. It does not judge semantic answer quality."
        )
        self.tasks = {
            task_id: EvalTask(
                id=task_id,
                prompt=scenario.prompt,
                seed=scenario_seed(scenario),
                score=lambda workdir, scenario=scenario: score_conversation_result(workdir, scenario),
                file="_multiagent_evidence.json",
                good=json.dumps(scenario.good_evidence(), indent=2, ensure_ascii=False) + "\n",
                bad=json.dumps(scenario.bad_evidence(), indent=2, ensure_ascii=False) + "\n",
                axis="safe",
            )
            for task_id, scenario in scenarios.items()
        }

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        for relative, content in task.seed.items():
            path = workdir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        content = task.good if kind == "good" else task.bad
        if content is None:
            raise ValueError(f"task {task.id} has no {kind} reference")
        (workdir / "_multiagent_evidence.json").write_text(content, encoding="utf-8")

    def run_cell(
        self,
        adapter: "ConversationTraceAdapter",
        task_id: str,
        arm: str,
        model: str,
        run_id: int,
        run_dir: Path,
        timeout: int,
        agent_cli: str,
    ) -> dict[str, object]:
        if agent_cli != "codex":
            raise ValueError("conversation-trace production arms require --agent-cli codex")
        env_name = f"MULTIAGENT_CONVERSATION_TRACE_{arm.upper()}_IMAGE"
        default_image = f"multiagent:conversation-trace-{arm}"
        runtime_root = Path(os.environ.get("MULTIAGENT_CONVERSATION_TRACE_RUNTIME_ROOT", "/tmp"))
        return run_production_cell(
            adapter=adapter,
            task_id=task_id,
            arm=arm,
            model=model,
            run_id=run_id,
            run_dir=run_dir,
            timeout=timeout,
            image=os.environ.get(env_name, default_image),
            runtime_prefix=f"conversation-eval-{arm}",
            prompt_profile="conversation",
            runtime_root=runtime_root,
        )


ADAPTER = ConversationTraceAdapter()
