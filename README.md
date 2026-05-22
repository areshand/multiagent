# Multiagent tmux Orchestrator

This project launches a tmux session with one `orchestrator` window. The orchestrator prompt coordinates worker agents and named long-running subagents.

## Features

- **Tmux Integration**: Seamless session management with configurable session names
- **Long-Running Subagents**: Persistent agents that maintain state across interactions
- **Flexible Configuration**: Environment-based setup for different project contexts
- **State Persistence**: Durable subagent state management with transcript logging

## Launch

```bash
./launch.sh --session multiagent --root /Users/bowu/projects/multiagent
```

Environment:

- `MULTIAGENT_SESSION`: tmux session name, default `multiagent`
- `MULTIAGENT_ROOT`: project root, default `/Users/bowu/projects/multiagent`
- `MULTIAGENT_STATE_DIR`: durable subagent state, default `$MULTIAGENT_ROOT/.multiagent`
- `MULTIAGENT_WRITE_POLICY`: repo write policy, default `$MULTIAGENT_ROOT/docs/write-policy.paths`
- `CODEX_BIN`: Codex CLI command, default `codex`

## Repo Write Guardrails

Workers and subagents default to writing only inside `MULTIAGENT_ROOT`, the root
passed to `launch.sh`. Outside-root writes are denied by policy unless an
approved outside path is listed in:

```bash
docs/write-policy.paths
```

Use the helper to initialize, inspect, check, and update the policy:

```bash
bin/write-policy.sh init
bin/write-policy.sh show
bin/write-policy.sh check README.md /tmp/outside-file
bin/write-policy.sh approve /tmp/approved-output
```

The launch script initializes the policy file and prints the active policy at
startup. The orchestrator must ask for explicit approval before allowing a
worker to write outside `MULTIAGENT_ROOT`, then record the narrowest practical
outside path with `bin/write-policy.sh approve PATH`.

Mechanical enforcement is limited to the helper's policy checks and startup
visibility. Codex is still launched with
`--dangerously-bypass-approvals-and-sandbox`, so shell sandboxing is not
enforcing the boundary. The orchestrator and worker instructions require agents
to check and follow the policy before writes.

## Long-Running Subagents

Use `bin/subagent.sh` for named subagents that should keep working or monitoring over time:

```bash
bin/subagent.sh spawn subagent-ci-monitor --instruction "Monitor CI and report status changes."
bin/subagent.sh poll subagent-ci-monitor
bin/subagent.sh inspect subagent-ci-monitor --lines 160
bin/subagent.sh recover-plan
bin/subagent.sh restore subagent-ci-monitor
bin/subagent.sh restore-all
bin/subagent.sh finalize subagent-ci-monitor
```

Each subagent persists state under:

```bash
$MULTIAGENT_STATE_DIR/subagents/NAME
```

The state directory includes `meta.env`, `status`, `current.txt`, and `transcript.log`, so the orchestrator can recover context after repeated polling or after finalization.

### Recovery

If the tmux session or orchestrator crashes, start a new orchestrator and run:

```bash
bin/subagent.sh recover-plan
```

The plan prints one row per persisted subagent with a conservative action:

- `restore`: closed subagent with enough prior context to resume.
- `skip-open`: a tmux window with that name already exists.
- `skip-finalized`: the subagent appears completed, finalized, killed, or intentionally stopped.
- `skip-blocked`: the subagent needs an orchestrator/user decision before resuming.
- `skip-unknown`: state is missing or unclear; inspect manually before acting.

Restore a specific resumable subagent with:

```bash
bin/subagent.sh restore NAME
```

The restored subagent gets a fresh tmux window with an instruction containing
its name, prior status, state directory, and a concise tail of `current.txt` and
`transcript.log`. Existing memory files are not deleted. Use
`bin/subagent.sh restore-all` only after reviewing the plan; it restores only
rows classified as `restore` and skips finalized, blocked, open, and unknown
subagents.

## Agent Progress

Use `bin/status.sh` when you want the orchestrator to check progress:

```bash
bin/status.sh
```

The status helper reports actual agents, not every local process. It captures
worker windows, polls open named subagents, refreshes subagent state, and prints
a table with agent type, name, status, window state, latest progress line, and
state directory.

## Tests

```bash
tests/run.sh
```
