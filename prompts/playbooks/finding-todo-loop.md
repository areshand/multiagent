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
bin/subagent.sh finding-create build-go-ofrep \
  --severity blocking \
  --type compile_failure \
  --summary "Changed Go packages do not compile" \
  --affected internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go \
  --evidence-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":1,"stderr_excerpt":"undefined: req.Request"}' \
  --required-resolution "Final diff must compile with rc=0 for both changed Go packages."
```

The verifier may still include human-readable analysis, but any blocking issue
that should drive repair must have a corresponding finding artifact.

## Orchestrator Todo

The orchestrator decides which findings are accepted as required follow-up and
creates a todo for each accepted blocking finding:

```bash
bin/subagent.sh todo-create todo-017 \
  --source-finding-id build-go-ofrep \
  --task "Fix Go compile failure in OFREP/evaluation changed packages." \
  --context "Exact verifier evidence and relevant contract ledger." \
  --done-criteria "run go test ./internal/server/ofrep" \
  --done-criteria "run go test ./internal/server/evaluation" \
  --done-criteria "record returncode=0 after final diff"
```

Do not paste raw verifier prose as an open-ended worker order. Give the worker a
bounded task, exact evidence, owned paths, and objective done criteria.

## Worker Resolution

A worker assigned a todo must close the todo with evidence, not only a sentence:

```bash
bin/subagent.sh resolution-create todo-017 \
  --worker worker-02-ofrep-build \
  --status resolved \
  --changed internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go \
  --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}]' \
  --why "The missing interface contract is implemented and both changed packages compile."
```

`resolved` means ready for verifier review. It is not final acceptance.

## Reverification And Gate

The verifier compares the worker resolution against the original finding and
done criteria. If the issue is fixed, the orchestrator records:

```bash
bin/subagent.sh todo-status todo-017 closed
```

If evidence is stale, partial, missing, or contradicted by source/commands,
reopen the todo:

```bash
bin/subagent.sh todo-status todo-017 reopened
```

Before final acceptance, run:

```bash
bin/subagent.sh gate-check
```

Do not accept while `gate-check` reports an unqueued blocking finding or any
open, assigned, resolved, or reopened todo. For code patches, build
verification is one required finding/todo class; behavior and hidden-contract
findings use the same loop.
