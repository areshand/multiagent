---
name: run-personal-wiki-steward
description: Process local personal LLM Wiki feedback into patch proposals, evals, run logs, and state updates inside the target vault. Also use when an older prompt names run_personal_wiki_steward.
---

# Run Personal Wiki Steward

1. Read `prompts/feedback-steward.md`.
2. Read the target vault index, schema, feedback inbox, and steward state.
3. Process inbox feedback.
4. Write proposed patches and evals under `LLM Wiki/system/`.
5. Require human review for sensitive edits.
6. Do not write private target-vault data into the engine repo.

For the conservative local implementation, run:

```bash
python3 scripts/personal_llm_wiki.py run-steward --vault /path/to/vault
```
