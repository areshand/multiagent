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
- `WORKER_CLI`: worker CLI for manual worker windows, default `claude`
- `SUBAGENT_CLI`: named subagent CLI, default `$WORKER_CLI`
- `VERIFIER_CLI`: verifier CLI, default `codex`
- `CODEX_BIN`: Codex CLI command, default `codex`
- `CLAUDE_BIN`: Claude CLI command, default `claude`

The default setup keeps the orchestrator on Codex, uses Claude for workers and
generic named subagents, and uses Codex for verifier agents. To use Codex for
workers and generic named subagents too:

```bash
ORCHESTRATOR_CLI=codex WORKER_CLI=codex SUBAGENT_CLI=codex ./launch.sh
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
reached. The cap limits accepted worker follow-up cycles after verifier review.
If the final allowed verifier pass still finds accepted follow-up, the
orchestrator stops the loop at the cap and explicitly accepts with residual
risk, rejects the work, or asks the user.

The loop cap is exported by `launch.sh`:

```bash
MULTIAGENT_VERIFIER_MAX_ITERATIONS=3
```

Override it when launching if needed:

```bash
MULTIAGENT_VERIFIER_MAX_ITERATIONS=2 ./launch.sh
```

Verifier agents use `VERIFIER_CLI`, which defaults to Codex. There is no
dedicated verifier spawn helper; when using the generic subagent helper, pass
the verifier CLI explicitly:

```bash
SUBAGENT_CLI="${VERIFIER_CLI:-codex}" bin/subagent.sh spawn verifier-01-docs --instruction "Review worker-01-docs."
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

## Harness Dispatch Prototype

`bin/harness.sh` is an alternate design surface for routing orchestrator actions
through an explicit evaluate-then-dispatch step. It accepts small `key=value`
action manifests, returns `ALLOW`, `DENY`, `REQUIRE_APPROVAL`, or `TERMINATE`,
and executes only allowed actions through existing repo helpers.

See `docs/harness-dispatch-design.md` for the action format, policy decisions,
and migration path from tmux supervision toward a mediated harness loop.

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
Workers default to Claude, so run the window from the worktree without
Codex-only flags:

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

## Organizational Learning Workflow

The orchestrator supports exploration/exploitation/reflection cycles for complex decisions requiring multiple approaches.

### Decision Management

Create and manage decisions with competing options:

```bash
# Create a new decision
bin/decision.sh init DEC-001 --title "Which API authentication approach?"

# Add competing options discovered during exploration
bin/decision.sh add-alternative DEC-001 \
  --plan-id PLN-001 \
  --summary "OAuth 2.0 with PKCE" \
  --proposed-by exploration-agent-01 \
  --expected-outcome "Secure auth with industry standard OAuth 2.0 and PKCE for mobile"

bin/decision.sh add-alternative DEC-001 \
  --plan-id PLN-002 \
  --summary "Custom JWT with refresh tokens" \
  --proposed-by exploration-agent-02 \
  --expected-outcome "Fast custom JWT implementation with refresh token security"

# Resolve decision and create implementation plan
bin/decision.sh commit DEC-001 \
  --selected-plan PLN-001 \
  --reason "Better security posture and industry standard"

# View decision history
bin/decision.sh list
bin/decision.sh show DEC-001
```

### Role-Tagged Agent Assignments

Assign specific roles to agents for structured workflows:

```bash
# Create exploration assignments for different approaches
bin/subagent.sh assignment-create worker-01-explore-oauth \
  --assignment-id AUTH-001 \
  --role exploration \
  --decision-id DEC-001 \
  --branch explore/oauth-approach \
  --owned exploration/oauth/

bin/subagent.sh assignment-create worker-02-explore-jwt \
  --assignment-id AUTH-002 \
  --role exploration \
  --decision-id DEC-001 \
  --branch explore/jwt-approach \
  --owned exploration/jwt/

# Create exploitation assignment after decision resolution
bin/subagent.sh assignment-create worker-03-implement-oauth \
  --assignment-id AUTH-003 \
  --role exploitation \
  --decision-id DEC-001 \
  --plan-id PLN-001 \
  --branch implement/oauth-auth \
  --owned src/auth/,tests/auth/

# Create reflection assignment after implementation
bin/subagent.sh assignment-create reflection-01-auth \
  --assignment-id REF-001 \
  --role reflection \
  --decision-id DEC-001 \
  --plan-id PLN-001 \
  --branch main \
  --owned docs/reflection/auth-decision.md

# Architecture review across multiple decisions
bin/subagent.sh assignment-create arch-01-security \
  --assignment-id ARCH-001 \
  --role architecture \
  --decision-id DEC-001,DEC-002 \
  --branch main \
  --owned architecture/security/

# QA verification of implementation
bin/subagent.sh assignment-create qa-01-auth-tests \
  --assignment-id QA-001 \
  --role qa \
  --decision-id DEC-001 \
  --plan-id PLN-001 \
  --branch implement/oauth-auth \
  --owned tests/integration/auth/
```

### Example Workflow: Multi-Approach Decision

Complete workflow for a complex architectural decision:

```bash
# 1. Create decision context
bin/decision.sh init DEC-003 --title "Database scaling strategy for user growth"

# 2. Spawn exploration agents for different approaches
bin/subagent.sh assignment-create worker-01-explore-sharding \
  --assignment-id DB-001 --role exploration --decision-id DEC-003 \
  --branch explore/db-sharding --owned exploration/sharding/

bin/subagent.sh assignment-create worker-02-explore-replication \
  --assignment-id DB-002 --role exploration --decision-id DEC-003 \
  --branch explore/db-replication --owned exploration/replication/

bin/subagent.sh assignment-create worker-03-explore-nosql \
  --assignment-id DB-003 --role exploration --decision-id DEC-003 \
  --branch explore/nosql-migration --owned exploration/nosql/

# 3. Architecture agent reviews consistency across approaches
bin/subagent.sh assignment-create arch-01-db-review \
  --assignment-id ARCH-002 --role architecture --decision-id DEC-003 \
  --branch main --owned architecture/database/

# 4. After exploration, record options and make decision
bin/decision.sh add-alternative DEC-003 \
  --plan-id PLN-001 \
  --summary "Horizontal sharding" \
  --proposed-by worker-01-explore-sharding \
  --expected-outcome "Scalable database with horizontal partitioning"

bin/decision.sh add-alternative DEC-003 \
  --plan-id PLN-002 \
  --summary "Read replicas with write scaling" \
  --proposed-by worker-02-explore-replication \
  --expected-outcome "Improved read performance with replica scaling"

bin/decision.sh commit DEC-003 \
  --selected-plan PLN-001 \
  --reason "Sharding provides better long-term scalability"

# 5. Implementation with focused exploitation
bin/subagent.sh assignment-create worker-04-implement-sharding \
  --assignment-id DB-004 --role exploitation --decision-id DEC-003 \
  --plan-id PLN-001 --branch implement/db-sharding \
  --owned src/database/,migrations/,config/sharding.yaml

# 6. QA verification against exploration predictions
bin/subagent.sh assignment-create qa-01-sharding-tests \
  --assignment-id QA-002 --role qa --decision-id DEC-003 \
  --plan-id PLN-001 --branch implement/db-sharding \
  --owned tests/performance/sharding/

# 7. Retrospective reflection on decision quality
bin/subagent.sh assignment-create reflection-01-db-scaling \
  --assignment-id REF-002 --role reflection --decision-id DEC-003 \
  --plan-id PLN-001 --branch main \
  --owned docs/reflection/db-scaling-decision.md
```

### Implementation Tracking and Pivots

Track implementations and handle pivots using assignment metadata:

```bash
# Create primary implementation assignment
bin/subagent.sh assignment-create worker-03-oauth-impl \
  --assignment-id AUTH-003 \
  --role exploitation \
  --decision-id DEC-001 \
  --plan-id PLN-001 \
  --branch implement/oauth \
  --owned src/auth/

# Create contingency implementation (ready but not active)
bin/subagent.sh assignment-create worker-04-jwt-fallback \
  --assignment-id AUTH-004 \
  --role exploitation \
  --decision-id DEC-001 \
  --plan-id PLN-002 \
  --branch fallback/jwt \
  --owned src/jwt/ \
  --status contingency

# Track progress via assignment status
bin/subagent.sh assignment-status worker-03-oauth-impl running
bin/subagent.sh checkpoint-update worker-03-oauth-impl \
  --step "PKCE flow implemented" --status running

# Handle pivot when primary approach encounters blockers
bin/subagent.sh checkpoint-update worker-03-oauth-impl \
  --step "blocked on PKCE library compatibility" \
  --blocker "third-party PKCE library incompatible with mobile framework" \
  --status blocked

# Orchestrator activates contingency by changing assignment status
bin/subagent.sh assignment-status worker-04-jwt-fallback running
```

### Role-Specific Agent Instructions

The orchestrator should include role-specific guidance when spawning agents:

- **Exploration agents**: Encouraged to disagree, document evidence, explore assigned approach independently
- **Exploitation workers**: Focus on chosen plan, report blockers rather than abandoning approach  
- **Reflection agents**: Retrospective analysis, compare predictions to outcomes, extract lessons
- **Architecture agents**: Maintain system coherence, identify integration points, review for consistency
- **QA/Verifier agents**: Validate implementations against exploration promises and requirements

Each role receives appropriate file ownership boundaries and collaboration constraints to prevent conflicts while preserving valuable disagreement during exploration phases.

## DAG-Controlled Workflows

The orchestrator supports DAG (Directed Acyclic Graph) workflow control for complex tasks with multiple dependencies. The orchestrator owns the workflow DAG and controls node sequencing, while agents execute individual nodes.

### Basic DAG Operations

Create and manage workflow DAGs:

```bash
# Initialize a new workflow
bin/dag.sh init auth-workflow-001 --title "Authentication system implementation"

# Add nodes with dependencies and role assignments
bin/dag.sh add-node auth-workflow-001 initial-architecture \
  --agent worker-initial-arch \
  --role architecture \
  --depends-on "" \
  --assignment-id ARCH-001 \
  --branch main \
  --owned architecture/auth/

bin/dag.sh add-node auth-workflow-001 explore-oauth \
  --agent worker-explore-oauth \
  --role exploration \
  --depends-on initial-architecture \
  --assignment-id AUTH-001 \
  --branch explore/oauth \
  --owned exploration/oauth/

bin/dag.sh add-node auth-workflow-001 explore-jwt \
  --agent worker-explore-jwt \
  --role exploration \
  --depends-on initial-architecture \
  --assignment-id AUTH-002 \
  --branch explore/jwt \
  --owned exploration/jwt/

# Note: Decision processing handled by orchestrator using bin/decision.sh commands
# Implementation depends on exploration results and architecture
bin/dag.sh add-node auth-workflow-001 implement-auth \
  --agent worker-implement-auth \
  --role exploitation \
  --depends-on explore-oauth,explore-jwt,initial-architecture \
  --assignment-id IMPL-001 \
  --branch implement/auth \
  --owned src/auth/,tests/auth/

bin/dag.sh add-node auth-workflow-001 verify-auth \
  --agent worker-verify-auth \
  --role qa \
  --depends-on implement-auth \
  --assignment-id QA-001 \
  --branch implement/auth \
  --owned tests/integration/auth/

bin/dag.sh add-node auth-workflow-001 reflect-auth \
  --agent worker-reflect-auth \
  --role reflection \
  --depends-on verify-auth \
  --assignment-id REF-001 \
  --branch main \
  --owned docs/reflection/auth-decision.md

# Check ready nodes
bin/dag.sh ready auth-workflow-001

# Show workflow visualization
bin/dag.sh show auth-workflow-001
```

### DAG-Driven Agent Spawning

The orchestrator uses DAG status to determine which agents to spawn:

```bash
# Get ready nodes (nodes with satisfied dependencies)
bin/dag.sh ready auth-workflow-001

# For each ready node, create assignment and spawn agent
bin/subagent.sh assignment-create worker-initial-arch \
  --assignment-id ARCH-001 \
  --role architecture \
  --branch main \
  --owned architecture/auth/ \
  --workflow-id auth-workflow-001 \
  --node-id initial-architecture

# Update node status when agent starts working
bin/dag.sh status auth-workflow-001 initial-architecture running

# Update node status when agent completes
bin/dag.sh status auth-workflow-001 initial-architecture done

# Check for newly ready nodes after status update
bin/dag.sh ready auth-workflow-001
```

### Node Status Management

Track and update node progress through the workflow:

```bash
# Update node status based on agent reports
bin/dag.sh status auth-workflow-001 explore-oauth running
bin/dag.sh status auth-workflow-001 explore-jwt running

# Mark nodes as completed when agents finish
bin/dag.sh status auth-workflow-001 explore-oauth done
bin/dag.sh status auth-workflow-001 explore-jwt done

# Handle blocked nodes
bin/dag.sh status auth-workflow-001 implement-auth blocked \
  --reason "Waiting for external API keys"

# Skip nodes when conditions change
bin/dag.sh status auth-workflow-001 verify-auth skipped \
  --reason "Implementation approach changed, verification not needed"

# Mark failed nodes for retry decisions
bin/dag.sh status auth-workflow-001 implement-auth failed \
  --reason "Implementation approach incompatible with requirements"
```

### Complete Multi-Phase Workflow Example

End-to-end example of a complex feature implementation:

```bash
# 1. Initialize workflow for database scaling feature
bin/dag.sh init db-scaling-workflow --title "Database scaling implementation"

# 2. Add architecture and exploration nodes
bin/dag.sh add-node db-scaling-workflow db-architecture \
  --agent worker-db-arch \
  --role architecture \
  --assignment-id ARCH-003 \
  --branch main \
  --owned architecture/database/

bin/dag.sh add-node db-scaling-workflow explore-sharding \
  --agent worker-explore-sharding \
  --role exploration \
  --depends-on db-architecture \
  --assignment-id DB-001 \
  --branch explore/sharding \
  --owned exploration/sharding/

bin/dag.sh add-node db-scaling-workflow explore-replication \
  --agent worker-explore-replication \
  --role exploration \
  --depends-on db-architecture \
  --assignment-id DB-002 \
  --branch explore/replication \
  --owned exploration/replication/

bin/dag.sh add-node db-scaling-workflow explore-nosql \
  --agent worker-explore-nosql \
  --role exploration \
  --depends-on db-architecture \
  --assignment-id DB-003 \
  --branch explore/nosql \
  --owned exploration/nosql/

# 3. Add implementation node (decision handled by orchestrator)
bin/dag.sh add-node db-scaling-workflow implement-scaling \
  --agent worker-implement-scaling \
  --role exploitation \
  --depends-on explore-sharding,explore-replication,explore-nosql,db-architecture \
  --assignment-id IMPL-002 \
  --branch implement/db-scaling \
  --owned src/database/,migrations/,config/

# 4. Add verification and metrics nodes
bin/dag.sh add-node db-scaling-workflow performance-tests \
  --agent worker-performance-tests \
  --role qa \
  --depends-on implement-scaling \
  --assignment-id QA-002 \
  --branch implement/db-scaling \
  --owned tests/performance/

bin/dag.sh add-node db-scaling-workflow load-testing \
  --agent worker-load-testing \
  --role qa \
  --depends-on implement-scaling \
  --assignment-id QA-003 \
  --branch implement/db-scaling \
  --owned tests/load/

bin/dag.sh add-node db-scaling-workflow metrics-collection \
  --agent worker-metrics \
  --role qa \
  --depends-on performance-tests,load-testing \
  --assignment-id METRICS-001 \
  --branch main \
  --owned monitoring/scaling-metrics/

# 5. Add reflection node
bin/dag.sh add-node db-scaling-workflow scaling-reflection \
  --agent worker-reflection \
  --role reflection \
  --depends-on metrics-collection \
  --assignment-id REF-002 \
  --branch main \
  --owned docs/reflection/db-scaling.md

# 6. Execute workflow (orchestrator loop)
# Check ready nodes
bin/dag.sh ready db-scaling-workflow

# Spawn agent for ready architecture node
bin/subagent.sh assignment-create worker-db-architecture \
  --assignment-id ARCH-003 \
  --role architecture \
  --workflow-id db-scaling-workflow \
  --node-id db-architecture \
  --branch main \
  --owned architecture/database/

# Update status and check for next ready nodes
bin/dag.sh status db-scaling-workflow db-architecture running
# ... (agent works) ...
bin/dag.sh status db-scaling-workflow db-architecture done
bin/dag.sh ready db-scaling-workflow

# Now exploration nodes should be ready - spawn multiple parallel agents
bin/dag.sh ready db-scaling-workflow
# Returns: explore-sharding,explore-replication,explore-nosql

# Spawn all ready exploration agents (orchestrator uses workflow definition)
bin/dag.sh ready db-scaling-workflow | while read node_id; do
  # Orchestrator looks up node details from the workflow definition it created
  # or inspects bin/dag.sh show db-scaling-workflow manually
  case "$node_id" in
    explore-sharding)
      ASSIGNMENT_ID="DB-001"; AGENT="worker-explore-sharding"
      BRANCH="explore/sharding"; OWNED="exploration/sharding/" ;;
    explore-replication)  
      ASSIGNMENT_ID="DB-002"; AGENT="worker-explore-replication"
      BRANCH="explore/replication"; OWNED="exploration/replication/" ;;
    explore-nosql)
      ASSIGNMENT_ID="DB-003"; AGENT="worker-explore-nosql"
      BRANCH="explore/nosql"; OWNED="exploration/nosql/" ;;
    *)
      continue ;;
  esac
  
  bin/subagent.sh assignment-create "$AGENT" \
    --assignment-id "$ASSIGNMENT_ID" \
    --role exploration \
    --branch "$BRANCH" \
    --owned "$OWNED" \
    --workflow-id db-scaling-workflow \
    --node-id "$node_id"
done

# Continue workflow execution cycle...
```

### DAG Workflow Status Monitoring

Monitor workflow progress and agent coordination:

```bash
# Get detailed node information
bin/dag.sh show db-scaling-workflow

# Check ready nodes for agent spawning
bin/dag.sh ready db-scaling-workflow

# Check blocked nodes
bin/dag.sh blocked db-scaling-workflow

# List all active workflows
bin/dag.sh list
```

### Integration with Agent Management

DAG workflows integrate with existing agent assignment and status tracking:

```bash
# Create agent assignments with workflow context
bin/subagent.sh assignment-create worker-implement-scaling \
  --assignment-id IMPL-002 \
  --role exploitation \
  --workflow-id db-scaling-workflow \
  --node-id implement-scaling \
  --branch implement/db-scaling \
  --owned src/database/,migrations/

# Check agent assignment against workflow node
bin/subagent.sh assignment-check worker-implement-scaling

# Update workflow status based on agent progress
bin/subagent.sh assignment-status worker-implement-scaling done
bin/dag.sh status db-scaling-workflow implement-scaling done
```

Note: DAG workflows provide structure and dependency tracking, but the orchestrator remains the active workflow controller. Agent spawning and status updates are orchestrator-driven, not automatic, preserving human oversight and intervention capabilities.

## Tests

```bash
tests/run.sh
```
