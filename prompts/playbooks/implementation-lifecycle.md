# Implementation Lifecycle Playbook

This mandatory lifecycle applies to source implementation. Production actions
use the selected Markdown runbook and ops review path instead.

## State Machine

Read persisted state with:

    multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"

Normal transitions are:

    pre-implementation -> implementation -> post-implementation
    post-implementation -> pre-implementation  when TODOs remain
    post-implementation -> complete            when supervisor gates pass

Never infer phase from conversation history or route a finding directly back
to implementation.

## Pre-Implementation

Clarify the intended outcome, required evidence, material choices, and bounded
ownership. The explicit task contract is already approved; ask the user only
when materially different outcomes remain consistent with it.

The orchestrator chooses whether a scout is useful. A scout artifact, once
registered, is immutable input. With or without a scout, an independent
decision-authority reviewer must accept the proposed plan before the supervisor
can approve implementation. User-owned security, public-contract, destructive,
or difficult-to-reverse choices require user approval.

Record the decision and prepare an implementation context containing the goal,
selected plan, authority basis, constraints, owned paths, and unresolved risks.
If a contract artifact exists, include its exact bytes and supervisor digest;
never paraphrase it.

    multiagent workflow prepare-implementation "$MULTIAGENT_WORKFLOW_ID" --decision-id DECISION_ID --plan-id PLAN_ID --decision-revision REVISION --implementation-context CONTEXT_PATH --authority-review REVIEW_ID
    multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" implementation

The supervisor rejects missing review evidence, changed context, or active
evidence and decision TODOs.

## Implementation

Spawn bounded workers only after the implementation permit passes. Include the
active workflow, decision, plan, complete approved context, and owned paths.
New uncertainty or a changed plan becomes a TODO and returns to
pre-implementation.

When writers stop, freeze the candidate and enter post-implementation:

    multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" post-implementation --diff-hash DIFF_HASH

## Post-Implementation

Query persisted obligations and run exactly the pending independent reviews
against the frozen diff. Record only finalized reviewer evidence with the exact
required marker. A finding cannot be replaced by a later pass; add accepted
findings to the TODO queue and use finding-todo-loop.md for repair evidence.

If TODOs remain:

    multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" pre-implementation

This invalidates the prior permit and begins a new reviewed iteration.

## Completion

Request completion only when TODOs are resolved, user-owned decisions are
answered, and every supervisor obligation passes against the current diff:

    multiagent orchestrator complete

The supervisor atomically accepts or rejects completion. Direct transition to
complete, post-completion writers, and acceptance based on iteration count are
forbidden.
