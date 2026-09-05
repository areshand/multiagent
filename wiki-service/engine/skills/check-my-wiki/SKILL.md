---
name: check-my-wiki
description: Answer questions using a local Obsidian LLM Wiki. Use when the user asks to check, search, summarize, explain, or answer from "my wiki", "LLM wiki", "Obsidian", "iCloud notes", or a personal Markdown knowledge base.
---

# Check My Wiki

## Overview

Use the user's local Obsidian LLM Wiki as the source of truth for questions about their notes or personal knowledge base. Follow the wiki's own query workflow: start from the index, read the relevant synthesized pages, and only search raw sources when the wiki calls for it.

## Source Location

Prefer these sources in order:

1. `CHECK_MY_WIKI_PATH` if the environment variable is set.
2. A user-provided path in the request.
3. `LLM_WIKI_VAULT_ROOT`, using its `LLM Wiki/` child.
4. `~/Documents/obsidian/LLM Wiki`.
5. Common iCloud/Obsidian locations:
   - `~/Library/Mobile Documents/iCloud~md~obsidian/Documents`
   - `~/Library/Mobile Documents/com~apple~CloudDocs/Documents/Obsidian`
   - `~/Library/Mobile Documents/com~apple~CloudDocs/Obsidian`
   - `~/Library/Mobile Documents/com~apple~CloudDocs/Documents`

If multiple plausible wiki roots exist, prefer a directory whose name contains `llm`, `wiki`, or `obsidian`. If no Markdown notes are found, ask the user for the vault path.

## Workflow

1. Open `LLM Wiki/index.md` first. It is the navigation root for the compiled wiki.

2. Use the index sections and Obsidian links such as `[[concepts/career narrative]]` to identify relevant synthesized wiki pages.

3. Use `LLM Wiki/graph/knowledge graph.md` when the question asks for relationships, clusters, maps, adjacent topics, source discovery, or "what is connected to X." Treat `explicit_link` and `cites` edges as stronger evidence; treat `semantic_link` edges as discovery leads, not proven claims.

4. Read the most relevant wiki pages before answering. Prefer `sed`, `rg`, or another local file reader over summaries from a search helper.

5. Search within `LLM Wiki/` when the index and knowledge graph do not directly identify the right page:

   ```bash
   rg -n "TERM|RELATED TERM" "$CHECK_MY_WIKI_PATH"
   ```

6. Search raw sources with `rg` only when:
   - the wiki lacks detail,
   - a claim needs verification,
   - the question asks about a specific source,
   - or the wiki may be stale.

7. Answer with links or local paths to the wiki pages and raw source paths used. Clearly distinguish wiki-grounded claims from inference.

8. If the answer creates durable synthesis and the user asked for an update, save it under `LLM Wiki/syntheses/` and update `LLM Wiki/log.md`.

9. If the wiki does not contain enough information, say what was searched and what was not found. Do not fill gaps with web results unless the user explicitly asks.

## Search Guidance

- Search exact names, quoted phrases, acronyms, and likely synonyms from the question.
- For broad questions, first identify overview/index notes, then inspect more specific linked notes.
- For recent or evolving facts, treat the wiki as the requested source; mention if the note dates or file modification times make freshness uncertain.
- Preserve private information. Do not expose long verbatim note excerpts unless the user asks; summarize and cite the local note instead.

## Optional Helper

Use `scripts/search_wiki.py --locate` only when the wiki root is unknown or paths need debugging. Do not use the helper as the primary question-answering workflow; the wiki's own workflow prefers reading `index.md`, following synthesized pages, and using `rg` selectively.

For a deployed organization Wiki, use the provider-neutral `wiki-query` client instead of attempting to mount or read the service volume.
