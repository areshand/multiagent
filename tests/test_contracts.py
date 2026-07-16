"""Focused tests for the framework contract boundary."""

import ast
import unittest
from pathlib import Path

from multiagent_framework.coding import contracts
from evaluation.native_solver import swe_prod_contracts


ROOT = Path(__file__).resolve().parents[1]


class ContractFrameworkTest(unittest.TestCase):
    def test_extracts_explicit_and_sentence_requirements(self):
        issue = (
            "Requirements:\n"
            "- `RequestCache` must preserve the request config.\n"
            "- Audit errors from `ServeHTTP` should be logged.\n"
        )

        requirements = contracts.extract_public_issue_requirements(issue)

        self.assertEqual(len(requirements), 2)
        self.assertEqual(requirements[0].id, "issue-requestcache-request-config")
        self.assertIn("servehttp", requirements[1].keywords)
        self.assertEqual(
            contracts.issue_coverage_requirements(issue),
            [requirement.as_dict() for requirement in requirements],
        )

    def test_issue_coverage_requires_strong_evidence_for_each_item(self):
        issue = "Cache config must persist.\nAudit request errors should be logged."

        self.assertTrue(contracts.issue_coverage_blockers(issue, "validation passed"))
        weak = contracts.issue_coverage_blockers(
            issue,
            "issue-coverage-ledger: cache source-not-changed; audit verifier-reviewed",
        )
        self.assertTrue(any("weak non-evidence" in blocker for blocker in weak))
        self.assertEqual(
            contracts.issue_coverage_blockers(
                issue,
                "issue-coverage-ledger: cache config implemented-by=cache.py; "
                "audit request already-satisfied-by=audit.py/source-inspection",
            ),
            [],
        )

    def test_provenance_and_history_contracts(self):
        provenance_issue = "Return a response copied from the initial request configuration."
        self.assertTrue(contracts.data_provenance_required(provenance_issue))
        self.assertEqual(
            contracts.data_provenance_blockers(
                provenance_issue,
                "data-provenance-ledger: source=request stored-as=job.request "
                "output=response field=timeout analogue=request.py:Request",
            ),
            [],
        )

        history_issue = "After upgrading, the migration breaks compatibility and users lose access."
        self.assertTrue(contracts.historical_contract_required(history_issue))
        self.assertEqual(
            contracts.historical_contract_blockers(
                history_issue,
                "historical-contract-ledger: baseline-source=git^ transition-path=upgrade "
                "mutated-outputs=user,mapping compatibility-invariant=preserve-access",
            ),
            [],
        )

    def test_model_renders_generic_ledger(self):
        ledger = contracts.ContractLedger.from_issue(
            "Requirements:\n- `Widget` should preserve config.",
            public_symbols=("Widget",),
            context_excerpt="Public context",
        )

        rendered = ledger.render()

        self.assertIn("# Contract Ledger", rendered)
        self.assertIn("`Widget`", rendered)
        self.assertIn("issue-widget-config", rendered)
        self.assertIn("Completion rules:", rendered)

    def test_framework_source_is_python38_and_environment_neutral(self):
        source = (ROOT / "multiagent_framework/coding/contracts.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 8))
        forbidden = (
            "swe_bench",
            "swe bench",
            "evalscope",
            "benchmark-row",
            "hidden-test",
            "eval_",
        )
        for marker in forbidden:
            self.assertNotIn(marker, source.lower())


class SweContractAdapterTest(unittest.TestCase):
    def test_adapter_reuses_framework_gates(self):
        issue = "Cache config must persist.\nAudit request errors should be logged."
        self.assertEqual(
            swe_prod_contracts.issue_coverage_requirements(issue),
            contracts.issue_coverage_requirements(issue),
        )
        self.assertEqual(
            swe_prod_contracts.issue_coverage_blockers(issue, "validation passed"),
            contracts.issue_coverage_blockers(issue, "validation passed"),
        )
        self.assertIs(swe_prod_contracts.data_provenance_blockers, contracts.data_provenance_blockers)
        self.assertIs(swe_prod_contracts.historical_contract_blockers, contracts.historical_contract_blockers)

    def test_adapter_uses_only_public_problem_statement(self):
        metadata = {
            "problem_statement": "Cache config must persist.",
            "requirements": "Private requirement",
            "interface": "PrivateInterface",
        }

        ledger = swe_prod_contracts.contract_ledger_text("Short symptom.", metadata)

        self.assertIn("Cache config must persist.", ledger)
        self.assertNotIn("Private requirement", ledger)
        self.assertNotIn("PrivateInterface", ledger)

    def test_adapter_strips_runtime_prompt_envelope(self):
        issue = (
            "Cache config must persist.\nAudit request errors should be logged.\n"
            "Current `/app` diff excerpt\n"
            "A response should preserve request state."
        )

        requirements = swe_prod_contracts.issue_coverage_requirements(issue)
        summaries = "\n".join(str(requirement["summary"]) for requirement in requirements)

        self.assertIn("Cache config", summaries)
        self.assertNotIn("response", summaries.lower())


if __name__ == "__main__":
    unittest.main()
