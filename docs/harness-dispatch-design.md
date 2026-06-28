# Harness Dispatch Design

This is a fresh design direction for making the control boundary explicit. The
important distinction is that the current Codex/Claude orchestrator is still a
CLI session; the repo cannot directly intercept its reasoning loop. In this
design, an external harness process owns observation, risk evaluation, and
dispatch. The CLI orchestrator becomes one observed participant, not the thing
that proves the loop is being followed.

The core rule is that side effects should flow through a harness action:

```text
agent intent -> action manifest -> policy decision -> executor
```

The current repo still launches tmux-hosted CLIs with broad local permissions.
This design is therefore a prototype control surface rather than a complete
sandbox. It is meant to make the next boundary visible in code: a process
outside the orchestrator CLI can evaluate risk and dispatch lifecycle actions.

## Components

- **External harness**: owns the loop. It observes tmux targets, evaluates
  risk, chooses whether an agent should continue, and dispatches allowed
  lifecycle actions.
- **CLI orchestrator**: a tmux-hosted agent that may plan and coordinate, but
  is itself observable by the external harness. It should not be treated as
  proof that policy is being followed.
- **Action manifest**: a small `key=value` file that describes one requested
  side effect or observation.
- **Policy evaluator**: returns `ALLOW`, `DENY`, `REQUIRE_APPROVAL`, or
  `TERMINATE`.
- **Dispatcher**: executes only allowed actions through existing executors such
  as `bin/subagent.sh` and `bin/write-policy.sh`.
- **Executors**: existing repo-local helpers that touch tmux, git worktrees,
  assignment state, and write policy.

## Action Format

Action files are deliberately boring shell-friendly manifests:

Examples:

```text
type=assignment_check
name=worker-01-docs
```

```text
type=assess_agent
name=worker-01-docs
```

```text
type=assess_agent
name=orchestrator
```

```text
type=kill_agent
name=worker-01-docs
```

```text
type=approve_write
path=/tmp/report
actor=orchestrator
assignment_id=docs-001
reason=user approved report export
approved=false
```

## Decisions

`bin/harness.sh evaluate ACTION_FILE` prints a tab-separated decision:

```text
decision	ALLOW
reason	acceptance check action
executor	bin/subagent.sh assignment-check worker-01-docs
```

`bin/harness.sh dispatch ACTION_FILE` evaluates first and executes only when the
decision is `ALLOW`. Non-allowed decisions return distinct exit codes:

- `10`: denied
- `20`: requires approval
- `30`: terminate requested

## Initial Policy

The prototype starts with conservative rules:

- Assignment checks are allowed because they are acceptance actions.
- Agent assessment is allowed because it is observation. It can inspect worker
  panes, persisted subagent state, assignment metadata, and the orchestrator
  pane. It returns recommendations such as `continue`, `verify`, `block`,
  `inspect`, `unknown`, or `kill`.
- Kill and finalize are allowed lifecycle actions because the harness owns
  agent scheduling.
- Outside write approval requires `approved=true`; without that, dispatch stops
  with `REQUIRE_APPROVAL`.
- Direct instruction dispatch is denied until it has a mediated executor that
  can capture, inspect, and confirm readiness first.
- Unsupported action types are denied.

## Migration Path

1. Keep existing helpers as executors.
2. Run external assessment with `bin/harness.sh assess NAME` or
   `type=assess_agent` manifests before trusting worker or orchestrator state.
3. Route lifecycle operations through `bin/harness.sh`.
4. Add action types for safe instruction delivery and worker spawning.
5. Move prompt examples from raw `tmux` commands to action manifests.
6. Replace bypass-enabled worker sessions with a mediated tool loop when the
   agent can emit actions without direct tool execution.

This design intentionally separates a clear control boundary from sandboxing.
The first milestone is making decisions observable and auditable; hard
prevention requires workers that cannot perform side effects except through the
dispatcher.
