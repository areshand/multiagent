# Reviewed Ops Cycle

Use one logical ops identity for all operational work in a session. The ops
agent chooses runbooks and request contents; the supervisor enforces publication,
review, and execution boundaries.

## Start once

```bash
multiagent subagent spawn OPS_NAME --role ops \
  --instruction "Follow the applicable runbook and prepare the reviewed operation."
multiagent subagent wait OPS_NAME --timeout 900
```

Do not replace this identity after review.

## Review and continue

After ops returns a published artifact descriptor:

```bash
multiagent subagent reviewed-ops-cycle OPS_NAME \
  --request-file "$PUBLISHED_REQUEST_PATH" \
  --reviewer ops-reviewer-NN \
  --timeout 900
```

Use a fresh reviewer name for each immutable request. This command:

1. publishes a safe legacy request when necessary;
2. binds the reviewer to the immutable request and exact runbook;
3. passes only a bounded artifact descriptor to the reviewer;
4. finalizes accepted review evidence before execution; and
5. continues the same ops identity in a fresh provider context with the exact
   execute command.

Do not reconstruct these mechanics manually. Prior panes, transcripts, final
messages, and native provider resume state are intentionally excluded from the
continuation boundary.

On rejection or preflight failure, report the blocker. A changed request needs a
new publication and reviewer. A review correction may use a fresh reviewer on
the same immutable request. Never create a second ops identity.
