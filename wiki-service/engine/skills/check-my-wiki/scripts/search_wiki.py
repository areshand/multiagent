#!/usr/bin/env python3
"""Locate and search a local Obsidian/iCloud Markdown wiki."""

from __future__ import annotations

import argparse
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CANDIDATES = [
    "~/Documents/obsidian/LLM Wiki",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Documents/Obsidian",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Documents",
]

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
}


@dataclass
class NoteHit:
    score: float
    path: Path
    title: str
    snippets: list[str]


def expand(path: str) -> Path:
    return Path(path).expanduser().resolve()


def markdown_files(root: Path) -> list[Path]:
    if root.is_file() and root.suffix.lower() == ".md":
        return [root]
    if not root.is_dir():
        return []
    ignored = {".git", ".obsidian", ".trash", "node_modules"}
    files: list[Path] = []
    for path in root.rglob("*.md"):
        if any(part.lower() in ignored for part in path.parts):
            continue
        files.append(path)
    return files


def candidate_roots(extra_roots: list[str]) -> list[Path]:
    raw = []
    env_path = os.environ.get("CHECK_MY_WIKI_PATH")
    if env_path:
        raw.append(env_path)
    raw.extend(extra_roots)
    raw.extend(DEFAULT_CANDIDATES)

    roots: list[Path] = []
    seen: set[Path] = set()
    for item in raw:
        root = expand(item)
        if root in seen or not root.exists():
            continue
        seen.add(root)
        roots.append(root)
    return roots


def score_root(root: Path) -> tuple[int, int, str]:
    files = markdown_files(root)
    name = str(root).lower()
    name_bonus = 0
    if root.name.lower() == "llm wiki":
        name_bonus += 10000
    for marker in ("llm", "wiki", "obsidian"):
        if marker in name:
            name_bonus += 100
    return (name_bonus + min(len(files), 1000), len(files), str(root))


def locate(extra_roots: list[str]) -> list[tuple[Path, int]]:
    roots = candidate_roots(extra_roots)
    ranked = sorted(roots, key=score_root, reverse=True)
    return [(root, len(markdown_files(root))) for root in ranked]


def tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}", text.lower())
        if token not in STOPWORDS
    ]


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def title_for(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip() or path.stem
    return path.stem


def snippets_for(text: str, query_tokens: set[str], limit: int = 3) -> list[str]:
    snippets: list[str] = []
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        lowered = clean.lower()
        if any(token in lowered for token in query_tokens):
            snippets.append(clean[:240])
            if len(snippets) >= limit:
                break
    return snippets


def score_note(path: Path, text: str, query: str, query_tokens: list[str]) -> float:
    lowered_path = str(path).lower()
    lowered_text = text.lower()
    title = path.stem.lower().replace("-", " ").replace("_", " ")
    counts = Counter(tokens(lowered_text))
    score = 0.0

    phrase = query.strip().lower()
    if phrase and phrase in lowered_text:
        score += 25
    if phrase and phrase in lowered_path:
        score += 40

    for token in query_tokens:
        score += min(counts[token], 8)
        if token in title:
            score += 12
        if token in lowered_path:
            score += 8
        if re.search(rf"^#+\s+.*\b{re.escape(token)}\b", lowered_text, re.MULTILINE):
            score += 5

    return score


def search(root: Path, query: str, limit: int) -> list[NoteHit]:
    query_tokens = tokens(query)
    if not query_tokens:
        raise SystemExit("Question does not contain searchable terms.")

    hits: list[NoteHit] = []
    for path in markdown_files(root):
        text = read_text(path)
        score = score_note(path, text, query, query_tokens)
        if score <= 0:
            continue
        hits.append(
            NoteHit(
                score=score,
                path=path,
                title=title_for(path, text),
                snippets=snippets_for(text, set(query_tokens)),
            )
        )
    return sorted(hits, key=lambda hit: (hit.score, str(hit.path)), reverse=True)[:limit]


def choose_root(explicit_root: str | None, extra_roots: list[str]) -> Path:
    if explicit_root:
        root = expand(explicit_root)
        if not root.exists():
            raise SystemExit(f"Root does not exist: {root}")
        return root

    located = locate(extra_roots)
    with_notes = [(root, count) for root, count in located if count > 0]
    if not with_notes:
        raise SystemExit("No Markdown notes found in known wiki locations. Set CHECK_MY_WIKI_PATH or pass --root.")
    return with_notes[0][0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Locate and search a local Obsidian/iCloud Markdown wiki.")
    parser.add_argument("--question", "-q", help="Question or search query.")
    parser.add_argument("--root", help="Wiki root directory or Markdown file.")
    parser.add_argument("--candidate", action="append", default=[], help="Additional candidate root to consider.")
    parser.add_argument("--limit", type=int, default=8, help="Maximum notes to print.")
    parser.add_argument("--locate", action="store_true", help="Print candidate wiki roots and Markdown counts.")
    args = parser.parse_args()

    if args.locate:
        located = locate(args.candidate + ([args.root] if args.root else []))
        if not located:
            print("No candidate roots found.")
            return
        for root, count in located:
            print(f"{count:5d} markdown files  {root}")
        return

    if not args.question:
        raise SystemExit("Pass --question or --locate.")

    root = choose_root(args.root, args.candidate)
    print(f"Root: {root}")
    hits = search(root, args.question, args.limit)
    if not hits:
        print("No matching Markdown notes found.")
        return

    for index, hit in enumerate(hits, 1):
        print(f"\n{index}. score={hit.score:.1f}  {hit.title}")
        print(f"   path: {hit.path}")
        for snippet in hit.snippets:
            print(f"   - {snippet}")


if __name__ == "__main__":
    main()
