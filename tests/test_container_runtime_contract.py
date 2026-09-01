from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ContainerRuntimeContractTests(unittest.TestCase):
    def test_session_bootstrap_does_not_read_trace_archive(self):
        entrypoint = (ROOT / "docker/runtime/container-entrypoint.sh").read_text()
        self.assertNotIn("MULTIAGENT_STATE_S3_URI", entrypoint)
        self.assertNotIn("aws s3 sync", entrypoint)

    def test_state_parent_is_traversable_by_isolated_roles_but_not_writable(self):
        runtime = (ROOT / "runtime/src/runtime.rs").read_text()
        state_entry = runtime.split('base.join("state")', 1)[1].split("),", 1)[0]
        self.assertIn("config::ROLE_GID", state_entry)
        self.assertIn("0o2750", state_entry)
        self.assertNotIn("SUPERVISOR_CREDENTIAL_GID", state_entry)


if __name__ == "__main__":
    unittest.main()
