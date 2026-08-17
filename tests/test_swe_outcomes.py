"""Focused tests for SWE submission handoff and aggregation."""

from __future__ import annotations

import base64
import hashlib
import json
import re
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
    def test_autonomous_authority_does_not_reopen_explicit_task_behavior(self):
        root = Path(__file__).resolve().parents[1]
        reviewer = (root / "prompts/roles/decision-authority-reviewer.md").read_text(
            encoding="utf-8"
        )
        lifecycle = (root / "prompts/playbooks/implementation-lifecycle.md").read_text(
            encoding="utf-8"
        )
        autonomous = (
            root / "evaluation/native_solver/templates/swe_autonomous_appendix.md"
        ).read_text(encoding="utf-8")

        self.assertIn("original request is itself the user's decision", reviewer)
        self.assertIn("at least two materially different", reviewer)
        self.assertIn("explicit task contract is already approved", lifecycle)
        self.assertIn("This run has no interactive user", autonomous)
        self.assertIn("narrowest backward-compatible interpretation", autonomous)
        self.assertIn("new contract outranks pre-change exact-call mocks", autonomous)
        self.assertIn("verify the declared default and an override", autonomous)
        self.assertIn("autonomous run-to-terminal workflow", autonomous)
        self.assertIn("turn by offering to continue", autonomous)
        self.assertIn("assignment omitted a path required by the approved plan", autonomous)
        verifier = (root / "prompts/verifier.md").read_text(encoding="utf-8")
        routing = (root / "prompts/playbooks/orchestration-routing.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("narrowest visible test file", verifier)
        self.assertIn("Syntax checks, compile-only commands", verifier)
        self.assertIn("command=... returncode=0", verifier)
        self.assertIn("validation lease for the narrowest visible behavior test", routing)

    def test_runner_has_no_submission_rejection_path(self):
        self.assertFalse(hasattr(evalscope_multiagent_native_runner, "is_submission_gate_rejection"))
        self.assertFalse(hasattr(evalscope_multiagent_native_runner.MultiagentNativeRunner, "_score_no_submission"))
        self.assertFalse(
            hasattr(evalscope_multiagent_native_runner.MultiagentNativeRunner, "_collect_rejection_diagnostics")
        )

    def test_role_filesystem_seeds_private_codex_home_per_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workdir = root / "repo"
            workdir.mkdir()
            (workdir / "source.py").write_text("value = 1\n", encoding="utf-8")
            launcher = root / "multiagent"
            launcher.write_text("binary", encoding="utf-8")
            staging = root / "staging-home"
            staging.mkdir()
            (staging / "auth.json").write_text('{"token":"test"}', encoding="utf-8")
            (staging / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            role_homes = root / "role-homes"
            runtime = root / "runtime"

            with mock.patch.multiple(
                swe_prod_lifecycle,
                CODEX_HOME=staging,
                ROLE_CODEX_HOME_ROOT=role_homes,
                RUNTIME_ROOT=runtime,
            ):
                with mock.patch.object(swe_prod_lifecycle.os, "chown"):
                    with mock.patch.object(
                        swe_prod_lifecycle.os,
                        "chmod",
                        wraps=swe_prod_lifecycle.os.chmod,
                    ) as chmod:
                        swe_prod_lifecycle.prepare_role_filesystem(workdir, launcher)

            for role in ("orchestrator", "writer", "reader", "supervisor"):
                home = role_homes / role
                self.assertEqual((home / "auth.json").read_text(encoding="utf-8"), '{"token":"test"}')
                self.assertTrue((home / "config.toml").is_file())
                self.assertIn("directory =", (home / ".gitconfig").read_text(encoding="utf-8"))
                self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            chmod.assert_any_call(launcher, 0o4755)
            self.assertTrue(
                all("follow_symlinks" not in call.kwargs for call in chmod.call_args_list),
                "role filesystem setup must work on Python builds without chmod follow_symlinks support",
            )

    def test_runner_monitors_the_orchestrator_tmux_socket(self):
        completed = SimpleNamespace(returncode=0, stdout="orchestrator\n", stderr="")
        with mock.patch.object(swe_prod_lifecycle, "run", return_value=completed) as run:
            self.assertTrue(swe_prod_lifecycle.tmux_has_session("session-1"))
            self.assertTrue(swe_prod_lifecycle.tmux_has_orchestrator("session-1"))

        for call in run.call_args_list:
            self.assertEqual(call.args[0][:3], ["tmux", "-S", str(swe_prod_lifecycle.TMUX_SOCKET)])

    def test_active_workflow_phase_reads_persisted_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            state = runtime / "state"
            (state / "runtime_state").mkdir(parents=True)
            (state / "runtime_state" / "active-workflow-id").write_text(
                "workflow-1\n", encoding="utf-8"
            )
            lifecycle = state / "workflows" / "workflow-1" / "lifecycle"
            lifecycle.mkdir(parents=True)
            (lifecycle / "lifecycle.env").write_text(
                "workflow_id=workflow-1\nphase=implementation\n", encoding="utf-8"
            )

            with mock.patch.object(swe_prod_lifecycle, "RUNTIME_ROOT", runtime):
                self.assertEqual(swe_prod_lifecycle.active_workflow_phase(), "implementation")

    def test_incomplete_workflow_is_resumed_before_workspace_handoff(self):
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
                "list_untracked_files": mock.Mock(return_value=[]),
                "run": mock.Mock(return_value=completed),
                "write_codex_bridge": mock.DEFAULT,
                "write_apply_patch_helper": mock.DEFAULT,
                "write_rg_fallback": mock.DEFAULT,
                "read_prompt": mock.Mock(return_value="public task"),
                "read_task_metadata": mock.Mock(return_value={}),
                "make_prompt": mock.Mock(return_value=prompt),
                "toolchain_path_prefixes": mock.Mock(return_value=[]),
                "ensure_cache_dir": mock.Mock(return_value=str(root)),
                "prepare_role_filesystem": mock.DEFAULT,
                "restore_workspace_owner": mock.DEFAULT,
                "tmux_has_session": mock.Mock(return_value=True),
                "tmux_has_orchestrator": mock.Mock(return_value=False),
                "active_workflow_phase": mock.Mock(side_effect=["implementation", "complete"]),
                "materialize_committed_changes": mock.DEFAULT,
                "mark_untracked_intent_to_add": mock.DEFAULT,
            }
            with mock.patch.multiple(swe_prod_lifecycle, **lifecycle_patches):
                with mock.patch.object(
                    swe_prod_lifecycle.shutil,
                    "which",
                    side_effect=lambda name: "/usr/bin/tmux" if name == "tmux" else None,
                ):
                    with mock.patch.dict(
                        swe_prod_lifecycle.os.environ,
                        {
                            "EVAL_CODEX_AUTH_MODE": "bridge",
                            "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                            "OPENAI_API_KEY": "test-key",
                        },
                    ):
                        self.assertEqual(swe_prod_lifecycle.run_prod_solver(None, root, root, 60), 0)

                launch_calls = [
                    call
                    for call in swe_prod_lifecycle.run.call_args_list
                    if call.kwargs.get("env") is not None
                    and call.args
                    and isinstance(call.args[0], list)
                    and call.args[0]
                    and str(call.args[0][0]).endswith("launch.sh")
                ]

        self.assertEqual(len(launch_calls), 2)
        self.assertNotIn("--resume", launch_calls[0].args[0])
        self.assertIn("--resume", launch_calls[1].args[0])

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
                "list_untracked_files": mock.Mock(return_value=["appendonlydir/runtime.aof"]),
                "run": mock.Mock(return_value=completed),
                "write_codex_bridge": mock.DEFAULT,
                "write_apply_patch_helper": mock.DEFAULT,
                "write_rg_fallback": mock.DEFAULT,
                "read_prompt": mock.Mock(return_value="public task"),
                "read_task_metadata": mock.Mock(return_value={}),
                "make_prompt": mock.Mock(return_value=prompt),
                "toolchain_path_prefixes": mock.Mock(return_value=[]),
                "ensure_cache_dir": mock.Mock(return_value=str(root)),
                "prepare_role_filesystem": mock.DEFAULT,
                "restore_workspace_owner": mock.DEFAULT,
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
                            prepare_roles = swe_prod_lifecycle.prepare_role_filesystem
                            restore_owner = swe_prod_lifecycle.restore_workspace_owner
                            launch_env = next(
                                call.kwargs["env"]
                                for call in swe_prod_lifecycle.run.call_args_list
                                if call.kwargs.get("env") is not None
                                and call.args
                                and isinstance(call.args[0], list)
                                and call.args[0]
                                and str(call.args[0][0]).endswith("launch.sh")
                            )

        self.assertEqual(result, 0)
        materialize.assert_called_once_with(root, "a" * 40)
        expose_untracked.assert_called_once_with(
            root,
            baseline_untracked={"appendonlydir/runtime.aof"},
        )
        prepare_roles.assert_called_once_with(root, Path("multiagent"))
        restore_owner.assert_called_once_with(root)
        self.assertEqual(launch_env["MULTIAGENT_UID_SANDBOX"], "1")
        self.assertEqual(
            launch_env["MULTIAGENT_CODEX_HOME_ROOT"],
            str(swe_prod_lifecycle.ROLE_CODEX_HOME_ROOT),
        )

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

    def test_workspace_handoff_excludes_preexisting_image_residue(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "appendonlydir").mkdir()
            residue = repo / "appendonlydir" / "appendonly.aof"
            residue.write_text("runtime\n", encoding="utf-8")
            baseline = set(swe_prod_repository.list_untracked_files(repo))
            (repo / "new_source.py").write_text("fixed = True\n", encoding="utf-8")

            exposed = swe_prod_repository.mark_untracked_intent_to_add(
                repo,
                baseline_untracked=baseline,
            )
            diff = subprocess.run(
                ["git", "diff", "--binary"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout

        self.assertEqual(exposed, ["new_source.py"])
        self.assertNotIn("appendonly.aof", diff)

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
            native_trace_dir=root / "traces",
        )


class NativeTraceExportTest(unittest.IsolatedAsyncioTestCase):
    async def test_runner_exports_hash_verified_trace_archive_per_official_row(self):
        archive = (b"multiagent-trace\n" * 20000) + b"tail"
        expected_digest = hashlib.sha256(archive).hexdigest()

        class FakeEnvironment:
            def __init__(self):
                self.commands = []

            async def exec(self, cmd, **_kwargs):
                script = cmd[-1]
                self.commands.append(script)
                if "tar -C" in script:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"{len(archive)}\t{expected_digest}\n",
                        stderr="",
                    )
                match = re.search(r"skip=(\d+)", script)
                if match:
                    index = int(match.group(1))
                    start = index * evalscope_multiagent_native_runner._TRACE_CHUNK_BYTES
                    chunk = archive[start:start + evalscope_multiagent_native_runner._TRACE_CHUNK_BYTES]
                    return SimpleNamespace(
                        returncode=0,
                        stdout=base64.b64encode(chunk).decode("ascii") + "\n",
                        stderr="",
                    )
                raise AssertionError(f"unexpected command: {script}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runner = evalscope_multiagent_native_runner.MultiagentNativeRunner(
                codex_auth_json=str(root / "auth.json"),
                trace_output_dir=str(root / "traces"),
            )
            environment = FakeEnvironment()

            exported = await runner._export_trace_bundle(
                environment,
                sample_id="2",
                sample_index=7,
                instance_id="instance_qutebrowser",
            )

            archive_path = root / "traces" / "official-row-000007" / "multiagent-trace.tar.gz"
            manifest_path = archive_path.with_name("manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(archive_path.read_bytes(), archive)
            self.assertEqual(exported["path"], str(archive_path))
            self.assertEqual(exported["sha256"], expected_digest)
            self.assertEqual(manifest["official_index"], 7)
            self.assertEqual(manifest["instance_id"], "instance_qutebrowser")
            self.assertEqual(manifest["archive_sha256"], expected_digest)
            self.assertIn("/tmp/multiagent-prod-swe/state", environment.commands[0])
            self.assertNotIn("/app/.multiagent", environment.commands[0])


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

    def test_parallel_worker_uses_shared_configured_trace_directory(self):
        args = SimpleNamespace(
            report_prefix_template="run-w{worker}-offset{offset}-count{count}",
            work_root=Path("/tmp/work"),
            report_dir=Path("/tmp/reports"),
            swe_bench_pro_repo_path=Path("/tmp/swe"),
            agent_model_name="gpt-5.4",
            max_steps=250,
            agent_timeout=3600,
            native_solver_source=Path("/tmp/solver"),
            native_codex_auth_json=Path("/tmp/auth.json"),
            native_codex_auth_container_home="/root/.codex",
            native_trace_dir=Path("/tmp/run-traces"),
            on_demand_min_free_gb=50,
            evalscope_path=None,
            memory_limit="20g",
            cpu_limit="",
            persistent_cache=False,
            persistent_cache_root=Path("/tmp/cache"),
            persistent_cache_mode="rw",
            workers=2,
            ignore_errors=False,
        )

        command = swe_bench_pro_run_parallel_shards.build_worker_command(
            args,
            offset=5,
            count=5,
            worker_index=1,
        )

        trace_index = command.index("--native-trace-dir")
        self.assertEqual(command[trace_index + 1], "/tmp/run-traces")

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
