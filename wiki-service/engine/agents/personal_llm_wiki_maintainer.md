# Personal LLM Wiki Maintainer Agent

Purpose: maintain the compiled LLM Wiki layer in a private vault while keeping raw sources and runtime state local.

Reconstruction context:

1. Engine prompt: `prompts/wiki-maintainer.md`
2. Target vault: configured by `vault_root`
3. Wiki index: `LLM Wiki/index.md`
4. Wiki schema: `LLM Wiki/schema.md`
5. Wiki log: `LLM Wiki/log.md`

Operating boundary:

- Engine repo files are reusable code and prompts.
- Target vault files are private deployment data.
- Runtime state belongs under `LLM Wiki/system/`.
