# Orchestration Routing Playbook

Use this playbook when the orchestrator must decide which specialist role or
workflow to run next. Keep the core orchestrator prompt focused on intent,
ownership, and decisions; load these details only when routing work.

Before implementation, load `prompts/playbooks/intent-contract.md` if the
contract is ambiguous or proxy/scaffold risk is present. Before planning
multi-worker waves or competing explorations, load
`prompts/playbooks/parallel-execution.md`. Before launching expensive compile
or test commands in live packages, load
`prompts/playbooks/validation-scheduling.md`.

## Contract Scout Workflow

When task risk justifies separating contract extraction from coding, load
`prompts/roles/contract-scout.md` and spawn a read-only scout with the task,
relevant files or benchmark metadata, known constraints, and any proxy/scaffold
risk.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn contract-scout-01-task --instruction "FIRST_INSTRUCTION_TEXT"
```

Paste the scout's compact contract ledger, must-preserve list, validation plan,
and mismatch risks into worker and verifier first instructions. If the scout
finds a fundamental mismatch, surface it before spawning implementation.
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
SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn validation-coordinator-01-task --instruction "FIRST_INSTRUCTION_TEXT"
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

Before behavior verification or submission, run the build-verifier workflow for
any code diff. Load `prompts/roles/build-verifier.md` and require
`build-verification-passed: final-diff-sha256=... compile_clean=true
returncode=0` bound to the current `git diff`, plus per-language package markers
such as `go-package-validation-passed:`. Do not treat behavior verifier prose as
build evidence, and do not submit a patch until both build verification and
behavior verification pass.

Before spawning the verifier, load `prompts/playbooks/validation-scheduling.md`
if the worker ran or is running expensive validation. Do not spawn the verifier
until the worker's validation lease has a captured passed, failed, timed-out,
stale, or released state. If the worker final message appears before its
validation command exits, poll the worker/process list instead of starting a
verifier that may duplicate the command.

The orchestrator decides which findings become accepted follow-up; never pass
raw verifier findings directly to the worker as orders.

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

## Progress And Status

When the user asks for agent progress, load `prompts/playbooks/agent-spawning.md`
and use its progress/status procedure.

## Safety Rules

- Always `capture-pane` before `send-keys`.
- Always inspect captured output before sending input.
- Never send input to a busy worker.
- Never ask a worker to edit outside its assigned files.
- Never ask a worker to write outside `$MULTIAGENT_ROOT` unless approved and recorded with `bin/write-policy.sh approve`.
- Use `prompts/playbooks/write-policy.md` for outside-write decisions.
- Never let two workers own the same files unless you explicitly coordinate the overlap.
- Never let a verifier receive writable ownership for a worker's owned paths.
- Before accepting completed worker or subagent work, run `bin/subagent.sh assignment-check NAME`.
- Always capture final output before killing a worker.
- Always poll or inspect a long-running subagent before finalizing it.
- Do not delete `$MULTIAGENT_STATE_DIR`; it is durable context.
- Prefer killing and respawning a stuck worker over manually untangling a confused one.
- Keep a state table of active agents, owned files, branch names, status, and state directory.

## Workflow

1. Plan: understand intent, run a contract scout when risk justifies it, update the contract ledger, split work, assign owner/branch/scope.
2. Spawn: create assignment metadata, load the right prompt module, start the agent, send the assignment.
3. Monitor: use `bin/status.sh`, inspect busy/blocked/done states, update checkpoints.
4. Coordinate: resolve blockers, prevent ownership conflicts, maintain validation leases, run scope guard when diff shape is risky, route verification, spawn independent follow-ups.
5. Accept: run `assignment-check`, review verifier findings, decide accepted follow-up, finalize agents.
6. Report: summarize status, branches, commits, blockers, state paths, validation, and residual risk.

## Optional Playbooks

- For exploration/exploitation/reflection and role-specific guidance, load `prompts/roles/organizational-learning.md`.
- For intent checks, contract ledgers, and proxy/scaffold mismatch prevention, load `prompts/playbooks/intent-contract.md`.
- For parallel fan-out, blocked-subtree routing, and exploration/exploitation balance, load `prompts/playbooks/parallel-execution.md`.
- For expensive compile/test ownership and duplicate-validator prevention, load `prompts/playbooks/validation-scheduling.md`.
- For worker, subagent, verifier, status, or checkpoint mechanics, load `prompts/playbooks/agent-spawning.md`.
- For pre-implementation contract extraction, load `prompts/roles/contract-scout.md`.
- For post-diff scope and blast-radius audits, load `prompts/roles/scope-guard.md`.
