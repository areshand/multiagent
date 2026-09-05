import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "personal_llm_wiki.py"


class CliSmokeTests(unittest.TestCase):
    def run_cli(self, *args: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(environment or {})},
        )

    def test_init_submit_validate_and_lint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            init = self.run_cli("init-vault", "--vault", str(vault))
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            self.assertTrue((vault / "LLM Wiki/index.md").exists())
            self.assertTrue((vault / "LLM Wiki/system/feedback/inbox.jsonl").exists())

            submit = self.run_cli(
                "submit-feedback",
                "--vault",
                str(vault),
                "--raw-feedback",
                "This synthetic answer missed the fake memory platform concept.",
                "--expected-behavior",
                "Future synthetic answers should mention the fake memory platform.",
                "--type",
                "missing_context",
                "--tags",
                "fake",
            )
            self.assertEqual(submit.returncode, 0, submit.stdout + submit.stderr)

            log_path = vault / "LLM Wiki/system/feedback/inbox.jsonl"
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["status"], "inbox")
            self.assertEqual(record["feedback_type"], "missing_context")

            validate = self.run_cli("validate-feedback", "--vault", str(vault))
            self.assertEqual(validate.returncode, 0, validate.stdout + validate.stderr)

            steward = self.run_cli("run-steward", "--vault", str(vault))
            self.assertEqual(steward.returncode, 0, steward.stdout + steward.stderr)
            updated = json.loads(lines[0])
            updated_lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(updated_lines), 1)
            updated = json.loads(updated_lines[0])
            self.assertEqual(updated["status"], "patch_proposed")
            self.assertTrue((vault / "LLM Wiki/system/state/feedback steward state.md").exists())
            self.assertTrue(list((vault / "LLM Wiki/system/patches/pending").glob("*.md")))
            self.assertTrue(list((vault / "LLM Wiki/system/evals/retrieval").glob("*.md")))
            self.assertTrue(list((vault / "LLM Wiki/system/runs/feedback-steward").glob("*.md")))

            lint = self.run_cli("lint-wiki", "--vault", str(vault))
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_lint_demo_fixture(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "demo-vault"
        result = self.run_cli("lint-wiki", "--vault", str(fixture))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_config_based_vault_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            config = root / "config.yml"
            config.write_text(f"vault_root: {vault}\n", encoding="utf-8")
            result = self.run_cli("--config", str(config), "init-vault")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((vault / "LLM Wiki/index.md").exists())

    def test_validate_feedback_rejects_bad_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            (vault / "LLM Wiki/system/feedback").mkdir(parents=True)
            (vault / "LLM Wiki/system/feedback/inbox.jsonl").write_text("{bad json}\n", encoding="utf-8")
            result = self.run_cli("validate-feedback", "--vault", str(vault))
            self.assertEqual(result.returncode, 1)
            self.assertIn("invalid JSON", result.stdout)

    def test_environment_discovery_accepts_llm_wiki_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            init = self.run_cli("init-vault", "--vault", str(vault))
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            lint = self.run_cli(
                "lint-wiki",
                environment={"CHECK_MY_WIKI_PATH": str(vault / "LLM Wiki")},
            )
            self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)

    def test_explicit_vault_may_point_to_llm_wiki_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            init = self.run_cli("init-vault", "--vault", str(vault / "LLM Wiki"))
            self.assertEqual(init.returncode, 0, init.stdout + init.stderr)
            self.assertTrue((vault / "LLM Wiki/index.md").exists())


if __name__ == "__main__":
    unittest.main()
