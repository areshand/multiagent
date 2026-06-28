# Harness Dispatch Design

This is a fresh design direction for making the orchestrator boundary explicit.
The core rule is that side effects should flow through a harness action:

```text
agent intent -> action manifest -> policy decision -> executor
```

The current repo still launches tmux-hosted CLIs with broad local permissions,
so this design is a prototype control surface rather than a complete sandbox.
It is meant to make the next boundary visible in code: the orchestrator should
ask the harness to evaluate and dispatch actions instead of directly spelling
every lifecycle command inline.

## Components

- **Orchestrator**: owns the loop. It observes state, selects the next action,
  and records outcomes. It should not implement task work.
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
- Kill and finalize are allowed lifecycle actions because the harness owns
  agent scheduling.
- Outside write approval requires `approved=true`; without that, dispatch stops
  with `REQUIRE_APPROVAL`.
- Direct instruction dispatch is denied until it has a mediated executor that
  can capture, inspect, and confirm readiness first.
- Unsupported action types are denied.

## Migration Path

1. Keep existing helpers as executors.
2. Route orchestrator-owned lifecycle operations through `bin/harness.sh`.
3. Add action types for health classification, safe instruction delivery, and
   worker spawning.
4. Move prompt examples from raw `tmux` commands to action manifests.
5. Replace bypass-enabled worker sessions with a mediated tool loop when the
   agent can emit actions without direct tool execution.

This design intentionally separates a clear control boundary from sandboxing.
The first milestone is making decisions observable and auditable; hard
prevention requires workers that cannot perform side effects except through the
dispatcher.
