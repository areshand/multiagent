"""Evaluation contract extraction, evidence gates, and ledger rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Tuple


ISSUE_COVERAGE_KEYWORDS = {
    "api",
    "audit",
    "cache",
    "cached",
    "caching",
    "cluster",
    "concurrent",
    "config",
    "context",
    "credential",
    "csr",
    "directory",
    "error",
    "exec",
    "expiry",
    "fallback",
    "field",
    "fields",
    "forwarder",
    "handler",
    "initialize",
    "initialization",
    "logging",
    "namespace",
    "persist",
    "request",
    "response",
    "router",
    "session",
    "state",
    "stream",
    "ttl",
    "tunnel",
    "uploader",
}

ISSUE_COVERAGE_TRIGGER_WORDS = {
    "bug",
    "canceled",
    "cancelled",
    "cache",
    "cached",
    "caching",
    "current",
    "disconnect",
    "disconnects",
    "harder",
    "error",
    "expected",
    "fail",
    "fails",
    "failure",
    "inconsistent",
    "inconsistently",
    "missing",
    "must",
    "prevent",
    "prematurely",
    "required",
    "requires",
    "should",
    "unnecessary",
    "unnecessarily",
}

ISSUE_COVERAGE_WEAK_CLOSURE_MARKERS = {
    "source-not-touched",
    "source-not-modified",
    "source-not-changed",
    "not-touched",
    "not-modified",
    "not-changed",
    "nonblocking",
    "non-blocking",
    "verifier-reviewed",
    "not alter",
    "not changed",
    "not modify",
    "preserved-not",
    "preserved-",
}

DEFAULT_COMPLETION_RULES = (
    "Do not remove, rename, or omit a required public symbol while fixing another issue.",
    "Preserve names, arity, parameter order, return shape, and package placement for any symbol referenced by visible tests, source callers, docs, public APIs, schemas, or runtime boundaries, including package-private helpers.",
    "For any new or changed call through a receiver, field, interface, protocol, trait, generated client/model, or adapter, prove the method exists on the declared type at that call site, not merely on a nearby concrete implementation.",
    "Visible-test success does not override this ledger; workers must preserve these invariants and verifiers must reject contradictions.",
    "Literal expected values, command argv, serialized outputs, error text, and ordered lists from legitimate task/source evidence are normative; workers and verifiers must probe that exact shape when practical.",
    "Hidden contracts must be inferred from user intent, issue text, visible tests, docs, source compatibility behavior, public APIs, data schemas, and runtime behavior.",
    "If the public issue lists multiple behavior contracts, final validation must include `issue-coverage-ledger:` mapping every public issue coverage item to a source change, source-level proof it was already satisfied, or a blocking todo.",
    "Verifier reports must explicitly say whether every listed invariant is preserved.",
)


@dataclass(frozen=True)
class IssueRequirement:
    """One independently verifiable requirement extracted from public text."""

    id: str
    summary: str
    keywords: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {"id": self.id, "summary": self.summary, "keywords": list(self.keywords)}


@dataclass(frozen=True)
class ContractLedger:
    """Framework-neutral inputs for a durable coding-task contract ledger."""

    requirements: Tuple[IssueRequirement, ...] = ()
    public_symbols: Tuple[str, ...] = ()
    context_excerpt: str = ""

    @classmethod
    def from_issue(
        cls,
        issue: str,
        public_symbols: Iterable[str] = (),
        context_excerpt: str = "",
    ) -> "ContractLedger":
        return cls(
            requirements=tuple(extract_public_issue_requirements(issue)),
            public_symbols=tuple(public_symbols),
            context_excerpt=context_excerpt,
        )

    def render(
        self,
        title: str = "Contract Ledger",
        introduction: Sequence[str] = (),
        context_label: str = "Public task context excerpt:",
        no_symbols_message: str = "No explicit public-symbol invariants were detected from public task text.",
        completion_rules: Sequence[str] = DEFAULT_COMPLETION_RULES,
        context_limit: int = 6000,
    ) -> str:
        return render_contract_ledger(
            self,
            title=title,
            introduction=introduction,
            context_label=context_label,
            no_symbols_message=no_symbols_message,
            completion_rules=completion_rules,
            context_limit=context_limit,
        )


def public_issue_text(issue: str, additional_instruction_markers: Sequence[str] = ()) -> str:
    """Return the public issue body without a surrounding instruction envelope."""

    description = re.search(
        r"<pr_description>\s*(.*?)\s*</pr_description>",
        issue,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if description:
        return description.group(1)
    markers = ("\n<instructions>", "\n# Task Instructions", "\n## Task Instructions")
    for marker in markers + tuple(additional_instruction_markers):
        if marker in issue:
            return issue.split(marker, 1)[0]
    return issue


def _clean_issue_sentence(sentence: str) -> str:
    return re.sub(r"\s+", " ", sentence.replace("**", " ")).strip(" -:*\t\r\n")


def _issue_sentences(issue: str) -> List[str]:
    lines = []  # type: List[str]
    for raw_line in public_issue_text(issue).replace("\r\n", "\n").splitlines():
        line = _clean_issue_sentence(raw_line)
        if not line or line.startswith("```"):
            continue
        if len(line) > 320:
            for part in re.split(r"(?<=[.!?])\s+", line):
                cleaned = _clean_issue_sentence(part)
                if cleaned:
                    lines.append(cleaned)
        else:
            lines.append(line)
    return lines


def _explicit_requirement_bullets(issue: str) -> List[str]:
    bullets = []  # type: List[str]
    current = []  # type: List[str]
    in_requirements = False
    for raw_line in public_issue_text(issue).replace("\r\n", "\n").splitlines():
        stripped = raw_line.strip()
        if re.match(r"^requirements?\s*:\s*$", stripped, flags=re.IGNORECASE):
            in_requirements = True
            continue
        if not in_requirements:
            continue
        if not stripped:
            continue
        if re.match(r"^(#{1,6}\s+|\w[\w -]{0,80}:\s*$)", stripped) and not re.match(
            r"^([-*]|\d+[.)])\s+", stripped
        ):
            break
        bullet_match = re.match(r"^([-*]|\d+[.)])\s+(.*)$", stripped)
        if bullet_match:
            if current:
                cleaned = _clean_issue_sentence(" ".join(current))
                if cleaned:
                    bullets.append(cleaned)
            current = [bullet_match.group(2)]
            continue
        if current:
            current.append(stripped)
    if current:
        cleaned = _clean_issue_sentence(" ".join(current))
        if cleaned:
            bullets.append(cleaned)
    return bullets


def _issue_sentence_keywords(sentence: str) -> List[str]:
    keywords = []  # type: List[str]
    seen = set()  # type: set
    for code in re.findall(r"`([^`]{2,80})`", sentence):
        token = re.sub(r"[^A-Za-z0-9_./-]+", "", code).strip("./-").lower()
        if token and len(token) >= 3 and token not in seen:
            seen.add(token)
            keywords.append(token)
    for camel in re.findall(r"\b[A-Za-z]+[A-Z][A-Za-z0-9_]*\b", sentence):
        token = camel.lower()
        if token not in seen:
            seen.add(token)
            keywords.append(token)
    for word in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{2,}\b", sentence.lower()):
        if word in ISSUE_COVERAGE_KEYWORDS and word not in seen:
            seen.add(word)
            keywords.append(word)
    return keywords[:8]


def _requirement_id(keywords: Sequence[str], index: int) -> str:
    parts = [re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-") for keyword in keywords[:3]]
    return "issue-" + "-".join(part for part in parts if part) if any(parts) else "issue-item-{}".format(index)


def _fallback_keywords(sentence: str, existing: List[str]) -> List[str]:
    if existing:
        return existing
    stopwords = {
        "and",
        "are",
        "for",
        "from",
        "into",
        "only",
        "should",
        "that",
        "the",
        "their",
        "this",
        "via",
        "when",
        "with",
    }
    keywords = []  # type: List[str]
    seen = set()  # type: set
    for word in re.findall(r"\b[a-zA-Z][a-zA-Z0-9_-]{3,}\b", sentence.lower()):
        if word in stopwords or word in seen:
            continue
        seen.add(word)
        keywords.append(word)
        if len(keywords) >= 5:
            break
    return keywords


def extract_public_issue_requirements(
    issue: str,
    additional_instruction_markers: Sequence[str] = (),
) -> List[IssueRequirement]:
    """Derive independently verifiable requirements from public issue text."""

    issue = public_issue_text(issue, additional_instruction_markers)
    requirements = []  # type: List[IssueRequirement]
    seen_ids = set()  # type: set
    seen_summaries = set()  # type: set

    def add_requirement(sentence: str, explicit: bool = False) -> None:
        summary = _clean_issue_sentence(sentence)
        if not summary or summary.lower() in seen_summaries:
            return
        keywords = _issue_sentence_keywords(summary)
        if explicit:
            keywords = _fallback_keywords(summary, keywords)
        elif len(keywords) < 2:
            return
        requirement_id = _requirement_id(keywords, len(requirements) + 1)
        if requirement_id in seen_ids:
            suffix = 2
            base_id = requirement_id
            while requirement_id in seen_ids:
                requirement_id = "{}-{}".format(base_id, suffix)
                suffix += 1
        seen_ids.add(requirement_id)
        seen_summaries.add(summary.lower())
        requirements.append(
            IssueRequirement(
                id=requirement_id,
                summary=summary[:320] if explicit else summary[:220],
                keywords=tuple(keywords),
            )
        )

    for bullet in _explicit_requirement_bullets(issue):
        add_requirement(bullet, explicit=True)
    for sentence in _issue_sentences(issue):
        lower = sentence.lower()
        if any(trigger in lower for trigger in ISSUE_COVERAGE_TRIGGER_WORDS):
            add_requirement(sentence)
    return requirements[:40]


extract_issue_requirements = extract_public_issue_requirements


def issue_coverage_requirements(issue: str) -> List[Dict[str, object]]:
    """Compatibility representation of extracted public requirements."""

    return [requirement.as_dict() for requirement in extract_public_issue_requirements(issue)]


def build_contract_ledger(
    issue: str,
    public_symbols: Iterable[str] = (),
    context_excerpt: str = "",
) -> ContractLedger:
    """Build a contract ledger from public task inputs."""

    return ContractLedger.from_issue(issue, public_symbols, context_excerpt)


def contract_blockers(issue: str, evidence_text: str) -> List[str]:
    """Return all generic contract-evidence blockers for an issue."""

    blockers = issue_coverage_blockers(issue, evidence_text)
    blockers.extend(data_provenance_blockers(issue, evidence_text))
    blockers.extend(historical_contract_blockers(issue, evidence_text))
    return blockers


def issue_coverage_blockers(issue: str, evidence_text: str) -> List[str]:
    requirements = extract_public_issue_requirements(issue)
    if len(requirements) < 2:
        return []
    lower = evidence_text.lower()
    if "issue-coverage-ledger:" not in lower:
        return [
            "public issue describes multiple independent contracts, but final validation lacks `issue-coverage-ledger:` "
            "mapping each issue-stated behavior to a source change, source-level already-satisfied proof, or blocking todo"
        ]
    ledger_text = lower.split("issue-coverage-ledger:", 1)[1]
    weak_markers = sorted(marker for marker in ISSUE_COVERAGE_WEAK_CLOSURE_MARKERS if marker in ledger_text)
    if weak_markers:
        return [
            "`issue-coverage-ledger:` closes public issue coverage with weak non-evidence marker(s): "
            + ", ".join(weak_markers[:8])
            + "; use `implemented-by=PATH`, source-specific `already-satisfied-by=PATH/evidence`, or `blocking-todo=ID` instead"
        ]
    missing = []  # type: List[str]
    for requirement in requirements:
        if not any(keyword.lower() in ledger_text for keyword in requirement.keywords):
            missing.append(requirement.id or requirement.summary or "issue item")
    if missing:
        return [
            "`issue-coverage-ledger:` does not account for public issue coverage item(s): "
            + ", ".join(missing[:8])
            + "; do not accept a one-symptom patch until every issue-stated contract is implemented, proved already satisfied, or queued as a blocking todo"
        ]
    return []


def data_provenance_required(issue: str) -> bool:
    """Return whether public task text requires state-to-output tracing."""

    normalized = " ".join(issue.lower().split())
    state_terms = r"(?:initial|original|existing|input|request|configuration|config|record|object|state)"
    transfer_terms = r"(?:copy|copied|copies|preserve|preserved|retains?|retained|carry|carried|propagate|propagated|derive|derived)"
    return bool(
        re.search(transfer_terms + r".{0,100}" + state_terms, normalized)
        or re.search(state_terms + r".{0,100}" + transfer_terms, normalized)
    )


def data_provenance_blockers(issue: str, evidence_text: str) -> List[str]:
    """Require source-visible dataflow evidence for copied or preserved outputs."""

    if not data_provenance_required(issue):
        return []
    lower = evidence_text.lower()
    if "data-provenance-ledger:" not in lower:
        return [
            "public task requires output copied, preserved, or derived from initial/original state, but final validation lacks "
            "`data-provenance-ledger:` with `source=`, `stored-as=`, `output=`, `field=`, and `analogue=` source evidence"
        ]
    ledger = lower.split("data-provenance-ledger:", 1)[1]
    missing = [key for key in ("source=", "stored-as=", "output=", "field=", "analogue=") if key not in ledger]
    if missing:
        return [
            "`data-provenance-ledger:` is incomplete; add "
            + ", ".join(missing)
            + " and trace every claimed copied/preserved output to stored input state plus the nearest source-visible analogous type/caller"
        ]
    return []


def historical_contract_required(issue: str) -> bool:
    """Return whether the public issue describes a transition-caused regression."""

    normalized = " ".join(issue.lower().split())
    transition = re.search(r"\b(upgrad(?:e|ed|ing)|migrat(?:e|ed|ion|ing)|compatibility transition|version)\b", normalized)
    regression = re.search(
        r"\b(regression|breaks?|broke|broken|lose|loses|lost|no longer|stale|after upgrading|introduced)\b",
        normalized,
    )
    return bool(transition and regression)


def historical_contract_blockers(issue: str, evidence_text: str) -> List[str]:
    """Require complete source-history evidence for transition regressions."""

    if not historical_contract_required(issue):
        return []
    lower = evidence_text.lower()
    if "historical-contract-ledger:" not in lower:
        return [
            "public task describes an upgrade/migration regression, but final validation lacks "
            "`historical-contract-ledger:` with `baseline-source=`, `transition-path=`, "
            "`mutated-outputs=`, and `compatibility-invariant=` source evidence"
        ]
    ledger = lower.split("historical-contract-ledger:", 1)[1]
    missing = [
        key
        for key in ("baseline-source=", "transition-path=", "mutated-outputs=", "compatibility-invariant=")
        if key not in ledger
    ]
    if missing:
        return [
            "`historical-contract-ledger:` is incomplete; add "
            + ", ".join(missing)
            + " and enumerate every persisted or emitted output changed by the transition"
        ]
    return []


def render_contract_ledger(
    ledger: ContractLedger,
    title: str = "Contract Ledger",
    introduction: Sequence[str] = (),
    context_label: str = "Public task context excerpt:",
    no_symbols_message: str = "No explicit public-symbol invariants were detected from public task text.",
    completion_rules: Sequence[str] = DEFAULT_COMPLETION_RULES,
    context_limit: int = 6000,
) -> str:
    """Render an evaluation contract ledger as stable Markdown."""

    sections = ["# " + title, ""]  # type: List[str]
    sections.extend(introduction)
    if introduction:
        sections.append("")
    if ledger.public_symbols:
        sections.append("- Required public source symbols/interfaces:")
        sections.extend("  - `{}`".format(symbol) for symbol in ledger.public_symbols)
    if ledger.context_excerpt:
        excerpt = ledger.context_excerpt[:context_limit]
        if len(ledger.context_excerpt) > len(excerpt):
            excerpt += "\n... truncated public task context."
        sections.extend(["- " + context_label, "", "```text", excerpt, "```"])
    if not ledger.public_symbols:
        sections.append("- " + no_symbols_message)
    if ledger.requirements:
        sections.append("- Public issue coverage items:")
        for requirement in ledger.requirements:
            sections.append(
                "  - {}: {} [keywords={}]".format(
                    requirement.id,
                    requirement.summary,
                    ",".join(requirement.keywords),
                )
            )
    sections.extend(["", "Completion rules:"])
    sections.extend("- " + rule for rule in completion_rules)
    sections.append("")
    return "\n".join(sections)


def contract_coverage_items_excerpt(issue: str, limit: int = 5000) -> str:
    """Render extracted requirements for worker and verifier checklists."""

    requirements = extract_public_issue_requirements(issue)
    if not requirements:
        return "No public issue coverage items were auto-derived."
    lines = ["Public issue coverage items that must be copied into worker/verifier checklists:"]
    summary_limit = max(80, min(220, (limit // max(1, len(requirements))) - 80))
    for requirement in requirements:
        summary = requirement.summary
        if len(summary) > summary_limit:
            summary = summary[:summary_limit].rstrip() + "..."
        lines.append(
            "- {}: {} [keywords={}]".format(
                requirement.id,
                summary,
                ",".join(requirement.keywords),
            )
        )
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    return "\n".join(line[: max(120, limit // max(1, len(lines)))] for line in lines)


__all__ = [
    "ContractLedger",
    "DEFAULT_COMPLETION_RULES",
    "IssueRequirement",
    "build_contract_ledger",
    "contract_blockers",
    "contract_coverage_items_excerpt",
    "data_provenance_blockers",
    "data_provenance_required",
    "extract_issue_requirements",
    "extract_public_issue_requirements",
    "historical_contract_blockers",
    "historical_contract_required",
    "issue_coverage_blockers",
    "issue_coverage_requirements",
    "public_issue_text",
    "render_contract_ledger",
]
