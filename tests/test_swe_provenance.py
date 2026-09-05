"""Focused tests for SWE Bench Pro provenance capture and validation."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from evaluation.swe_bench_pro import native_runner_summary_from_text
from evaluation.swe_bench_pro_on_demand import (
    OnDemandImageManager,
    SOLVER_SOURCE_LABEL,
    docker_inspect_reports_missing,
    inspect_image_identity,
    native_solver_source_digest,
)
from evaluation.swe_bench_pro_provenance import capture_bundle, validate_bundle


@unittest.skipUnless(shutil.which("git"), "git is required for provenance capture")
class SweProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.solver = self._git_repo("solver", {"launch.sh": "#!/bin/sh\n", "prompts/orchestrator.md": "solve\n"})
        self.evalscope = self._git_repo("evalscope", {"evalscope/version.py": '__version__ = "1.8.1"\n'})
        self.swe = self._git_repo(
            "swe",
            {"helper_code/sweap_eval_full_v2.jsonl": '{"instance_id":"instance_org__repo-a"}\n'},
        )
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.bundle = self.root / "bundle"
        self._write_run_artifacts()

    def tearDown(self):
        self.temporary.cleanup()

    def _git_repo(self, name, files):
        repo = self.root / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        for relative, content in files.items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"], cwd=repo, check=True
        )
        return repo

    def _write_json(self, name, payload):
        path = self.sources / name
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_run_artifacts(self):
        self.solver_digest = native_solver_source_digest(self.solver)
        instance_id = "instance_org__repo-a"
        self.eval_log = self.sources / "eval-log.log"
        log_text = (
            "multiagent-native exited: sample=0 rc=0 wall=1.5s timed_out=False\n"
            'multiagent-native runtime: sample=0 identity={"codex_version":"codex-cli 0.144.1","node_version":"v22.12.0"}\n'
        )
        self.eval_log.write_text(log_text, encoding="utf-8")
        native = native_runner_summary_from_text(log_text)
        selected = [{"official_index": 0, "instance_id": instance_id}]
        self.summary = self._write_json(
            "summary.json",
            {
                "benchmark": "swe-bench-pro",
                "status": "completed",
                "sample_size": 1,
                "score": 1.0,
                "end_to_end_score": 1.0,
                "official_verifier_evidence": True,
                "sample_shard": {"selected_instances": selected},
                "native_runner": native,
            },
        )
        self.config = self._write_json(
            "config.json",
            {
                "agent_config": {
                    "kwargs": {"model_name": "gpt-5.4", "codex_auth_json": "/private/auth.json"}
                },
                "ignore_errors": False,
                "sandbox": {"default_config": {"platform": "linux/amd64"}},
            },
        )
        self.config_yaml = self.sources / "config.yaml"
        self.config_yaml.write_text("model: gpt-5.4\n", encoding="utf-8")
        self.preflight = self._write_json("preflight.json", {"sample_shard": {"selected_instances": selected}})
        identity = {
            "image_id": "sha256:" + "a" * 64,
            "os": "linux",
            "architecture": "amd64",
        }
        self.image_status = self._write_json(
            "image-status.json",
            {
                "status": "completed",
                "platform": "linux/amd64",
                "counts": {"failed": 0, "stopped_low_disk": 0, "bake_failed": 0, "prune_failed": 0},
                "records": [
                    {
                        "instance_id": instance_id,
                        "status": "baked",
                        "baked_image": "multiagent-native-swe:fixture-" + self.solver_digest[:16],
                        "solver_source_sha256": self.solver_digest,
                        "base_identity": identity,
                        "baked_identity": {
                            **identity,
                            "image_id": "sha256:" + "b" * 64,
                            "labels": {SOLVER_SOURCE_LABEL: self.solver_digest},
                        },
                    }
                ],
            },
        )
        self.evalscope_report = self._write_json("evalscope-report.json", {"score": 1.0, "num": 1})

    def _args(self):
        return Namespace(
            bundle=self.bundle,
            solver_repo=self.solver,
            evalscope_repo=self.evalscope,
            swe_bench_pro_repo=self.swe,
            summary=self.summary,
            config_json=self.config,
            config_yaml=self.config_yaml,
            preflight=self.preflight,
            image_status=self.image_status,
            evalscope_report=self.evalscope_report,
            eval_log=self.eval_log,
        )

    def test_capture_is_relocatable_and_recomputed(self):
        manifest = capture_bundle(self._args())
        self.assertEqual(manifest["evidence"]["sample_size"], 1)
        self.assertEqual(manifest["evidence"]["solver_source_sha256"], self.solver_digest)
        moved = self.root / "moved"
        self.bundle.rename(moved)
        self.assertEqual(validate_bundle(moved)["evidence"], manifest["evidence"])

    def test_rejects_tampered_artifact_and_manifest_evidence(self):
        capture_bundle(self._args())
        (self.bundle / "artifacts/summary.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_bundle(self.bundle)

        self.bundle = self.root / "bundle-2"
        capture_bundle(self._args())
        manifest_path = self.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["evidence"]["score"] = 0.0
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_bundle(self.bundle)

    def test_rejects_dirty_source_and_unbound_image(self):
        (self.solver / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be clean"):
            capture_bundle(self._args())
        (self.solver / "dirty.txt").unlink()

        image_status = json.loads(self.image_status.read_text(encoding="utf-8"))
        image_status["records"][0]["baked_image"] = "multiagent-native-swe:wrong"
        self.image_status.write_text(json.dumps(image_status), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not bound"):
            capture_bundle(self._args())

    def test_rejects_image_with_unbound_source_label(self):
        image_status = json.loads(self.image_status.read_text(encoding="utf-8"))
        image_status["records"][0]["baked_identity"]["labels"][SOLVER_SOURCE_LABEL] = "0" * 64
        self.image_status.write_text(json.dumps(image_status), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "image label is not bound"):
            capture_bundle(self._args())


class ImageIdentityTest(unittest.TestCase):
    def test_docker_inspect_distinguishes_missing_image_from_infrastructure_failure(self):
        self.assertTrue(docker_inspect_reports_missing("Error response from daemon: No such image: local:test"))
        self.assertFalse(
            docker_inspect_reports_missing(
                "permission denied while trying to connect to the docker API"
            )
        )

    def test_on_demand_image_manager_fails_fast_when_docker_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = OnDemandImageManager(
                archive_dir=root,
                status_path=root / "status.json",
                platform="linux/amd64",
                image_timeout=60,
                retries=3,
                backoff_s=180,
                min_free_gb=0,
                prune_after_sample=False,
                native_solver_source=root,
            )
            with mock.patch(
                "evaluation.swe_bench_pro_on_demand.docker_image_present",
                return_value=(False, "permission denied while trying to connect to the docker API"),
            ):
                with mock.patch(
                    "evaluation.swe_bench_pro_on_demand.preload_image_with_retries"
                ) as preload:
                    with self.assertRaisesRegex(RuntimeError, "cannot determine whether Docker image"):
                        manager.ensure_image("local:test", "row")
                    preload.assert_not_called()

    def test_adapter_is_python38_and_within_line_budget(self):
        source = (Path(__file__).resolve().parents[1] / "evaluation/swe_bench_pro_provenance.py").read_text(
            encoding="utf-8"
        )
        ast.parse(source, feature_version=(3, 8))
        self.assertLessEqual(len(source.splitlines()), 300)

    def test_docker_identity_uses_local_content_id(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "Id": "sha256:" + "c" * 64,
                    "RepoDigests": [],
                    "Os": "linux",
                    "Architecture": "amd64",
                    "Config": {"Labels": {SOLVER_SOURCE_LABEL: "d" * 64}},
                }
            ),
            stderr="",
        )
        with mock.patch("evaluation.swe_bench_pro_on_demand.subprocess.run", return_value=completed):
            identity = inspect_image_identity("local:test")
        self.assertEqual(identity["image_id"], "sha256:" + "c" * 64)
        self.assertEqual(identity["repo_digests"], [])
        self.assertEqual(identity["labels"][SOLVER_SOURCE_LABEL], "d" * 64)

    def test_solver_digest_tracks_included_content_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "launch.sh").write_text("one\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs/ignored.md").write_text("ignored one\n", encoding="utf-8")
            (root / "target/debug").mkdir(parents=True)
            (root / "target/debug/multiagent").write_bytes(b"build artifact one")
            first = native_solver_source_digest(root)
            (root / "launch.sh").chmod(0o755)
            self.assertNotEqual(native_solver_source_digest(root), first)
            first = native_solver_source_digest(root)
            (root / "docs/ignored.md").write_text("ignored two\n", encoding="utf-8")
            self.assertEqual(native_solver_source_digest(root), first)
            (root / "target/debug/multiagent").write_bytes(b"build artifact two")
            self.assertEqual(native_solver_source_digest(root), first)
            (root / "launch.sh").write_text("two\n", encoding="utf-8")
            self.assertNotEqual(native_solver_source_digest(root), first)


if __name__ == "__main__":
    unittest.main()
