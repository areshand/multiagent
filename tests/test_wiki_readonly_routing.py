import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WikiReadOnlyRoutingTests(unittest.TestCase):
    def test_caller_facing_wiki_evidence_uses_reader_not_scout(self) -> None:
        orchestrator = (ROOT / "orchestrator_prompt.md").read_text(encoding="utf-8")
        routing = (ROOT / "prompts/playbooks/orchestration-routing.md").read_text(
            encoding="utf-8"
        )

        for document in (orchestrator, routing):
            normalized = " ".join(document.split())
            self.assertIn("When Wiki", normalized)
            self.assertIn("caller-facing", normalized)
            self.assertIn("`reader`", normalized)
            self.assertIn("`scout`", normalized)
            self.assertIn("mechanical read-only completion gate", normalized)

    def test_integrity_reviewer_excludes_only_its_active_launch_state(self) -> None:
        reviewer = (
            ROOT / "prompts/roles/read-only-integrity-reviewer.md"
        ).read_text(encoding="utf-8")
        normalized = " ".join(reviewer.split())

        self.assertIn("Exclude only your own active", normalized)
        self.assertIn("state=running", normalized)
        self.assertIn("Every other launch", normalized)
        self.assertIn("after your output is sealed", normalized)
        self.assertIn("at least one completed reader", normalized)

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
        self.assertIn("never receive source ownership", routing_normalized)
        self.assertIn("rather than source ownership", spawning_normalized)


if __name__ == "__main__":
    unittest.main()
