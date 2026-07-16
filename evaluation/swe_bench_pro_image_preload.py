#!/usr/bin/env python3
"""Preload SWE Bench Pro Docker images from the registry manifest.

The SWE Bench Pro official scaffold needs one per-instance
``jefzda/sweap-images`` image per task. Docker CLI pulls have been unreliable in
this environment, so this wrapper uses ``evaluation.docker_registry_preload`` to
assemble a docker-loadable archive through registry HTTP blob downloads, then
loads the archive into the local Docker daemon.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


DEFAULT_PREFLIGHT = Path("evaluation/reports/swe-bench-pro-official-preflight.json")
DEFAULT_OUTPUT = Path("evaluation/reports/swe-bench-pro-image-preload-status.json")
DEFAULT_ARCHIVE_DIR = Path("/private/tmp/swe-bench-pro-image-preload")
HTTP_429_PATTERN = re.compile(r"HTTP Error 429|Too Many Requests", re.IGNORECASE)
TRANSIENT_PRELOAD_STATUSES = {"build_failed", "build_timed_out", "load_failed"}


def load_preflight(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"preflight report does not exist: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def unique_images(preflight: dict[str, Any]) -> list[str]:
    images = sorted({str(item["image"]) for item in preflight.get("instances", []) if item.get("image")})
    if not images:
        raise ValueError("preflight report does not contain any instance images")
    return images


def dataset_order_images(preflight: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    images: list[str] = []
    for item in preflight.get("instances", []):
        image = str(item.get("image") or "")
        if image and image not in seen:
            seen.add(image)
            images.append(image)
    if not images:
        raise ValueError("preflight report does not contain any instance images")
    return images


def image_slug(image: str) -> str:
    digest = hashlib.sha256(image.encode("utf-8")).hexdigest()[:16]
    safe = "".join(ch if ch.isalnum() else "-" for ch in image.lower()).strip("-")
    safe = "-".join(part for part in safe.split("-") if part)
    return f"{safe[:96]}-{digest}"


def docker_image_present(image: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker command not found"
    except subprocess.TimeoutExpired:
        return False, "docker image inspect timed out"
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or "").strip().splitlines()[-1] if result.stderr else "docker image inspect failed"


def run_command(argv: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)


def free_disk_gib(path: Path) -> float:
    probe = path if path.exists() else path.parent
    return shutil.disk_usage(probe).free / (1024**3)


def registry_rate_limited(record: dict[str, Any]) -> bool:
    text = "\n".join(
        str(record.get(key, ""))
        for key in ("build_stdout_tail", "build_stderr_tail", "load_stdout_tail", "load_stderr_tail")
    )
    return bool(HTTP_429_PATTERN.search(text))


def should_retry_preload(record: dict[str, Any]) -> bool:
    if record.get("status") == "loaded":
        return False
    if registry_rate_limited(record):
        return True
    return str(record.get("status") or "") in TRANSIENT_PRELOAD_STATUSES


def preload_image(
    image: str,
    platform: str,
    archive_dir: Path,
    *,
    keep_archive: bool,
    image_timeout: int | None,
) -> dict[str, Any]:
    slug = image_slug(image)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{slug}.tar"
    metadata = archive_dir / f"{slug}.json"
    started = time.time()
    try:
        build = run_command(
            [
                sys.executable,
                "-m",
                "evaluation.docker_registry_preload",
                image,
                "--platform",
                platform,
                "--archive",
                str(archive),
                "--metadata",
                str(metadata),
            ],
            timeout=image_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "image": image,
            "archive": str(archive),
            "metadata": str(metadata),
            "status": "build_timed_out",
            "timeout_s": image_timeout,
            "build_stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "build_stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
            "duration_s": round(time.time() - started, 3),
        }
    record: dict[str, Any] = {
        "image": image,
        "archive": str(archive),
        "metadata": str(metadata),
        "build_returncode": build.returncode,
        "build_stdout_tail": build.stdout[-4000:],
        "build_stderr_tail": build.stderr[-4000:],
        "duration_s": round(time.time() - started, 3),
    }
    if build.returncode != 0:
        record["status"] = "build_failed"
        return record

    load = run_command(["docker", "load", "-i", str(archive)], timeout=None)
    record.update(
        {
            "load_returncode": load.returncode,
            "load_stdout_tail": load.stdout[-4000:],
            "load_stderr_tail": load.stderr[-4000:],
            "duration_s": round(time.time() - started, 3),
        }
    )
    if load.returncode != 0:
        record["status"] = "load_failed"
        return record

    present, inspect_error = docker_image_present(image)
    if not present and metadata.exists():
        meta = json.loads(metadata.read_text(encoding="utf-8"))
        image_id = str(meta.get("manifest_digest") or "")
        if image_id:
            tag = run_command(["docker", "tag", image_id, image], timeout=60)
            record["retag_returncode"] = tag.returncode
            record["retag_stdout_tail"] = tag.stdout[-4000:]
            record["retag_stderr_tail"] = tag.stderr[-4000:]
            present, inspect_error = docker_image_present(image)
    record["present_after_load"] = present
    if inspect_error:
        record["inspect_error"] = inspect_error
    record["status"] = "loaded" if present else "loaded_but_not_inspectable"
    if present and not keep_archive:
        archive.unlink(missing_ok=True)
    return record


def preload_image_with_retries(
    image: str,
    platform: str,
    archive_dir: Path,
    *,
    keep_archive: bool,
    image_timeout: int | None,
    retries: int,
    backoff_s: int,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    for attempt in range(retries + 1):
        record = preload_image(
            image,
            platform,
            archive_dir,
            keep_archive=keep_archive,
            image_timeout=image_timeout,
        )
        record["attempt"] = attempt + 1
        attempts.append(dict(record))
        if record.get("status") == "loaded" or not should_retry_preload(record) or attempt >= retries:
            if len(attempts) > 1:
                record["attempts"] = [dict(item) for item in attempts]
            return record
        sleep_s = backoff_s * (2**attempt)
        time.sleep(sleep_s)
    return attempts[-1]


def build_payload(
    *,
    args: argparse.Namespace,
    preflight: dict[str, Any],
    counts: dict[str, int],
    records: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": status,
        "preflight": str(args.preflight),
        "platform": args.platform,
        "archive_dir": str(args.archive_dir),
        "manifest_image_count": len(unique_images(preflight)),
        "counts": counts,
        "records": records,
    }


def write_payload(
    *,
    args: argparse.Namespace,
    preflight: dict[str, Any],
    counts: dict[str, int],
    records: list[dict[str, Any]],
    status: str,
) -> None:
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            build_payload(args=args, preflight=preflight, counts=counts, records=records, status=status),
            indent=2,
        ),
        encoding="utf-8",
    )


def selected_images(images: list[str], args: argparse.Namespace) -> list[str]:
    if args.image:
        requested = set(args.image)
        missing = sorted(requested - set(images))
        if missing:
            raise ValueError(f"requested image(s) not in preflight manifest: {', '.join(missing)}")
        images = [image for image in images if image in requested]
    if args.start_after:
        if args.start_after not in images:
            raise ValueError(f"--start-after image is not in manifest: {args.start_after}")
        images = images[images.index(args.start_after) + 1 :]
    if args.limit is not None:
        images = images[: args.limit]
    return images


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--order",
        choices=["sorted", "dataset"],
        default="sorted",
        help="image selection order; dataset follows official JSONL order",
    )
    parser.add_argument("--image", action="append", help="specific image to preload; may be repeated")
    parser.add_argument("--start-after", help="resume after this image in sorted manifest order")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-archive", action="store_true")
    parser.add_argument("--image-timeout", type=int, default=900, help="seconds before one image build is failed")
    parser.add_argument("--retry-rate-limit", type=int, default=3, help="retries for Docker registry HTTP 429")
    parser.add_argument("--retry-backoff", type=int, default=60, help="initial seconds to wait before retrying HTTP 429")
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=0.0,
        help="stop cleanly before preloading an image if archive-dir has less free space than this",
    )
    args = parser.parse_args()

    preflight = load_preflight(args.preflight)
    manifest_images = dataset_order_images(preflight) if args.order == "dataset" else unique_images(preflight)
    images = selected_images(manifest_images, args)
    records: list[dict[str, Any]] = []
    counts = {
        "selected": len(images),
        "skipped_present": 0,
        "loaded": 0,
        "failed": 0,
        "would_preload": 0,
    }
    stopped_low_disk = False

    for image in images:
        present, inspect_error = docker_image_present(image)
        if present:
            counts["skipped_present"] += 1
            records.append({"image": image, "status": "skipped_present"})
            write_payload(args=args, preflight=preflight, counts=counts, records=records, status="running")
            continue
        if args.dry_run:
            counts["would_preload"] += 1
            records.append({"image": image, "status": "would_preload", "inspect_error": inspect_error})
            write_payload(args=args, preflight=preflight, counts=counts, records=records, status="running")
            continue

        if args.min_free_gb > 0:
            free_gib = free_disk_gib(args.archive_dir)
            if free_gib < args.min_free_gb:
                records.append(
                    {
                        "image": image,
                        "status": "stopped_low_disk",
                        "free_gib": round(free_gib, 3),
                        "min_free_gb": args.min_free_gb,
                        "inspect_error": inspect_error,
                    }
                )
                stopped_low_disk = True
                write_payload(args=args, preflight=preflight, counts=counts, records=records, status="stopped_low_disk")
                break

        record = preload_image_with_retries(
            image,
            args.platform,
            args.archive_dir,
            keep_archive=args.keep_archive,
            image_timeout=args.image_timeout,
            retries=args.retry_rate_limit,
            backoff_s=args.retry_backoff,
        )
        records.append(record)
        if record["status"] == "loaded":
            counts["loaded"] += 1
        else:
            counts["failed"] += 1
            write_payload(args=args, preflight=preflight, counts=counts, records=records, status="failed")
            break
        write_payload(args=args, preflight=preflight, counts=counts, records=records, status="running")

    final_status = "stopped_low_disk" if stopped_low_disk else "failed" if counts["failed"] else "completed"
    write_payload(args=args, preflight=preflight, counts=counts, records=records, status=final_status)
    print(f"wrote {args.output}")
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
