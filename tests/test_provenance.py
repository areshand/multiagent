"""Focused tests for generic provenance primitives."""

import ast
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from multiagent_framework.provenance import (
    capture_git_identity,
    copy_artifact_bundle,
    sha256_file,
    validate_artifact_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


class ArtifactBundleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.txt"
        self.source.write_bytes(b"portable evidence\n")
        self.bundle = self.root / "bundle"

    def tearDown(self):
        self.temporary.cleanup()

    def test_sha256_and_valid_relocatable_bundle(self):
        self.assertEqual(sha256_file(self.source), hashlib.sha256(self.source.read_bytes()).hexdigest())
        records = copy_artifact_bundle(self.bundle, {"run.log": self.source})

        self.assertEqual(records[0]["kind"], "run.log")
        self.assertEqual(records[0]["path"], "artifacts/run.log")
        self.assertFalse(Path(records[0]["path"]).is_absolute())
        validate_artifact_bundle(self.bundle, records, {"run.log"})

        moved = self.root / "moved"
        self.bundle.rename(moved)
        validate_artifact_bundle(moved, records, {"run.log"})

    def test_rejects_tampering_and_missing_file(self):
        records = copy_artifact_bundle(self.bundle, {"result": self.source})
        artifact = self.bundle / records[0]["path"]
        artifact.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_artifact_bundle(self.bundle, records, {"result"})

        artifact.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            validate_artifact_bundle(self.bundle, records, {"result"})

    def test_rejects_duplicate_kind_and_path(self):
        records = copy_artifact_bundle(self.bundle, {"one": self.source, "two": self.source})
        duplicate_kind = [records[0], dict(records[0], path="artifacts/two")]
        with self.assertRaisesRegex(ValueError, "duplicate artifact kind"):
            validate_artifact_bundle(self.bundle, duplicate_kind, set())

        duplicate_path = [records[0], dict(records[1], path=records[0]["path"])]
        with self.assertRaisesRegex(ValueError, "duplicate artifact path"):
            validate_artifact_bundle(self.bundle, duplicate_path, set())

    def test_rejects_unsafe_and_mismatched_paths(self):
        records = copy_artifact_bundle(self.bundle, {"result": self.source})
        for path in ("../outside", "/tmp/outside", "..\\outside"):
            altered = [dict(records[0], path=path)]
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_artifact_bundle(self.bundle, altered, {"result"})

        altered = [dict(records[0], kind="other")]
        with self.assertRaisesRegex(ValueError, "kind/path mismatch"):
            validate_artifact_bundle(self.bundle, altered, set())

    def test_rejects_missing_kind_and_unsafe_kind(self):
        records = copy_artifact_bundle(self.bundle, {"result": self.source})
        with self.assertRaisesRegex(ValueError, "missing required"):
            validate_artifact_bundle(self.bundle, records, {"result", "log"})
        with self.assertRaises(ValueError):
            copy_artifact_bundle(self.bundle, {"../result": self.source})

    def test_source_is_python38_and_within_line_budget(self):
        source = (ROOT / "multiagent_framework/provenance.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 8))
        self.assertLessEqual(len(source.splitlines()), 220)


@unittest.skipUnless(shutil.which("git"), "git is required")
class GitIdentityTest(unittest.TestCase):
    def test_capture_clean_and_dirty_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            tracked = repo / "tracked.txt"
            tracked.write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "base"],
                cwd=repo,
                check=True,
            )

            identity = capture_git_identity(repo)
            expected_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
            ).stdout.strip()
            expected_tree = subprocess.run(
                ["git", "rev-parse", "HEAD^{tree}"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            self.assertEqual(identity, {"commit": expected_commit, "tree": expected_tree, "dirty": False})

            (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            dirty_identity = capture_git_identity(repo)
            self.assertEqual(dirty_identity["commit"], expected_commit)
            self.assertEqual(dirty_identity["tree"], expected_tree)
            self.assertIs(dirty_identity["dirty"], True)


if __name__ == "__main__":
    unittest.main()
