"""Small CLI bridge for shell-owned framework components."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .snapshot import RepositorySnapshot
from .verification import behavior_verification_has_evidence, build_verification_has_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--root", type=Path, required=True)
    snapshot_parser.add_argument("--base", default="HEAD")
    snapshot_parser.add_argument("--format", choices=("json", "shell"), default="json")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--kind", choices=("build", "behavior"), required=True)
    verify_parser.add_argument("--diff-file", type=Path, required=True)
    verify_parser.add_argument("--evidence-file", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "snapshot":
        snapshot = RepositorySnapshot.capture(args.root, args.base)
        if args.format == "shell":
            print(f"{snapshot.sha256} {snapshot.changed_file_count}")
            return 0
        print(
            json.dumps(
                {
                    "final_diff_sha256": snapshot.sha256,
                    "changed_files": snapshot.changed_file_count,
                    "changed_paths": list(snapshot.changed_paths),
                    "changed_code_paths": list(snapshot.changed_code_paths),
                },
                sort_keys=True,
            )
        )
        return 0

    diff = args.diff_file.read_text(encoding="utf-8", errors="replace")
    evidence = args.evidence_file.read_text(encoding="utf-8", errors="replace")
    accepted = (
        build_verification_has_evidence(evidence, diff)
        if args.kind == "build"
        else behavior_verification_has_evidence(evidence, diff)
    )
    print(json.dumps({"accepted": accepted, "kind": args.kind}, sort_keys=True))
    return 0 if accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
