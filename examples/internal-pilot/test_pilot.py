#!/usr/bin/env python3
"""No-network fixture tests for pilot.py."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


HERE = Path(__file__).resolve().parent
PILOT = HERE / "pilot.py"
HARNESS_ROOT = HERE.parents[1]


def run(argv, cwd: Path, check: bool = True):
    return subprocess.run(argv, cwd=str(cwd), text=True, capture_output=True, check=check)


class PilotFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="multiagent-pilot-test-")
        self.root = Path(self.tempdir.name)
        self.target = self.root / "target"
        self.target.mkdir()
        run(["git", "init", "--quiet"], self.target)
        (self.target / "state.txt").write_text("broken\n", encoding="utf-8")
        run(["git", "add", "state.txt"], self.target)
        run(
            [
                "git", "-c", "user.name=Pilot Fixture", "-c",
                "user.email=pilot@example.invalid", "-c", "commit.gpgsign=false",
                "commit", "--quiet", "-m", "fixture",
            ],
            self.target,
        )
        self.base_commit = run(["git", "rev-parse", "HEAD"], self.target).stdout.strip()
        self.harness_commit = run(["git", "rev-parse", "HEAD"], HARNESS_ROOT).stdout.strip()

        self.driver = self.root / "fake-driver.sh"
        self.driver.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "printf 'fixed\\n' > \"$PILOT_WORKTREE/state.txt\"\n"
            "printf 'new evidence\\n' > \"$PILOT_WORKTREE/new-file.txt\"\n"
            "printf 'fixture solver completed\\n'\n",
            encoding="utf-8",
        )
        self.driver.chmod(0o755)

        tasks = []
        for index in range(1, 6):
            task_id = f"fixture-{index:02d}"
            issue_file = self.root / f"{task_id}.md"
            issue_file.write_text("Change state.txt from broken to fixed.\n", encoding="utf-8")
            tasks.append(
                {
                    "id": task_id,
                    "team": "fixture-only",
                    "issue_url": f"internal://fixture/{task_id}",
                    "issue_file": issue_file.name,
                    "repository": str(self.target),
                    "base_commit": self.base_commit,
                    "solver_timeout_seconds": 60,
                    "preflight": [
                        {
                            "name": "reproduce",
                            "command": "grep -qx broken state.txt",
                            "expect_exit": 0,
                            "timeout_seconds": 10,
                        }
                    ],
                    "validation": [
                        {
                            "name": "fixed",
                            "command": "grep -qx fixed state.txt && test -f new-file.txt",
                            "expect_exit": 0,
                            "timeout_seconds": 10,
                        }
                    ],
                    "acceptance_criteria": ["state.txt contains exactly fixed"],
                }
            )
        self.manifest = self.root / "pilot.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "pilot_id": "fixture-pilot",
                    "harness_commit": self.harness_commit,
                    "arms": {
                        "baseline": {"driver": [str(self.driver)]},
                        "orchestrated": {"driver": [str(self.driver)]},
                    },
                    "tasks": tasks,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_template_is_deliberately_not_runnable(self) -> None:
        completed = run(
            ["python3", str(PILOT), "validate", str(HERE / "manifest.template.json")],
            HARNESS_ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("template_only", completed.stderr)
        self.assertIn("non-placeholder", completed.stderr)

    def test_five_task_run_captures_evidence_and_requires_review(self) -> None:
        validation = run(["python3", str(PILOT), "validate", str(self.manifest)], HARNESS_ROOT)
        self.assertIn("5 tasks, 2 arms", validation.stdout)

        output = self.root / "run"
        completed = run(
            [
                "python3", str(PILOT), "run", str(self.manifest),
                "--arm", "baseline", "--output", str(output), "--allow-dirty-harness",
            ],
            HARNESS_ROOT,
        )
        self.assertIn("(5 cells)", completed.stdout)

        results = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(results["counts"]["pending-review"], 5)
        self.assertEqual(results["counts"]["success"], 0)
        cell = output / "cells" / "fixture-01--baseline"
        evidence = json.loads((cell / "evidence.json").read_text(encoding="utf-8"))
        self.assertEqual(evidence["target_base_commit"], self.base_commit)
        self.assertEqual(evidence["mechanical_status"], "passed")
        self.assertEqual(len(evidence["diff_sha256"]), 64)
        self.assertIn("new-file.txt", (cell / "change.patch").read_text(encoding="utf-8"))
        self.assertIn("fixture solver completed", (cell / "driver.stdout.log").read_text(encoding="utf-8"))

        review = json.loads((cell / "review.template.json").read_text(encoding="utf-8"))
        review.update(
            {
                "reviewer": "fixture-reviewer",
                "reviewed_at": "2026-01-01T00:00:00Z",
                "outcome": "accepted",
            }
        )
        (cell / "review.json").write_text(json.dumps(review), encoding="utf-8")
        run(["python3", str(PILOT), "summarize", str(output)], HARNESS_ROOT)
        reviewed = json.loads((output / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(reviewed["counts"]["success"], 1)
        self.assertEqual(reviewed["counts"]["pending-review"], 4)

    def test_baseline_driver_passes_prompt_to_codex(self) -> None:
        fake_bin = self.root / "baseline-bin"
        fake_bin.mkdir()
        fake_codex = fake_bin / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\ncat > \"$PILOT_CELL_DIR/codex-stdin.txt\"\nprintf 'baseline complete\\n'\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        cell = self.root / "baseline-cell"
        cell.mkdir()
        prompt = cell / "task.md"
        prompt.write_text("fixture baseline prompt\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "PILOT_WORKTREE": str(self.target),
                "PILOT_CELL_DIR": str(cell),
                "PILOT_PROMPT_FILE": str(prompt),
            }
        )
        completed = subprocess.run(
            [str(HERE / "drivers" / "codex-baseline.sh")],
            cwd=str(HARNESS_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("baseline complete", completed.stdout)
        self.assertEqual((cell / "codex-stdin.txt").read_text(encoding="utf-8"), "fixture baseline prompt\n")

    def test_orchestrated_driver_builds_bounded_assignment(self) -> None:
        fake_harness = self.root / "fake-harness"
        fake_harness.mkdir()
        (fake_harness / "orchestrator_prompt.md").write_text("# Fixture orchestrator\n", encoding="utf-8")
        fake_launch = fake_harness / "launch.sh"
        fake_launch.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nmkdir -p \"$MULTIAGENT_STATE_DIR\"\nprintf 'orchestrated complete\\n' > \"$MULTIAGENT_STATE_DIR/orchestrator-last-message.txt\"\n",
            encoding="utf-8",
        )
        fake_launch.chmod(0o755)
        fake_bin = self.root / "orchestrated-bin"
        fake_bin.mkdir()
        for name in ("codex", "tmux"):
            path = fake_bin / name
            path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)

        cell = self.root / "orchestrated-cell"
        cell.mkdir()
        prompt = cell / "task.md"
        prompt.write_text("fixture orchestrated prompt\n", encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "PILOT_HARNESS_ROOT": str(fake_harness),
                "PILOT_WORKTREE": str(self.target),
                "PILOT_CELL_DIR": str(cell),
                "PILOT_PROMPT_FILE": str(prompt),
                "PILOT_TASK_ID": "fixture-orchestrated",
                "PILOT_ARM": "orchestrated",
                "PILOT_SOLVER_TIMEOUT_SECONDS": "60",
            }
        )
        completed = subprocess.run(
            [str(HERE / "drivers" / "multiagent-codex.sh")],
            cwd=str(HARNESS_ROOT),
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("orchestrated complete", completed.stdout)
        full_prompt = (cell / "orchestrator-prompt.md").read_text(encoding="utf-8")
        self.assertIn("fixture orchestrated prompt", full_prompt)
        self.assertIn(str(fake_harness / "bin" / "subagent.sh"), full_prompt)


if __name__ == "__main__":
    unittest.main()
