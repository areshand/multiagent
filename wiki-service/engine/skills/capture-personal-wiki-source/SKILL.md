---
name: capture-personal-wiki-source
description: Capture new user-provided source material into a private Obsidian vault as raw data, then update the synthesized personal LLM Wiki with cited claims. Use when the user asks to add, remember, record, ingest, save, or put new personal facts, notes, documents, conversation context, health details, career details, project notes, or other durable source material into both raw vault storage and the LLM Wiki.
---

# Capture Personal Wiki Source

Use this skill when the user wants new material preserved as raw source data and also reflected in the compiled LLM Wiki. This is different from feedback intake: feedback records a problem for later steward processing; source capture writes the raw evidence and updates the wiki now.

## Workflow

1. Resolve the target private vault. If unspecified and the current workspace has `LLM Wiki/index.md`, use the current workspace root.
2. Read `LLM Wiki/index.md`, `LLM Wiki/schema.md`, and the most relevant existing wiki page(s) before editing.
3. Preserve the original source material under the target vault, outside the `multiagent` source checkout.
4. Prefer `Raw Materials/<Domain>/YYYY-MM-DD <short-slug>.md` for user-provided text that does not already have a raw source file.
5. If the source already exists in a raw root such as `Notion Export/`, `Evernote Export/`, `Slack Export/`, `Raw Materials/`, or `New/`, do not duplicate it; cite the existing raw path.
6. Add source frontmatter or a short source note that records capture date, origin, privacy level when relevant, and intended wiki targets.
7. Update the smallest appropriate synthesized page under `LLM Wiki/concepts/`, `LLM Wiki/entities/`, `LLM Wiki/projects/`, `LLM Wiki/sources/`, `LLM Wiki/syntheses/`, or `LLM Wiki/workflows/`.
8. In the synthesized page, cite the raw source path, update frontmatter `updated`, `source_count`, `source_ids` or `source_paths` as appropriate, and distinguish confirmed facts from user-reported claims or unknowns.
9. Add a concise entry to `LLM Wiki/log.md` for meaningful captures.
10. Verify that the raw source exists, the wiki update links back to it, and no private source data was written into `multiagent/wiki-service/engine/`.

## Raw Source Shape

For new user-provided text, use this pattern:

```markdown
---
id: raw:<domain>:<yyyy-mm-dd>-<slug>
title: Human Title
type: raw-source
created: YYYY-MM-DD
source: user-provided
privacy: private | sensitive_health | sensitive_financial | sensitive_legal | public
intended_wiki_targets:
  - LLM Wiki/path/to/page.md
tags:
  - raw-material
---

# Human Title

## Original Material

<preserve the user-provided material with minimal cleanup>

## Capture Notes

- Captured for synthesis into `<wiki target>`.
- Separate confirmed facts from verification needs when updating the wiki.
```

Use domain folders such as `Raw Materials/Health/`, `Raw Materials/Career/`, `Raw Materials/Projects/`, `Raw Materials/Family/`, `Raw Materials/Research/`, or `Raw Materials/Misc/`. Create the smallest sensible folder if no existing domain fits.

## Synthesis Rules

- Preserve raw material verbatim enough that future agents can inspect the original evidence.
- Do not paste large raw dumps into synthesized wiki pages; summarize and cite the raw source.
- Prefer updating an existing page over creating a near-duplicate.
- If creating a new wiki page, follow `LLM Wiki/schema.md` frontmatter and link it from `LLM Wiki/index.md` when it should be discoverable.
- For sensitive health, legal, financial, family, or identity data, mark privacy explicitly in the raw note and include a privacy note or verification caveat in the wiki page.
- If the user has not clearly approved synthesizing sensitive material, write the raw note and create a feedback or pending patch item instead of applying the compiled wiki update.
- Never write private target-vault data, raw materials, generated wiki pages, feedback logs, patch proposals from a real vault, or evals into `multiagent/wiki-service/engine/`.

## Validation

Before finishing, check:

- Raw source path is under the private vault, usually `Raw Materials/...`.
- Synthesized wiki page cites the raw source path.
- `LLM Wiki/log.md` records the capture when the update is durable.
- `multiagent/wiki-service/engine/` contains only reusable instructions, code, schemas, templates, fake fixtures, and tests.
