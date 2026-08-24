# Orchestration Routing Playbook

Use this playbook to select and coordinate the smallest role DAG. Role modules
own role-specific procedure; this file does not repeat them.

## Select A Role

- Use a worker when the required output is a bounded workspace change.
- Use ops when the required output is an external action covered by a Markdown
  runbook and prod-mcp contract.
- Use a scout only when a material unknown must be resolved read-only.
- Use a reviewer or verifier when an independent verdict can change acceptance
  or the supervisor reports a review obligation.
- Use specialized roles only for their declared capability.

Do not hard-code provider operations, request parsing, pagination, time windows,
or action sequences into the orchestrator. Do not spawn a role merely because
its module exists.

## Build The DAG

1. List required outputs and dependencies.
2. Query workflow status and pending supervisor obligations.
3. Add the minimum nodes that produce those outputs and verdicts.
4. Give each node only the authenticated goal, its role module, and immutable
   inputs it needs.
5. Spawn a node only when its dependencies are ready.
6. Reuse accepted artifacts; replace only a rejected or blocked node and its
   dependents.

For parallel work, load parallel-execution.md. For source ownership and spawn
commands, load agent-spawning.md. For overlapping or expensive validation, load
validation-scheduling.md and hold one validation lease per package. Give technical verification a validation lease for the narrowest visible behavior test that covers the changed path.

## Supervisor Gates

- A source worker needs an approved implementation context and active
  implementation permit.
- Ops execution needs finalized reviewer evidence bound to the exact request,
  goal, runbook metadata, and runbook bytes.
- Post-implementation review types and diff bindings come from persisted
  obligations, not a prompt checklist.
- Findings become TODOs and return through the lifecycle before repair.
- Completion is a supervisor request, not an orchestrator assertion.

If a gate rejects, use its concrete reason as the next dependency. Never create
or edit supervisor-owned evidence.

## Agent Contract

Before spawning, load the selected role module and
prompts/playbooks/agent-spawning.md. Spawn only through multiagent subagent
spawn, wait for durable output, finalize completed read-only reviewers, and run
assignment checks for workers. The role module owns request shape, output
markers, and provider procedure.

## Repair And Safety

Load finding-todo-loop.md for accepted findings. Stop or finalize the current
owner before replacement, release its ownership and validation lease, and give
the replacement only the implicated paths and accepted evidence.

Never overlap writable ownership, mutate sealed artifacts, bypass role
isolation, or infer success from prose when the supervisor gate has not passed.
Preserve MULTIAGENT_STATE_DIR and keep a compact table of active nodes, owners,
status, and durable outputs.
