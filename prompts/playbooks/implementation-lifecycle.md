# Implementation Lifecycle Playbook

This playbook is mandatory for every orchestrated task. The launcher includes
it in the orchestrator's initial prompt. It is the canonical authority for task
phases, transitions, TODO convergence, and completion; role and routing
playbooks must not weaken its gates.

## Durable State

Read the active workflow before routing work:

```bash
multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
```

Do not infer the current phase from conversation history. Use the persisted
phase and record every transition with `multiagent workflow transition`.

## Phase Machine

The only normal lifecycle is:

```text
pre-implementation -> implementation -> post-implementation
post-implementation -> pre-implementation  when active TODOs remain
post-implementation -> complete            when terminal gates pass
```

Never route a post-implementation finding directly to implementation. Add it to
the TODO queue, return to pre-implementation, and reconsider evidence,
decisions, authority, and the approved implementation context first.

## Pre-Implementation

For every active TODO, determine whether it is:

- direct implementation under an already approved contract;
- factual uncertainty requiring bounded evidence collection; or
- a choice requiring a decision and authority classification.

Group TODOs that depend on the same choice. Record alternatives, assumptions,
evidence, and the proposed choice. Evidence collection must state its question,
sources, expected signal, and stop condition.

Use `multiagent decision` for durable alternatives, assumptions, the committed plan,
and later reflection. The lifecycle record is the phase/authority gate around
that decision ledger; it does not replace the ledger.

A decision is user-owned when it changes public behavior or contracts, roles or
responsibilities, persisted state or migration, security or trust boundaries,
destructive or difficult-to-reverse behavior, material scope or cost, or a
prior explicit user decision. Treat uncertain authority as user-owned. Evidence
may clarify a choice but does not transfer authority.

The user's explicit task contract is already approved public behavior. Do not
ask the user to choose it again or reinterpret repository alternatives as an
unanswered product decision. A user-owned question exists only when two
materially different outcomes both remain consistent with the complete request
after bounded source/test inspection. Otherwise select the narrowest
backward-compatible implementation of the stated contract and record that
source-backed choice as orchestrator-owned.

For consequential or uncertain decisions, run the independent
`decision-authority-reviewer` role. It must check both the proposed authority
and whether the TODOs or proposed assignment contain omitted decisions. Ask the
user before committing any user-owned decision.

Spawn that review read-only through the normal subagent path, for example:

```bash
SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn decision-authority-reviewer \
  --role reviewer --instruction-file AUTHORITY_REVIEW_INPUT
multiagent subagent wait decision-authority-reviewer --timeout 900
```

Do not continue merely because an immediate poll still reports `running`.
Inspect the completed or blocked result after the bounded wait and persist its
actual authority finding before preparing implementation.

Create an approved implementation context document containing the selected
plan, decision and plan IDs, authority and approval basis, intended outcome,
rejected alternatives and reasons, must-do and must-not-do constraints, migration choice,
responsibility boundary, affected paths, unresolved questions, and revision.
Commit the selected alternative with `multiagent decision commit`, then record the
passed authority review and approved context with:

```bash
multiagent workflow prepare-implementation "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DECISION_ID \
  --plan-id PLAN_ID \
  --decision-revision REVISION \
  --implementation-context CONTEXT_PATH \
  --authority-review REVIEW_ID
multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" implementation
```

Do not leave active evidence or decision TODOs when entering implementation.
Direct implementation TODOs may remain active and must be assigned to bounded
workers.

## Implementation

Spawn bounded write-capable workers only after the implementation gate passes.
Choose their count and responsibilities from the approved plan rather than a
framework default. Concurrent writers must have disjoint owned paths.
Every assignment must reference the active workflow, decision, and plan. The
worker's first instruction must contain the complete current approved
implementation context; a decision ID alone is insufficient.

Do not silently change the approved plan. A newly discovered choice or factual
uncertainty becomes a TODO and returns through pre-implementation.

When implementation stops, capture worker output, stop or freeze every writer,
record the candidate diff hash, and enter post-implementation:

```bash
multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" post-implementation \
  --diff-hash DIFF_HASH
```

## Post-Implementation

Run independent reviews against the frozen candidate diff:

- `decision-drift`: compare the diff to the authorized implementation context;
- `scope`: check scope, simplicity, ownership, and unnecessary complexity;
- `technical`: verify behavior and the accepted contract;
- `reflection`: compare expected and actual results and identify improvements.

Every reviewer final message must include an exact durable marker on its own
line: `review-record: type=TYPE verdict=pass|findings diff=DIFF_HASH` (use
`diff=-` for decision-authority). A Markdown list prefix or enclosing backticks
are accepted as cosmetic formatting, but surrounding prose is not. Wait for and finalize that reviewer before
recording its result. Record each review with `multiagent workflow
record-review ... --reviewer REVIEWER_NAME`; the supervisor rejects an
orchestrator-authored verdict that is not backed by the finalized read-only
reviewer's matching marker. The supervisor also scans finalized reviewer
messages: a findings marker for the current candidate cannot be discarded by
launching a replacement reviewer and recording only the later pass. Record the
finding, route it through the repair loop, and begin a new lifecycle iteration
before attempting completion. Every actionable finding must be added with
`multiagent workflow add-todo`; a review with findings is not a terminal review.

Technical verifier findings must also use the existing structured
`finding-create -> todo-create -> resolution-create -> todo-close` protocol in
`prompts/playbooks/finding-todo-loop.md`. Mirror each accepted repair item into
the lifecycle queue using the finding or TODO ID as `--origin`. Resolve the
lifecycle item only after the structured repair evidence passes. The lifecycle
queue governs iteration and decision reconsideration; the structured finding
store governs technical closure.

Resolve a TODO only as:

- `completed`, with implementation and validation evidence; or
- `skipped`, with `out-of-scope` or `unavailable-now`, a concrete reason,
  evidence, deciding authority, and a destination or resume condition when the
  work remains relevant.

Do not use a skip to weaken the accepted contract. User approval is required
to skip a user-owned requirement or accept user-visible residual risk.

If active TODOs remain, return to pre-implementation:

```bash
multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" pre-implementation
```

This increments the iteration and invalidates the prior implementation permit.

## Completion

Complete only when every TODO is completed or validly skipped, no user-owned
decision is unanswered, and all four required reviews pass against the current
candidate diff hash:

```bash
multiagent orchestrator complete
```

This is only a completion request. The supervisor holds the lifecycle lock,
runs the lifecycle completion check and `subagent gate-check`, and only then
atomically writes `phase=complete`. Direct `workflow transition ... complete`
is forbidden. A rejected request leaves the workflow in post-implementation so
the orchestrator can route repairs.
After it succeeds, the candidate is sealed: stop launching workers or reviewers
and do not mutate the repository. The privileged writer bridge independently
rechecks the live lifecycle phase and rejects any post-completion writer, even
if a shell command overrides `MULTIAGENT_LIFECYCLE_ENFORCEMENT`.

`MULTIAGENT_VERIFIER_MAX_ITERATIONS` is an escalation threshold, not an
acceptance condition. At the threshold, reconsider the route, surface a
blocker, or ask the user. Never accept merely because the threshold was reached.
