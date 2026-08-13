# Recovery Playbook

Use this playbook only when `MULTIAGENT_RESUME=1` or after a crash/interruption
where durable subagent state may matter.

## Clean Launch

Clean launch is the default. When `MULTIAGENT_RESUME=0`, list the current tmux
session, worker windows, named subagent windows, and persisted directories, then
wait for user direction. Do not inspect recovery state by default.

## Resume Launch

When `MULTIAGENT_RESUME=1`, run:

```bash
multiagent subagent recover-plan
```

Read the plan before spawning replacement work. This is required even if tmux
looks empty, because a prior orchestrator or tmux session may have crashed after
subagents persisted memory.

## Recovery Actions

- `restore`: closed subagent with recoverable context. Report the restore, then run `multiagent subagent restore NAME` when appropriate.
- `skip-open`: active tmux window already exists. Poll or inspect it; do not restore it.
- `skip-finalized`: appears done, finalized, killed, or intentionally stopped. Do not restore by default.
- `skip-blocked`: blocked or waiting for input. Report the blocker and ask the user or make an explicit orchestrator decision before `restore --force`.
- `skip-unknown`: state is stale or unclear. Inspect the state directory before deciding.

Use `multiagent subagent restore-all` only after reviewing the plan. It restores
only conservative `restore` rows.
