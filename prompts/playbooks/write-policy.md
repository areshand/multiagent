# Write Policy Playbook

Workers and subagents default to writing only inside `MULTIAGENT_ROOT`.
Outside-root writes require explicit user/orchestrator approval.

## Commands

```bash
bin/write-policy.sh show
bin/write-policy.sh check PATH
bin/write-policy.sh approve PATH --actor orchestrator --assignment-id ID --reason "why this outside path is needed"
```

## Rules

- The policy file is orchestrator-owned.
- Do not ask workers to edit `docs/write-policy.paths` directly.
- Workers must check uncertain paths with `bin/write-policy.sh check PATH`.
- If a worker needs an outside-root write, ask the user for approval before continuing.
- If approved, record the narrowest practical outside path and tell the worker to retry.

Reject broad outside approvals by default, including `/`, `$HOME`, the repo
parent, `/tmp`, and broad shared roots such as `/Users`, `/home`, `/usr`,
`/var`, `/private`, and `/Applications`.

Use `--force` only after an explicit user/orchestrator decision.
