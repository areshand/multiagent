import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LoggerComponentContractTests(unittest.TestCase):
    def test_logger_is_a_rust_workspace_service(self):
        cargo = (ROOT / "Cargo.toml").read_text()
        dockerfile = (ROOT / "docker/logger/Dockerfile").read_text()
        self.assertIn('"logger"', cargo)
        self.assertIn("cargo build --locked --release -p multiagent-logger", dockerfile)
        self.assertIn('ENTRYPOINT ["/usr/local/bin/logger", "serve"]', dockerfile)
        self.assertNotIn("node:", dockerfile)

    def test_logger_keeps_authority_out_of_the_append_contract(self):
        source = "\n".join(path.read_text() for path in (ROOT / "logger/src").glob("*.rs"))
        self.assertIn("StatusCode::NO_CONTENT", source)
        self.assertNotIn("append-receipt", source.lower())
        self.assertNotIn("operation permit", source.lower())

    def test_logger_uses_an_append_only_file_not_a_database(self):
        cargo = (ROOT / "logger/Cargo.toml").read_text()
        store = (ROOT / "logger/src/store.rs").read_text()
        self.assertNotIn("rusqlite", cargo)
        self.assertNotIn("sqlite", store.lower())
        self.assertIn("sync_data", store)
        self.assertIn("read_until", store)
        self.assertIn("LOCK_EX | libc::LOCK_NB", store)

    def test_event_contract_is_bounded_metadata(self):
        schema = json.loads((ROOT / "contracts/logger-event-v1.schema.json").read_text())
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["artifactReferences"]["maxItems"], 64)
        self.assertNotIn("payload", schema["properties"])


if __name__ == "__main__":
    unittest.main()
