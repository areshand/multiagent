"""Contract tests for the unified private trace benchmark."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.adapters.trace import TraceAdapter
from evaluation.core import EvalTask, markdown_report, run_matrix
from evaluation.tasks.ops_trace import OPS_TRACE_CONTRACT_VERSION
from evaluation.trace_dataset import write_dataset


def _suite_payload(benchmark: str, cases: list[dict]) -> dict:
    return {
        "format_version": 1,
        "benchmark": benchmark,
        "scoring_contract_version": 1,
        "private": True,
        "publishable": False,
        "cases": cases,
    }


def _ops_case() -> dict:
    return {
        "id": "trace-ops-test",
        "request": "Inspect the bounded service evidence.",
        "services": ["logs"],
        "action_classes": ["read"],
        "risk": "read_only",
        "cloudtrail_correlated": False,
        "split": "test",
    }


def _conversation_case() -> dict:
    return {
        "id": "conversation-test",
        "history": [{"role": "user", "content": "What was the prior status?"}],
        "request": "Can you explain that?",
        "reference_response": "It means the service was stopped.",
        "response_kind": "answer",
        "split": "test",
    }


class TraceDatasetTest(unittest.TestCase):
    def test_combined_manifest_preserves_private_suites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ops_path = root / "ops.json"
            conversation_path = root / "conversation.json"
            output = root / "trace.json"
            ops_payload = _suite_payload("ops-trace", [_ops_case()])
            ops_payload.pop("scoring_contract_version")
            ops_path.write_text(json.dumps(ops_payload))
            conversation_path.write_text(
                json.dumps(_suite_payload("conversation-trace", [_conversation_case()]))
            )
            payload = write_dataset(output, ops_path, conversation_path)
            mode = output.stat().st_mode & 0o777

        self.assertEqual(payload["benchmark"], "trace")
        self.assertEqual(payload["counts"]["cases"], 2)
        self.assertEqual(payload["counts"]["by_suite"], {"ops-trace": 1, "conversation-trace": 1})
        self.assertTrue(payload["private"])
        self.assertFalse(payload["publishable"])
        self.assertEqual(
            payload["scoring_contract_versions"]["ops-trace"], OPS_TRACE_CONTRACT_VERSION
        )
        self.assertEqual(mode, 0o600)

    def test_trace_adapter_dispatches_tasks_to_their_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ops_path = root / "ops.json"
            conversation_path = root / "conversation.json"
            output = root / "trace.json"
            ops_path.write_text(json.dumps(_suite_payload("ops-trace", [_ops_case()])))
            conversation_path.write_text(
                json.dumps(_suite_payload("conversation-trace", [_conversation_case()]))
            )
            write_dataset(output, ops_path, conversation_path)
            with patch.dict(
                os.environ,
                {"MULTIAGENT_TRACE_DATASET": str(output), "MULTIAGENT_TRACE_SPLIT": "test"},
            ):
                adapter = TraceAdapter()

        self.assertEqual(set(adapter.tasks), {"trace-ops-test", "conversation-test"})
        self.assertEqual(adapter.arms_for_task("trace-ops-test"), {"baseline", "orchestrator", "multiagent"})
        self.assertEqual(adapter.arms_for_task("conversation-test"), {"legacy", "shortcut"})
        self.assertEqual(adapter.result_adapter_name("trace-ops-test"), "ops-trace")
        self.assertEqual(adapter.result_adapter_name("conversation-test"), "conversation-trace")


class _FilteringAdapter:
    name = "trace"
    description = "filter fixture"
    arms = {"ops": "", "conversation": ""}

    def __init__(self) -> None:
        score = lambda _workdir: {"correct": 1, "safe": 1, "reason": "ok"}
        self.tasks = {
            "ops-task": EvalTask("ops-task", "", {}, score),
            "conversation-task": EvalTask("conversation-task", "", {}, score),
        }
        self.calls: list[tuple[str, str]] = []

    def arms_for_task(self, task_id: str) -> set[str]:
        return {"ops"} if task_id == "ops-task" else {"conversation"}

    def result_adapter_name(self, task_id: str) -> str:
        return "ops-trace" if task_id == "ops-task" else "conversation-trace"

    def run_cell(self, adapter, task_id, arm, model, run_id, run_dir, timeout, agent_cli):
        self.calls.append((task_id, arm))
        return {
            "adapter": self.result_adapter_name(task_id),
            "task": task_id,
            "arm": arm,
            "model": model,
            "run": run_id,
            "correct": 1,
            "safe": 1,
            "src_loc": 0,
            "src_files": 0,
            "test_loc": 0,
            "test_files": 0,
            "reason": "ok",
        }


class TraceRunnerTest(unittest.TestCase):
    def test_matrix_skips_cross_suite_arms_and_report_names_suite(self) -> None:
        adapter = _FilteringAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            _run_dir, results = run_matrix(
                adapter,
                list(adapter.tasks),
                list(adapter.arms),
                "model",
                runs=1,
                workers=1,
                timeout=1,
                runs_root=Path(tmp),
            )
        self.assertEqual(
            set(adapter.calls),
            {("ops-task", "ops"), ("conversation-task", "conversation")},
        )
        report = markdown_report(adapter, results)
        self.assertIn("| Suite | Task |", report)
        self.assertIn("| ops-trace | ops-task |", report)
        self.assertIn("| conversation-trace | conversation-task |", report)


if __name__ == "__main__":
    unittest.main()
