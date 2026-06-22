"""Adapter for orchestration and planning evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from evaluation.tasks.orchestration import SCENARIOS, score_plan
from evaluation.core import EvalTask, ROOT, die


CODEX_PLANNING_BASELINE = """\
You are Codex in planning mode.

Create a clear software implementation plan for the user's task. Identify the
work items, true dependencies, ownership boundaries, and verification steps.
Use the requested output format exactly. Do not assume any special multiagent
orchestrator policy beyond what the task itself states.
"""

ORCHESTRATION_ARMS = {
    "baseline": "Plain Codex planning-mode style prompt.",
    "orchestrator": "Current multiagent orchestrator prompt.",
}


def _orchestrator_system() -> str:
    prompt_path = ROOT / "orchestrator_prompt.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception:
        return "You are the multiagent orchestrator. Produce a correct parallel plan."


@dataclass
class OrchestrationAdapter:
    name: str = "orchestration"
    description: str = (
        "Synthetic orchestration tasks that score dependency accuracy, worker concurrency, "
        "path ownership, repo-native spawning, and final consolidation."
    )
    default_arms: str = "baseline,orchestrator"
    arms = ORCHESTRATION_ARMS

    def __post_init__(self) -> None:
        self.tasks = {
            task_id: EvalTask(
                id=task_id,
                prompt=scenario.prompt,
                seed={"scenario.md": scenario.prompt},
                score=lambda workdir, scenario=scenario: score_plan(workdir, scenario),
                file="plan.json",
                good=scenario.good,
                bad=scenario.bad,
                axis="safe",
            )
            for task_id, scenario in SCENARIOS.items()
        }

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        for rel, content in task.seed.items():
            path = workdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        content = task.good if kind == "good" else task.bad
        if content is None:
            raise ValueError(f"task {task.id} has no {kind} reference")
        target = workdir / (task.file or "plan.json")
        target.write_text(content, encoding="utf-8")

    def system_for_arm(self, arm: str) -> str:
        if arm == "baseline":
            return CODEX_PLANNING_BASELINE
        if arm == "orchestrator":
            return _orchestrator_system()
        die(f"unknown arm: {arm}; expected one of {', '.join(sorted(ORCHESTRATION_ARMS))}")


ADAPTER = OrchestrationAdapter()
