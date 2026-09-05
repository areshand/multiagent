---
id: wiki:workflows:feedback-loop
title: Feedback Loop Workflow
type: workflow
status: active
confidence: high
created: {{DATE}}
updated: {{DATE}}
owner: llm
source_count: 0
source_ids: []
source_paths: []
tags:
  - llm-wiki
  - workflow
  - feedback-loop
---

# Feedback Loop Workflow

1. Capture feedback as JSONL under `LLM Wiki/system/feedback/inbox.jsonl`.
2. Keep intake separate from wiki edits.
3. Convert feedback into patch proposals, evals, or human-review requests.
4. Do not close feedback without a durable outcome.
