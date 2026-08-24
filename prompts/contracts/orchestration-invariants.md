# Orchestration Invariants

This document is a regression index, not an agent prompt. Runtime gates and the
named role/playbook modules own enforcement and procedure.

## Role And Launch Boundaries

- Core Disciplines
- intent-contract.md covers proxy/scaffold risk.
- parallel-execution.md owns parallel planning.
- validation-scheduling.md owns validation coordination.
- agent-spawning.md owns the Required Worker First Instruction.
- contract-scout.md and acceptance-scout.md own scout contracts.
- scope-guard.md owns scope review.
- validation-coordinator.md owns duplicate validation prevention.
- Spawn read-only roles through: SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn

## Routing And Repair Boundaries

- Contract Scout Workflow
- Scope Guard Workflow
- Validation Coordinator Workflow
- Validation Failure Repair Workflow
- Safety Rules
- A failed relevant validation is repair evidence, not acceptance.
- finding-todo-loop.md owns todo-close and structured repair evidence.
- required-path-outside-owned: is an ownership blocker.
- At most one same-owned-path replacement is allowed.
- A live worker remains no-diff after a planning checkpoint only until an edit-or-blocker handoff.
- Do not let an active generic scout block a ready bounded worker.
- Record assignment-status NAME failed before replacing a killed owner.

## Evidence Boundaries

- Preserve historical-contract-ledger: in role instructions that consume it.
- Preserve source-owner-ledger: when source ownership is ambiguous.
- prompts/roles/build-verifier.md owns build-verification-passed: and final-diff binding.
- Build verification failures are not eval-wrapper paperwork.
