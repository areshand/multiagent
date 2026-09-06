#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path

from trace_exporter.trace_exporter import TraceExporter


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def upload(self, source, uri):
        self.objects[uri] = Path(source).read_bytes()

    def copy(self, source_uri, destination_uri):
        self.objects[destination_uri] = self.objects[source_uri]

    def download(self, uri, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[uri])

    def list_keys(self, bucket, prefix):
        root = f"s3://{bucket}/"
        return {uri.removeprefix(root) for uri in self.objects if uri.startswith(root + prefix)}


class TraceExporterTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        self.work = root / "work"
        self.status = root / "status.json"
        self.token = root / "token"
        self.token.write_text("trace-token-0123456789abcdef", encoding="utf-8")
        self.s3 = MemoryS3()
        self.posts = []

    def tearDown(self):
        self.temporary.cleanup()

    def exporter(self, post=None):
        def successful(url, token, body):
            self.posts.append((url, token, json.loads(body)))
            return 204

        return TraceExporter(
            source=self.source,
            work=self.work,
            status_file=self.status,
            bucket="trace-bucket",
            trace_prefix="production/sessions/session-1",
            outbox_prefix="production/logger-outbox",
            delivered_prefix="production/logger-delivered",
            session_id="session-1",
            logger_url="http://logger",
            logger_token_file=self.token,
            s3=self.s3,
            http_post=post or successful,
            clock=lambda: "2026-09-05T12:00:00Z",
        )

    def test_successful_export_commits_digest_reference_and_no_body(self):
        body = b"private trace body"
        (self.source / "trace.jsonl").write_bytes(body)

        status = self.exporter().run_once()

        self.assertTrue(status["ok"])
        self.assertTrue(status["loggerOk"])
        self.assertEqual(status["loggerPending"], 0)
        self.assertEqual(len(self.posts), 1)
        event = self.posts[0][2]
        self.assertEqual(event["eventType"], "trace.artifact_exported")
        self.assertEqual(event["sessionId"], "session-1")
        self.assertEqual(event["payloadDigest"], event["artifactReferences"][0]["digest"])
        self.assertEqual(event["artifactReferences"][0]["size"], len(body))
        self.assertNotIn(body, self.posts[0][2].__str__().encode())
        artifact_uri = event["artifactReferences"][0]["uri"]
        self.assertEqual(self.s3.objects[artifact_uri], body)

    def test_delivered_marker_makes_restart_idempotent(self):
        (self.source / "usage.json").write_text("{}", encoding="utf-8")
        first = self.exporter()
        first.run_once()
        self.assertEqual(len(self.posts), 1)
        # A fresh local cache re-uploads metadata, but the durable delivered
        # marker prevents another Logger request.
        for path in self.work.iterdir():
            if path.is_dir():
                import shutil

                shutil.rmtree(path)
        self.exporter().run_once()
        self.assertEqual(len(self.posts), 1)

    def test_logger_outage_leaves_durable_backlog_and_later_drains(self):
        (self.source / "report.md").write_text("report", encoding="utf-8")

        def unavailable(_url, _token, _body):
            raise OSError("temporary Logger outage")

        failed = self.exporter(unavailable).run_once()
        self.assertTrue(failed["ok"], "Logger availability must not change S3 export success")
        self.assertFalse(failed["loggerOk"])
        self.assertEqual(failed["loggerPending"], 1)
        self.assertIn("temporary Logger outage", failed["loggerError"])

        recovered = self.exporter().run_once()
        self.assertTrue(recovered["loggerOk"])
        self.assertEqual(recovered["loggerPending"], 0)
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()
