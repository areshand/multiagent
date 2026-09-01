# Orchestration Routing Playbook

Use this playbook to select and coordinate the smallest role DAG. Role modules
own role-specific procedure; this file does not repeat them.

## Select A Role

- Answer directly, or ask one bounded clarification, when the authenticated
  request can be handled from the current conversation without reading the
  repository, calling an external service, or producing an artifact. Persist
  the exact response at the `resultCandidate.path` returned by workflow context,
  then request
  `multiagent orchestrator complete --direct-response --result-file PATH`.
- Use a `reader` when answering requires repository inspection but no source
  mutation. Readers run in the repository working directory with mechanically
  read-only access. After readers finish, spawn one independent reviewer named
  `read-only-integrity-reviewer-NN`; require it to inspect the live repository
  diff, the supervisor launch manifests, and the sealed reader outputs, and to
  emit exactly
  `review-record: type=read-only-integrity verdict=pass diff=DIFF_SHA256` only
  when all launches were read-only and the diff is empty. Then request
  `multiagent orchestrator complete --read-only --result-file PATH --reviewer NAME`.
- Use a worker when the required output is a bounded workspace change.
- Use ops when the required output needs access to an external provider or
  deployed service covered by a Markdown runbook and prod-mcp contract. This
  includes read-only retrieval: external access is an authority boundary, not
  a mutability classification.
- Use a scout only when a material unknown can be resolved from repository,
  workspace, session, or already-returned immutable evidence.
- Use a reviewer or verifier when an independent verdict can change acceptance
  or the supervisor reports a review obligation.
- Use specialized roles only for their declared capability.

Do not hard-code provider operations, request parsing, pagination, time windows,
or action sequences into the orchestrator. Do not spawn a role merely because
its module exists.

A scout never calls Slack, GitHub, Grafana, AWS, Kubernetes, prod-mcp, or any
other deployed service. Spawn ops first to acquire external evidence under a
reviewed runbook. A scout may then analyze the immutable artifact returned by
ops when that separate analysis can affect acceptance.

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
- Direct-response completion is rejected if any role was launched, any source
  diff exists, any external receipt exists, or any workflow TODO remains.
- Read-only completion is rejected unless every launch is a completed reader or
  reviewer with supervisor-recorded read-only access, the repository diff is
  empty, and the named independent reviewer has sealed passing integrity
  evidence bound to that diff.
- Ops execution needs finalized reviewer evidence bound to the exact request,
  goal, runbook metadata, and runbook bytes.
- Post-implementation review types and diff bindings come from persisted
  obligations, not a prompt checklist.
- Findings become TODOs and return through the lifecycle before repair.
- Completion is a supervisor request, not an orchestrator assertion.

An external-only task with no repository mutation bypasses the source
implementation lifecycle. Its minimum DAG starts with the persistent ops
identity and uses the reviewed-ops cycle for each immutable request. Do not
manufacture a decision, approved implementation context, decision-authority
review, or source phase transition merely to authorize ops; the runbook,
request binding, independent ops reviewer, caller approval, and prod-mcp permit
are that path's authority chain.

For this ops-only route, load reviewed-ops-cycle.md instead of
agent-spawning.md. The reviewed ops playbook owns the initial ops spawn and the
complete reviewed execution lifecycle.

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
