from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AuditLogComponentContractTests(unittest.TestCase):
    def test_image_is_a_separate_non_root_component(self):
        dockerfile = (ROOT / "docker/audit-log/Dockerfile").read_text()
        self.assertIn("USER 10020:10020", dockerfile)
        self.assertIn("COPY audit-log audit-log", dockerfile)
        self.assertNotIn("COPY runtime", dockerfile)
        self.assertNotIn("COPY control-server", dockerfile)
        self.assertNotIn("multiagent container-bootstrap", dockerfile)

    def test_service_has_no_production_or_model_credential_knowledge(self):
        source = "\n".join(path.read_text() for path in (ROOT / "audit-log/src").glob("*.mjs"))
        for forbidden in (
            "PROD_MCP_BEARER_TOKEN",
            "MULTIAGENT_KMS_KEY_ID",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GRAFANA_TOKEN",
            "KUBECONFIG",
        ):
            self.assertNotIn(forbidden, source)

    def test_bulk_trace_bodies_are_not_an_ingestion_field(self):
        schema = (ROOT / "contracts/audit-log-event-v1.schema.json").read_text()
        self.assertIn('"additionalProperties": false', schema)
        self.assertIn('"payloadDigest"', schema)
        self.assertIn('"artifactReferences"', schema)
        self.assertNotIn('"payload"', schema)

    def test_audit_logger_has_no_per_append_receipt_contract(self):
        self.assertFalse((ROOT / "contracts/audit-log-append-receipt-v1.schema.json").exists())
        source = "\n".join(path.read_text() for path in (ROOT / "audit-log/src").glob("*.mjs"))
        self.assertNotIn("AppendReceipt", source)
        self.assertNotIn("/v1/receipts/", source)


if __name__ == "__main__":
    unittest.main()
