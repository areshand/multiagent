---
name: submit-personal-wiki-feedback
description: Record feedback about a private personal LLM Wiki answer by appending a structured JSONL event to the target vault. Also use when an older prompt names submit_personal_wiki_feedback.
---

# Submit Personal Wiki Feedback

Use the CLI:

```bash
python3 scripts/personal_llm_wiki.py submit-feedback --vault /path/to/vault --raw-feedback "..." --expected-behavior "..."
```

Rules:

1. Preserve the raw feedback text.
2. Set status to `inbox`.
3. Write only to `LLM Wiki/system/feedback/inbox.jsonl` in the target vault.
4. Do not edit synthesized wiki pages during intake.
