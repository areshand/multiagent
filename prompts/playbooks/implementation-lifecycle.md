# Implementation Lifecycle Playbook

This mandatory lifecycle applies to source implementation. Production actions
use the selected Markdown runbook and ops review path instead.

Do not use this lifecycle for an external-only task that does not modify
repository source. In particular, do not create an implementation context,
spawn a decision-authority reviewer, or transition to `implementation` before
starting ops. Spawn the session's persistent ops identity directly and use
`prompts/playbooks/reviewed-ops-cycle.md` for its immutable requests.

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

The orchestrator chooses whether a scout is useful. Skip it when the original
task already specifies an exact bounded artifact schema and values and there is
no material source-visible uncertainty that can change the plan. A scout
artifact, once registered, is immutable input. With or without a scout, one
independent decision-authority reviewer must accept the proposed plan before
the supervisor can approve implementation. User-owned security,
public-contract, destructive, or difficult-to-reverse choices require user
approval.

Use this order exactly:

1. Initialize the decision, add its alternative or alternatives, and commit the
   selected plan.
2. Spawn exactly one reviewer named `decision-authority-reviewer-01` with
   `--role reviewer --workflow-id WORKFLOW_ID --decision-id DECISION_ID
   --plan-id PLAN_ID --decision-revision REVISION` and a concise task-specific
   instruction that enumerates
   the selected plan's exact outcome, constraints, owned paths, and prohibitions
   rather than only naming its decision ID. Do not pass worker assignment flags
   such as `--own`; the launcher injects the canonical decision-authority role
   prompt and semantic envelope.
3. Wait for and finalize that reviewer.
4. Confirm its accepted report contains the supervisor-supplied
   `decision-review: capsule-sha256=SHA256 verdict=pass` marker, then record its
   accepted evidence with this exact argument order (replace only the uppercase
   placeholders):

       multiagent workflow record-review "$MULTIAGENT_WORKFLOW_ID" REVIEW_ID --type decision-authority --verdict pass --evidence decision-authority-reviewer-01 --reviewer decision-authority-reviewer-01

   `WORKFLOW_ID` and `REVIEW_ID` are the two required positional arguments. Do
   not probe alternate argument orders after the reviewer has passed.
5. Only after `record-review` succeeds, prepare the implementation context and
   transition to implementation.

The supervisor generates the immutable decision capsule, seals it with the
reviewer evidence, and rejects a review or implementation permit when the
decision ID, selected plan, workflow revision, or capsule digest differs. The
orchestrator must never manufacture or edit that capsule.

Do not call `prepare-implementation` as a probe before the decision is committed
or the review is recorded. Do not spawn a replacement reviewer solely because
the first identity, role, or output was assembled incorrectly; correct the
orchestration command or report the concrete blocker. A semantic finding may
require a revised decision and a new reviewer.

Prepare an implementation context containing the goal, selected plan,
authority basis, constraints, exact target-repository paths, owned paths, and
unresolved risks. A control-plane context or instruction path never becomes an
implementation output path. If a contract
artifact exists, include its exact bytes and supervisor digest; never
paraphrase it.

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

Query persisted obligations once after freezing the diff and run exactly the
pending independent reviews against that same diff. Spawn all mutually
independent pending reviewers before waiting for any of them; then wait,
finalize, and record each result. Name each identity for its obligation and use
`--role reviewer --own CHANGED_PATHS`, for example
`technical-verifier-01` and `decision-drift-reviewer-01`. Reviewer access stays
mechanically read-only; `--own` binds assignment metadata to the frozen
candidate and is required by the launcher. Do not first attempt a spawn without
it. Do not serialize independent reviews, and do not launch a replacement merely
to correct a role metadata mismatch. Record only finalized reviewer evidence
with the exact required marker. Put the literal obligation marker and frozen
hash in each first instruction: `review-record: type=technical verdict=pass
diff=DIFF_HASH` for the technical verifier and `review-record:
type=decision-drift verdict=pass diff=DIFF_HASH` for the drift reviewer. The
role prompt must reproduce the assigned type.

Use these command shapes without exploratory variants:

    multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
    multiagent subagent spawn REVIEWER_NAME --role reviewer --own CHANGED_PATHS --workflow-id "$MULTIAGENT_WORKFLOW_ID" --instruction "REQUIRED_MARKER and bounded review scope"
    multiagent workflow record-review "$MULTIAGENT_WORKFLOW_ID" REVIEW_ID --type TYPE --verdict pass --diff-hash DIFF_HASH --evidence REVIEWER_NAME --reviewer REVIEWER_NAME

The technical verifier's acceptance is the final verifier acceptance. When the
persisted obligations and `gate-check` pass, do not spawn another final verifier.
A finding cannot be replaced by a later pass; add accepted findings to the TODO
queue and use finding-todo-loop.md for repair evidence.

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
