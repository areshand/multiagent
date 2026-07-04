# Agent Spawning Playbook

Use this playbook whenever the orchestrator is about to create, monitor,
replace, verify, or finalize worker windows or named subagents.

## Worker First Instruction

Before spawning a worker, load `prompts/worker.md` and prepend it to the
task-specific assignment. Also pass assignment ID, branch, owned paths, task
statement, and the relevant contract ledger. For high-risk coding tasks,
include the contract scout's `must-preserve` list and validation plan. The
worker module contains shared worker rules and Ponytail implementation discipline.

## Worker Spawn Skill

Before spawning a worker, create durable assignment metadata:

```bash
bin/subagent.sh assignment-create worker-01-task \
  --assignment-id ASSIGNMENT_ID \
  --branch BRANCH \
  --owned PATH[,PATH...]
bin/subagent.sh worktree-create worker-01-task
bin/subagent.sh checkpoint-update worker-01-task --step "assignment created" --status assigned
```

Use a separate git worktree per worker unless the user explicitly directs
otherwise. Spawn from that worktree path:

```bash
WORKTREE_PATH="$(bin/subagent.sh worktree-show worker-01-task | awk -F= '$1 == "path" {print $2}')"
WORKER_CLI="${WORKER_CLI:-claude}"
case "$WORKER_CLI" in
  codex)
    WORKER_COMMAND="cd '$WORKTREE_PATH' && ${CODEX_BIN:-codex} --cd '$WORKTREE_PATH' --dangerously-bypass-approvals-and-sandbox --no-alt-screen"
    ;;
  claude)
    WORKER_COMMAND="cd '$WORKTREE_PATH' && ${CLAUDE_BIN:-claude} --dangerously-skip-permissions"
    ;;
  *)
    echo "Unsupported WORKER_CLI: $WORKER_CLI" >&2
    exit 2
    ;;
esac
tmux new-window -d -t "$MULTIAGENT_SESSION" -n "worker-01-task" "$WORKER_COMMAND"
```

Capture repeatedly until the selected CLI prompt is visible. If the pane shows
authentication/setup blockers or never becomes ready, report the blocker
instead of sending instructions.

## Long-Running Subagent Skill

Prefer `bin/subagent.sh spawn` for named long-running subagents because it
persists context:

```bash
bin/subagent.sh spawn subagent-build-watch --instruction "FIRST_INSTRUCTION_TEXT"
bin/subagent.sh poll subagent-build-watch
bin/subagent.sh inspect subagent-build-watch --lines 160
bin/subagent.sh finalize subagent-build-watch
```

Use `checkpoint-update NAME --step TEXT --status STATUS` after meaningful
progress, before stopping, and whenever a blocker appears.

## Verifier Agent Workflow

Spawn a verifier after a worker reports final status or is otherwise ready for
acceptance review. Load `prompts/verifier.md` and include it in the verifier's
first instruction with worker name, assignment ID, branch, owned paths, relevant
commit hash, task statement, contract ledger, and verifier iteration number.
For tasks that used a contract scout, include the scout's contract ledger and
validation plan as normative review input.

```bash
SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn verifier-01-task --instruction "FIRST_INSTRUCTION_TEXT"
```

Run `bin/subagent.sh assignment-check WORKER_NAME` before relying on verifier
results. Resolve branch or file ownership rejection before verification.

Use the configurable iteration cap:

```bash
MAX_ITERATIONS="${MULTIAGENT_VERIFIER_MAX_ITERATIONS:-3}"
```

Stop the worker/verifier loop when the verifier suggests no follow-up, the
orchestrator accepts no follow-up, or the accepted follow-up count reaches
`MAX_ITERATIONS`. If the final allowed verifier pass still produces findings
you would otherwise accept, explicitly accept with residual risk, reject, or ask
the user.

The verifier module requires a verifier contract ledger, Synthesize hidden-test-style probes,
assumption challenges, and the instruction to Run a Ponytail over-engineering pass.
The orchestrator decides which findings become accepted follow-up; never pass
raw verifier findings directly to the worker as orders.

## Progress And Status

When the user asks for agent progress, run:

```bash
bin/status.sh
```

Report only actual agents: worker windows and named subagents. Exclude the
orchestrator. If the helper fails, fall back to `tmux list-windows`,
`tmux capture-pane` for each non-orchestrator worker, and
`bin/subagent.sh poll NAME` for named subagents.
