#!/usr/bin/env python3
"""Export trace files to S3 and durably commit metadata to Logger.

The S3 outbox is deliberately metadata-only. Trace bodies are uploaded to a
content-addressed artifact key and never sent to Logger.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def safe_segment(value: str, name: str) -> str:
    if (
        not value
        or len(value) > 128
        or not value[0].isascii()
        or not value[0].isalnum()
        or any(
            not character.isascii()
            or not (character.isalnum() or character in "._:-")
            for character in value
        )
    ):
        raise ValueError(f"{name} has an invalid format")
    return value


def normalized_prefix(value: str, name: str) -> str:
    parts = PurePosixPath(value.strip("/")).parts
    if (
        not parts
        or value != value.strip("/")
        or any(part in ("", ".", "..") for part in parts)
        or any(character in value for character in "\r\n?#\\")
    ):
        raise ValueError(f"{name} has an invalid format")
    return "/".join(parts)


class AwsS3:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.runner = runner or subprocess.run

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self.runner(args, check=True, capture_output=True, text=True)

    def upload(self, source: Path, uri: str) -> None:
        self._run(["aws", "s3", "cp", str(source), uri, "--only-show-errors"])

    def copy(self, source_uri: str, destination_uri: str) -> None:
        self._run(["aws", "s3", "cp", source_uri, destination_uri, "--only-show-errors"])

    def download(self, uri: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["aws", "s3", "cp", uri, str(destination), "--only-show-errors"])

    def list_keys(self, bucket: str, prefix: str) -> set[str]:
        result = self._run(
            [
                "aws",
                "s3api",
                "list-objects-v2",
                "--bucket",
                bucket,
                "--prefix",
                prefix,
                "--output",
                "json",
            ]
        )
        decoded = json.loads(result.stdout or "{}")
        return {item["Key"] for item in decoded.get("Contents", [])}


class TraceExporter:
    def __init__(
        self,
        *,
        source: Path,
        work: Path,
        status_file: Path,
        bucket: str,
        trace_prefix: str,
        outbox_prefix: str,
        delivered_prefix: str,
        session_id: str,
        logger_url: str,
        logger_token_file: Path,
        s3: AwsS3 | None = None,
        http_post: Callable[[str, str, bytes], int] | None = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.source = source
        self.work = work
        self.status_file = status_file
        self.bucket = bucket
        self.trace_prefix = normalized_prefix(trace_prefix, "trace prefix")
        self.outbox_prefix = normalized_prefix(outbox_prefix, "outbox prefix")
        self.delivered_prefix = normalized_prefix(delivered_prefix, "delivered prefix")
        self.session_id = safe_segment(session_id, "session ID")
        self.logger_url = logger_url.rstrip("/")
        self.logger_token_file = logger_token_file
        self.s3 = s3 or AwsS3()
        self.http_post = http_post or self._post
        self.clock = clock
        self.cache = work / "cache"
        self.staging = work / "staging"
        self.queue_cache = work / "queue"

    def run_once(self) -> dict[str, object]:
        attempted_at = self.clock()
        uploaded = 0
        scanned = 0
        export_error: str | None = None
        logger_error: str | None = None
        self.work.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir(parents=True, exist_ok=True)

        try:
            for source_file, relative in self._source_files():
                scanned += 1
                staged = self.staging / relative
                staged.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(source_file, staged)
                except (FileNotFoundError, PermissionError):
                    continue
                digest, size = sha256_file(staged)
                cached = self.cache / relative
                if cached.is_file() and sha256_file(cached) == (digest, size):
                    continue
                media_type = mimetypes.guess_type(relative.as_posix())[0] or "application/octet-stream"
                encoded_relative = quote(relative.as_posix(), safe="/")
                canonical_key = (
                    f"{self.trace_prefix}/artifacts/{self.session_id}/"
                    f"{digest.removeprefix('sha256:')}/{encoded_relative}"
                )
                canonical_uri = f"s3://{self.bucket}/{canonical_key}"
                current_uri = f"s3://{self.bucket}/{self.trace_prefix}/{relative.as_posix()}"
                self.s3.upload(staged, canonical_uri)
                self.s3.copy(canonical_uri, current_uri)
                event = self._event(canonical_uri, digest, size, media_type)
                self._enqueue(event)
                cached.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, cached)
                uploaded += 1
        except Exception as error:  # errors are reported through the shared status contract
            export_error = str(error)

        pending = 0
        delivered = 0
        try:
            pending, delivered = self._deliver_pending()
        except Exception as error:
            logger_error = str(error)
            try:
                pending = len(self._pending_keys())
            except Exception:
                pending = max(pending, 1)

        previous = self._read_status()
        status: dict[str, object] = {
            "ok": export_error is None,
            "lastAttemptAt": attempted_at,
            "fileCount": scanned,
            "uploadedCount": uploaded,
            "loggerOk": logger_error is None and pending == 0,
            "loggerPending": pending,
            "loggerDeliveredCount": delivered,
        }
        if export_error is None:
            status["lastSuccessAt"] = attempted_at
        elif previous.get("lastSuccessAt"):
            status["lastSuccessAt"] = previous["lastSuccessAt"]
            status["error"] = export_error
        else:
            status["error"] = export_error
        if logger_error is None and pending == 0:
            status["loggerLastSuccessAt"] = attempted_at
        else:
            if previous.get("loggerLastSuccessAt"):
                status["loggerLastSuccessAt"] = previous["loggerLastSuccessAt"]
            if logger_error:
                status["loggerError"] = logger_error[:512]
        self._write_json_atomic(self.status_file, status)
        return status

    def _source_files(self) -> Iterable[tuple[Path, Path]]:
        if not self.source.is_dir():
            return
        for root, directories, files in os.walk(self.source, followlinks=False):
            directories[:] = sorted(name for name in directories if name != "worktrees")
            root_path = Path(root)
            for name in sorted(files):
                if name == "orchestrator-bootstrap.sh":
                    continue
                path = root_path / name
                try:
                    if not stat.S_ISREG(path.lstat().st_mode):
                        continue
                except (FileNotFoundError, PermissionError, OSError):
                    continue
                yield path, path.relative_to(self.source)

    def _event(self, uri: str, digest: str, size: int, media_type: str) -> dict[str, object]:
        identity = hashlib.sha256(f"{self.session_id}\0{uri}\0{digest}".encode()).hexdigest()
        return {
            "eventId": f"trace-export-{identity}",
            "sessionId": self.session_id,
            "eventType": "trace.artifact_exported",
            "payloadDigest": digest,
            "artifactReferences": [
                {"uri": uri, "digest": digest, "size": size, "mediaType": media_type}
            ],
        }

    def _enqueue(self, event: dict[str, object]) -> None:
        event_id = str(event["eventId"])
        local = self.queue_cache / "outbox" / f"{event_id}.json"
        self._write_json_atomic(local, event)
        self.s3.upload(local, self._outbox_uri(event_id))

    def _pending_keys(self) -> list[str]:
        outbox_root = f"{self.outbox_prefix}/{self.session_id}/"
        delivered_root = f"{self.delivered_prefix}/{self.session_id}/"
        outbox = self.s3.list_keys(self.bucket, outbox_root)
        delivered = self.s3.list_keys(self.bucket, delivered_root)
        delivered_ids = {Path(key).stem for key in delivered if key.endswith(".json")}
        return sorted(
            key for key in outbox if key.endswith(".json") and Path(key).stem not in delivered_ids
        )

    def _deliver_pending(self) -> tuple[int, int]:
        pending_keys = self._pending_keys()
        delivered = 0
        token = self.logger_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Logger token file is empty")
        for key in pending_keys:
            event_id = Path(key).stem
            local = self.queue_cache / "download" / f"{event_id}.json"
            self.s3.download(f"s3://{self.bucket}/{key}", local)
            body = local.read_bytes()
            status = self.http_post(f"{self.logger_url}/v1/events", token, body)
            if status != 204:
                raise RuntimeError(f"Logger returned HTTP {status}")
            marker = self.queue_cache / "delivered" / f"{event_id}.json"
            self._write_json_atomic(marker, {"eventId": event_id, "deliveredAt": self.clock()})
            self.s3.upload(marker, self._delivered_uri(event_id))
            delivered += 1
        return len(pending_keys) - delivered, delivered

    def _outbox_uri(self, event_id: str) -> str:
        return f"s3://{self.bucket}/{self.outbox_prefix}/{self.session_id}/{event_id}.json"

    def _delivered_uri(self, event_id: str) -> str:
        return f"s3://{self.bucket}/{self.delivered_prefix}/{self.session_id}/{event_id}.json"

    def _post(self, url: str, token: str, body: bytes) -> int:
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def _read_status(self) -> dict[str, object]:
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    @staticmethod
    def _write_json_atomic(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)


def from_environment() -> TraceExporter:
    required = [
        "TRACE_SOURCE",
        "TRACE_BUCKET",
        "TRACE_PREFIX",
        "TRACE_SESSION_ID",
        "TRACE_OUTBOX_PREFIX",
        "TRACE_DELIVERED_PREFIX",
        "LOGGER_URL",
        "LOGGER_TOKEN_FILE",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(f"missing required environment: {', '.join(missing)}")
    work = Path(os.environ.get("TRACE_EXPORT_WORK_DIR", "/tmp/trace-exporter"))
    return TraceExporter(
        source=Path(os.environ["TRACE_SOURCE"]),
        work=work,
        status_file=Path(os.environ.get("TRACE_EXPORT_STATUS_FILE", str(work / "status.json"))),
        bucket=os.environ["TRACE_BUCKET"],
        trace_prefix=os.environ["TRACE_PREFIX"],
        outbox_prefix=os.environ["TRACE_OUTBOX_PREFIX"],
        delivered_prefix=os.environ["TRACE_DELIVERED_PREFIX"],
        session_id=os.environ["TRACE_SESSION_ID"],
        logger_url=os.environ["LOGGER_URL"],
        logger_token_file=Path(os.environ["LOGGER_TOKEN_FILE"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="run one export/delivery cycle")
    args = parser.parse_args()
    exporter = from_environment()
    interval = int(os.environ.get("TRACE_EXPORT_INTERVAL_SECONDS", "60"))
    stop = threading.Event()

    def request_stop(_signal: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    while True:
        status = exporter.run_once()
        print(json.dumps({"traceExporter": status}, sort_keys=True), flush=True)
        if args.once:
            return 0
        if stop.wait(interval):
            final_status = exporter.run_once()
            print(json.dumps({"traceExporter": final_status}, sort_keys=True), flush=True)
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
