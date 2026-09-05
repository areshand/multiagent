import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WikiReadOnlyRoutingTests(unittest.TestCase):
    def test_caller_facing_wiki_evidence_does_not_force_reader_or_reviewer(self) -> None:
        orchestrator = (ROOT / "prompts/orchestrator.md").read_text(encoding="utf-8")
        routing = (ROOT / "prompts/playbooks/orchestration-routing.md").read_text(
            encoding="utf-8"
        )

        for document in (orchestrator, routing):
            normalized = " ".join(document.split())
            self.assertIn("Wiki", normalized)
            self.assertIn("caller-facing", normalized)
            self.assertTrue(
                "reader is not a prerequisite" in normalized.lower()
                or "does not force a reader" in normalized.lower()
            )
            self.assertIn("not", normalized.lower())
            self.assertIn("reviewer", normalized)
            self.assertNotIn("mechanical read-only completion gate", normalized)

    def test_reader_spawn_does_not_require_implementation_metadata(self) -> None:
        spawning = (ROOT / "prompts/playbooks/agent-spawning.md").read_text(
            encoding="utf-8"
        )
        routing = (ROOT / "prompts/playbooks/orchestration-routing.md").read_text(
            encoding="utf-8"
        )
        spawning_normalized = " ".join(spawning.split())
        routing_normalized = " ".join(routing.split())

        self.assertIn("## Read-Only Reader Spawn", spawning)
        self.assertIn('SUBAGENT_CLI="$VERIFIER_CLI"', spawning)
        self.assertIn("--role reader", spawning)
        self.assertIn("--access read-only", spawning)
        self.assertIn("without `--own`", spawning_normalized)
        self.assertIn("without `--own`", routing_normalized)
        self.assertIn("never receives source ownership", routing_normalized)
        self.assertIn("rather than source ownership", spawning_normalized)


if __name__ == "__main__":
    unittest.main()
