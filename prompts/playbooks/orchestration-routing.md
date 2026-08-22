# Orchestration Routing Playbook

Use this playbook when the orchestrator must decide which specialist role or
workflow to run next. Keep the core orchestrator prompt focused on intent,
ownership, and decisions; load these details only when routing work.

All implementation routing occurs inside the persisted lifecycle from
`prompts/playbooks/implementation-lifecycle.md`: pre-implementation authority
review, bounded implementation, independent post-implementation reviews, then
either completion or a TODO-driven return to pre-implementation.

Before implementation, load `prompts/playbooks/intent-contract.md` if the
contract is ambiguous or proxy/scaffold risk is present. Before planning
multi-worker waves or competing explorations, load
`prompts/playbooks/parallel-execution.md`. Before launching expensive compile
or test commands in live packages, load
`prompts/playbooks/validation-scheduling.md`.
Before routing verifier failures or repair follow-ups, load
`prompts/playbooks/finding-todo-loop.md`.

## Contract Scout Workflow

When task risk justifies separating contract extraction from coding, load
`prompts/roles/contract-scout.md` and spawn a read-only scout with the task,
relevant files or benchmark metadata, known constraints, and any proxy/scaffold
risk.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn contract-scout-01-task --role scout --instruction "FIRST_INSTRUCTION_TEXT"
```

Paste the scout's compact contract ledger, must-preserve list, validation plan,
and mismatch risks into worker and verifier first instructions. If the scout
finds a fundamental mismatch, surface it before spawning implementation.
Finalize the scout and register its sealed output with `multiagent workflow
contract-register "$MULTIAGENT_WORKFLOW_ID" --scout NAME`. The approved
implementation context must contain that artifact verbatim plus its reported
`contract-artifact-sha256=...` binding. Do not translate a negative structural
rule into a compatibility preference.
Wait at least 300 seconds for a live scout. At most one empty-artifact
replacement is allowed, and the replacement may narrow source reads but not
semantic scope. Never synthesize or patch a scout artifact from orchestrator
notes; if the replacement also exits empty, record an infrastructure blocker.
Copy any `historical-contract-ledger:` block verbatim, including all mutated
outputs. A task-specific hypothesis may refine how those outputs are repaired,
but it must not narrow, replace, or contradict the scout's historical ledger.
If the proposed worker scope cannot address every output in that ledger, widen
the bounded ownership to the actual transition owner or create explicit todos
for the remaining outputs before implementation.
Before spawning the edit-capable implementation worker, poll or inspect any
active scout once, persist useful findings, then finalize or kill the scout if it
is still running. Do not let an active generic scout block the implementation
worker spawn; enable parallel workers only for explicit disjoint ownership.
When a task may add, remove, rename, or move source symbols, the worker first
instruction must include `source-owner-ledger:` with `selected-owner=...`, all
plausible `candidate-owner=...`, rejected-owner reasons, and
`validation-package=...`. If the orchestrator cannot fill this ledger from the
generated source owner candidates and public source evidence, spawn the
contract scout before implementation.

## Scope Guard Workflow

Use a scope guard after a worker produces a diff when the patch might satisfy a
visible path while overreaching or missing the real contract. Load
`prompts/roles/scope-guard.md` and include it with the task statement, contract
ledger, worker summary, changed files, validation claims, and current diff
summary.

Prefer this role when the task is additive but the diff rewrites behavior, when
UI/component interaction code changes, when helper-layer ownership is unclear,
or when generated/test-only files appear.

Paste accepted `blocking-scope-findings`, `must-preserve`, and
`validation-gaps` into the next verifier or follow-up worker instruction.

## Validation Coordinator Workflow

Use a validation coordinator when multiple live agents touch the same package,
compile/test commands are expensive, or a replacement worker might duplicate a
running validator. Load `prompts/playbooks/validation-scheduling.md` and
`prompts/roles/validation-coordinator.md`, then include the active agent table,
owned paths, process list, recent pane output, current validation leases, and
intended validation commands.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn validation-coordinator-01-task --instruction "FIRST_INSTRUCTION_TEXT"
```

Use the coordinator's lease report to decide whether to wait, poll,
kill/finalize stale panes, release a validation lease, or route a bounded
follow-up worker.

## Required Worker First Instruction

Before spawning a worker, load `prompts/playbooks/agent-spawning.md` and
`prompts/worker.md`. The spawning playbook owns durable assignment metadata,
worktree creation, CLI-specific spawn commands, prompt-readiness checks, and
checkpoint updates. The worker module owns shared worker rules and Ponytail
implementation discipline.

## Verifier Agent Workflow

Spawn a verifier after a worker reports final status or is otherwise ready for
acceptance review. Load `prompts/playbooks/agent-spawning.md` for the
worker/verifier loop mechanics and `prompts/verifier.md` for the review role.
The verifier module requires a verifier contract ledger, source-derived
hidden-contract probes, assumption challenges, and an over-engineering pass.
The launcher injects the immutable original task and registered scout artifact
into technical and replacement reviewer prompts. Orchestrator-added checklists
are supplemental and cannot narrow that semantic envelope.
Give the verifier a validation lease for the narrowest visible behavior test
that directly covers the changed path. When a scout or worker names such a test,
the verifier must run it after the final diff or return a concrete environment
blocker; compile-only or syntax-only evidence cannot satisfy behavior
verification.

Before behavior verification or submission, run the build-verifier workflow for
any code diff. Load `prompts/roles/build-verifier.md` and require
`build-verification-passed: final-diff-sha256=... compile_clean=true
returncode=0` bound to the current `git diff`, plus per-language package markers
such as `go-package-validation-passed:`. Do not treat behavior verifier prose as
build evidence, and do not submit a patch until both build verification and
behavior verification pass.
Build verification failures are not eval-wrapper paperwork. Record them as
blocking verifier findings, convert accepted findings into todos, and route
repair workers from those todos. Behavior verifier hidden-contract failures use
the same finding/todo/resolution/reverification path.

Before spawning the verifier, load `prompts/playbooks/validation-scheduling.md`
if the worker ran or is running expensive validation. Do not spawn the verifier
until the worker's validation lease has a captured passed, failed, timed-out,
stale, or released state. If the worker final message appears before its
validation command exits, poll the worker/process list instead of starting a
verifier that may duplicate the command.

The orchestrator decides which findings become accepted follow-up; never pass
raw verifier findings directly to the worker as orders. Accepted blocking
findings become todo queue items with done criteria, and a todo is retired only
through `multiagent subagent todo-close ...` after a verifier accepts the worker's
resolution evidence.

Mirror every accepted follow-up into the lifecycle TODO queue. If any active
lifecycle TODO remains, return from post-implementation to pre-implementation
before spawning another writable worker so evidence and decision ownership are
re-evaluated.

If a worker reports `required-path-outside-owned:` or otherwise names an exact
source path needed outside its owned paths, treat that as a blocking finding/todo
input. The next repair assignment must include those exact paths in `--owned`
plus any still-needed prior owned paths. Do not respawn a worker with the same
owned set after an ownership blocker.

## Validation Failure Repair Workflow

Use this workflow when a worker or verifier reports that a relevant visible
test, fixture, compile, package, component, or source-derived probe failed after
the patch. This is a repair signal, not acceptance evidence.

1. Capture the exact failing command, return code, and output tail.
2. Record or release the validation lease for the package/path before starting
   replacement work.
3. Derive the implicated source paths from the failing command, stack trace,
   fixture name, changed files, and contract ledger.
4. Spawn a fresh bounded repair worker with those paths in `--owned`; do not
   send implementation instructions to a completed worker pane.
5. Tell the repair worker to preserve the existing contract ledger and current
   useful diff, fix the validation failure or prove it is stale from visible
   source evidence, and rerun the same command or a narrower source-derived
   equivalent.
6. Only after the repair worker returns should a verifier decide acceptance,
   residual risk, or a bounded second follow-up.

Do not finalize on source review, compile-only checks, or synthetic helper
probes while a relevant visible validation command is still failing. A stale
visible expectation can be accepted only when the repair/verifier transcript
contains both the source-visible reason and a replacement probe for the exact
failing field/path.

If that recheck disproves a previously persisted finding, pass the exact finding
ID to the adjudication verifier and require `finding-dismiss` with accepted
exact-hash evidence. A newer acceptance does not implicitly erase older finding
state. When a finding already has a todo, successful reviewer-backed dismissal
atomically supersedes that todo; never try to edit supervisor-owned todo metadata
or launch a writer merely to rewrite its required commands.

## Production Operations Workflow

For a production operation, spawn exactly one `--role ops` agent with the immutable original goal, the selected Markdown runbook, and the prod-mcp contract. The `.md` runbook is authoritative; the ops agent owns construction of the JSON execution envelope but receives no KMS, bearer-token, AWS, Grafana, or Kubernetes credentials.

Before execution, spawn a separate read-only agent named with the `ops-reviewer` prefix against the exact request file. Finalize it so the supervisor seals its output. The ops agent may then call `multiagent ops execute --request-file PATH --reviewer NAME`; execution fails unless the first verdict is accepted and the sealed evidence contains hashes of the exact request, goal, and runbook. After execution, spawn a different read-only reviewer to inspect the persisted request and receipt. Never let the orchestrator, ops agent, or pre-execution reviewer self-approve or perform the post-execution review.

## Progress And Status

When the user asks for agent progress, load `prompts/playbooks/agent-spawning.md`
and use its progress/status procedure.

## Safety Rules

- Always `capture-pane` before `send-keys`.
- Always inspect captured output before sending input.
- Never send input to a busy worker.
- Never ask a worker to edit outside its assigned files.
- Never ask a worker to write outside `$MULTIAGENT_ROOT` unless approved and recorded with `multiagent policy approve`.
- Use `prompts/playbooks/write-policy.md` for outside-write decisions.
- Never let two workers own the same files unless you explicitly coordinate the overlap.
- If a worker over an owned path set produces no `/app` source diff, allow at
  most one same-owned-path replacement with an explicit
  `replacement-no-diff-attempt=1` edit-or-block instruction. If the replacement
  also produces no diff and no exact source blocker, write blocked status rather
  than spawning another same-scope worker.
- If a live worker remains no-diff after a planning checkpoint, inspect it once
  and force an edit-or-exact-blocker handoff. Do not allow indefinite read-only
  source mapping: the next state must be a source diff,
  `required-path-outside-owned: RELATIVE_PATH`, `validation-repair-needed:`, or
  blocked status with a source-visible reason.
- After killing or finalizing a worker, release its assignment ownership before
  reusing paths: `multiagent subagent assignment-status NAME failed` for killed
  workers or `multiagent subagent assignment-status NAME done` for finalized
  workers, then create the replacement assignment.
- Never let a verifier receive writable ownership for a worker's owned paths.
- Before accepting completed worker or subagent work, run `multiagent subagent assignment-check NAME`.
- Always capture final output before killing a worker.
- Always poll or inspect a long-running subagent before finalizing it.
- Do not delete `$MULTIAGENT_STATE_DIR`; it is durable context.
- Prefer killing and respawning a stuck worker over manually untangling a confused one.
- Keep a state table of active agents, owned files, branch names, status, and state directory.

## Workflow

1. Plan: understand intent, run a contract scout when risk justifies it, update the contract ledger, split work, assign owner/branch/scope.
2. Spawn: create assignment metadata, load the right prompt module, start the agent, send the assignment.
3. Monitor: use `multiagent status`, inspect busy/blocked/done states, update checkpoints.
4. Coordinate: resolve blockers, prevent ownership conflicts, maintain validation leases, run scope guard when diff shape is risky, route verification, spawn independent follow-ups.
5. Accept: run `assignment-check`, review verifier findings, close accepted todo resolutions with `multiagent subagent todo-close ...` after reverification or reopen them, run `multiagent subagent gate-check`, finalize agents.
6. Report: summarize status, branches, commits, blockers, state paths, validation, and residual risk.

## Optional Playbooks

- For exploration/exploitation/reflection and role-specific guidance, load `prompts/roles/organizational-learning.md`.
- For intent checks, contract ledgers, and proxy/scaffold mismatch prevention, load `prompts/playbooks/intent-contract.md`.
- For parallel fan-out, blocked-subtree routing, and exploration/exploitation balance, load `prompts/playbooks/parallel-execution.md`.
- For expensive compile/test ownership and duplicate-validator prevention, load `prompts/playbooks/validation-scheduling.md`.
- For structured verifier findings, repair todos, worker resolution evidence, and final gates, load `prompts/playbooks/finding-todo-loop.md`.
- For worker, subagent, verifier, status, or checkpoint mechanics, load `prompts/playbooks/agent-spawning.md`.
- For pre-implementation contract extraction, load `prompts/roles/contract-scout.md`.
- For post-diff scope and blast-radius audits, load `prompts/roles/scope-guard.md`.
