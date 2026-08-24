# Reviewed Ops Cycle

Use this playbook whenever an ops agent has materialized an immutable prod-mcp
request. It centralizes the review and continuation mechanics; provider runbooks
define what operations mean, not how agents are spawned.

## Session invariant

Use one persistent `ops` identity for the entire session. That agent may follow
multiple runbooks and materialize multiple requests, but no second ops identity
may be created. Agent judgment selects the runbook, operation, and parameters.
`multiagent ops bind-runbook` copies canonical target metadata from the exact
Markdown runbook when it is declared there. This playbook only makes the
authorization lifecycle deterministic.

## Start the ops identity

Spawn exactly one ops identity for the session. The runtime composes the ops
role module; do not load the general agent-spawning playbook or role prompt
files to reconstruct it.

```bash
multiagent subagent spawn OPS_NAME --role ops --instruction "Inspect the request, follow the applicable runbook, and prepare the reviewed operation."
multiagent subagent wait OPS_NAME --timeout 900
```

Keep this identity for every reviewed operation in the session. Do not spawn a
replacement ops identity after review.

## Reviewed request

After the ops agent writes and binds its request, run:

```bash
multiagent subagent reviewed-ops-cycle OPS_NAME \
  --request-file "$MULTIAGENT_LOG_DIR/agents/OPS_NAME/request.json" \
  --reviewer ops-reviewer-NN \
  --timeout 900
```

Use a fresh `ops-reviewer-NN` identity for every immutable request. The command:

1. verifies that the request belongs to the session's ops identity;
2. computes the exact review binding;
3. spawns an independent ops reviewer with the literal request and binding;
4. waits for and finalizes the reviewer;
5. rejects missing, negative, or incorrectly bound evidence before execution;
6. restores the same ops identity with the exact execute command; and
7. waits for that ops identity to inspect the receipt and continue its runbook.

Do not manually reconstruct these steps in prompts or shell commands.

## Failure behavior

If review is rejected, binding preflight fails, restoration fails, or prod-mcp
rejects execution, stop and report the exact blocker. If a corrected independent
review is appropriate, use a fresh reviewer on the same immutable request. If
the request bytes must change, the same ops identity materializes the new bytes
and starts a new reviewed cycle. Never spawn a replacement ops identity.
