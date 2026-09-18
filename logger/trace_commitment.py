#!/usr/bin/env python3
"""Commit an already-exported trace tree to this Logger without sending trace bodies."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def remove_prefix(value: str, prefix: str) -> str:
    return value[len(prefix) :] if value.startswith(prefix) else value


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def safe_identifier(value: str, name: str) -> str:
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


def s3_destination(value: str) -> tuple[str, str]:
    if not value.startswith("s3://") or any(character in value for character in "\r\n?#\\"):
        raise ValueError("trace destination must be a bounded s3:// URI")
    bucket, separator, raw_prefix = remove_prefix(value, "s3://").partition("/")
    parts = PurePosixPath(raw_prefix.strip("/")).parts
    if not bucket or not separator or not parts or raw_prefix != raw_prefix.strip("/"):
        raise ValueError("trace destination requires a bucket and relative prefix")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("trace destination prefix is invalid")
    return bucket, "/".join(parts)


class AwsS3:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess[str]] | None = None):
        self.runner = runner or subprocess.run

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self.runner(args, check=True, capture_output=True, text=True)

    def upload(self, source: Path, uri: str) -> None:
        self._run(["aws", "s3", "cp", str(source), uri, "--only-show-errors"])

    def download(self, uri: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._run(["aws", "s3", "cp", uri, str(destination), "--only-show-errors"])

    def list_keys(self, bucket: str, prefix: str) -> set[str]:
        result = self._run(
            ["aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--output", "json"]
        )
        return {item["Key"] for item in json.loads(result.stdout or "{}").get("Contents", [])}


class TraceCommitter:
    def __init__(
        self,
        *,
        source: Path,
        destination: str,
        session_id: str,
        work: Path,
        status_file: Path,
        logger_url: str,
        logger_token_file: Path,
        s3: AwsS3 | None = None,
        http_post: Callable[[str, str, bytes], int] | None = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.source = source
        self.bucket, self.prefix = s3_destination(destination)
        self.session_id = safe_identifier(session_id, "session ID")
        self.work = work
        self.status_file = status_file
        self.logger_url = logger_url.rstrip("/")
        self.logger_token_file = logger_token_file
        self.s3 = s3 or AwsS3()
        self.http_post = http_post or self._post
        self.clock = clock

    def commit(self) -> dict[str, object]:
        attempted_at = self.clock()
        error: str | None = None
        delivered = 0
        pending = 0
        try:
            event = self._publish_manifest_and_event()
            pending, delivered = self._deliver_pending()
            event_id = event["eventId"]
        except Exception as caught:  # Logger path must not alter trace-export success
            error = str(caught)
            event_id = None
            try:
                pending = len(self._pending_keys())
            except Exception:
                pending = max(pending, 1)

        status = self._read_status()
        status.update(
            {
                "loggerOk": error is None and pending == 0,
                "loggerPending": pending,
                "loggerDeliveredCount": delivered,
                "loggerLastAttemptAt": attempted_at,
            }
        )
        if event_id:
            status["loggerEventId"] = event_id
        if error is None and pending == 0:
            status["loggerLastSuccessAt"] = attempted_at
            status.pop("loggerError", None)
        else:
            status["loggerError"] = (error or "Logger backlog remains pending")[:512]
        self._write_json_atomic(self.status_file, status)
        return status

    def _publish_manifest_and_event(self) -> dict[str, object]:
        artifacts = []
        for path, relative in self._source_files():
            digest, size = sha256_file(path)
            artifacts.append(
                {
                    "digest": digest,
                    "mediaType": mimetypes.guess_type(relative.as_posix())[0] or "application/octet-stream",
                    "size": size,
                    "storageReference": f"s3://{self.bucket}/{self.prefix}/{relative.as_posix()}",
                }
            )
        manifest = {
            "apiVersion": "trace.multiagent.dev/v1",
            "kind": "TraceExportCommitment",
            "sessionId": self.session_id,
            "artifacts": artifacts,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        digest = sha256_bytes(encoded)
        digest_hex = remove_prefix(digest, "sha256:")
        manifest_uri = f"s3://{self.bucket}/{self.prefix}/commitments/{digest_hex}.json"
        manifest_file = self.work / "manifests" / f"{digest_hex}.json"
        self._write_bytes_atomic(manifest_file, encoded)
        self.s3.upload(manifest_file, manifest_uri)

        identity = hashlib.sha256(f"{self.session_id}\0{manifest_uri}\0{digest}".encode()).hexdigest()
        event = {
            "eventId": f"trace-export-{identity}",
            "sessionId": self.session_id,
            "eventType": "trace.artifact_exported",
            "payloadDigest": digest,
            "artifactReferences": [
                {
                    "uri": manifest_uri,
                    "digest": digest,
                    "size": len(encoded),
                    "mediaType": "application/vnd.multiagent.trace-commitment+json",
                }
            ],
        }
        event_file = self.work / "outbox" / f"{event['eventId']}.json"
        self._write_json_atomic(event_file, event)
        self.s3.upload(event_file, self._outbox_uri(str(event["eventId"])))
        return event

    def _source_files(self) -> Iterable[tuple[Path, Path]]:
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

    def _pending_keys(self) -> list[str]:
        outbox_root = f"{self.prefix}/logger-outbox/"
        delivered_root = f"{self.prefix}/logger-delivered/"
        outbox = self.s3.list_keys(self.bucket, outbox_root)
        delivered = self.s3.list_keys(self.bucket, delivered_root)
        delivered_ids = {Path(key).stem for key in delivered if key.endswith(".json")}
        return sorted(key for key in outbox if key.endswith(".json") and Path(key).stem not in delivered_ids)

    def _deliver_pending(self) -> tuple[int, int]:
        pending_keys = self._pending_keys()
        token = self.logger_token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("Logger token file is empty")
        delivered = 0
        for key in pending_keys:
            event_id = Path(key).stem
            local = self.work / "download" / f"{event_id}.json"
            self.s3.download(f"s3://{self.bucket}/{key}", local)
            status = self.http_post(f"{self.logger_url}/v1/events", token, local.read_bytes())
            if status != 204:
                raise RuntimeError(f"Logger returned HTTP {status}")
            marker = self.work / "delivered" / f"{event_id}.json"
            self._write_json_atomic(marker, {"eventId": event_id, "deliveredAt": self.clock()})
            self.s3.upload(marker, self._delivered_uri(event_id))
            delivered += 1
        return len(pending_keys) - delivered, delivered

    def _outbox_uri(self, event_id: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}/logger-outbox/{event_id}.json"

    def _delivered_uri(self, event_id: str) -> str:
        return f"s3://{self.bucket}/{self.prefix}/logger-delivered/{event_id}.json"

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
    def _write_bytes_atomic(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, delete=False) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)

    @classmethod
    def _write_json_atomic(cls, path: Path, value: object) -> None:
        cls._write_bytes_atomic(path, (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())


def from_environment() -> TraceCommitter:
    required = [
        "TRACE_COMMITMENT_SOURCE",
        "TRACE_EXPORT_DESTINATION",
        "TRACE_SESSION_ID",
        "LOGGER_URL",
        "LOGGER_TOKEN_FILE",
    ]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise ValueError(f"missing required environment: {', '.join(missing)}")
    work = Path(os.environ.get("TRACE_COMMITMENT_WORK_DIR", "/tmp/trace-commitment"))
    return TraceCommitter(
        source=Path(os.environ["TRACE_COMMITMENT_SOURCE"]),
        destination=os.environ["TRACE_EXPORT_DESTINATION"],
        session_id=os.environ["TRACE_SESSION_ID"],
        work=work,
        status_file=Path(os.environ.get("TRACE_EXPORT_STATUS_FILE", "/tmp/trace-exporter/status.json")),
        logger_url=os.environ["LOGGER_URL"],
        logger_token_file=Path(os.environ["LOGGER_TOKEN_FILE"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    status = from_environment().commit()
    print(json.dumps({"traceCommitment": status}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
