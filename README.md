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
- `CODEX_BIN`: Codex CLI command, default `codex`

## Long-Running Subagents

Use `bin/subagent.sh` for named subagents that should keep working or monitoring over time:

```bash
bin/subagent.sh spawn subagent-ci-monitor --instruction "Monitor CI and report status changes."
bin/subagent.sh poll subagent-ci-monitor
bin/subagent.sh inspect subagent-ci-monitor --lines 160
bin/subagent.sh finalize subagent-ci-monitor
```

Each subagent persists state under:

```bash
$MULTIAGENT_STATE_DIR/subagents/NAME
```

The state directory includes `meta.env`, `status`, `current.txt`, and `transcript.log`, so the orchestrator can recover context after repeated polling or after finalization.

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
