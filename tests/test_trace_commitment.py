#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from trace_commitment.trace_commitment import TraceCommitter


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def upload(self, source, uri):
        self.objects[uri] = Path(source).read_bytes()

    def download(self, uri, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[uri])

    def list_keys(self, bucket, prefix):
        root = f"s3://{bucket}/"
        return {uri.removeprefix(root) for uri in self.objects if uri.startswith(root + prefix)}


class TraceCommitterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "existing-export-stage"
        self.source.mkdir()
        self.work = root / "work"
        self.status = root / "status.json"
        self.status.write_text('{"ok":true,"lastSuccessAt":"2026-09-05T11:59:00Z"}\n')
        self.token = root / "token"
        self.token.write_text("trace-token-0123456789abcdef", encoding="utf-8")
        self.s3 = MemoryS3()
        self.posts = []

    def tearDown(self):
        self.temporary.cleanup()

    def committer(self, post=None):
        def successful(url, token, body):
            self.posts.append((url, token, json.loads(body)))
            return 204

        return TraceCommitter(
            source=self.source,
            destination="s3://trace-bucket/production/sessions/session-1",
            session_id="session-1",
            work=self.work,
            status_file=self.status,
            logger_url="http://logger",
            logger_token_file=self.token,
            s3=self.s3,
            http_post=post or successful,
            clock=lambda: "2026-09-05T12:00:00Z",
        )

    def test_successful_sync_gets_separate_manifest_and_metadata_only_event(self):
        trace_body = b"private trace body"
        (self.source / "trace.jsonl").write_bytes(trace_body)

        status = self.committer().commit()

        self.assertTrue(status["ok"])
        self.assertTrue(status["loggerOk"])
        self.assertEqual(status["loggerPending"], 0)
        event = self.posts[0][2]
        self.assertEqual(event["eventType"], "trace.artifact_exported")
        self.assertEqual(len(event["artifactReferences"]), 1)
        manifest_uri = event["artifactReferences"][0]["uri"]
        self.assertIn("/commitments/", manifest_uri)
        manifest = json.loads(self.s3.objects[manifest_uri])
        self.assertEqual(manifest["artifacts"][0]["size"], len(trace_body))
        self.assertNotIn(trace_body, json.dumps(event).encode())

    def test_delivered_marker_makes_repeated_post_upload_hook_idempotent(self):
        (self.source / "usage.json").write_text("{}", encoding="utf-8")
        self.committer().commit()
        self.committer().commit()
        self.assertEqual(len(self.posts), 1)

    def test_logger_outage_preserves_s3_success_and_durable_backlog(self):
        (self.source / "report.md").write_text("report", encoding="utf-8")

        def unavailable(_url, _token, _body):
            raise OSError("temporary Logger outage")

        failed = self.committer(unavailable).commit()
        self.assertTrue(failed["ok"])
        self.assertFalse(failed["loggerOk"])
        self.assertEqual(failed["loggerPending"], 1)

        recovered = self.committer().commit()
        self.assertTrue(recovered["loggerOk"])
        self.assertEqual(recovered["loggerPending"], 0)
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()
