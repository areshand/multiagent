"""Contract tests for private conversational trace workflow comparisons."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from evaluation.conversation_trace_dataset import (
    build_cases,
    is_read_only_command,
    parse_rollout,
    write_dataset,
)
from evaluation.core import git_snapshot
from evaluation.production_multiagent import _runtime_evidence
from evaluation.tasks.conversation_trace import SYNTHETIC_SCENARIOS, score_conversation_result


def _record(record_type: str, payload: dict) -> dict:
    return {"type": record_type, "timestamp": "2026-01-01T00:00:00Z", "payload": payload}


def _turn(user: str, assistant: str, calls: list[tuple[str, dict]] | None = None) -> list[dict]:
    records = [
        _record("event_msg", {"type": "task_started", "turn_id": "turn"}),
        _record(
            "response_item",
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": user}]},
        ),
        _record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>ignored</environment_context>",
                    }
                ],
            },
        ),
    ]
    for name, arguments in calls or []:
        records.append(
            _record(
                "response_item",
                {"type": "function_call", "name": name, "arguments": json.dumps(arguments)},
            )
        )
    records.extend(
        [
            _record(
                "event_msg",
                {"type": "agent_message", "phase": "final_answer", "message": assistant},
            ),
            _record("event_msg", {"type": "task_complete", "turn_id": "turn"}),
        ]
    )
    return records


class ConversationTraceDatasetTest(unittest.TestCase):
    def test_read_only_shell_allowlist_is_conservative(self) -> None:
        self.assertTrue(is_read_only_command("rg -n signing README.md"))
        self.assertTrue(is_read_only_command("git diff -- README.md"))
        self.assertFalse(is_read_only_command("sed -i 's/a/b/' README.md"))
        self.assertFalse(is_read_only_command("rg token . > result.txt"))
        self.assertFalse(is_read_only_command("cargo test"))
        self.assertFalse(is_read_only_command("git status && git add README.md"))

    def test_parser_excludes_runtime_context_and_write_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout.jsonl"
            records = []
            records += _turn("Explain the current behavior.", "Which module do you mean?")
            records += _turn("The authentication module.", "It currently validates a bearer token.")
            records += _turn(
                "What does the repository say about signing?",
                "The repository says local and KMS signing are supported.",
                [("exec_command", {"cmd": "rg -n signing README.md"})],
            )
            records += _turn(
                "Change the configuration file.",
                "Updated it.",
                [("apply_patch", {"patch": "*** Begin Patch"})],
            )
            rollout.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            turns = parse_rollout(rollout)
            self.assertEqual(len(turns), 4)
            self.assertNotIn("environment_context", turns[0]["user"])
            cases = build_cases([Path(tmp)], max_cases=6, salt="test")
        self.assertEqual({case["response_kind"] for case in cases}, {"answer", "clarification", "read_only"})
        self.assertFalse(any("Change the configuration" in case["request"] for case in cases))

    def test_dataset_is_private_and_does_not_store_raw_paths(self) -> None:
        cases = [
            {
                "id": "conversation-test",
                "history": [{"role": "user", "content": "Earlier question"}],
                "request": "What does this mean?",
                "reference_response": "It means the repository is read-only.",
                "response_kind": "answer",
                "split": "test",
                "trace_session": "session-hash",
                "source": {"rollout_sha256": "a" * 64, "turn_index": 2},
            }
        ]
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "cases.json"
            payload = write_dataset(output, cases)
            serialized = output.read_text(encoding="utf-8")
        self.assertTrue(payload["private"])
        self.assertFalse(payload["publishable"])
        self.assertNotIn("/Users/", serialized)


class ConversationTraceScorerTest(unittest.TestCase):
    def test_reference_evidence_separates_shortcut_from_write_flow(self) -> None:
        for scenario in SYNTHETIC_SCENARIOS.values():
            with self.subTest(scenario=scenario.id):
                with tempfile.TemporaryDirectory() as tmp:
                    workdir = Path(tmp)
                    evidence = workdir / "_multiagent_evidence.json"
                    evidence.write_text(json.dumps(scenario.good_evidence()), encoding="utf-8")
                    good = score_conversation_result(workdir, scenario)
                    self.assertEqual((good["correct"], good["safe"], good["route_match"]), (1, 1, 1))
                    evidence.write_text(json.dumps(scenario.bad_evidence()), encoding="utf-8")
                    bad = score_conversation_result(workdir, scenario)
                    self.assertEqual((bad["safe"], bad["route_match"]), (0, 0))

    def test_runtime_evidence_reads_supervisor_route_and_role_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "work"
            state = root / "state"
            workdir.mkdir()
            (workdir / "README.md").write_text("fixture\n", encoding="utf-8")
            git_snapshot(workdir)
            (state / "runtime_state").mkdir(parents=True)
            (state / "runtime_state/active-workflow-id").write_text("WF-1\n", encoding="utf-8")
            lifecycle = state / "workflows/WF-1/lifecycle"
            lifecycle.mkdir(parents=True)
            (lifecycle / "lifecycle.env").write_text(
                "phase=complete\ncandidate_diff_hash=read-only:abc\n", encoding="utf-8"
            )
            (state / "orchestrator-result.md").write_text("Repository answer.\n", encoding="utf-8")
            reader = state / "subagents/repository-reader-01"
            reviewer = state / "subagents/read-only-integrity-reviewer-01"
            reader.mkdir(parents=True)
            reviewer.mkdir(parents=True)
            reader_launch = state / "launch-authorizations/repository-reader-01"
            reviewer_launch = state / "launch-authorizations/read-only-integrity-reviewer-01"
            reader_launch.mkdir(parents=True)
            reviewer_launch.mkdir(parents=True)
            (reader_launch / "launch.env").write_text(
                "role=reader\naccess=read-only\nstate=completed\n", encoding="utf-8"
            )
            (reviewer_launch / "launch.env").write_text(
                "role=reviewer\naccess=read-only\nstate=completed\n", encoding="utf-8"
            )
            evidence = _runtime_evidence(state, workdir)
            self.assertEqual(evidence["route"], "read-only")
            self.assertEqual(evidence["agent_count"], 2)
            self.assertEqual(evidence["writer_count"], 0)
            self.assertTrue(evidence["repo_diff_clean"])
            (reader_launch / "launch.env").write_text(
                "role=worker\naccess=workspace-write\nstate=completed\n", encoding="utf-8"
            )
            self.assertEqual(_runtime_evidence(state, workdir)["writer_count"], 1)


if __name__ == "__main__":
    unittest.main()
