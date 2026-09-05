import subprocess
import tempfile
import unittest
from pathlib import Path


ENGINE_ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ENGINE_ROOT.parent


class MigrationContractTests(unittest.TestCase):
    def test_migrated_engine_surfaces_are_present(self) -> None:
        required = [
            "agents/personal_llm_wiki_maintainer.md",
            "agents/personal_wiki_feedback_steward.md",
            "prompts/feedback-steward.md",
            "prompts/query-answering.md",
            "prompts/source-ingest.md",
            "prompts/wiki-maintainer.md",
            "schemas/eval.schema.json",
            "schemas/feedback-event.schema.json",
            "schemas/frontmatter.schema.json",
            "schemas/patch-proposal.schema.json",
            "schemas/steward-state.schema.json",
            "templates/wiki/index.md",
            "templates/wiki/schema.md",
            "scripts/personal_llm_wiki.py",
            "scripts/privacy_check.py",
            "scripts/search_wiki.py",
        ]
        for relative in required:
            with self.subTest(path=relative):
                self.assertTrue((ENGINE_ROOT / relative).is_file())

    def test_current_codex_skill_contracts_are_preserved(self) -> None:
        check = (ENGINE_ROOT / "skills/check-my-wiki/SKILL.md").read_text(encoding="utf-8")
        capture = (ENGINE_ROOT / "skills/capture-personal-wiki-source/SKILL.md").read_text(encoding="utf-8")
        for expected in [
            "LLM Wiki/index.md",
            "LLM Wiki/graph/knowledge graph.md",
            "explicit_link",
            "semantic_link",
            "Search raw sources",
        ]:
            self.assertIn(expected, check)
        for expected in [
            "Raw Materials/<Domain>/YYYY-MM-DD <short-slug>.md",
            "LLM Wiki/log.md",
            "source_count",
            "privacy: private | sensitive_health",
        ]:
            self.assertIn(expected, capture)

    def test_stable_wrapper_runs_the_migrated_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            result = subprocess.run(
                [str(SERVICE_ROOT / "bin/personal-llm-wiki"), "init-vault", "--vault", str(vault)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((vault / "LLM Wiki/index.md").is_file())


if __name__ == "__main__":
    unittest.main()
