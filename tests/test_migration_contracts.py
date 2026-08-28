"""Black-box contracts that a replacement control-plane implementation must preserve.

These tests intentionally exercise the Rust public CLI and durable files rather
than importing implementation details.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluation.support.cli import multiagent_command, multiagent_subcommand
from evaluation.support.state import AtomicStatusStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MULTIAGENT = Path(
    os.environ.get("MULTIAGENT_BIN", PROJECT_ROOT / "target" / "debug" / "multiagent")
)
CLI_PREFIX = {
    "decision": ["decision"],
    "dag": ["dag"],
    "workflow": ["workflow"],
    "policy": ["policy"],
    "subagent": ["subagent"],
    "multiagent": [],
}


def read_env_file(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class RustCliResolutionTest(unittest.TestCase):
    def test_packaged_binary_and_environment_override_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            packaged = root / "bin" / "multiagent"
            packaged.parent.mkdir()
            packaged.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            packaged.chmod(0o755)
            override = root / "custom-multiagent"
            override.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            override.chmod(0o755)

            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(multiagent_command(root), [str(packaged)])
                self.assertEqual(
                    multiagent_subcommand(root, "subagent", "gate-check"),
                    [str(packaged), "subagent", "gate-check"],
                )
            with mock.patch.dict(os.environ, {"MULTIAGENT_BIN": str(override)}, clear=True):
                self.assertEqual(multiagent_command(root), [str(override)])

    def test_missing_rust_binary_does_not_fall_back_to_a_shell_or_python_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
                self.assertEqual(multiagent_command(Path(temporary)), [])
                self.assertEqual(multiagent_subcommand(Path(temporary), "subagent"), [])


class MigrationCliContractTest(unittest.TestCase):
    def test_launch_is_the_only_production_shell_bootstrap(self):
        self.assertTrue((PROJECT_ROOT / "launch.sh").is_file())
        self.assertEqual(
            list((PROJECT_ROOT / "bin").glob("*.sh")),
            [PROJECT_ROOT / "bin" / "container-entrypoint.sh"],
        )
        launch = (PROJECT_ROOT / "launch.sh").read_text(encoding="utf-8")
        self.assertIn('exec "$MULTIAGENT_BIN" launch "$@"', launch)
        self.assertIn('"$SCRIPT_DIR/bin/multiagent"', launch)
        self.assertNotIn("python", launch.lower())

    def test_launch_executes_packaged_binary_without_cargo(self):
        packaged_root = self.root / "packaged"
        packaged_bin = packaged_root / "bin"
        packaged_bin.mkdir(parents=True)
        launch = packaged_root / "launch.sh"
        launch.write_bytes((PROJECT_ROOT / "launch.sh").read_bytes())
        launch.chmod(0o755)
        executable = packaged_bin / "multiagent"
        executable.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
        executable.chmod(0o755)

        env = {"PATH": "/usr/bin:/bin"}
        result = subprocess.run(
            [str(launch), "--session", "packaged-test"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["launch", "--session", "packaged-test"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.state = self.root / "state"
        self.repo.mkdir()
        self.state.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Migration Test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "lib.rs").write_text("pub fn value() -> u8 { 1 }\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", "src/lib.rs"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.repo, check=True)
        self.env = os.environ.copy()
        self.env.update(
            {
                "MULTIAGENT_ROOT": str(self.repo),
                "MULTIAGENT_STATE_DIR": str(self.state),
                "MULTIAGENT_WRITE_POLICY": str(self.root / "write-policy.paths"),
                "MULTIAGENT_LIFECYCLE_ENFORCEMENT": "0",
                "MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER": "0",
                "PYTHONPATH": str(PROJECT_ROOT)
                + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
            }
        )

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, relative, *args, check=True):
        result = subprocess.run(
            [str(MULTIAGENT), *CLI_PREFIX[relative], *args],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(
                "command failed: {}\nstdout:\n{}\nstderr:\n{}".format(
                    " ".join(result.args), result.stdout, result.stderr
                )
            )
        return result

    def test_decision_v1_persistence_and_output_contract(self):
        created = self.run_cli(
            "decision", "init", "DEC-RUST", "--title", "Rust migration", "--owner", "user"
        )
        self.assertEqual(created.stdout, "decision created\tDEC-RUST\tRust migration\n")
        self.run_cli(
            "decision",
            "add-alternative",
            "DEC-RUST",
            "--plan-id",
            "PLAN-HYBRID",
            "--summary",
            "Port the control plane",
            "--proposed-by",
            "architect",
            "--expected-outcome",
            "one binary",
            "--risk",
            "behavior drift",
        )
        self.run_cli(
            "decision",
            "add-assumption",
            "DEC-RUST",
            "--assumption-id",
            "A-STATE",
            "--statement",
            "v1 state remains readable",
            "--confidence",
            "high",
            "--validation-method",
            "compatibility suite",
            "--expected-signal",
            "identical state",
        )
        committed = self.run_cli(
            "decision",
            "commit",
            "DEC-RUST",
            "--selected-plan",
            "PLAN-HYBRID",
            "--reason",
            "lowest migration risk",
            "--rollback-policy",
            "revert the Rust control-plane changes",
            "--reflection-due",
            "after parity",
        )
        self.assertEqual(
            committed.stdout,
            "decision committed\tDEC-RUST\tPLAN-HYBRID\tlowest migration risk\n",
        )

        decision_dir = self.state / "decisions" / "DEC-RUST"
        metadata = read_env_file(decision_dir / "decision.env")
        self.assertTrue((self.state / "decisions" / ".lock").is_file())
        self.assertEqual(
            set(metadata),
            {"decision_id", "title", "owner", "status", "created_at", "committed_at"},
        )
        self.assertEqual(metadata["status"], "committed")
        outcome = read_env_file(decision_dir / "outcome.env")
        self.assertEqual(
            set(outcome),
            {
                "selected_plan",
                "reason",
                "rollback_policy",
                "reflection_due",
                "committed_at",
                "status",
            },
        )
        self.assertEqual(outcome["selected_plan"], "PLAN-HYBRID")
        self.assertEqual(
            (decision_dir / "alternatives.tsv").read_text(encoding="utf-8").splitlines()[0],
            "plan_id\tsummary\tproposed_by\tbranch\tassignment_name\texpected_outcome\trisk\tadded_at",
        )
        self.assertEqual(
            (decision_dir / "assumptions.tsv").read_text(encoding="utf-8").splitlines()[0],
            "assumption_id\tstatement\tconfidence\tvalidation_method\texpected_signal\tadded_at",
        )

    def test_finding_and_todo_read_contracts(self):
        self.run_cli(
            "subagent",
            "finding-create",
            "F-RUST",
            "--severity",
            "blocking",
            "--type",
            "validation_failure",
            "--summary",
            "Rust parity failed",
            "--evidence-json",
            '{"command":"cargo test","returncode":1}',
            "--required-resolution",
            "restore compatibility",
            "--affected",
            "src,state",
        )
        shown_finding = json.loads(
            self.run_cli("subagent", "finding-show", "F-RUST").stdout
        )
        self.assertEqual(
            set(shown_finding),
            {
                "id",
                "severity",
                "type",
                "summary",
                "affected_paths",
                "evidence",
                "required_resolution",
                "created_at",
            },
        )
        self.assertEqual(shown_finding["id"], "F-RUST")
        self.assertEqual(shown_finding["affected_paths"], ["src", "state"])
        self.assertIn(
            "F-RUST\tblocking\tvalidation_failure\tRust parity failed",
            self.run_cli(
                "subagent", "finding-list", "--severity", "blocking"
            ).stdout,
        )
        self.assertEqual(
            self.run_cli(
                "subagent", "finding-list", "--severity", "warning"
            ).stdout,
            "",
        )

        self.run_cli(
            "subagent",
            "todo-create",
            "T-RUST",
            "--source-finding-id",
            "F-RUST",
            "--task",
            "repair parity",
            "--done-criteria",
            "run cargo test",
            "--context",
            "preserve v1 behavior",
        )
        shown_todo = json.loads(self.run_cli("subagent", "todo-show", "T-RUST").stdout)
        self.assertEqual(
            set(shown_todo),
            {
                "todo_id",
                "source_finding_id",
                "source_finding_hash",
                "assigned_to",
                "status",
                "task",
                "context",
                "done_criteria",
                "required_commands",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(shown_todo["todo_id"], "T-RUST")
        self.assertEqual(shown_todo["status"], "open")
        self.assertEqual(shown_todo["required_commands"], ["cargo test"])
        self.assertIn(
            "T-RUST\topen\tF-RUST\t-\trepair parity",
            self.run_cli("subagent", "todo-list", "--status", "open").stdout,
        )
        self.assertEqual(
            self.run_cli("subagent", "todo-list", "--status", "closed").stdout,
            "",
        )

    def test_unknown_command_exit_codes_are_stable(self):
        for script in ("decision", "dag", "subagent", "policy"):
            with self.subTest(script=script):
                result = self.run_cli(script, "not-a-command", check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("unknown command", result.stderr)

    def test_assignment_rejects_path_outside_repository(self):
        result = self.run_cli(
            "subagent",
            "assignment-create",
            "escape",
            "--assignment-id",
            "A-ESCAPE",
            "--branch",
            "main",
            "--owned",
            "../outside",
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("assigned path is outside MULTIAGENT_ROOT", result.stderr)

    def test_concurrent_overlapping_assignments_admit_exactly_one_owner(self):
        processes = []
        for index in range(2):
            processes.append(
                subprocess.Popen(
                    [
                        str(MULTIAGENT),
                        "subagent",
                        "assignment-create",
                        "worker-overlap-{}".format(index),
                        "--assignment-id",
                        "A-OVERLAP-{}".format(index),
                        "--branch",
                        "worker/overlap-{}".format(index),
                        "--owned",
                        "src",
                    ],
                    cwd=self.repo,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sum(code == 0 for code, _stdout, _stderr in results), 1)
        self.assertTrue(
            all(
                code == 0 or "active assignment owned-path overlap" in stderr
                for code, _stdout, stderr in results
            )
        )
        assignment_dirs = [
            path
            for path in (self.state / "assignments").iterdir()
            if path.is_dir()
        ]
        self.assertEqual(len(assignment_dirs), 1)
        self.assertTrue((self.state / "assignments" / ".lock").is_file())

    def test_snapshot_cli_json_contract(self):
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        (self.repo / "src" / "lib.rs").write_text("pub fn value() -> u8 { 2 }\n", encoding="utf-8")
        (self.repo / "src" / "new.rs").write_text("pub fn added() {}\n", encoding="utf-8")
        result = subprocess.run(
            [
                str(MULTIAGENT),
                "snapshot",
                "--root",
                str(self.repo),
                "--format",
                "json",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            set(payload),
            {"final_diff_sha256", "changed_files", "changed_paths", "changed_code_paths"},
        )
        self.assertEqual(payload["changed_files"], 3)
        self.assertEqual(
            payload["changed_paths"], ["README.md", "src/lib.rs", "src/new.rs"]
        )
        self.assertEqual(payload["changed_code_paths"], ["src/lib.rs", "src/new.rs"])
        self.assertRegex(payload["final_diff_sha256"], r"^[0-9a-f]{64}$")

    def test_snapshot_excludes_only_baseline_untracked_files(self):
        residue = self.repo / "runtime-residue.txt"
        residue.write_text("created before the solver starts\n", encoding="utf-8")
        baseline = self.root / "baseline-untracked.txt"
        baseline.write_text("runtime-residue.txt\n", encoding="utf-8")
        (self.repo / "src" / "new.rs").write_text("pub fn added() {}\n", encoding="utf-8")
        env = dict(self.env)
        env["MULTIAGENT_BASELINE_UNTRACKED_FILE"] = str(baseline)

        result = subprocess.run(
            [
                str(MULTIAGENT),
                "snapshot",
                "--root",
                str(self.repo),
                "--format",
                "json",
            ],
            cwd=self.repo,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed_paths"], ["src/new.rs"])
        self.assertEqual(payload["changed_code_paths"], ["src/new.rs"])

    def test_snapshot_excludes_framework_control_plane_files(self):
        internal = self.repo / ".multiagent" / "subagents" / "reviewer"
        internal.mkdir(parents=True)
        (internal / "status").write_text("missing\n", encoding="utf-8")
        (self.repo / "src" / "new.rs").write_text("pub fn added() {}\n", encoding="utf-8")

        result = subprocess.run(
            [
                str(MULTIAGENT),
                "snapshot",
                "--root",
                str(self.repo),
                "--format",
                "json",
            ],
            cwd=self.repo,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["changed_paths"], ["src/new.rs"])
        self.assertNotIn(".multiagent", json.dumps(payload))

    def test_dag_concurrent_node_updates_do_not_lose_rows(self):
        self.run_cli("dag", "init", "WF-DAG-CONCURRENT", "--title", "Concurrent DAG")
        processes = []
        for index in range(12):
            processes.append(
                subprocess.Popen(
                    [
                        str(MULTIAGENT),
                        "dag",
                        "add-node",
                        "WF-DAG-CONCURRENT",
                        "NODE-{:02d}".format(index),
                        "--agent",
                        "worker-{:02d}".format(index),
                        "--assignment-id",
                        "A-{:02d}".format(index),
                        "--role",
                        "qa",
                        "--branch",
                        "worker/{:02d}".format(index),
                        "--owned",
                        "src/node_{:02d}.rs".format(index),
                    ],
                    cwd=self.repo,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])

        dag_dir = self.state / "workflows" / "WF-DAG-CONCURRENT"
        with (dag_dir / "nodes.tsv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(rows), 12)
        self.assertEqual(
            {row["node_id"] for row in rows},
            {"NODE-{:02d}".format(index) for index in range(12)},
        )
        self.assertTrue((dag_dir / ".dag.lock").is_file())

    def test_policy_concurrent_approvals_do_not_lose_records(self):
        self.run_cli("policy", "init")
        processes = []
        approved_paths = [self.root / "outside" / "path-{:02d}".format(index) for index in range(12)]
        for index, path in enumerate(approved_paths):
            processes.append(
                subprocess.Popen(
                    [
                        str(MULTIAGENT),
                        "policy",
                        "approve",
                        str(path),
                        "--actor",
                        "migration-test",
                        "--assignment-id",
                        "POLICY-{:02d}".format(index),
                        "--reason",
                        "concurrent approval {:02d}".format(index),
                    ],
                    cwd=self.repo,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])

        policy_path = Path(self.env["MULTIAGENT_WRITE_POLICY"])
        records = [
            line.split("\t")
            for line in policy_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("approval\t")
        ]
        self.assertEqual(len(records), 12)
        self.assertEqual(
            {record[3] for record in records},
            {"POLICY-{:02d}".format(index) for index in range(12)},
        )
        self.assertTrue(policy_path.with_name(".write-policy.paths.lock").is_file())

    def test_concurrent_validation_leases_admit_one_target_owner(self):
        processes = []
        for index in range(8):
            processes.append(
                subprocess.Popen(
                    [
                        str(MULTIAGENT),
                        "subagent",
                        "validation-lease-acquire",
                        "LEASE-{:02d}".format(index),
                        "--owner",
                        "worker-{:02d}".format(index),
                        "--target",
                        "shared-build-target",
                        "--command",
                        "cargo test --workspace",
                    ],
                    cwd=self.repo,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sum(code == 0 for code, _stdout, _stderr in results), 1)
        self.assertTrue(
            all(
                code == 0 or "validation lease conflict" in stderr
                for code, _stdout, stderr in results
            )
        )
        lease_dirs = [
            path
            for path in (self.state / "validation-leases").iterdir()
            if path.is_dir()
        ]
        self.assertEqual(len(lease_dirs), 1)
        self.assertTrue((self.state / "validation-leases" / ".lock").is_file())

    def test_workflow_concurrent_updates_do_not_lose_rows(self):
        self.run_cli("workflow", "init", "WF-CONCURRENT")
        processes = []
        for index in range(12):
            processes.append(
                subprocess.Popen(
                    [
                        str(MULTIAGENT),
                        "workflow",
                        "add-todo",
                        "WF-CONCURRENT",
                        "T-{:02d}".format(index),
                        "--kind",
                        "direct",
                        "--summary",
                        "concurrent update {:02d}".format(index),
                    ],
                    cwd=self.repo,
                    env=self.env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        failures = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            if process.returncode != 0:
                failures.append((process.returncode, stdout, stderr))
        self.assertEqual(failures, [])

        todos_path = self.state / "workflows" / "WF-CONCURRENT" / "lifecycle" / "todos.tsv"
        with todos_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(len(rows), 12)
        self.assertEqual({row["todo_id"] for row in rows}, {"T-{:02d}".format(i) for i in range(12)})
        self.assertTrue(all(row["status"] == "open" for row in rows))

    def test_workflow_concurrent_duplicate_creates_exactly_one_row(self):
        self.run_cli("workflow", "init", "WF-DUPLICATE")
        processes = [
            subprocess.Popen(
                [
                    str(MULTIAGENT),
                    "workflow",
                    "add-todo",
                    "WF-DUPLICATE",
                    "T-SAME",
                    "--kind",
                    "direct",
                    "--summary",
                    "same logical update",
                ],
                cwd=self.repo,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _index in range(8)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            results.append((process.returncode, stdout, stderr))
        self.assertEqual(sum(returncode == 0 for returncode, _stdout, _stderr in results), 1)
        self.assertTrue(
            all(
                returncode == 0 or "TODO already exists: T-SAME" in stderr
                for returncode, _stdout, stderr in results
            )
        )

        todos_path = self.state / "workflows" / "WF-DUPLICATE" / "lifecycle" / "todos.tsv"
        with todos_path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["todo_id"] for row in rows], ["T-SAME"])

    def test_workflow_v1_state_resumes_and_rejects_invalid_phase(self):
        self.run_cli("workflow", "init", "WF-RESUME")
        lifecycle = self.state / "workflows" / "WF-RESUME" / "lifecycle" / "lifecycle.env"
        initial = read_env_file(lifecycle)
        self.assertEqual(
            list(initial),
            [
                "workflow_id",
                "phase",
                "iteration",
                "original_task",
                "original_task_sha256",
                "contract_scout",
                "contract_artifact",
                "contract_artifact_sha256",
                "preimplementation_gate",
                "decision_id",
                "plan_id",
                "decision_revision",
                "decision_capsule",
                "decision_capsule_sha256",
                "implementation_context",
                "implementation_context_sha256",
                "authority_review_id",
                "iteration_plan_sha256",
                "iteration_worker_count",
                "candidate_diff_hash",
                "reviewed_diff_hash",
                "resume_count",
                "created_at",
                "updated_at",
            ],
        )
        resumed = self.run_cli("workflow", "init-or-resume", "WF-RESUME", "--resume", "1")
        self.assertIn("workflow resumed\tWF-RESUME\tpre-implementation", resumed.stdout)
        self.assertEqual(read_env_file(lifecycle)["resume_count"], "1")

        lifecycle.write_text(lifecycle.read_text(encoding="utf-8").replace(
            "phase=pre-implementation", "phase=corrupt"
        ), encoding="utf-8")
        rejected = self.run_cli(
            "workflow", "init-or-resume", "WF-RESUME", "--resume", "1", check=False
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertIn("persisted workflow has invalid phase: corrupt", rejected.stderr)


class AtomicStateCompatibilityTest(unittest.TestCase):
    def test_atomic_status_publish_and_invalid_json_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            store = AtomicStatusStore(path, settle_seconds=0)
            store.publish({"status": "running", "step": 2})
            self.assertEqual(store.read(), {"status": "running", "step": 2})
            self.assertFalse(path.with_name("status.json.tmp").exists())

            path.write_text('{"status":', encoding="utf-8")
            self.assertEqual(store.read(), {"status": "invalid-json", "raw": '{"status":'})

    def test_terminal_status_detects_publish_during_settle_window(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text('{"status":"completed"}', encoding="utf-8")
            store = AtomicStatusStore(path, settle_seconds=0.01)

            def replace_during_sleep(_seconds):
                path.write_text('{"status":"completed","result":"new"}', encoding="utf-8")

            with mock.patch("evaluation.support.state.time.sleep", side_effect=replace_during_sleep):
                self.assertEqual(store.read(), {"status": "publishing"})


if __name__ == "__main__":
    unittest.main()
