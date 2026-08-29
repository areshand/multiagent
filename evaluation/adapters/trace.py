"""Unified entry point for ops and conversational trace benchmark suites."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from evaluation.adapters.conversation_trace import (
    CONVERSATION_TRACE_ARMS,
    ConversationTraceAdapter,
)
from evaluation.adapters.ops_trace import OPS_TRACE_ARMS, OpsTraceAdapter
from evaluation.core import EvalTask
from evaluation.tasks.conversation_trace import (
    SYNTHETIC_SCENARIOS as SYNTHETIC_CONVERSATION_SCENARIOS,
)
from evaluation.tasks.conversation_trace import (
    scenario_from_dict as conversation_scenario_from_dict,
)
from evaluation.tasks.ops_trace import SYNTHETIC_SCENARIOS as SYNTHETIC_OPS_SCENARIOS
from evaluation.tasks.ops_trace import scenario_from_dict as ops_scenario_from_dict


TRACE_ARMS = {**OPS_TRACE_ARMS, **CONVERSATION_TRACE_ARMS}


def _dataset_path() -> Path | None:
    configured = os.environ.get("MULTIAGENT_TRACE_DATASET")
    if configured in {"synthetic", "none", "off"}:
        return None
    if configured:
        return Path(configured).expanduser().resolve()
    default = Path.home() / "projects" / "traces" / "benchmark" / "trace-cases.json"
    return default if default.is_file() else None


def _selected_cases(payload: dict, suite: str, split: str, path: Path) -> list[dict]:
    suites = payload.get("suites")
    nested = suites.get(suite) if isinstance(suites, dict) else None
    if not isinstance(nested, dict) or nested.get("benchmark") != suite:
        raise ValueError(f"trace dataset {path} is missing suite {suite!r}")
    raw_cases = nested.get("cases")
    if not isinstance(raw_cases, list):
        raise ValueError(f"trace dataset suite {suite!r} has invalid cases: {path}")
    cases = [case for case in raw_cases if isinstance(case, dict)]
    selected = cases if split == "all" else [case for case in cases if case.get("split") == split]
    if not selected:
        raise ValueError(
            f"trace dataset {path} suite {suite!r} has no cases for split {split!r}"
        )
    return selected


def _load_scenarios() -> tuple[dict, dict, str]:
    path = _dataset_path()
    if path is None:
        return (
            dict(SYNTHETIC_OPS_SCENARIOS),
            dict(SYNTHETIC_CONVERSATION_SCENARIOS),
            "built-in synthetic contract cases",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"cannot load unified trace dataset {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format_version") != 1
        or payload.get("benchmark") != "trace"
        or payload.get("private") is not True
        or payload.get("publishable") is not False
    ):
        raise ValueError(f"unified trace dataset has invalid schema or privacy flags: {path}")
    split = os.environ.get("MULTIAGENT_TRACE_SPLIT", "test")
    if split not in {"train", "validation", "test", "all"}:
        raise ValueError("MULTIAGENT_TRACE_SPLIT must be train, validation, test, or all")
    ops_cases = _selected_cases(payload, "ops-trace", split, path)
    conversation_cases = _selected_cases(payload, "conversation-trace", split, path)
    ops_scenarios = {scenario.id: scenario for scenario in map(ops_scenario_from_dict, ops_cases)}
    conversation_scenarios = {
        scenario.id: scenario
        for scenario in map(conversation_scenario_from_dict, conversation_cases)
    }
    if len(ops_scenarios) != len(ops_cases) or len(conversation_scenarios) != len(
        conversation_cases
    ):
        raise ValueError(f"unified trace dataset contains duplicate case IDs: {path}")
    if set(ops_scenarios) & set(conversation_scenarios):
        raise ValueError(f"unified trace dataset contains cross-suite duplicate case IDs: {path}")
    return ops_scenarios, conversation_scenarios, f"private unified dataset {path} split={split}"


@dataclass
class TraceAdapter:
    name: str = "trace"
    default_arms: str = "baseline,multiagent,legacy,shortcut"
    arms = TRACE_ARMS

    def __post_init__(self) -> None:
        ops_scenarios, conversation_scenarios, source = _load_scenarios()
        self.ops = OpsTraceAdapter(
            scenarios_override=ops_scenarios,
            source_override=f"{source} suite=ops-trace",
        )
        self.conversation = ConversationTraceAdapter(
            scenarios_override=conversation_scenarios,
            source_override=f"{source} suite=conversation-trace",
        )
        duplicate_ids = set(self.ops.tasks) & set(self.conversation.tasks)
        if duplicate_ids:
            raise ValueError(
                f"duplicate task IDs across trace suites: {', '.join(sorted(duplicate_ids))}"
            )
        self.tasks = {**self.ops.tasks, **self.conversation.tasks}
        self.description = (
            "Unified private trace benchmark. Ops tasks retain their solve and authority-boundary "
            "scorer; conversation tasks retain their route, fanout, write-safety, and latency "
            f"scorer. Suite metrics are reported separately using {source}."
        )

    def _owner(self, task_id: str) -> OpsTraceAdapter | ConversationTraceAdapter:
        if task_id in self.ops.tasks:
            return self.ops
        if task_id in self.conversation.tasks:
            return self.conversation
        raise KeyError(task_id)

    def result_adapter_name(self, task_id: str) -> str:
        return self._owner(task_id).name

    def arms_for_task(self, task_id: str) -> set[str]:
        return set(self._owner(task_id).arms)

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        self._owner(task.id).write_seed(workdir, task)

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        self._owner(task.id).write_reference(workdir, task, kind)

    def run_cell(
        self,
        adapter: "TraceAdapter",
        task_id: str,
        arm: str,
        model: str,
        run_id: int,
        run_dir: Path,
        timeout: int,
        agent_cli: str,
    ) -> dict[str, object]:
        owner = self._owner(task_id)
        if arm not in owner.arms:
            raise ValueError(f"arm {arm!r} is not compatible with {owner.name} task {task_id!r}")
        return owner.run_cell(owner, task_id, arm, model, run_id, run_dir, timeout, agent_cli)


ADAPTER = TraceAdapter()
