# DAG Workflow Playbook

Use this playbook only for complex tasks with real dependencies. The
orchestrator owns the workflow DAG and controls node sequencing; agents execute
individual nodes but do not control workflow progression.

## Orchestrator Ownership

The orchestrator owns:

- workflow DAG creation and modification
- node status updates
- dependency resolution and ready-node computation
- agent spawning decisions
- workflow completion detection

Workers and subagents must not update their own DAG status, spawn dependent
nodes, modify workflow structure, or abandon nodes without orchestrator approval.

## Sequencing Loop

1. Initialize the workflow with `multiagent dag init`.
2. Add nodes with `multiagent dag add-node`.
3. Compute ready nodes with `multiagent dag ready`.
4. Spawn agents only for ready nodes using normal assignment metadata.
5. Mark nodes `running`, `done`, `blocked`, `failed`, or `skipped` based on agent reports.
6. Recompute ready nodes after each status change.
7. Continue until no ready nodes remain or the workflow completes.

## Node Lifecycle

```text
pending -> ready -> running -> done
    |        |        |
    v        v        v
 blocked  skipped  failed
```

Only the orchestrator updates node status. Agents report their state; the
orchestrator translates reports into DAG state.

## Typical Role Dependencies

- Exploration nodes usually depend only on initial architecture or research.
- Exploitation nodes depend on the selected decision and required architecture.
- QA/verifier nodes depend on the implementation nodes they verify.
- Reflection nodes depend on implementation, QA, or metrics nodes.

The DAG provides structure and dependency tracking; it does not automatically
spawn agents. The orchestrator remains the active workflow controller.
