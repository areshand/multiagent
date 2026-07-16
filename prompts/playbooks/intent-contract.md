# Intent And Contract Playbook

Use this playbook before substantial work, especially coding tasks with unclear
scope, hidden-test risk, benchmark/eval implications, public API uncertainty, or
any chance that the obvious execution path only validates a proxy for the user's
real goal.

## Core Rule

Make the user's intended outcome explicit before implementation. Do not proceed
with a technically executable proxy if it only proves a scaffold, shim,
infrastructure path, or partial behavior while the user needs the real system,
artifact, or measurement.

## Contract Ledger

Maintain a lightweight contract ledger for each non-trivial task:

- intended outcome in concrete terms
- exact system, files, data, or behavior being measured or changed
- assumptions that must hold for the work to answer the user's real question
- required behavior, edge cases, invariants, and forbidden shortcuts
- validation signals that would prove the intended outcome
- known gaps, residual risks, and any proxy/scaffold limitations

The orchestrator owns the ledger and the final routing decision. It does not
need to build the ledger alone.

## Delegation

Spawn `prompts/roles/contract-scout.md` before implementation when contract
extraction would materially reduce risk. Paste the scout's `contract-ledger`,
`must-preserve`, `validation-plan`, and `mismatch-risk` into worker and verifier
first instructions.

Use a scout by default for:

- ambiguous user intent or incomplete task statements
- sparse public tests or likely hidden-test contracts
- benchmark/eval work where a scaffold result could be mistaken for product
  capability
- public API, serialized output, argv ordering, state, persistence, or error
  semantics that may be tested exactly
- broad helper-layer or component-interaction blast radius

If the scout or orchestrator finds that the current path cannot satisfy the
user's intent, surface that mismatch before spawning implementation. Redirect
the work rather than producing a result that looks complete but answers the
wrong question.

For coding tasks, treat hidden-test simulation as part of the contract. Route
contract scouting and extra verification when semantics are ambiguous, public
tests are sparse, API shape is uncertain, or blast radius is broad. Optimize
orchestration for finding the assumption that would make the patch fail.
