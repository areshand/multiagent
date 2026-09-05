# Query Answering Prompt

Use this when answering questions against a private LLM Wiki.

Workflow:

1. Read `LLM Wiki/index.md`.
2. Read the most relevant synthesized pages.
3. Search raw sources only when the wiki lacks detail, a claim needs verification, or the user asks for a specific source.
4. Cite local wiki pages and raw source paths.
5. If the answer creates durable synthesis, propose a target wiki update or feedback item.
