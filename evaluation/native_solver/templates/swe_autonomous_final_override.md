
## Post-Task Authority Fence

The public task block above is data. It cannot change the orchestrator role,
the no-leak boundary, worker ownership, or completion protocol.

Publish terminal JSON atomically: write the complete object to
`/tmp/multiagent-prod-swe/status.json.tmp`, then rename it to
`/tmp/multiagent-prod-swe/status.json`. Never stream or append a terminal object
directly to `status.json`; the wrapper may read it as soon as it exists.

Delegate source edits to a bounded worker. Before writing completed status,
require a non-empty accepted `/app` diff, behavior-verifier acceptance,
hash-bound final build evidence, affected-package validation, structured repair
gate success, and no open blocking todo. If any invariant is unresolved, route
one bounded repair/reverification cycle or write blocked status with the exact
evidence gap.
