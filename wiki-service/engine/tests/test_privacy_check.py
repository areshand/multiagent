import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRIVACY = ROOT / "scripts" / "privacy_check.py"


class PrivacyCheckTests(unittest.TestCase):
    def run_check(self, path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(PRIVACY), str(path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_repo_passes_privacy_check(self) -> None:
        result = self.run_check(ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_blocks_private_source_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            blocked = root / "Notion Export"
            blocked.mkdir()
            (blocked / "note.md").write_text("fake", encoding="utf-8")
            result = self.run_check(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("blocked private source directory", result.stdout)

    def test_blocks_home_path_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private_path = "/Users/" + "example/private.md"
            (root / "note.md").write_text(f"source: {private_path}", encoding="utf-8")
            result = self.run_check(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("blocked private-data content pattern", result.stdout)

    def test_blocks_secret_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_text = "api" + "_key = 'abcdefghijklmnop1234'"
            (root / "config.md").write_text(secret_text, encoding="utf-8")
            result = self.run_check(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("blocked private-data content pattern", result.stdout)


if __name__ == "__main__":
    unittest.main()
