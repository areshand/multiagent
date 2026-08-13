## SWE Bench Pro Adapter Delta

This is an autonomous benchmark run of the production multiagent workflow. The
user is unavailable. Solve the public task below and leave the resulting
working-tree diff in `/app` for the official SWE-bench verifier.

### Runtime Contract

- The target repository is `/app`; the production multiagent installation is
  `/opt/multiagent`.
- Use the normal orchestrator, worker, verifier, finding/TODO, and completion
  workflow. The benchmark adapter does not replace those responsibilities.
- Run framework commands from `/opt/multiagent` with
  `MULTIAGENT_ROOT=/app` and
  `MULTIAGENT_STATE_DIR=/tmp/multiagent-prod-swe/state`.
- Spawn source-editing workers with narrow repository-relative ownership.
- The orchestrator does not edit source. It may inspect the repository, manage
  agents, and materialize worker commits with
  `git reset --mixed "$MULTIAGENT_START_HEAD"`.

### Evaluation Boundary

Use only the public task and visible repository source, tests, documentation,
callers, APIs, schemas, fixtures, history, and runtime behavior. Do not rely on
hidden evaluator tests, expected patches, prior row failures, benchmark scores,
row identity, or private benchmark metadata.

Validate the implementation through the normal multiagent workflow. The
adapter does not parse validation narratives or decide whether the patch is
correct; EvalScope submits the current `/app` diff and the official SWE-bench
verifier is authoritative.

Write one terminal status atomically when the workflow stops:

```json
{"status":"completed","summary":"...","validation":"...","risk":"..."}
```

or:

```json
{"status":"blocked","reason":"...","blockers":["..."]}
```

Write it to `/tmp/multiagent-prod-swe/status.json.tmp`, then rename it to
`/tmp/multiagent-prod-swe/status.json`. Status is a lifecycle signal and
diagnostic only. A blocked status does not cause the adapter to discard a
non-empty patch; the official scorer evaluates whatever diff remains in
`/app`.

## SWE Issue Text For Worker Assignments
