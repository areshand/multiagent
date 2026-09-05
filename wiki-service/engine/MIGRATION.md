# Personal Wiki Engine Consolidation

## Decision

`multiagent/wiki-service` is the canonical home of the reusable LLM Wiki
engine. The former `areshand/personal-llm-wiki-engine` repository is deprecated
after its source and compatibility tests are preserved here.

This is a source-code consolidation, not a data migration. Existing private
vaults remain in place and are not copied into Git, S3, or this repository.

## Provenance

- Source repository: `areshand/personal-llm-wiki-engine`
- Imported commit: `59ffec5f0ae1eeae2df4005e71f4b27b7e344770`
- Preserved surfaces: CLI commands, templates, schemas, prompts, agent
  instructions, feedback steward, privacy scanner, fake fixtures, and tests.
- Added compatibility surfaces: current `check-my-wiki` and
  `capture-personal-wiki-source` Codex skills, portable vault discovery, the
  local search helper, and an HTTP adapter that understands Obsidian links.

## Compatibility contract

| Workflow | Consolidated surface | Data access |
| --- | --- | --- |
| Query a local personal Wiki | `check-my-wiki` skill and `wiki-search-local` | Local read of `LLM Wiki/`; raw-source fallback only when needed |
| Capture a personal source | `capture-personal-wiki-source` skill | Writes only to the selected private vault |
| Initialize a vault | `personal-llm-wiki init-vault` | Writes only to the selected private vault |
| Submit/validate feedback | `personal-llm-wiki submit-feedback` / `validate-feedback` | Private `LLM Wiki/system/feedback/` |
| Produce steward proposals | `personal-llm-wiki run-steward` | Private `LLM Wiki/system/`; no automatic patch application |
| Lint pages and Obsidian links | `personal-llm-wiki lint-wiki` | Local read of the selected vault |
| Query organization knowledge | `wiki-query` to the private HTTP service | Bounded cited excerpts; no client volume or S3 access |
| Seed the organization catalog | `wiki-seed` in a local staging directory | Deployment owner uploads Markdown and publishes `index.md` last |

## Adapter boundary

The engine's durable format is Markdown. A local personal deployment can be
writable because it runs with the user's filesystem authority. The Kubernetes
query deployment is intentionally read-only and receives a mounted S3-backed
corpus from `InternalServices`. These are separate authorities using the same
engine contract; consolidating source does not grant the cluster service access
to a personal vault or grant local Codex an S3 credential.
