# Finding Todo Loop Playbook

Use this playbook whenever verifier output creates required repair work. The
framework contract is structured state, not memory or prose:

```text
worker patch
-> verifier writes structured findings
-> orchestrator converts blocking findings into todos
-> worker repairs one todo with context
-> worker records resolution evidence
-> verifier rechecks the original finding
-> final gate accepts only when required todos are closed
```

## Verifier Finding

A blocking verifier issue must be machine-readable. It must identify the issue,
severity, affected paths, evidence, and the required resolution. Use:

```bash
bin/subagent.sh finding-create build-go-feature \
  --severity blocking \
  --type compile_failure \
  --summary "Changed Go packages do not compile" \
  --affected internal/feature/handler.go,internal/feature/bridge.go \
  --evidence-json '{"command":"go test ./internal/feature","returncode":1,"stderr_excerpt":"undefined: request.Config"}' \
  --required-resolution "Final diff must compile with rc=0 for the changed Go package."
```

The verifier may still include human-readable analysis, but any blocking issue
that should drive repair must have a corresponding finding artifact. Blocking
compile, build, test, and validation failure findings must include command
evidence with a return code; source-only evidence is reserved for source-level
contract findings such as hidden API shape or adapter parity risks.
For Go compile/build findings, command evidence must identify the affected
changed packages separately or use a package list that contains only affected
buildable packages. Do not create a blocking changed-package finding from a
mixed command such as `go test ./changed/pkg .` when the changed package passed
and only repo-root `.` failed because it is not a buildable package. In that
case, rerun `go test ./changed/pkg` and use the focused command result as the
finding or closure evidence.

## Orchestrator Todo

The orchestrator decides which findings are accepted as required follow-up and
creates a todo for each accepted blocking finding:

```bash
bin/subagent.sh todo-create todo-017 \
  --source-finding-id build-go-feature \
  --task "Fix the compile failure in the changed Go package." \
  --context "Exact verifier evidence and relevant contract ledger." \
  --done-criteria "run go test ./internal/feature" \
  --done-criteria "record returncode=0 after final diff"
```

Do not paste raw verifier prose as an open-ended worker order. Give the worker a
bounded task, exact evidence, owned paths, and objective done criteria. Any
done criterion that starts with `run ` becomes a machine-checkable required
command. For commands that are not naturally phrased as a `run ...` done
criterion, add `--required-command "exact command"` so the worker resolution
and verifier recheck must both cover it.

Required commands must be achievable rc=0 closure conditions. Do not make a
full test command mandatory after it has been classified as failing only because
of unavailable runtime fixtures, assets, credentials, services, or platform
support while simultaneously allowing a compile-only fallback. In that case,
record the full failure in finding/context evidence, make the hash-bound
compile-only command mandatory, and require an independent behavior verifier to
accept or identify a concrete source defect. A source worker must not be asked
to repair the environment by changing product code.

Todo creation snapshots the source finding hash. This prevents the orchestrator
from closing a task against stale, mutated, or prose-reconstructed verifier
state; the gate rechecks the current finding artifact against that hash.

## Worker Resolution

A worker assigned a todo must record resolution evidence, not only a sentence:

```bash
"${MULTIAGENT_HELPER:-/opt/multiagent/bin/subagent.sh}" resolution-create todo-017 \
  --worker worker-02-feature-build \
  --status resolved \
  --changed internal/feature/handler.go,internal/feature/bridge.go \
  --validation-json '[{"cmd":"go test ./internal/feature","rc":0}]' \
  --why "The missing interface contract is implemented and the changed package compiles."
```

Use the helper path from `MULTIAGENT_HELPER` when present. If a worker is running
from a task checkout such as `/app`, do not assume `bin/subagent.sh` exists in
the current repo.

`resolved` means ready for verifier review. It is not final acceptance.
All `validation-json` entries in a resolved report must be objective passing
evidence with `rc: 0`. Preserve an environment-failing full command in the
resolution reason or a blocked report; do not mix it into a resolved validation
array beside a passing compile fallback.

## Reverification And Gate

The verifier compares the worker resolution against the original finding and
done criteria. Required commands must appear with `rc=0` in both the worker
resolution and the verifier recheck; a nearby successful command does not close
the todo. If the issue is fixed, the orchestrator closes the todo with verifier
recheck evidence:

```bash
bin/subagent.sh todo-close todo-017 \
  --verified-by verifier-01-feature-build \
  --recheck-json '{"accepted":true,"finding_rechecked":"build-go-feature","commands":[{"cmd":"go test ./internal/feature","rc":0}],"final_diff_hash":"..."}' \
  --notes "Verifier rechecked the original finding after worker resolution."
```

If evidence is stale, partial, missing, or contradicted by source/commands,
reopen the todo:

```bash
bin/subagent.sh todo-status todo-017 reopened
```

Process verifier artifacts in final-diff order. Once a newer verifier accepts
the exact current diff and explicitly rechecks a resolved todo's original
finding and required commands, close that todo before considering any earlier
blocking transcript. Do not create or reopen a todo from command evidence bound
to an older diff hash after the same command has passed in that accepted current
diff recheck. Earlier evidence remains history; it is not a new finding unless
the current diff reproduces the failure. Pass `--recheck-json` a complete JSON
object using the shape above, then rerun `gate-check`.

Before final acceptance, run:

```bash
bin/subagent.sh gate-check
```

Do not accept while `gate-check` reports an unqueued blocking finding or any
open, assigned, resolved, or reopened todo. A closed todo also fails the gate if
it lacks worker resolution evidence, verifier closure evidence, source-finding
binding/hash consistency, or required-command coverage. For code patches, build
verification is one required finding/todo class; behavior and hidden-contract
findings use the same loop.

`gate-check` also rejects when the latest durable `verifier-*` or `review-*`
last message starts with `BLOCKING`. This is a fail-safe for protocol errors: a
verifier that reports a blocker but fails to call `finding-create` cannot be
silently ignored merely because the structured store is empty. Persist the
finding/todo, repair it, and obtain a later `ACCEPTED` verifier recheck.
A newer verifier last message without either `BLOCKING` or `ACCEPTED` is also
rejected as an incomplete recheck; do not fall back to an older acceptance.
When the target repository has a non-empty source diff, `ACCEPTED` and every
closed todo recheck must name the exact current `final-diff-sha256`. The gate
computes the live hash and rejects stale or unbound evidence.

## Verifier Infrastructure Failures

If a verifier cannot complete its review because a tool call failed, a command
schema was malformed, `/app` could not be inspected even though the task
checkout exists, or the verifier process exited before reading the final diff,
classify that as an orchestration infrastructure finding. Do not translate it
into "patch accepted" or a source-level rejection. Requeue a fresh read-only
verifier or resume the orchestrator with the current diff, original finding
state, and objective done criteria preserved.

Infrastructure failures may block final acceptance, but only until a verifier
successfully rechecks the relevant source/commands or emits structured semantic
findings. They must not close or reopen a todo without source or command
evidence tied to the final diff.
