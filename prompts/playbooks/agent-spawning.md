# Agent Spawning Playbook

Use this playbook whenever the orchestrator is about to create, monitor,
replace, verify, or finalize worker windows or named subagents.

## Worker First Instruction

The launcher automatically injects the canonical worker role prompt and the
supervisor-owned semantic envelope. Do not load, copy, or prepend
`prompts/worker.md`. Lifecycle-enforced workers must use `--instruction-file`
with the complete approved implementation context followed by only a concise,
task-specific assignment; an inline instruction cannot satisfy that mechanical
gate. Pass assignment ID, owned paths, done criteria, and any execution detail
not already present in the approved context. Before passing `--branch`, resolve
the exact current branch with `git branch --show-current`; never use a
placeholder such as `workspace`. Omit the optional flag if no branch is
resolved. The launcher separately supplies the original task and any registered
contract artifact. Never reconstruct, paraphrase, or paste a second copy of
that artifact beyond the exact copy required inside the approved context. This
includes every `historical-contract-ledger:` block. Worker ownership and done
criteria must cover every required mutated output or explicitly preserve an
open blocking todo for outputs assigned elsewhere. The injected worker module
contains shared worker rules and Ponytail implementation discipline.

Before creating assignment metadata, compare the worker's owned paths and hard
constraints with the approved implementation context. The assignment may split
the approved plan across coordinated TODOs, but it must not silently narrow or
contradict that plan. In particular, if the approved plan or contract ledger
requires updating visible tests, fixtures, callers, generated files, or other
outputs, either include those paths in this worker's ownership or assign them
to another active TODO. Never forbid a required path and then accept the
resulting partial diff or failed validation as completion.

## Worker Spawn Skill

Create durable assignment metadata and launch the worker with one atomic Rust
CLI operation. Do not issue a separate `assignment-create` concurrently with
`spawn`; doing so creates an avoidable race between authority registration and
worker launch:

```bash
SUBAGENT_CLI="$WORKER_CLI" multiagent subagent spawn worker-01-task \
  --role worker \
  --own PATH[,PATH...] \
  --assignment-id ASSIGNMENT_ID \
  --workflow-id "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DECISION_ID \
  --plan-id PLAN_ID \
  --branch BRANCH \
  --instruction-file WORKER_INSTRUCTION
multiagent subagent wait worker-01-task --timeout 1800
```

The supervisor creates the assignment under its lock, completes authority
registration, and only then launches the trusted workspace-write worker. The
orchestrator remains unable to edit the target workspace. Inspect a terminal
`blocked` or `failed` result instead of treating it as completion. Separate git
worktrees remain available for intentionally parallel, disjoint assignments,
but require an explicit integration step before completion; do not use them for
the normal SWE single-writer path.

## Long-Running Subagent Skill

Prefer `multiagent subagent spawn` for named long-running subagents because it
persists context:

```bash
multiagent subagent spawn subagent-build-watch --instruction "FIRST_INSTRUCTION_TEXT"
multiagent subagent wait subagent-build-watch --timeout 900
multiagent subagent inspect subagent-build-watch --lines 160
multiagent subagent finalize subagent-build-watch
```

For a bounded worker in the current worktree, `spawn` can create the durable
assignment and worker in one command:

```bash
multiagent subagent spawn worker-02-repair \
  --own src/affected/,tests/affected/ \
  -- "FIRST_INSTRUCTION_TEXT"
```

If that worker already has an assignment, every requested path must be covered
by its existing ownership. The shorthand never widens existing ownership.

Use `checkpoint-update NAME --step TEXT --status STATUS` after meaningful
progress, before stopping, and whenever a blocker appears.

## Scout To Worker Handoff

Read-only scouts are temporary evidence gatherers. Before spawning the first
edit-capable worker, poll or inspect any active scout once, persist the useful
ledger/findings, then finalize or kill the scout if it is still running. Do not
let an active generic scout block `multiagent subagent spawn` for the implementation
worker. Use `MULTIAGENT_ALLOW_PARALLEL_WORKERS=1` only when you intentionally
want parallel disjoint workers and have recorded non-overlapping ownership.

For a contract scout, finalize it and register its sealed output before any
worker or reviewer starts:

```bash
multiagent subagent finalize CONTRACT_SCOUT_NAME
multiagent workflow contract-register "$MULTIAGENT_WORKFLOW_ID" \
  --scout CONTRACT_SCOUT_NAME
```

Copy the registered artifact verbatim into the approved implementation context,
including its `contract-artifact-sha256=...` binding. Do not paraphrase or
replace individual `must` or `must-not` rules. The launcher automatically
injects the supervisor-owned original task and registered contract into every
later worker and reviewer instruction.

Give a live contract scout one bounded wait of at least 300 seconds before
classifying it as stalled. Do not kill or finalize a running scout merely
because one short poll has no final message. If it exits with an empty sealed
artifact, allow at most one replacement with a narrower source list and an
explicit "return the structured artifact before any ninth tool call" reminder.
If that replacement also has no artifact, stop with a recorded infrastructure
blocker. The orchestrator must never author, patch, copy, reconstruct, or force
an environment bypass for scout output; only supervisor-sealed scout bytes may
be registered.

## Verifier Agent Workflow

Spawn a verifier after a worker reports final status or is otherwise ready for
acceptance review. The launcher automatically injects the canonical verifier
role prompt, supervisor-owned original task, approved context, registered
contract, and frozen diff binding. Do not load, copy, or include
`prompts/verifier.md` in the first instruction. Supply only the task-specific
review type, worker or assignment identity, changed paths, relevant commit, and
iteration. Use `--role reviewer` for every post-implementation reviewer whose
evidence will be submitted with `multiagent workflow record-review`; the
supervisor accepts review evidence only from that read-only role. Load
`prompts/playbooks/finding-todo-loop.md` whenever a verifier may produce
blocking repair work. Blocking verifier findings must be recorded as structured
finding artifacts before the orchestrator turns them into bounded repair todos.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn technical-verifier-01-task \
  --role reviewer --instruction "TASK_SPECIFIC_REVIEW_INSTRUCTION"
```

Use a `verifier-` identity for the technical obligation so the durable
acceptance also satisfies `multiagent subagent gate-check`; do not add a second
final verifier after the technical obligation passes.

Run `multiagent subagent assignment-check WORKER_NAME` before relying on verifier
results. Resolve branch or file ownership rejection before verification.

Use the configurable iteration cap:

```bash
MAX_ITERATIONS="${MULTIAGENT_VERIFIER_MAX_ITERATIONS:-3}"
```

Stop the worker/verifier loop only when no accepted follow-up remains. Reaching
`MAX_ITERATIONS` is an escalation threshold: reconsider the route, surface a
blocker, or ask the user. It never permits acceptance while required work or
unanswered user-owned decisions remain.

The verifier module requires a verifier contract ledger, source-derived
hidden-contract probes, assumption challenges, and the instruction to Run a
Ponytail over-engineering pass.
The orchestrator decides which findings become accepted follow-up; never pass
raw verifier findings directly to the worker as orders. Convert accepted
blocking findings into `multiagent subagent todo-create ...` records with objective
done criteria, assign workers from open todos, require worker resolution
evidence, then close the todo with `multiagent subagent todo-close ...` only after
verifier recheck. `resolved` is a handoff state, not acceptance.

When a worker says `required-path-outside-owned:` or names a required path
outside its assignment, the next todo/worker must own that exact path. Preserve
the relevant previous owned paths if they still contain the active diff or call
site. Never spawn a replacement worker with the same owned path set after an
ownership blocker.

When a worker exits or is killed with no materialized source diff, same-owned-path
replacement is allowed at most once. The replacement instruction must say
`replacement-no-diff-attempt=1`, must include an edit-or-block requirement, and
must start from the narrowest source-visible hypothesis. If that replacement also
produces no diff and no exact outside-owned path/source blocker, stop the loop:
write blocked status with the no-diff worker names, owned paths, and concrete
source discovery gap. Do not spawn worker-03/worker-04 over the same owned path
set without a new verifier finding, failed validation command, or exact
source-derived ownership blocker.

The same semantic-preservation rule applies when replacing stalled reviewers.
You may narrow commands, timeout, or runtime/file inspection for an operational
reason, but may not narrow the original task, registered contract rules, issue
clauses, or acceptance meaning. Every replacement receives the same
supervisor-owned semantic envelope automatically and must cover it rather than
a plan-confirming checklist.

If a live worker remains no-diff after a planning checkpoint, inspect it once and
force an edit-or-exact-blocker handoff. Do not let read-only source mapping
continue indefinitely: the next state must be a source diff,
`required-path-outside-owned: RELATIVE_PATH`, `validation-repair-needed:`, or
blocked status with a source-visible reason.

## Ops Review Continuation

Use one ops identity per session. Load
`prompts/playbooks/reviewed-ops-cycle.md` and invoke its supervisor-owned command
for each immutable request. Do not manually reproduce the reviewer, binding,
restore, or wait sequence, and never create a replacement ops identity.

After `multiagent subagent kill NAME` or `multiagent subagent finalize NAME`, ensure the
assignment no longer owns paths before reusing them. If needed, run
`multiagent subagent assignment-status NAME failed` for killed workers or
`multiagent subagent assignment-status NAME done` for finalized workers before
creating the replacement assignment.

Before final acceptance, run:

```bash
multiagent subagent gate-check
```

Do not accept while required findings are unqueued or repair todos are open,
assigned, resolved, or reopened. A closed todo must have both worker resolution
evidence and verifier closure evidence.

## Progress And Status

When the user asks for agent progress, run:

```bash
multiagent status
```

Report only actual agents: worker windows and named subagents. Exclude the
orchestrator. If the helper fails, fall back to `tmux list-windows`,
`tmux capture-pane` for each non-orchestrator worker, and
`multiagent subagent poll NAME` for named subagents.
