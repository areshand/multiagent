#!/usr/bin/env python3
"""Small stdlib CLI for installing and maintaining a private LLM Wiki."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

VAULT_ENVIRONMENT_VARIABLES = ("LLM_WIKI_VAULT_ROOT", "CHECK_MY_WIKI_PATH")
DEFAULT_VAULT_CANDIDATES = (
    "~/Documents/obsidian",
    "~/Library/Mobile Documents/iCloud~md~obsidian/Documents",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Documents/Obsidian",
    "~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian",
)

REQUIRED_FRONTMATTER = {
    "id",
    "title",
    "type",
    "status",
    "confidence",
    "created",
    "updated",
    "owner",
    "source_count",
    "source_ids",
    "source_paths",
    "tags",
}

VALID_FEEDBACK_TYPES = {
    "missing_context",
    "wrong_fact",
    "stale_fact",
    "bad_link",
    "duplicate_note",
    "weak_synthesis",
    "retrieval_failure",
    "metadata_gap",
    "privacy_boundary",
    "other",
}

VALID_STATUSES = {
    "inbox",
    "triaged",
    "patch_proposed",
    "eval_created",
    "merged",
    "duplicate",
    "needs_human_review",
    "rejected",
}

TEMPLATE_MAP = {
    "templates/wiki/index.md": "LLM Wiki/index.md",
    "templates/wiki/schema.md": "LLM Wiki/schema.md",
    "templates/wiki/log.md": "LLM Wiki/log.md",
    "templates/wiki/workflows/ingest workflow.md": "LLM Wiki/workflows/ingest workflow.md",
    "templates/wiki/workflows/query workflow.md": "LLM Wiki/workflows/query workflow.md",
    "templates/wiki/workflows/lint workflow.md": "LLM Wiki/workflows/lint workflow.md",
    "templates/wiki/workflows/feedback loop workflow.md": "LLM Wiki/workflows/feedback loop workflow.md",
}

RUNTIME_DIRS = [
    "LLM Wiki/concepts",
    "LLM Wiki/entities",
    "LLM Wiki/projects",
    "LLM Wiki/sources",
    "LLM Wiki/syntheses",
    "LLM Wiki/workflows",
    "LLM Wiki/templates",
    "LLM Wiki/tools",
    "LLM Wiki/system/feedback",
    "LLM Wiki/system/state",
    "LLM Wiki/system/patches/pending",
    "LLM Wiki/system/patches/accepted",
    "LLM Wiki/system/patches/rejected",
    "LLM Wiki/system/evals/retrieval",
    "LLM Wiki/system/evals/answer-quality",
    "LLM Wiki/system/runs/feedback-steward",
]


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def now_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def render_template(text: str) -> str:
    return (
        text.replace("{{DATE}}", today())
        .replace("{{CREATED_AT}}", now_timestamp())
        .replace("{{ENGINE_REPO}}", "multiagent/wiki-service")
    )


def parse_simple_config(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip().strip("'\"")
        if value and not value.startswith("[") and not value.startswith("-"):
            data[key.strip()] = value
    return data


def normalize_vault_root(path: Path) -> Path:
    """Return the vault root whether given the vault or its `LLM Wiki` child."""
    resolved = path.expanduser().resolve()
    if resolved.name == "LLM Wiki":
        return resolved.parent
    return resolved


def discover_vault() -> Path | None:
    for name in VAULT_ENVIRONMENT_VARIABLES:
        raw = os.environ.get(name)
        if raw:
            candidate = normalize_vault_root(Path(raw))
            if (candidate / "LLM Wiki/index.md").is_file():
                return candidate
    for raw in DEFAULT_VAULT_CANDIDATES:
        candidate = normalize_vault_root(Path(raw))
        if (candidate / "LLM Wiki/index.md").is_file():
            return candidate
        if candidate.is_dir():
            matches = sorted(candidate.glob("*/LLM Wiki/index.md"))
            if matches:
                return matches[0].parents[1]
    return None


def resolve_vault(args: argparse.Namespace) -> Path:
    if getattr(args, "vault", None):
        return normalize_vault_root(Path(args.vault))
    config_path = Path(getattr(args, "config", "") or "config.yml")
    config = parse_simple_config(config_path)
    if "vault_root" in config:
        return normalize_vault_root(Path(config["vault_root"]))
    discovered = discover_vault()
    if discovered:
        return discovered
    raise ValueError(
        "missing target vault; pass --vault PATH, set LLM_WIKI_VAULT_ROOT or "
        "CHECK_MY_WIKI_PATH, or provide config.yml with vault_root"
    )


def write_if_missing(path: Path, text: str) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def command_init_vault(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    vault.mkdir(parents=True, exist_ok=True)
    for rel_dir in RUNTIME_DIRS:
        (vault / rel_dir).mkdir(parents=True, exist_ok=True)

    created = []
    for template_rel, target_rel in TEMPLATE_MAP.items():
        template = REPO_ROOT / template_rel
        target = vault / target_rel
        if write_if_missing(target, render_template(template.read_text(encoding="utf-8"))):
            created.append(target_rel)

    feedback_log = vault / "LLM Wiki/system/feedback/inbox.jsonl"
    if write_if_missing(feedback_log, ""):
        created.append("LLM Wiki/system/feedback/inbox.jsonl")

    print(f"initialized vault: {vault}")
    print(f"created files: {len(created)}")
    for rel in created:
        print(f"- {rel}")
    return 0


def feedback_record(args: argparse.Namespace) -> dict[str, object]:
    feedback_type = args.feedback_type
    if feedback_type not in VALID_FEEDBACK_TYPES:
        raise ValueError(f"invalid feedback type: {feedback_type}")
    return {
        "id": f"fwf_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}",
        "created_at": now_timestamp(),
        "source": args.source,
        "status": "inbox",
        "feedback_type": feedback_type,
        "severity": args.severity,
        "raw_feedback": args.raw_feedback,
        "expected_behavior": args.expected_behavior,
        "user_query": args.user_query or "",
        "suggested_target_notes": args.suggested_target_notes or [],
        "tags": args.tags or [],
        "privacy": args.privacy,
    }


def validate_feedback_item(item: object, line_number: int = 0) -> list[str]:
    prefix = f"line {line_number}: " if line_number else ""
    failures: list[str] = []
    if not isinstance(item, dict):
        return [f"{prefix}feedback item is not an object"]
    for field in ["id", "created_at", "source", "status", "feedback_type", "raw_feedback", "expected_behavior"]:
        if not item.get(field):
            failures.append(f"{prefix}missing required field: {field}")
    if item.get("status") not in VALID_STATUSES:
        failures.append(f"{prefix}invalid status: {item.get('status')}")
    if item.get("feedback_type") not in VALID_FEEDBACK_TYPES:
        failures.append(f"{prefix}invalid feedback_type: {item.get('feedback_type')}")
    return failures


def command_submit_feedback(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    log_path = vault / "LLM Wiki/system/feedback/inbox.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = feedback_record(args)
    failures = validate_feedback_item(record)
    if failures:
        for failure in failures:
            print(failure)
        return 1
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    print(f"appended feedback: {record['id']}")
    print(f"path: {log_path}")
    return 0


def command_validate_feedback(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    log_path = vault / "LLM Wiki/system/feedback/inbox.jsonl"
    if not log_path.exists():
        print(f"feedback log not found: {log_path}")
        return 1
    failures: list[str] = []
    count = 0
    for index, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        count += 1
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            failures.append(f"line {index}: invalid JSON: {exc}")
            continue
        failures.extend(validate_feedback_item(item, index))
    if failures:
        print("feedback validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"feedback validation passed: {count} item(s)")
    return 0


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return slug[:80] or "feedback"


def write_steward_artifacts(vault: Path, item: dict[str, object], run_id: str) -> tuple[str, list[str]]:
    feedback_id = str(item["id"])
    short_slug = slugify(str(item.get("feedback_type", "feedback")))
    artifacts: list[str] = []
    review_required = (
        item.get("feedback_type") == "privacy_boundary"
        or item.get("privacy") not in {"normal", "", None}
        or item.get("severity") == "high"
    )

    patch_path = vault / "LLM Wiki/system/patches/pending" / f"{feedback_id}-{short_slug}.md"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    target_notes = item.get("suggested_target_notes") or []
    if not isinstance(target_notes, list):
        target_notes = []
    patch_text = "\n".join(
        [
            "# Patch Proposal",
            "",
            f"Feedback id: {feedback_id}",
            f"Target notes: {', '.join(str(note) for note in target_notes) or 'to be determined'}",
            f"Risk level: {'high' if review_required else 'medium'}",
            f"Human review required: {'yes' if review_required else 'no'}",
            "",
            "## Feedback",
            "",
            str(item.get("raw_feedback", "")),
            "",
            "## Expected Behavior",
            "",
            str(item.get("expected_behavior", "")),
            "",
            "## Proposed Change",
            "",
            "Identify the smallest existing wiki page update that would make the expected behavior true. Do not apply this patch without review.",
            "",
        ]
    )
    patch_path.write_text(patch_text, encoding="utf-8")
    artifacts.append(patch_path.relative_to(vault).as_posix())

    eval_kind = "retrieval" if item.get("feedback_type") in {"missing_context", "retrieval_failure", "weak_synthesis"} else "answer-quality"
    eval_path = vault / "LLM Wiki/system/evals" / eval_kind / f"{feedback_id}-{short_slug}.md"
    eval_path.parent.mkdir(parents=True, exist_ok=True)
    eval_text = "\n".join(
        [
            f"# {'Retrieval' if eval_kind == 'retrieval' else 'Answer Quality'} Eval",
            "",
            f"Feedback id: {feedback_id}",
            f"Test query: {item.get('user_query') or item.get('raw_feedback')}",
            f"Expected behavior: {item.get('expected_behavior')}",
            f"Failure mode: {item.get('feedback_type')}",
            "",
            "## Pass Criteria",
            "",
            "- The answer uses the relevant wiki context.",
            "- The answer avoids inventing unsourced private facts.",
            "- The answer cites local wiki or source paths when making factual claims.",
            "",
        ]
    )
    eval_path.write_text(eval_text, encoding="utf-8")
    artifacts.append(eval_path.relative_to(vault).as_posix())

    return ("needs_human_review" if review_required else "patch_proposed"), artifacts


def command_run_steward(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    log_path = vault / "LLM Wiki/system/feedback/inbox.jsonl"
    if not log_path.exists():
        print(f"feedback log not found: {log_path}")
        return 1

    items: list[dict[str, object]] = []
    failures: list[str] = []
    for index, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            failures.append(f"line {index}: invalid JSON: {exc}")
            continue
        item_failures = validate_feedback_item(item, index)
        if item_failures:
            failures.extend(item_failures)
        elif isinstance(item, dict):
            items.append(item)
    if failures:
        print("steward input validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    processed: list[str] = []
    all_artifacts: list[str] = []
    for item in items:
        if item.get("status") != "inbox":
            continue
        new_status, artifacts = write_steward_artifacts(vault, item, run_id)
        item["status"] = new_status
        item["steward_run_id"] = run_id
        item["steward_decision"] = (
            "human review required before applying sensitive change"
            if new_status == "needs_human_review"
            else "patch proposal and eval created"
        )
        processed.append(str(item["id"]))
        all_artifacts.extend(artifacts)

    with log_path.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=True, sort_keys=True) + "\n")

    state_path = vault / "LLM Wiki/system/state/feedback steward state.md"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        "\n".join(
            [
                "# Feedback Steward State",
                "",
                f"last_run_id: {run_id}",
                "processed_feedback_ids:",
                *(f"- {item_id}" for item_id in processed),
                "unresolved_blockers: []",
                "next_run_scope: process remaining inbox feedback",
                "",
            ]
        ),
        encoding="utf-8",
    )
    all_artifacts.append(state_path.relative_to(vault).as_posix())

    run_log = vault / "LLM Wiki/system/runs/feedback-steward" / f"{run_id}.md"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    run_log.write_text(
        "\n".join(
            [
                "# Feedback Steward Run Log",
                "",
                f"Run id: {run_id}",
                f"Created at: {now_timestamp()}",
                "",
                "## Inputs",
                "",
                f"- Feedback log: {log_path.relative_to(vault).as_posix()}",
                "",
                "## Processed Feedback",
                "",
                *(f"- {item_id}" for item_id in processed),
                "",
                "## Artifacts Written",
                "",
                *(f"- {artifact}" for artifact in all_artifacts),
                "",
            ]
        ),
        encoding="utf-8",
    )
    all_artifacts.append(run_log.relative_to(vault).as_posix())

    print(f"steward run: {run_id}")
    print(f"processed feedback: {len(processed)}")
    for artifact in all_artifacts:
        print(f"- {artifact}")
    return 0


def parse_frontmatter(path: Path) -> dict[str, str] | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end == -1:
        return None
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
    return fields


def wiki_pages(wiki_root: Path) -> set[str]:
    pages = set()
    for path in wiki_root.rglob("*.md"):
        rel = path.relative_to(wiki_root).with_suffix("").as_posix()
        pages.add(rel)
        pages.add(Path(rel).name)
    return pages


def command_lint_wiki(args: argparse.Namespace) -> int:
    vault = resolve_vault(args)
    wiki_root = vault / "LLM Wiki"
    failures: list[str] = []
    for rel in ["index.md", "schema.md", "log.md"]:
        if not (wiki_root / rel).exists():
            failures.append(f"missing required wiki file: LLM Wiki/{rel}")

    pages = wiki_pages(wiki_root) if wiki_root.exists() else set()
    link_pattern = re.compile(r"\[\[([^\]|#]+)")
    for path in sorted(wiki_root.rglob("*.md")) if wiki_root.exists() else []:
        if "/system/" in path.as_posix():
            continue
        rel = path.relative_to(vault).as_posix()
        frontmatter = parse_frontmatter(path)
        if frontmatter is None:
            failures.append(f"missing frontmatter: {rel}")
        else:
            missing = sorted(REQUIRED_FRONTMATTER - set(frontmatter))
            if missing:
                failures.append(f"frontmatter missing {missing}: {rel}")
        text = path.read_text(encoding="utf-8")
        for target in link_pattern.findall(text):
            normalized = target.strip().strip("/")
            if normalized and normalized not in pages:
                failures.append(f"broken wiki link [[{target}]] in {rel}")

    if failures:
        print("wiki lint failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"wiki lint passed: {wiki_root}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage a private personal LLM Wiki deployment.")
    parser.add_argument("--config", help="optional config.yml path")
    subcommands = parser.add_subparsers(dest="command", required=True)

    init_vault = subcommands.add_parser("init-vault", help="initialize reusable wiki scaffold in a target vault")
    init_vault.add_argument("--vault", help="target private vault root")
    init_vault.set_defaults(func=command_init_vault)

    submit = subcommands.add_parser("submit-feedback", help="append a feedback event to the target vault")
    submit.add_argument("--vault", help="target private vault root")
    submit.add_argument("--raw-feedback", required=True)
    submit.add_argument("--expected-behavior", required=True)
    submit.add_argument("--type", dest="feedback_type", default="other")
    submit.add_argument("--source", default="chatgpt")
    submit.add_argument("--severity", default="medium")
    submit.add_argument("--privacy", default="normal")
    submit.add_argument("--user-query", default="")
    submit.add_argument("--suggested-target-notes", action="append")
    submit.add_argument("--tags", action="append")
    submit.set_defaults(func=command_submit_feedback)

    validate = subcommands.add_parser("validate-feedback", help="validate feedback JSONL in a target vault")
    validate.add_argument("--vault", help="target private vault root")
    validate.set_defaults(func=command_validate_feedback)

    steward = subcommands.add_parser("run-steward", help="turn inbox feedback into local patch proposals and evals")
    steward.add_argument("--vault", help="target private vault root")
    steward.set_defaults(func=command_run_steward)

    lint = subcommands.add_parser("lint-wiki", help="lint target wiki frontmatter and links")
    lint.add_argument("--vault", help="target private vault root")
    lint.set_defaults(func=command_lint_wiki)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
