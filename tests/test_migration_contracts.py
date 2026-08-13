"""Black-box contracts that a replacement control-plane implementation must preserve.

These tests intentionally exercise the public CLI and durable files instead of
importing shell implementation details.  A Rust implementation can therefore
run the same suite during a side-by-side migration.
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

from multiagent_framework.state import AtomicStatusStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def read_env_file(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


class MigrationCliContractTest(unittest.TestCase):
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
            [str(PROJECT_ROOT / relative), *args],
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

    def run_cli_with_env(self, env, relative, *args, check=True):
        result = subprocess.run(
            [str(PROJECT_ROOT / relative), *args],
            cwd=self.repo,
            env=env,
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
            "bin/decision.sh", "init", "DEC-RUST", "--title", "Rust migration", "--owner", "user"
        )
        self.assertEqual(created.stdout, "decision created\tDEC-RUST\tRust migration\n")
        self.run_cli(
            "bin/decision.sh",
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
            "bin/decision.sh",
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
            "bin/decision.sh",
            "commit",
            "DEC-RUST",
            "--selected-plan",
            "PLAN-HYBRID",
            "--reason",
            "lowest migration risk",
            "--rollback-policy",
            "restore shell entrypoints",
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

    def test_rust_and_legacy_modes_read_each_others_v1_state(self):
        legacy = self.env.copy()
        legacy.update(
            {
                "MULTIAGENT_USE_LEGACY_DECISION": "1",
                "MULTIAGENT_USE_LEGACY_DAG": "1",
                "MULTIAGENT_USE_LEGACY_WORKFLOW": "1",
                "MULTIAGENT_USE_LEGACY_POLICY": "1",
            }
        )

        self.run_cli_with_env(
            legacy,
            "bin/decision.sh",
            "init",
            "DEC-LEGACY",
            "--title",
            "Legacy state",
            "--owner",
            "test",
        )
        self.assertIn(
            "decision_id=DEC-LEGACY",
            self.run_cli("bin/decision.sh", "show", "DEC-LEGACY").stdout,
        )

        self.run_cli("bin/decision.sh", "init", "DEC-RUST-READ", "--title", "Rust state")
        self.assertIn(
            "decision_id=DEC-RUST-READ",
            self.run_cli_with_env(
                legacy, "bin/decision.sh", "show", "DEC-RUST-READ"
            ).stdout,
        )

        self.run_cli_with_env(
            legacy, "bin/dag.sh", "init", "WF-LEGACY-DAG", "--title", "Legacy DAG"
        )
        self.run_cli_with_env(
            legacy,
            "bin/dag.sh",
            "add-node",
            "WF-LEGACY-DAG",
            "NODE-A",
            "--agent",
            "worker-a",
            "--assignment-id",
            "A-1",
            "--role",
            "qa",
            "--branch",
            "worker/a",
            "--owned",
            "src",
        )
        self.assertIn(
            "NODE-A\tworker-a",
            self.run_cli("bin/dag.sh", "show", "WF-LEGACY-DAG").stdout,
        )

        self.run_cli_with_env(legacy, "bin/workflow.sh", "init", "WF-LEGACY-LIFECYCLE")
        resumed = self.run_cli(
            "bin/workflow.sh",
            "init-or-resume",
            "WF-LEGACY-LIFECYCLE",
            "--resume",
            "1",
        )
        self.assertIn("workflow resumed\tWF-LEGACY-LIFECYCLE", resumed.stdout)

        outside = self.root / "legacy-approved"
        self.run_cli_with_env(legacy, "bin/write-policy.sh", "init")
        self.run_cli_with_env(
            legacy,
            "bin/write-policy.sh",
            "approve",
            str(outside),
            "--actor",
            "compatibility-test",
            "--assignment-id",
            "POLICY-LEGACY",
            "--reason",
            "verify Rust reader",
        )
        checked = self.run_cli("bin/write-policy.sh", "check", str(outside / "file.txt"))
        self.assertIn("allowed\t", checked.stdout)

    def test_finding_and_todo_read_contracts(self):
        self.run_cli(
            "bin/subagent.sh",
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
            self.run_cli("bin/subagent.sh", "finding-show", "F-RUST").stdout
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
                "bin/subagent.sh", "finding-list", "--severity", "blocking"
            ).stdout,
        )
        self.assertEqual(
            self.run_cli(
                "bin/subagent.sh", "finding-list", "--severity", "warning"
            ).stdout,
            "",
        )

        self.run_cli(
            "bin/subagent.sh",
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
        shown_todo = json.loads(self.run_cli("bin/subagent.sh", "todo-show", "T-RUST").stdout)
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
            self.run_cli("bin/subagent.sh", "todo-list", "--status", "open").stdout,
        )
        self.assertEqual(
            self.run_cli("bin/subagent.sh", "todo-list", "--status", "closed").stdout,
            "",
        )

    def test_unknown_command_exit_codes_are_stable(self):
        for script in ("bin/decision.sh", "bin/dag.sh", "bin/subagent.sh", "bin/write-policy.sh"):
            with self.subTest(script=script):
                result = self.run_cli(script, "not-a-command", check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("unknown command", result.stderr)

    def test_assignment_rejects_path_outside_repository(self):
        result = self.run_cli(
            "bin/subagent.sh",
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
                        str(PROJECT_ROOT / "bin/subagent.sh"),
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
        result = subprocess.run(
            [
                str(PROJECT_ROOT / "bin/multiagent"),
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
        self.assertEqual(payload["changed_files"], 2)
        self.assertEqual(payload["changed_paths"], ["README.md", "src/lib.rs"])
        self.assertEqual(payload["changed_code_paths"], ["src/lib.rs"])
        self.assertRegex(payload["final_diff_sha256"], r"^[0-9a-f]{64}$")

    def test_dag_concurrent_node_updates_do_not_lose_rows(self):
        self.run_cli("bin/dag.sh", "init", "WF-DAG-CONCURRENT", "--title", "Concurrent DAG")
        processes = []
        for index in range(12):
            processes.append(
                subprocess.Popen(
                    [
                        str(PROJECT_ROOT / "bin/dag.sh"),
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
        self.run_cli("bin/write-policy.sh", "init")
        processes = []
        approved_paths = [self.root / "outside" / "path-{:02d}".format(index) for index in range(12)]
        for index, path in enumerate(approved_paths):
            processes.append(
                subprocess.Popen(
                    [
                        str(PROJECT_ROOT / "bin/write-policy.sh"),
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
                        str(PROJECT_ROOT / "bin/subagent.sh"),
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
        self.run_cli("bin/workflow.sh", "init", "WF-CONCURRENT")
        processes = []
        for index in range(12):
            processes.append(
                subprocess.Popen(
                    [
                        str(PROJECT_ROOT / "bin/workflow.sh"),
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
        self.run_cli("bin/workflow.sh", "init", "WF-DUPLICATE")
        processes = [
            subprocess.Popen(
                [
                    str(PROJECT_ROOT / "bin/workflow.sh"),
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
        self.run_cli("bin/workflow.sh", "init", "WF-RESUME")
        lifecycle = self.state / "workflows" / "WF-RESUME" / "lifecycle" / "lifecycle.env"
        initial = read_env_file(lifecycle)
        self.assertEqual(
            list(initial),
            [
                "workflow_id",
                "phase",
                "iteration",
                "preimplementation_gate",
                "decision_id",
                "plan_id",
                "decision_revision",
                "implementation_context",
                "implementation_context_sha256",
                "authority_review_id",
                "candidate_diff_hash",
                "reviewed_diff_hash",
                "resume_count",
                "created_at",
                "updated_at",
            ],
        )
        resumed = self.run_cli("bin/workflow.sh", "init-or-resume", "WF-RESUME", "--resume", "1")
        self.assertIn("workflow resumed\tWF-RESUME\tpre-implementation", resumed.stdout)
        self.assertEqual(read_env_file(lifecycle)["resume_count"], "1")

        lifecycle.write_text(lifecycle.read_text(encoding="utf-8").replace(
            "phase=pre-implementation", "phase=corrupt"
        ), encoding="utf-8")
        rejected = self.run_cli(
            "bin/workflow.sh", "init-or-resume", "WF-RESUME", "--resume", "1", check=False
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

            with mock.patch("multiagent_framework.state.time.sleep", side_effect=replace_during_sleep):
                self.assertEqual(store.read(), {"status": "publishing"})


if __name__ == "__main__":
    unittest.main()
