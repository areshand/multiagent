#!/usr/bin/env python3
"""Fail if the engine repo appears to contain private wiki data."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path


BLOCKED_DIR_NAMES = {
    "Notion Export",
    "Evernote Export",
    "Slack Export",
    "Raw Materials",
    "New",
}

BLOCKED_SUFFIXES = {
    ".enex",
    ".heic",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".pdf",
    ".png",
}

BLOCKED_PATTERNS = [
    re.compile(r"inbox\.jsonl$"),
    re.compile(r"archive\.jsonl$"),
    re.compile(r"feedback steward state\.md$"),
    re.compile(r"runs/feedback-steward"),
    re.compile(r"LLM Wiki/system"),
    re.compile(r"/Users/[^/]+/"),
    re.compile(r"/home/[^/]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\]+\\"),
]

CONTENT_PATTERNS = [
    re.compile(r"/Users/[A-Za-z0-9._-]+/"),
    re.compile(r"/home/[A-Za-z0-9._-]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+\\"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)?PRIVATE KEY-----"),
]

TEXT_SUFFIXES = {
    "",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".gitignore",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sh",
    ".text",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    return (
        ".git" in parts
        or ".multiagent" in parts
        or "__pycache__" in parts
        or ".pytest_cache" in parts
        or path.as_posix().endswith("docs/write-policy.paths")
    )


def scan_text(path: Path, rel: str) -> list[str]:
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        return [f"could not read text file {rel}: {exc}"]

    failures = []
    for pattern in CONTENT_PATTERNS:
        if pattern.search(text):
            failures.append(f"blocked private-data content pattern in: {rel}")
            break
    return failures


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    failures: list[str] = []

    for current_root, dirs, files in os.walk(root):
        current = Path(current_root)
        if should_skip(current):
            dirs[:] = []
            continue

        for dirname in list(dirs):
            path = current / dirname
            rel = path.relative_to(root).as_posix()
            if dirname in BLOCKED_DIR_NAMES:
                failures.append(f"blocked private source directory: {rel}")

        for filename in files:
            path = current / filename
            if should_skip(path):
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix.lower() in BLOCKED_SUFFIXES:
                failures.append(f"blocked attachment type: {rel}")
            for pattern in BLOCKED_PATTERNS:
                if pattern.search(rel):
                    failures.append(f"blocked private-data path pattern: {rel}")

            if path.stat().st_size > 1_000_000:
                failures.append(f"large file requires review: {rel}")
            failures.extend(scan_text(path, rel))

    if failures:
        print("Privacy check failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("Privacy check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
