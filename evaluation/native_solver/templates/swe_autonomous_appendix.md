## SWE Bench Pro Runtime

Solve the public task below with the production multiagent workflow. The target
repository is `/app` and the framework is installed at `/opt/multiagent`.

Use only the public task and visible repository contents. Do not use hidden
tests, expected patches, benchmark scores, row identity, or private metadata.
Resolve every relative deliverable path in the public task under the target
repository `/app`; in particular, `ops_plan.json` means
`/app/ops_plan.json`. `MULTIAGENT_STATE_DIR` contains control-plane metadata and
instruction files only. Never redirect a requested repository artifact there.

Use a read-only contract scout only when a material source, API, or behavioral
unknown can change the implementation plan. Skip the scout when the public task
already supplies an exact bounded output schema and values and visible source
inspection exposes no material contract uncertainty. If a scout is used,
finalize it and register its structured output with
`multiagent workflow contract-register`. The approved implementation context
must include the registered contract artifact verbatim and its exact
`contract-artifact-sha256=...` binding. Preserve every explicit `must` and
`must-not` rule; do not replace a task-requested structural migration with
legacy aliases merely because pre-change tests still compile against the old
shape.

This run has no interactive user. Treat every behavior explicitly stated in the
public task as already user-approved. Do not stop to ask the user to reselect an
explicit requirement because the repository exposes aliases, legacy APIs, or
additional possible behavior. When the public task leaves an implementation
detail open, use the narrowest backward-compatible interpretation supported by
visible source/tests, record the assumption, and continue. Stop only for a true
contradiction that makes the public task impossible to implement safely.

If the public task explicitly changes an API, option default, or wrapper
propagation path, that new contract outranks pre-change exact-call mocks that
only encode the old argument shape. Preserve unrelated compatibility, but do
not omit a newly required default at an intermediate layer merely to keep such
a stale mock green; verify the declared default and an override reach the next
layer.

When an explicit API removal or rename leaves visible pre-change tests referring
to the removed symbol, that test-only dependency is not by itself a true contradiction.
Update the production callers, keep the public-contract source change in `/app`,
and record the stale-test compile failure as residual
validation evidence. Do not create a cleanup worker or revert a non-empty
public-contract candidate to an empty diff merely to restore the old test API.
Preserve the best task-directed candidate unless source review shows that the
candidate itself violates the public task or causes an unrelated regression
that cannot be separated from it.

Leave the final working-tree changes in `/app`. The adapter only transports
that workspace to EvalScope; the official SWE-bench verifier evaluates it.
`/app/_base_commit` is immutable adapter metadata created after the baseline
snapshot and excluded from Git status. Preserve it unchanged: it is not a
worker output, owned path, candidate diff, cleanup target, or TODO trigger.

This is an autonomous run-to-terminal workflow. Do not end the orchestrator
turn by offering to continue, reporting that implementation is still in
flight, or submitting a known incomplete candidate. If a worker stops because
its assignment omitted a path required by the approved plan or visible
validation, create the bounded follow-up TODO and worker with that path. Exit
only after the lifecycle completes or after recording a true source-visible
blocker that the workflow cannot safely resolve.

## SWE Issue Text For Worker Assignments
