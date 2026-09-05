---
id: wiki:meta:schema
title: LLM Wiki Schema
type: schema
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
---

# LLM Wiki Schema

Required frontmatter:

```yaml
id: wiki:domain:unique-slug
title: Human Title
type: concept | entity | project | source-summary | synthesis | workflow | index | log
status: seed | active | needs-review | stale | archived
confidence: low | medium | high
created: YYYY-MM-DD
updated: YYYY-MM-DD
owner: llm
source_count: 0
source_ids: []
source_paths: []
tags:
  - llm-wiki
```
