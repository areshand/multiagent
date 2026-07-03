#!/usr/bin/env python3
"""Inspect or prune the local SWE Bench Pro Docker image cache.

The official EvalScope run processes samples in dataset JSONL order. If disk is
limited, keeping images outside the next dataset-order prefix is less useful
than freeing space for the on-demand loader.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

from evaluation.swe_bench_pro_image_preload import DEFAULT_PREFLIGHT, docker_image_present


DEFAULT_OUTPUT = Path("evaluation/reports/swe-bench-pro-image-cache.json")


def load_preflight(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_order_images(preflight: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    images: list[str] = []
    for item in preflight.get("instances", []):
        image = str(item.get("image") or "")
        if image and image not in seen:
            seen.add(image)
            images.append(image)
    return images


def local_expected_images(images: list[str]) -> list[str]:
    present: list[str] = []
    for image in images:
        ok, _ = docker_image_present(image)
        if ok:
            present.append(image)
    return present


def docker_image_rm(image: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "image", "rm", image],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    return {
        "image": image,
        "returncode": result.returncode,
        "status": "pruned" if result.returncode == 0 else "prune_failed",
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preflight", type=Path, default=DEFAULT_PREFLIGHT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--keep-prefix", type=int, default=0, help="keep first N dataset-order images")
    parser.add_argument("--execute", action="store_true", help="actually remove images outside the keep set")
    args = parser.parse_args()

    preflight = load_preflight(args.preflight)
    images = dataset_order_images(preflight)
    keep = set(images[: args.keep_prefix])
    present = local_expected_images(images)
    prune_candidates = [image for image in present if image not in keep]
    records = [docker_image_rm(image) for image in prune_candidates] if args.execute else []
    pruned = sum(1 for item in records if item.get("status") == "pruned")
    failed = sum(1 for item in records if item.get("status") == "prune_failed")
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "preflight": str(args.preflight),
        "execute": args.execute,
        "dataset_order_image_count": len(images),
        "keep_prefix": args.keep_prefix,
        "keep_count": len(keep),
        "local_expected_count": len(present),
        "prune_candidate_count": len(prune_candidates),
        "pruned_count": pruned,
        "prune_failed_count": failed,
        "keep_images": images[: args.keep_prefix],
        "prune_candidates": prune_candidates,
        "records": records,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.output}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
