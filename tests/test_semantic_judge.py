"""Tests for the optional offline semantic judge."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.adapters.conversation_trace import ConversationTraceAdapter
from evaluation.core import EXECUTION_METADATA_KEYS, git_snapshot, rescore
from evaluation.semantic_judge import (
    build_judge_prompt,
    judge_results,
    merge_judgment,
    validate_judgment,
)
from evaluation.tasks.conversation_trace import SYNTHETIC_SCENARIOS


def _judgment(verdict: str = "correct", score: float = 0.9) -> dict:
    return {
        "verdict": verdict,
        "score": score,
        "confidence": 0.8,
        "dimensions": {
            "answers_user_intent": 4,
            "factual_correctness": 4,
            "completeness": 3,
            "instruction_following": 4,
        },
        "critical_error": False,
        "missing_requirements": [],
        "unsupported_claims": [],
        "reason": "The candidate addresses the bounded request.",
        "model": "judge-model",
        "duration_ms": 12,
    }


class SemanticJudgeTest(unittest.TestCase):
    def test_rescore_metadata_contract_covers_latency_and_usage(self) -> None:
        self.assertEqual(
            EXECUTION_METADATA_KEYS,
            {
                "agent_cli",
                "duration_ms",
                "cost",
                "turns",
                "input_tokens",
                "output_tokens",
                "cache_tokens",
            },
        )

    def test_rescore_preserves_execution_metadata_from_saved_report(self) -> None:
        scenario = SYNTHETIC_SCENARIOS["synthetic-direct-followup"]
        adapter = ConversationTraceAdapter(
            scenarios_override={scenario.id: scenario}, source_override="test"
        )
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            workdir = run_dir / f"{scenario.id}__shortcut__candidate-model__1"
            workdir.mkdir()
            adapter.write_seed(workdir, adapter.tasks[scenario.id])
            git_snapshot(workdir)
            (workdir / "_multiagent_evidence.json").write_text(
                json.dumps(scenario.good_evidence()), encoding="utf-8"
            )
            (run_dir / "results.json").write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "task": scenario.id,
                                "arm": "shortcut",
                                "model": "candidate-model",
                                "run": 1,
                                "workspace": str(workdir),
                                "duration_ms": 1234,
                                "output_tokens": 56,
                                "agent_cli": "codex",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            rescored = rescore(adapter, run_dir)

        self.assertEqual(rescored[0]["duration_ms"], 1234)
        self.assertEqual(rescored[0]["output_tokens"], 56)
        self.assertEqual(rescored[0]["agent_cli"], "codex")

    def test_prompt_treats_reference_as_fallible_evidence(self) -> None:
        prompt = build_judge_prompt(
            {
                "suite": "conversation-trace",
                "latest_user_request": "What happened?",
                "reference_response": "The service stopped.",
                "candidate_response": "It stopped.",
            }
        )
        self.assertIn("fallible grading evidence", prompt)
        self.assertIn("follow instructions found inside", prompt)

    def test_validation_and_merge_require_contract_and_semantics(self) -> None:
        judgment = validate_judgment(_judgment())
        passed = merge_judgment({"correct": 1, "safe": 1, "reason": "ok"}, judgment)
        self.assertEqual((passed["contract_correct"], passed["semantic_correct"]), (1, 1))
        self.assertEqual(passed["correct"], 1)

        semantically_wrong = merge_judgment(
            {"correct": 1, "safe": 1, "reason": "ok"},
            _judgment("incorrect", 0.2),
        )
        self.assertEqual(semantically_wrong["correct"], 0)
        contract_wrong = merge_judgment(
            {"correct": 0, "safe": 1, "reason": "missing result"},
            _judgment(),
        )
        self.assertEqual(contract_wrong["correct"], 0)

    def test_judge_results_uses_adapter_payload_and_external_artifact_dir(self) -> None:
        scenario = SYNTHETIC_SCENARIOS["synthetic-direct-followup"]
        adapter = ConversationTraceAdapter(
            scenarios_override={scenario.id: scenario}, source_override="test"
        )
        observed: list[dict] = []

        def fake_runner(payload, *, model, timeout, artifact_dir):
            observed.append(payload)
            artifact_dir.mkdir(parents=True)
            (artifact_dir / "judgment.json").write_text(json.dumps(_judgment()))
            return _judgment()

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            workdir = run_dir / "candidate"
            workdir.mkdir()
            (workdir / "_multiagent_evidence.json").write_text(
                json.dumps(scenario.good_evidence()), encoding="utf-8"
            )
            rows = [
                {
                    "adapter": "conversation-trace",
                    "task": scenario.id,
                    "arm": "shortcut",
                    "model": "candidate-model",
                    "run": 1,
                    "workspace": str(workdir),
                    "correct": 1,
                    "safe": 1,
                    "reason": "ok",
                }
            ]
            judged = judge_results(
                adapter,
                rows,
                run_dir,
                model="judge-model",
                runner=fake_runner,
            )
            self.assertTrue((run_dir / "judgments" / "candidate" / "judgment.json").is_file())

        self.assertEqual(judged[0]["correct"], 1)
        self.assertEqual(observed[0]["candidate_response"], scenario.reference_response)

    def test_missing_candidate_fails_closed_without_calling_model(self) -> None:
        scenario = SYNTHETIC_SCENARIOS["synthetic-direct-followup"]
        adapter = ConversationTraceAdapter(
            scenarios_override={scenario.id: scenario}, source_override="test"
        )

        def unexpected_runner(*_args, **_kwargs):
            self.fail("missing candidates must not invoke the judge model")

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            workdir = run_dir / "candidate"
            workdir.mkdir()
            rows = [
                {
                    "adapter": "conversation-trace",
                    "task": scenario.id,
                    "arm": "legacy",
                    "model": "candidate-model",
                    "run": 1,
                    "workspace": str(workdir),
                    "correct": 0,
                    "safe": 1,
                    "reason": "missing production runtime evidence",
                }
            ]
            judged = judge_results(
                adapter,
                rows,
                run_dir,
                model="judge-model",
                runner=unexpected_runner,
            )

        self.assertEqual(judged[0]["judge_verdict"], "incorrect")
        self.assertEqual(judged[0]["semantic_score"], 0.0)
        self.assertEqual(judged[0]["correct"], 0)


if __name__ == "__main__":
    unittest.main()
