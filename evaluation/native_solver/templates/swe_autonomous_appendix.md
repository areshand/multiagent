
## SWE Bench Pro Adapter Delta

This is an autonomous benchmark run of the production multiagent system. The
user is unavailable. Solve the public task below through the normal
orchestrator, worker, verifier, and repair workflow. Leave the accepted source
diff in `/app`.

### Runtime Contract

- The target repository is `/app`; the production multiagent installation is
  `/opt/multiagent`.
- Use Codex for orchestrator, workers, scouts, and verifiers.
- The production prompt modules are under
  `$MULTIAGENT_PROMPT_MODULE_ROOT/prompts`. Load the normal worker, verifier,
  build-verifier, acceptance-scout, agent-spawning, orchestration-routing, and
  finding-todo-loop modules when those roles are used.
- Run helper commands from `/opt/multiagent` with `MULTIAGENT_ROOT=/app` and
  `MULTIAGENT_STATE_DIR=/tmp/multiagent-prod-swe`.
- Spawn workers and verifiers with `bin/subagent.sh`; this preserves the Codex
  bridge. Assign narrow repository-relative source paths. Never own `.` or the
  whole `/app` tree.
- The orchestrator does not edit source. It may inspect source and git state,
  manage agents, remove generated artifacts, and materialize a worker commit
  with `git reset --mixed "$MULTIAGENT_START_HEAD"`.
- Do not modify tests, lockfiles, generated/bundled assets, or unrelated config
  unless the public task explicitly requires a legitimate product artifact.

### Evidence Boundary

Use only the public task, visible repository source/tests/docs/callers/APIs,
schemas, fixtures, and runtime behavior. Do not rely on leaked evaluator tests,
hidden test names, expected patches, prior row failures, scores, row identity,
or benchmark-only metadata. Hidden-contract reasoning must be derived from
public intent and visible source.

The adapter writes two public/source-derived files:

- `/tmp/multiagent-prod-swe/contract-ledger.md`
- `/tmp/multiagent-prod-swe/source-owner-candidates.md`

Every worker and verifier receives the relevant contract ledger items. For a
multi-clause issue, preserve each clause separately. A one-symptom fix is not
complete until every public clause is mapped by `issue-coverage-ledger:` to
`implemented-by=PATH`, source-specific `already-satisfied-by=...`, or
`blocking-todo=ID`.

When the issue promises extensibility, configurability, registration,
overrides, or adding behavior without editing core logic, treat that as an
architectural contract. Centralizing a hardcoded table is insufficient unless
visible source proves the requested extension point exists. Require a concrete
registration/configuration API, its production integration path, and a
source-derived probe of default plus overridden behavior.

### Solve Loop

1. Inspect the public task and generated ledger. If ownership or the behavioral
   contract is materially ambiguous, spawn one bounded read-only contract or
   acceptance scout.
2. Spawn an implementation worker with an observable behavior target, relevant
   ledger items, exact owned paths, and focused validation expectations. The
   worker must edit or report an exact source-visible blocker; a checklist is
   not a terminal result.
3. Inspect the materialized diff and worker evidence. If a required path lies
   outside ownership, release the assignment and spawn one fresh bounded worker
   owning the exact required paths.
4. Run focused visible validation. One active validator per package/path is the
   default; use the production validation lease helpers for expensive commands.
   A nonzero relevant command, compile error, timed-out build, or partial
   multi-package result is blocking and routes to a fresh repair worker.
5. Spawn a read-only behavior verifier and a build verifier after the final
   worker edit. The behavior verifier checks every public clause, exact API and
   extension-point shape, compatibility, callers, edge cases, and likely hidden
   contracts from source. The build verifier proves the final diff compiles or
   tests in each changed/affected package.
6. Convert every accepted blocking verifier finding into structured state:
   `finding-create` -> `todo-create` -> bounded worker -> `resolution-create`
   -> verifier recheck -> `todo-close`. Run `bin/subagent.sh gate-check` before
   completion. Do not close a todo from worker narrative alone. The gate also
   rejects a latest durable `BLOCKING` verifier verdict even if the verifier
   failed to persist its finding; route repair and a later accepted recheck
   instead of writing contradictory completed status.
7. Stop exploration once evidence supports one of four terminal actions:
   accepted completion, one concrete repair worker, one verifier recheck, or a
   blocked status with the exact source/environment reason.

### Final Gate

Before completion:

- Ensure `/app` has a non-empty source diff and no disallowed artifacts.
- Bind validation to the final diff with
  `build-verification-passed: final-diff-sha256=... changed-files=N
  compile_clean=true returncode=0`.
- For changed Go source, derive packages from `git diff --name-only`, run real
  affected package tests after the final edit, and record one
  `go-package-validation-passed: package=... command=... returncode=0` per
  changed/contract package. `undefined:`, `has no field or method`, `FAIL`,
  `build failed`, any nonzero return code, or a no-test-only command blocks.
- Preserve source-level symbol/package placement and declared receiver or
  interface compatibility. When symbols change, record source-owner and symbol
  evidence through the normal worker/verifier modules.
- A known relevant visible failure remains blocking unless public task/source
  evidence proves the expectation changed and a rerunnable exact replacement
  probe passes.
- Require read-only verifier acceptance, all blocking todos closed with
  accepted evidence, and `bin/subagent.sh gate-check` success.

Write exactly one terminal file:

```json
{"status":"completed","summary":"...","validation":"...","risk":"..."}
```

or:

```json
{"status":"blocked","reason":"...","blockers":["..."]}
```

The path is `/tmp/multiagent-prod-swe/status.json`. Natural-language output is
not completion. The official scorer uses only the final `git diff --binary`
from `/app`.

## SWE Issue Text For Worker Assignments
