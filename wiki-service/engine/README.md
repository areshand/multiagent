# Reusable LLM Wiki Engine

This is the canonical, data-free maintenance engine for both personal and
organization LLM Wiki deployments. It was migrated from
`areshand/personal-llm-wiki-engine` at commit
`59ffec5f0ae1eeae2df4005e71f4b27b7e344770`.

The engine owns reusable prompts, Codex skills, agent instructions, schemas,
templates, local maintenance commands, privacy checks, and fake fixtures. Real
vault content and runtime state never belong in this source tree.

## Local maintenance

From the repository root:

```bash
wiki-service/bin/personal-llm-wiki init-vault --vault /path/to/private-vault
wiki-service/bin/personal-llm-wiki submit-feedback --vault /path/to/private-vault \
  --raw-feedback "missed context" --expected-behavior "use the relevant concept"
wiki-service/bin/personal-llm-wiki validate-feedback --vault /path/to/private-vault
wiki-service/bin/personal-llm-wiki run-steward --vault /path/to/private-vault
wiki-service/bin/personal-llm-wiki lint-wiki --vault /path/to/private-vault
wiki-service/bin/wiki-search-local --root /path/to/private-vault/'LLM Wiki' \
  --question "memory platform"
```

`--vault` may point to either the private vault or its `LLM Wiki` child. When it
is omitted, the maintenance CLI checks `LLM_WIKI_VAULT_ROOT`,
`CHECK_MY_WIKI_PATH`, `config.yml`, and common Obsidian locations. Explicit
arguments and configuration take precedence over discovery.

## Source/data boundary

```text
multiagent/wiki-service/engine/     reusable code and operating contracts
private vault/                      real raw sources and generated knowledge
  LLM Wiki/                         auditable synthesized Markdown
    system/                         private feedback and steward state
```

Run the migrated compatibility and privacy suites with:

```bash
python3 -m unittest discover -s wiki-service/engine/tests -p 'test_*.py' -v
python3 wiki-service/engine/scripts/privacy_check.py wiki-service/engine
```

## Local Codex skill cutover

The two preferred skill packages are self-contained:

- `skills/check-my-wiki/`
- `skills/capture-personal-wiki-source/`

After this migration is merged, copy those directories into the local Codex
skills directory to replace an older installed copy. Existing installed copies
continue to work until that explicit cutover; the private vault does not depend
on the old engine checkout. Validate each copied package with the Codex
`skill-creator` validator before removing the old checkout.
