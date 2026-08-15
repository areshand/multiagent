# Agent Spawning Playbook

Use this playbook whenever the orchestrator is about to create, monitor,
replace, verify, or finalize worker windows or named subagents.

## Worker First Instruction

Before spawning a worker, load `prompts/worker.md` and prepend it to the
task-specific assignment. Also pass assignment ID, branch, owned paths, task
statement, and the relevant contract ledger. For high-risk coding tasks,
include the contract scout's `must-preserve` list and validation plan. The
worker module contains shared worker rules and Ponytail implementation discipline.
For lifecycle-enforced exploitation work, also include the active workflow,
decision, plan, decision revision, and the complete approved implementation context.
When the scout emits `historical-contract-ledger:`, copy that block verbatim
into every implementation, repair, and verifier assignment. Do not replace it
with a narrower locked hypothesis. Worker ownership and done criteria must
cover every listed mutated output or explicitly preserve an open blocking todo
for outputs assigned elsewhere.

## Worker Spawn Skill

Before spawning a worker, create durable assignment metadata:

```bash
multiagent subagent assignment-create worker-01-task \
  --assignment-id ASSIGNMENT_ID \
  --role exploitation \
  --workflow-id "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DECISION_ID \
  --plan-id PLAN_ID \
  --branch BRANCH \
  --owned PATH[,PATH...]
multiagent subagent checkpoint-update worker-01-task --step "assignment created" --status assigned
```

For the normal single-writer path, spawn through the Rust supervisor in the
shared target workspace. The trusted worker role receives workspace-write
access while the orchestrator remains unable to edit that workspace:

```bash
SUBAGENT_CLI="$WORKER_CLI" multiagent subagent spawn worker-01-task \
  --role worker --instruction-file WORKER_INSTRUCTION
multiagent subagent wait worker-01-task --timeout 1800
```

The supervisor handles readiness and capture. Inspect a terminal `blocked` or
`failed` result instead of treating it as completion. Separate git worktrees
remain available for intentionally parallel, disjoint assignments, but require
an explicit integration step before completion; do not use them for the normal
SWE single-writer path.

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

## Verifier Agent Workflow

Spawn a verifier after a worker reports final status or is otherwise ready for
acceptance review. Load `prompts/verifier.md` and include it in the verifier's
first instruction with worker name, assignment ID, branch, owned paths, relevant
commit hash, task statement, contract ledger, and verifier iteration number.
For tasks that used a contract scout, include the scout's contract ledger and
validation plan as normative review input.
Load `prompts/playbooks/finding-todo-loop.md` whenever the verifier may produce
blocking repair work. Blocking verifier findings must be recorded as structured
finding artifacts before the orchestrator turns them into bounded repair todos.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn verifier-01-task --instruction "FIRST_INSTRUCTION_TEXT"
```

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

If a live worker remains no-diff after a planning checkpoint, inspect it once and
force an edit-or-exact-blocker handoff. Do not let read-only source mapping
continue indefinitely: the next state must be a source diff,
`required-path-outside-owned: RELATIVE_PATH`, `validation-repair-needed:`, or
blocked status with a source-visible reason.

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
