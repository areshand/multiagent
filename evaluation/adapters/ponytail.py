"""Ponytail safety/minimalism evaluation adapter."""

from __future__ import annotations

from pathlib import Path

from evaluation.tasks.ponytail import TASKS as RAW_TASKS
from evaluation.core import EvalTask


class PonytailAdapter:
    name = "ponytail"
    description = (
        "Small deterministic coding evaluation for measuring whether Ponytail-style "
        "worker instructions reduce code while preserving correctness and safety."
    )

    def __init__(self) -> None:
        self.tasks = {
            task_id: EvalTask(
                id=task_id,
                prompt=task["prompt"],
                seed=dict(task["seed"]),
                score=task["score"],
                file=task.get("file"),
                good=task.get("good"),
                bad=task.get("bad"),
                axis=task.get("axis", "safe"),
            )
            for task_id, task in RAW_TASKS.items()
        }

    def write_seed(self, workdir: Path, task: EvalTask) -> None:
        for rel, content in task.seed.items():
            path = workdir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def write_reference(self, workdir: Path, task: EvalTask, kind: str) -> None:
        content = task.good if kind == "good" else task.bad
        if task.file is None or content is None:
            raise ValueError(f"task {task.id} has no {kind} reference")
        path = workdir / task.file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


ADAPTER = PonytailAdapter()
