# Orchestration Routing Playbook

Use this playbook to select and coordinate the smallest role DAG. Role modules
own role-specific procedure; this file does not repeat them.

## Select A Role

- A fresh execution is observe-only. Answer directly from conversation, Wiki,
  repository, or non-mutating external evidence when another agent would not
  materially improve the result. Persist the response at the
  `resultCandidate.path` returned by workflow context, then request
  `multiagent orchestrator complete --observe --result-file PATH`.
- A reader is optional for a larger or parallel repository investigation. It
  runs with mechanically read-only access, without `--own` or implementation
  decision metadata, and never receives source ownership or an implementation
  permit. Its own final response should be self-checked; do not spawn another
  model solely to review a read-only answer.
- Query the organizational Wiki directly for both routing and caller-facing
  cited evidence. Wiki use does not force a reader, scout, or reviewer.
- If repair is required, inspect enough to state one bounded question and the
  exact effects requested. For source writes, include every affected repository
  path with `--path REPO_PATH`. For production mutation, include
  `--reviewed-ops`. End the observe execution with
  `multiagent orchestrator complete --request-review --result-file PATH [--path REPO_PATH ...] [--reviewed-ops]`.
- Use a worker or reviewed-ops flow only in the fresh `approved-repair`
  execution created after the user approves those exact effects.
- Let the assigned confined role request a bounded external read or repository
  materialization directly through the supervisor when prod-mcp advertises it as
  non-mutating read/materialize with no approval roles.
- Use ops when the required output would write, execute, or otherwise mutate an
  external provider or deployed service under a Markdown runbook.
- Use a scout only when a material unknown can be resolved from repository,
  workspace, session, or already-returned immutable evidence.
- Use a reviewer or verifier when an independent verdict can change acceptance
  or the supervisor reports a review obligation.
- Use specialized roles only for their declared capability.

Do not hard-code provider operations, request parsing, pagination, time windows,
or action sequences into the orchestrator. Do not spawn a role merely because
its module exists.

A scout never calls provider endpoints directly. When its bounded assignment
requires fresh external evidence, it may submit a direct supervisor-mediated
read request that passes the live non-mutation gate. A scout may analyze the
returned immutable evidence when that separate analysis can affect acceptance.

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
- Observe completion is available only to an immutable observe-only session.
  Source writes and mutating production operations are denied before execution,
  so completion does not infer safety from role count, a second model, or a
  post-hoc diff check.
- A repair review request must contain one bounded question and at least one
  explicit effect: exact repository-relative source paths, `reviewed-ops`, or
  both. Approval starts a fresh execution with only those effects; it does not
  upgrade the completed observe session.
- Ops execution needs finalized reviewer evidence bound to the exact request,
  goal, runbook metadata, and runbook bytes.
- Post-implementation review types and diff bindings come from persisted
  obligations, not a prompt checklist.
- Findings become TODOs and return through the lifecycle before repair.
- Completion is a supervisor request, not an orchestrator assertion.

An external-only task with no repository mutation bypasses the source
implementation lifecycle. If every required capability is advertised as
non-mutating read/materialize with no approval roles, the assigned confined
roles use the direct supervisor-mediated path and no ops identity or reviewer is
created. If any required capability writes, executes, mutates, or requires an
approval role, load reviewed-ops-cycle.md; that playbook owns the persistent ops
identity and reviewed execution lifecycle. Do not manufacture a source phase
transition for either path.

If a gate rejects, use its concrete reason as the next dependency. Never create
or edit supervisor-owned evidence.

## Agent Contract

For non-ops roles, load prompts/playbooks/agent-spawning.md and spawn only
through `multiagent subagent spawn`. For ops, load reviewed-ops-cycle.md instead.
The runtime selects and composes the canonical role module from the role and
identity name; do not find, list, or read role prompt files at runtime. Wait for
durable output, finalize completed read-only reviewers, and run assignment
checks for workers. The role module owns request shape, output markers, and
provider procedure.

## Repair And Safety

Load finding-todo-loop.md for accepted findings. Stop or finalize the current
owner before replacement, release its ownership and validation lease, and give
the replacement only the implicated paths and accepted evidence.

Never overlap writable ownership, mutate sealed artifacts, bypass role
isolation, or infer success from prose when the supervisor gate has not passed.
Preserve MULTIAGENT_STATE_DIR and keep a compact table of active nodes, owners,
status, and durable outputs.
