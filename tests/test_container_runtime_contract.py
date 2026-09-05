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

    def test_runtime_exposes_only_the_wiki_query_command(self):
        dockerfile = (ROOT / "docker/runtime/Dockerfile").read_text()
        self.assertIn("wiki-service/bin/wiki-query.mjs /usr/local/bin/wiki-query", dockerfile)
        self.assertNotIn("npm install --global /opt/multiagent/wiki-service", dockerfile)
        self.assertNotIn("WIKI_ROOT=/var/lib/wiki", dockerfile)

    def test_wiki_image_is_independent_and_unprivileged(self):
        dockerfile = (ROOT / "docker/wiki-service/Dockerfile").read_text()
        self.assertIn("COPY wiki-service/src ./src", dockerfile)
        self.assertIn("USER 10030:10030", dockerfile)
        self.assertIn("WIKI_ROOT=/var/lib/wiki", dockerfile)
        self.assertNotIn("runtime/", dockerfile)
        self.assertNotIn("control-server/", dockerfile)

    def test_role_environment_propagates_only_the_non_secret_wiki_url(self):
        runtime = (ROOT / "runtime/src/runtime.rs").read_text()
        self.assertIn('"MULTIAGENT_WIKI_URL"', runtime)
        self.assertNotIn('"WIKI_ROOT"', runtime)


if __name__ == "__main__":
    unittest.main()
