# Implementation Lifecycle Playbook

This mandatory lifecycle applies to source implementation. Production actions
use the selected Markdown runbook and reviewed ops path instead.

Do not use this lifecycle for an external-only task that does not modify
repository source. Such work uses `prompts/playbooks/reviewed-ops-cycle.md`.

## Iteration Plan

Adapt the role graph at the beginning of an iteration, then let the runtime
execute that sealed graph. The orchestrator makes semantic choices between
iterations; it does not drive routine transitions between nodes within one
iteration.

Normal lifecycle phases remain:

    pre-implementation -> implementation -> post-implementation -> complete
    post-implementation -> pre-implementation  when a repair TODO remains

A substantive alignment finding, changed assumption, expanded scope, risk
change, or needed user input ends the current iteration. It must produce a newly
reviewed plan; never mutate a sealed plan in place.

## Build One Complete Plan

Read the authenticated task once through `multiagent workflow context`. Choose
the smallest worker dependency graph that can satisfy it. Skip a contract scout
when the task already gives an exact bounded artifact schema and values. Add a
scout only when an unknown can materially change the plan.
The original user request is authoritative. Ask the user only when required
information is genuinely absent or materially different outcomes remain
consistent with the complete request and available evidence.

Write one UTF-8 JSON plan under `MULTIAGENT_STATE_DIR`, using exactly this
schema:

```json
{
  "apiVersion": "multiagent.moveindustries.io/v1",
  "kind": "IterationPlan",
  "workflowId": "WORKFLOW_ID",
  "iteration": 1,
  "implementationContext": "Complete goal, constraints, exact target paths, ownership, prohibitions, acceptance criteria, and unresolved risks.",
  "workers": [
    {
      "id": "worker-primary-01",
      "ownedPaths": ["exact/repository/path"],
      "instruction": "Exact node output and acceptance contract.",
      "dependsOn": []
    }
  ],
  "resolvesTodos": [],
  "additionalReviews": []
}
```

Use `worker-ops-plan-01` for a bounded repository artifact whose deliverable is
an operations plan so the launcher selects the focused planning role. Use
ordinary `worker-*` identities for other source work. Worker ownership must be
non-overlapping. Dependencies name other worker IDs. Put only `scope` or
`reflection` in `additionalReviews`, and only when that extra review can affect
acceptance.
On a repair iteration, `resolvesTodos` must exactly list every active direct
TODO that the sealed worker graph will address. Resolve evidence or decision
TODOs before submitting the plan; the runtime marks declared direct TODOs
complete only after the candidate passes every supervisor review.

The supervisor always requires one independent plan-alignment review before
implementation and one technical review for a produced diff. It may add other
obligations from persisted artifacts. The orchestrator may add review but
cannot remove supervisor obligations.

## Execute the Sealed Iteration

Make one blocking runtime call after the plan file is complete:

    multiagent subagent execute-iteration --plan-file PLAN_PATH --timeout 900

The runtime validates and records the plan digest, launches exactly one
digest-bound plan-alignment reviewer, and stops on misalignment. After alignment
it prepares the complete implementation context, schedules ready worker nodes,
waits and finalizes them,
checks the candidate against the union of owned paths, freezes the diff, asks
the supervisor for review obligations, launches independent reviewers in
parallel, records their structured evidence, and requests supervisor
completion. Do not duplicate any of these commands around the executor.

The review is mechanically bound to both the sealed plan digest and the
authenticated original-task digest. This binding proves which inputs were
reviewed; it is not a substitute contract or a separate authority decision.
Reviewer access remains mechanically read-only, and worker writes remain
bounded by assignments.

The executor returns one `IterationExecutionResult` JSON object:

- `status=completed` means all supervisor correctness and safety gates passed.
- `status=needs_replan` means semantic control returns to the orchestrator. Read
  its reason and the durable finding artifacts, create or route the required
  TODO, transition through the documented repair loop, and submit a new plan
  for the next iteration.
- A command error is an infrastructure or invalid-contract failure. Inspect it
  once and correct the plan or environment; never launch a replacement reviewer
  merely because the first output or command was malformed.

Do not poll individual agents while `execute-iteration` is running, do not
replay its internal transitions, and do not spawn another final verifier after
it reports completion.
