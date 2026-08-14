"""Focused tests for SWE submission handoff and aggregation."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


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
from evaluation import swe_bench_pro_run_parallel_shards  # noqa: E402
from evaluation.native_solver import swe_prod_lifecycle  # noqa: E402
from evaluation.native_solver import swe_prod_repository  # noqa: E402


class NativeOutcomeTest(unittest.TestCase):
    def test_runner_has_no_submission_rejection_path(self):
        self.assertFalse(hasattr(evalscope_multiagent_native_runner, "is_submission_gate_rejection"))
        self.assertFalse(hasattr(evalscope_multiagent_native_runner.MultiagentNativeRunner, "_score_no_submission"))
        self.assertFalse(
            hasattr(evalscope_multiagent_native_runner.MultiagentNativeRunner, "_collect_rejection_diagnostics")
        )

    def test_shard_problem_statement_uses_relative_sample_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            helper = repo / "helper_code"
            helper.mkdir()
            dataset = helper / "sweap_eval_full_v2.jsonl"
            dataset.write_text(
                "\n".join(
                    json.dumps({"problem_statement": f"public issue {index}"})
                    for index in range(7)
                )
                + "\n",
                encoding="utf-8",
            )

            absolute_index = evalscope_multiagent_native_runner._absolute_sample_index(5, "1")
            metadata = evalscope_multiagent_native_runner._public_problem_statement_metadata(
                str(repo), absolute_index
            )

            self.assertEqual(absolute_index, 6)
            self.assertEqual(metadata, {"problem_statement": "public issue 6"})

    def test_orchestrator_exit_prepares_workspace_for_official_scorer(self):
        completed = SimpleNamespace(returncode=0, stdout="codex-cli 1.0\n", stderr="")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "prompt.md"
            prompt.write_text("prompt", encoding="utf-8")
            lifecycle_patches = {
                "require_path": mock.DEFAULT,
                "multiagent_command": mock.Mock(return_value=["multiagent"]),
                "find_codex_cli": mock.Mock(return_value="/usr/bin/codex"),
                "git_head": mock.Mock(return_value="a" * 40),
                "run": mock.Mock(return_value=completed),
                "write_codex_bridge": mock.DEFAULT,
                "write_apply_patch_helper": mock.DEFAULT,
                "write_rg_fallback": mock.DEFAULT,
                "read_prompt": mock.Mock(return_value="public task"),
                "read_task_metadata": mock.Mock(return_value={}),
                "make_prompt": mock.Mock(return_value=prompt),
                "toolchain_path_prefixes": mock.Mock(return_value=[]),
                "ensure_cache_dir": mock.Mock(return_value=str(root)),
                "tmux_has_session": mock.Mock(return_value=True),
                "tmux_has_orchestrator": mock.Mock(return_value=False),
                "materialize_committed_changes": mock.DEFAULT,
                "mark_untracked_intent_to_add": mock.DEFAULT,
            }
            with mock.patch.multiple(swe_prod_lifecycle, **lifecycle_patches):
                with mock.patch.object(
                    swe_prod_lifecycle.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
                ):
                    with mock.patch.object(swe_prod_lifecycle.time, "sleep"):
                        with mock.patch.dict(
                            swe_prod_lifecycle.os.environ,
                            {
                                "EVAL_CODEX_AUTH_MODE": "bridge",
                                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                                "OPENAI_API_KEY": "test-key",
                            },
                        ):
                            result = swe_prod_lifecycle.run_prod_solver(None, root, root, 60)
                            materialize = swe_prod_lifecycle.materialize_committed_changes
                            expose_untracked = swe_prod_lifecycle.mark_untracked_intent_to_add

        self.assertEqual(result, 0)
        materialize.assert_called_once_with(root, "a" * 40)
        expose_untracked.assert_called_once_with(root)

    def test_workspace_handoff_includes_new_source_and_test_files(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / "base.py").write_text("base = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "base"],
                cwd=repo,
                check=True,
            )
            (repo / "feature.py").write_text("fixed = True\n", encoding="utf-8")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_feature.py").write_text("def test_feature(): pass\n", encoding="utf-8")

            exposed = swe_prod_repository.mark_untracked_intent_to_add(repo)
            diff = subprocess.run(
                ["git", "diff", "--binary"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout

        self.assertEqual(exposed, ["feature.py", "tests/test_feature.py"])
        self.assertIn("feature.py", diff)
        self.assertIn("tests/test_feature.py", diff)

    def test_summary_counts_submitted_patch_even_when_official_score_is_zero(self):
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
                "multiagent-native exited: sample=0 rc=0 wall=1.5s timed_out=False\n"
                'multiagent-native runtime: sample=0 identity={"codex_version":"codex-cli 0.144.1","node_version":"v22.12.0"}\n',
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

            self.assertEqual(payload["clean_native_score"], 0.0)
            self.assertEqual(payload["end_to_end_score"], 0.0)
            self.assertTrue(payload["official_verifier_evidence"])
            self.assertEqual(payload["native_runner"]["outcome_counts"]["clean_patch"], 1)
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
    def test_parallel_refresh_aggregates_from_configured_report_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory) / "custom-reports"
            args = SimpleNamespace(
                aggregate_json=report_dir / "aggregate.json",
                report_dir=report_dir,
                shard_size=5,
                swe_bench_pro_repo_path=Path("/tmp/swe-bench-pro"),
                aggregate_reports=None,
            )

            with mock.patch.object(swe_bench_pro_run_parallel_shards, "run_checked") as run_checked:
                swe_bench_pro_run_parallel_shards.refresh_aggregate(args)

            command = run_checked.call_args.args[0]
            report_dir_index = command.index("--report-dir")
            self.assertEqual(command[report_dir_index + 1], str(report_dir))

    def test_default_discovery_accepts_custom_parallel_report_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = Path(directory)
            shard = reports / "swe-bench-pro-7971da1-w0-offset0-count5.json"
            sidecar = reports / "swe-bench-pro-7971da1-w0-offset0-count5-config.json"
            shard.write_text("{}", encoding="utf-8")
            sidecar.write_text("{}", encoding="utf-8")

            discovered = swe_bench_pro_official_aggregate.discover_reports(
                reports,
                swe_bench_pro_official_aggregate.DEFAULT_REPORT_PATTERNS,
            )

            self.assertEqual(discovered, [shard])

    def test_passing_and_failing_submitted_patches_weight_to_half(self):
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
                json.dumps(self._summary(rows, 1, 0.0, "clean_patch")), encoding="utf-8"
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
