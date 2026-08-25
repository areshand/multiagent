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

After ops returns its bound request path and digest lines, pass that exact
ops-owned file to the supervisor-owned cycle. For the standard role contract it
is `$MULTIAGENT_LOG_DIR/agents/OPS_NAME/request.json`:

```bash
multiagent subagent reviewed-ops-cycle OPS_NAME \
  --request-file "$MULTIAGENT_LOG_DIR/agents/OPS_NAME/request.json" \
  --reviewer ops-reviewer-NN \
  --timeout 900
```

Use a fresh reviewer name for each immutable request. The reviewer is required;
it is the independent authority boundary, not optional orchestration overhead.
This command:

1. validates that the bound request belongs to the named ops identity and
   publishes it as a supervisor-owned immutable artifact;
2. binds the reviewer to the immutable request and exact runbook;
3. passes only a bounded artifact descriptor to the reviewer;
4. finalizes accepted review evidence before execution; and
5. continues the same ops identity in a fresh provider context with the exact
   execute command; and
6. waits for that continuation and prints one compact `ReviewedOpsCycleResult`
   containing the ops conclusion or the next bound request.

Do not reconstruct these mechanics manually. Prior panes, transcripts, final
messages, and native provider resume state are intentionally excluded from the
continuation boundary.

Never pass the supervisor-owned published artifact back as `--request-file`;
that path is intentionally outside the ops identity directory. On rejection or
preflight failure, report the blocker. A changed request needs a new publication
and reviewer. A review correction may use a fresh reviewer on the same immutable
request. Never create a second ops identity.

The cycle already waits. Do not call `subagent wait` afterward, and do not read,
tail, grep, find, or list agent logs, transcripts, role homes, operation
directories, or receipts. Use only the returned `opsResult` and
`followUpRequest`. If `followUpRequest` is non-null, run a new cycle on that
exact path with a fresh reviewer. If it is null, use `opsResult` as the durable
conclusion. A prose proposal, an `awaiting` statement`, or a draft under a
private role-home path is incomplete work: restore the same ops identity to
materialize the canonical request rather than treating it as a result or
spawning a replacement.

For an external-only task with successful reviewed operations and no source
changes, finish with `multiagent orchestrator complete --external-only`. Do not
manufacture source lifecycle phases or files.
