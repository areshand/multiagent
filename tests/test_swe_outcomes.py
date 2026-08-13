"""Focused tests for production terminal outcomes and SWE aggregation."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


def _install_evalscope_stubs() -> None:
    sys.modules.setdefault("evalscope", SimpleNamespace())
    sys.modules.setdefault("evalscope.agent", SimpleNamespace())
    sys.modules.setdefault("evalscope.agent.external", SimpleNamespace())
    sys.modules["evalscope.agent.external.runners"] = SimpleNamespace(
        AgentRunResult=SimpleNamespace,
        AgentRunner=object,
        BridgeEndpoint=object,
        ExternalAgentTask=object,
        RunnerTimeoutError=RuntimeError,
    )
    sys.modules.setdefault("evalscope.api", SimpleNamespace())
    sys.modules["evalscope.api.agent"] = SimpleNamespace(AgentEnvironment=object)
    sys.modules["evalscope.api.registry"] = SimpleNamespace(register_runner=lambda _name: (lambda cls: cls))
    sys.modules.setdefault("evalscope.utils", SimpleNamespace())
    sys.modules["evalscope.utils.logger"] = SimpleNamespace(
        get_logger=lambda: SimpleNamespace(
            error=lambda *args, **kwargs: None,
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        )
    )


_install_evalscope_stubs()

from evaluation import evalscope_multiagent_native_runner  # noqa: E402
from evaluation import swe_bench_pro  # noqa: E402
from evaluation import swe_bench_pro_official_aggregate  # noqa: E402
from evaluation.native_solver import solve_swe_prod  # noqa: E402
from evaluation.native_solver import swe_prod_transitions  # noqa: E402
from evaluation.native_solver.swe_prod_types import LifecycleProgress  # noqa: E402
from evaluation.support.coding.outcomes import (  # noqa: E402
    SUBMISSION_GATE_REJECTION,
    load_terminal_outcome,
)


class _NoSubmissionEnv:
    def __init__(self) -> None:
        self.calls = []

    async def exec(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class NativeOutcomeTest(unittest.TestCase):
    def test_rejection_requires_dedicated_exit_and_complete_schema(self):
        payload = {
            "schema_version": 1,
            "outcome": "submission_gate_rejection",
            "reason": "final gate rejected the patch",
            "blockers": ["missing build evidence"],
        }

        self.assertTrue(evalscope_multiagent_native_runner.is_submission_gate_rejection(3, payload))
        self.assertFalse(evalscope_multiagent_native_runner.is_submission_gate_rejection(2, payload))
        self.assertFalse(
            evalscope_multiagent_native_runner.is_submission_gate_rejection(3, {**payload, "reason": ""})
        )
        self.assertFalse(
            evalscope_multiagent_native_runner.is_submission_gate_rejection(3, {**payload, "schema_version": 2})
        )

    def test_no_submission_discards_rejected_diff(self):
        env = _NoSubmissionEnv()
        runner = object.__new__(evalscope_multiagent_native_runner.MultiagentNativeRunner)
        runner._working_dir = "/app"

        result = asyncio.run(
            runner._score_no_submission(
                env,
                sample_id="sample-1",
                result=SimpleNamespace(returncode=3, duration=1.5, timed_out=False),
                stdout_tail="",
                stderr_tail="",
                diagnostics="typed gate rejection",
                reason="submission_gate_rejection",
                runtime_identity={"codex_version": "codex-cli 0.144.1", "node_version": "v22.12.0"},
            )
        )

        self.assertEqual(result.metrics["submission_status"], "no_submission")
        self.assertEqual(env.calls[0][0], ["bash", "-lc", "git reset --hard HEAD && git clean -fd"])
        self.assertEqual(env.calls[0][1]["cwd"], "/app")

    @unittest.skipUnless(shutil.which("git"), "git is required for lifecycle finalization")
    def test_final_gate_publishes_production_owned_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "base"], cwd=repo, check=True
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            original_status = solve_swe_prod.STATUS_PATH
            original_terminal = solve_swe_prod.TERMINAL_OUTCOME_PATH
            original_emit = swe_prod_transitions.emit_failure_diagnostics
            try:
                solve_swe_prod.STATUS_PATH = root / "status.json"
                solve_swe_prod.TERMINAL_OUTCOME_PATH = root / "terminal-outcome.json"
                solve_swe_prod.STATUS_PATH.write_text(
                    json.dumps(
                        {"status": "blocked", "reason": "final gate rejected", "blockers": ["compile failed"]}
                    ),
                    encoding="utf-8",
                )
                swe_prod_transitions.emit_failure_diagnostics = lambda _session: None
                progress = LifecycleProgress(
                    exit_code=2,
                    outcome="blocked",
                    terminal_outcome=SUBMISSION_GATE_REJECTION,
                )

                returncode = swe_prod_transitions.finalize_solver_run(
                    workdir=repo,
                    start_head=head,
                    issue="Fix the public issue.",
                    task_metadata={},
                    session="test-session",
                    progress=progress,
                )

                self.assertEqual(returncode, 3)
                published = load_terminal_outcome(solve_swe_prod.TERMINAL_OUTCOME_PATH)
                self.assertEqual(published["outcome"], SUBMISSION_GATE_REJECTION)
                self.assertEqual(published["reason"], "final gate rejected")
            finally:
                swe_prod_transitions.emit_failure_diagnostics = original_emit
                solve_swe_prod.STATUS_PATH = original_status
                solve_swe_prod.TERMINAL_OUTCOME_PATH = original_terminal

    def test_summary_keeps_no_submission_in_denominator(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            report_dir = work_dir / "reports" / "codex-scaffold-parity"
            log_dir = work_dir / "logs"
            report_dir.mkdir(parents=True)
            log_dir.mkdir(parents=True)
            report_path = report_dir / "swe_bench_pro.json"
            report_path.write_text('{"score": 0.0, "num": 1}\n', encoding="utf-8")
            (log_dir / "eval_log.log").write_text(
                "multiagent-native exited: sample=0 rc=3 wall=1.5s timed_out=False\n"
                'multiagent-native runtime: sample=0 identity={"codex_version":"codex-cli 0.144.1","node_version":"v22.12.0"}\n'
                "multiagent-native no-submission: sample=0 original_rc=3 reason=submission_gate_rejection\n",
                encoding="utf-8",
            )
            args = self._summary_args(root, work_dir)
            config = {
                "agent_config": {"mode": "external", "framework": "multiagent-native"},
                "dataset_args": {
                    "swe_bench_pro": {"extra_params": {"command_timeout": 60, "eval_timeout": 3600}}
                },
            }

            payload = swe_bench_pro.summarize_result(
                args=args,
                config=config,
                run_result={"status": "completed"},
                evalscope_report_path=report_path,
                preflight={"official_scaffold_ready": True, "official_image_set_ready": False},
                started_at=swe_bench_pro.dt.datetime.now(swe_bench_pro.dt.timezone.utc),
                completed_at=swe_bench_pro.dt.datetime.now(swe_bench_pro.dt.timezone.utc),
                status="completed",
            )

            self.assertIsNone(payload["clean_native_score"])
            self.assertEqual(payload["end_to_end_score"], 0.0)
            self.assertTrue(payload["official_verifier_evidence"])
            self.assertEqual(payload["native_runner"]["outcome_counts"]["no_submission"], 1)
            self.assertEqual(
                payload["native_runner"]["latest"]["runtime_identity"]["codex_version"],
                "codex-cli 0.144.1",
            )

    @staticmethod
    def _summary_args(root: Path, work_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(
            work_dir=work_dir,
            limit=1,
            on_demand_image_preload=True,
            sample_count=None,
            sample_offset=0,
            output=root / "summary.json",
            config_json=root / "config.json",
            config_yaml=root / "config.yaml",
            preflight_output=root / "preflight.json",
            swe_bench_pro_repo_path=Path("/tmp/swe"),
            dockerhub_username="jefzda",
            platform="linux/amd64",
            command_timeout=60.0,
            agent_timeout=3600.0,
            eval_timeout=3600,
            agent_model_name="gpt-5",
            agent_working_dir="/app",
            on_demand_prune_after_sample=False,
            on_demand_image_status=root / "image-status.json",
            persistent_cache=False,
            persistent_cache_root=Path("/tmp/cache"),
            persistent_cache_mode="rw",
            native_solver_source=Path(__file__).resolve().parents[1],
            native_codex_auth_json="",
            native_codex_auth_container_home="/root/.codex-multiagent-prod",
        )


class AggregateOutcomeTest(unittest.TestCase):
    def test_verified_patch_and_no_submission_weight_to_half(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            benchmark_repo = root / "benchmark"
            helper_code = benchmark_repo / "helper_code"
            reports = root / "reports"
            helper_code.mkdir(parents=True)
            reports.mkdir()
            rows = [
                {"instance_id": "instance_org__repo-a", "repo": "org/repo", "base_commit": "a" * 40},
                {"instance_id": "instance_org__repo-b", "repo": "org/repo", "base_commit": "b" * 40},
            ]
            (helper_code / "sweap_eval_full_v2.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            (reports / "row-0.json").write_text(
                json.dumps(self._summary(rows, 0, 1.0, "clean_patch")), encoding="utf-8"
            )
            (reports / "row-1.json").write_text(
                json.dumps(self._summary(rows, 1, 0.0, "no_submission")), encoding="utf-8"
            )
            args = SimpleNamespace(
                swe_bench_pro_repo_path=benchmark_repo,
                dockerhub_username="jefzda",
                report_dir=reports,
                reports=["row-*.json"],
                expected_full_split_size=2,
                suggest_shard_size=1,
                max_ranges=10,
            )

            payload = swe_bench_pro_official_aggregate.aggregate(args)

            self.assertTrue(payload["official_complete"])
            self.assertEqual(payload["official_score"], 0.5)
            invalid_null = self._summary(rows, 0, 0.0, "clean_patch")
            invalid_null["end_to_end_score"] = None
            self.assertFalse(swe_bench_pro_official_aggregate.report_matches(invalid_null))
            invalid_size = self._summary(rows, 0, 1.0, "clean_patch")
            invalid_size["sample_size"] = 2
            self.assertFalse(swe_bench_pro_official_aggregate.report_matches(invalid_size))

    @staticmethod
    def _summary(rows, index, score, outcome):
        return {
            "benchmark": "swe-bench-pro",
            "status": "completed",
            "official_verifier_evidence": True,
            "sample_size": 1,
            "score": score,
            "end_to_end_score": score,
            "sample_shard": {
                "selected_instances": [{"official_index": index, "instance_id": rows[index]["instance_id"]}]
            },
            "native_runner": {
                "end_to_end_scored": True,
                "scored_outcome_count": 1,
                "outcome_counts": {outcome: 1},
            },
            "parity": {"agent_config": "external multiagent-native"},
        }


if __name__ == "__main__":
    unittest.main()
