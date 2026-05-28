# Multiagent tmux Orchestrator

This project launches a tmux session with one `orchestrator` window. The orchestrator prompt coordinates worker agents and named long-running subagents.

## Features

- **Tmux Integration**: Seamless session management with configurable session names
- **Long-Running Subagents**: Persistent agents that maintain state across interactions
- **Flexible Configuration**: Environment-based setup for different project contexts
- **State Persistence**: Durable subagent state management with transcript logging
- **Assignment Checks**: Repo-local metadata and post-work acceptance checks for branch and file ownership

## Launch

```bash
./launch.sh --session multiagent --root /Users/bowu/projects/multiagent
```

Launches are clean by default. The orchestrator receives
`MULTIAGENT_RESUME=0`, lists the current session/windows/subagents, and waits
for direction without inspecting recovery state.

To explicitly resume after a previous crashed or interrupted session:

```bash
./launch.sh --resume --session multiagent --root /Users/bowu/projects/multiagent
```

With `--resume`, the orchestrator receives `MULTIAGENT_RESUME=1` and should run
`bin/subagent.sh recover-plan` before deciding whether to restore persisted
subagents.

Environment:

- `MULTIAGENT_SESSION`: tmux session name, default `multiagent`
- `MULTIAGENT_ROOT`: project root, default launcher directory
- `MULTIAGENT_RESUME`: launch mode exported by `launch.sh`; `0` clean launch, `1` explicit `--resume`
- `MULTIAGENT_STATE_DIR`: durable subagent state, default `$MULTIAGENT_ROOT/.multiagent`
- `MULTIAGENT_WRITE_POLICY`: repo write policy, default `$MULTIAGENT_ROOT/docs/write-policy.paths`
- `MULTIAGENT_VERIFIER_MAX_ITERATIONS`: worker/verifier follow-up loop cap, default `3`
- `MULTIAGENT_PROMPT`: orchestrator prompt, default `<launcher directory>/orchestrator_prompt.md`
- `ORCHESTRATOR_CLI`: orchestrator CLI, default `codex`
- `WORKER_CLI`: worker CLI for manual worker windows, default `codex`
- `SUBAGENT_CLI`: named subagent CLI, default `$WORKER_CLI`
- `CODEX_BIN`: Codex CLI command, default `codex`
- `CLAUDE_BIN`: Claude CLI command, default `claude`

The default setup preserves the original all-Codex behavior. To keep the
orchestrator on Codex while using Claude for workers and long-running
subagents:

```bash
ORCHESTRATOR_CLI=codex WORKER_CLI=claude SUBAGENT_CLI=claude ./launch.sh
```

Codex launches with `--cd`, `--dangerously-bypass-approvals-and-sandbox`, and
`--no-alt-screen`. Claude launches from the target worktree/root with
`claude --dangerously-skip-permissions`; Codex-only flags are intentionally not
passed to Claude.

`--root` selects the target project repo for `MULTIAGENT_ROOT`, state, write
policy, and the orchestrator CLI working directory. The default orchestrator
prompt is still loaded from this launcher's directory, so cross-repo launches do
not need an `orchestrator_prompt.md` in the target repo. Set
`MULTIAGENT_PROMPT=/path/to/prompt.md` to override that default.

## Verifier Workflow

After a worker reports completion, the orchestrator may spawn one read-only
verifier window for that assignment, usually named from the worker, such as
`verifier-01-docs` for `worker-01-docs`. The verifier reviews the finished work
and reports findings back to the orchestrator only.

The verifier checks:

- correctness gaps
- quality gaps
- missing tests or docs
- whether the task scope is fully satisfied
- whether there is a simpler approach

The orchestrator reviews the verifier's findings and gives the verdict. Only
accepted follow-ups are passed back to the original worker. The worker then
reports done again, the orchestrator reruns assignment checks, and verification
may repeat until no accepted follow-up remains or the max iteration cap is
reached.

The loop cap is exported by `launch.sh`:

```bash
MULTIAGENT_VERIFIER_MAX_ITERATIONS=3
```

Override it when launching if needed:

```bash
MULTIAGENT_VERIFIER_MAX_ITERATIONS=2 ./launch.sh
```

Verifiers are reviewers, not implementers. They should not receive duplicate
writable ownership over worker-owned files, should not edit or commit code, and
should not coordinate directly with workers. This preserves orchestrator
authority over verdicts and prevents worker/verifier ownership conflicts.

## Repo Write Guardrails

Workers and subagents default to writing only inside `MULTIAGENT_ROOT`, the root
passed to `launch.sh`. Outside-root writes are denied by policy unless an
approved outside path is listed in:

```bash
docs/write-policy.paths
```

Use the helper to initialize, inspect, check, and update the policy:

```bash
bin/write-policy.sh init
bin/write-policy.sh show
bin/write-policy.sh check README.md /tmp/outside-file
bin/write-policy.sh approve /tmp/approved-output --actor orchestrator --assignment-id docs-001 --reason "export report"
```

The launch script initializes the policy file and prints the active policy at
startup. The orchestrator must ask for explicit approval before allowing a
worker to write outside `MULTIAGENT_ROOT`, then record the narrowest practical
outside path with `bin/write-policy.sh approve PATH --actor ACTOR
--assignment-id ID --reason TEXT`.

`docs/write-policy.paths` is orchestrator-owned. Workers should not edit it
directly. Approval records are TSV lines containing timestamp, actor,
assignment ID, requested path, canonical path, reason, and a force marker.
Legacy bare path lines are still read for compatibility, but new approvals
should be created only by the helper.

Broad outside approvals are rejected by default, including `/`, `$HOME`, the
repo parent, `/tmp`, and broad shared roots such as `/Users`, `/home`, `/usr`,
`/var`, `/private`, and `/Applications`. Use `--force` only after an explicit
orchestrator/user decision:

```bash
bin/write-policy.sh approve /tmp --actor orchestrator --assignment-id build-logs --reason "user approved shared temp output" --force
```

Mechanical enforcement is limited to the helper's policy checks and startup
visibility. Codex is still launched with
`--dangerously-bypass-approvals-and-sandbox`, so shell sandboxing is not
enforcing the boundary. The orchestrator and worker instructions require agents
to check and follow the policy before writes.

## Assignment Metadata and Acceptance

Use repo-local assignment records for every worker or named subagent before
work starts:

```bash
bin/subagent.sh assignment-create worker-01-docs \
  --assignment-id docs-001 \
  --branch worker/docs-001 \
  --owned README.md,orchestrator_prompt.md
bin/subagent.sh worktree-create worker-01-docs
bin/subagent.sh assignment-show worker-01-docs
bin/subagent.sh assignment-status worker-01-docs running
bin/subagent.sh checkpoint-update worker-01-docs --step "started implementation" --status running
```

Assignment state is stored under:

```bash
$MULTIAGENT_STATE_DIR/assignments/NAME
```

Each assignment stores the agent name, assignment ID, expected branch, owned
repo paths, status, and start commit. Owned paths are repo-relative and may be
files or directories.

Worktrees are optional for compatibility, but recommended for worker isolation.
`worktree-create` places the checkout at
`$MULTIAGENT_STATE_DIR/worktrees/NAME` by default and records metadata at
`$MULTIAGENT_STATE_DIR/worktrees/NAME.env`. Use `worktree-show NAME` to inspect
the assigned checkout and `worktree-remove NAME` after the worker is finalized.
When you spawn manually, start the worker from the recorded worktree path.
For a Claude worker, set `WORKER_CLI=claude` and run the window from the
worktree without Codex-only flags:

```bash
WORKTREE_PATH="$(bin/subagent.sh worktree-show worker-01-docs | awk -F= '$1 == "path" {print $2}')"
tmux new-window -d -t "$MULTIAGENT_SESSION" -n "worker-01-docs" \
  "cd '$WORKTREE_PATH' && ${CLAUDE_BIN:-claude} --dangerously-skip-permissions"
```

Workers and orchestrators can write structured recovery checkpoints:

```bash
bin/subagent.sh checkpoint-update worker-01-docs \
  --step "tests passing locally" \
  --idempotency "rerun tests/run.sh before acceptance" \
  --last-commit HEAD \
  --status running
bin/subagent.sh checkpoint-show worker-01-docs
```

Checkpoints include the assignment ID, branch, owned path file, last commit,
completed step, blocker, idempotency notes, status, and update timestamp.

After a worker reports completion, run:

```bash
bin/subagent.sh assignment-check worker-01-docs
```

The check mechanically rejects a branch mismatch and rejects any file changed
since the assignment start commit, in the working tree, in the index, or as an
untracked file, when that file is outside the assigned owned paths. It does not
inspect tmux instructions, prove authorship, enforce runtime sandboxing, or
prevent a worker from editing files before the check runs.

## Long-Running Subagents

Use `bin/subagent.sh` for named subagents that should keep working or monitoring over time:

```bash
bin/subagent.sh spawn subagent-ci-monitor --instruction "Monitor CI and report status changes."
SUBAGENT_CLI=claude bin/subagent.sh spawn subagent-ci-monitor --instruction "Monitor CI and report status changes."
bin/subagent.sh poll subagent-ci-monitor
bin/subagent.sh inspect subagent-ci-monitor --lines 160
bin/subagent.sh recover-plan
bin/subagent.sh restore subagent-ci-monitor
bin/subagent.sh restore-all
bin/subagent.sh finalize subagent-ci-monitor
```

Each subagent persists state under:

```bash
$MULTIAGENT_STATE_DIR/subagents/NAME
```

The state directory includes `meta.env`, `status`, `current.txt`, and
`transcript.log`, so the orchestrator can recover context after repeated
polling or after finalization. `meta.env` records the selected CLI, and
`restore` uses that persisted CLI so a Claude subagent restores with Claude
even if the current environment defaults back to Codex.

### Recovery

If the tmux session or orchestrator crashes, start a new orchestrator with
`--resume`. In resume mode, the orchestrator should run:

```bash
bin/subagent.sh recover-plan
```

The plan prints one row per persisted subagent with a conservative action.
Structured status and checkpoint metadata are the primary recovery signal.
`current.txt` and `transcript.log` are fallback context only when structured
state is missing.

- `restore`: closed subagent with enough prior context to resume.
- `skip-open`: a tmux window with that name already exists.
- `skip-finalized`: the subagent appears completed, finalized, killed, or intentionally stopped.
- `skip-blocked`: the subagent needs an orchestrator/user decision before resuming.
- `skip-unknown`: state is missing or unclear; inspect manually before acting.

Restore a specific resumable subagent with:

```bash
bin/subagent.sh restore NAME
```

The restored subagent gets a fresh tmux window with an instruction containing
its name, prior status, state directory, and a concise tail of `current.txt` and
`transcript.log`. Existing memory files are not deleted. Use
`bin/subagent.sh restore-all` only after reviewing the plan; it restores only
rows classified as `restore` and skips finalized, blocked, open, and unknown
subagents.

`spawn` and `restore` wait for an obvious ready prompt before delivering
instructions. They record `delivery-blocked` and fail instead of blindly
sending input when the pane shows Codex authentication/setup blockers, Claude
login/setup/trust prompts, or never becomes ready.

## Agent Progress

Use `bin/status.sh` when you want the orchestrator to check progress:

```bash
bin/status.sh
```

The status helper reports actual agents, not every local process. It captures
worker windows, polls open named subagents, refreshes subagent state, and prints
a table with agent type, name, status, window state, latest progress line, and
state directory.

## Tests

```bash
tests/run.sh
```
