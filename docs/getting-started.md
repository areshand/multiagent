# Getting Started and Operations

This guide explains how to install, launch, operate, inspect, and recover the
current Multiagent implementation. Read [Architecture](architecture.md) for the
system boundary and [Decisions](decisions.md) for its rationale.

## Requirements

Source-checkout operation requires:

- Rust 1.75 or newer and Cargo;
- Bash and Git;
- tmux;
- at least one authenticated coding-agent CLI: Codex, Claude Code, or Qwen
  Code.

Python 3.8+ is needed only for evaluation and evidence-analysis commands. The
production launch path is Rust.

Build and test the binary:

```bash
cargo build
cargo test
```

The binary exposes its command groups with:

```bash
target/debug/multiagent
target/debug/multiagent decision --help
target/debug/multiagent workflow --help
```

## Launch

From this repository, launch against any Git repository:

```bash
./launch.sh \
  --session multiagent \
  --root /absolute/path/to/target-repo
```

`launch.sh` builds or locates `multiagent` and execs:

```bash
multiagent launch \
  --session multiagent \
  --root /absolute/path/to/target-repo
```

The default launch creates an orchestrator window and a Rust lifecycle-supervisor
window. It is a clean launch:
persisted subagents are not automatically restored.

Resume after an interrupted run:

```bash
./launch.sh --resume \
  --session multiagent \
  --root /absolute/path/to/target-repo
```

Use `--no-attach` for automation or monitoring from another terminal:

```bash
./launch.sh --no-attach --session multiagent --root /absolute/path/to/repo
```

## Configure Agent Backends

Role selection is environment-based:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ORCHESTRATOR_CLI` | `codex` | orchestrator backend |
| `WORKER_CLI` | `claude` | writable worker backend |
| `SUBAGENT_CLI` | value of `WORKER_CLI` | generic named subagent backend |
| `VERIFIER_CLI` | `codex` | scout and reviewer backend |
| `CODEX_BIN` | `codex` | Codex executable |
| `CLAUDE_BIN` | `claude` | Claude Code executable |
| `QWEN_BIN` | `qwen` | Qwen Code executable |

Supported backend names are `codex`, `claude`, and `qwen`.

Use Codex for every role:

```bash
ORCHESTRATOR_CLI=codex \
WORKER_CLI=codex \
SUBAGENT_CLI=codex \
VERIFIER_CLI=codex \
./launch.sh --root /absolute/path/to/repo
```

Use Qwen Code for every role:

```bash
ORCHESTRATOR_CLI=qwen \
WORKER_CLI=qwen \
SUBAGENT_CLI=qwen \
VERIFIER_CLI=qwen \
./launch.sh --root /absolute/path/to/repo
```

Qwen Code remains responsible for its model provider, tools, and context. Test
an authenticated Qwen installation with:

```bash
bash tests/live-qwen-smoke.sh
```

Inspect declared backend capabilities:

```bash
multiagent agent backend-info codex
multiagent agent backend-info claude
multiagent agent backend-info qwen
```

Headless execution controls:

| Variable | Purpose |
| --- | --- |
| `MULTIAGENT_AGENT_HEADLESS` | normalized headless runner for Codex/Claude; Qwen v1 is headless |
| `MULTIAGENT_NATIVE_RESUME` | request provider-native resume when supported |
| `MULTIAGENT_AGENT_TIMEOUT_SECONDS` | outer wall-clock limit for a backend process |
| `MULTIAGENT_AGENT_MAX_TURNS` | optional Qwen turn budget |
| `MULTIAGENT_AGENT_MAX_WALL_TIME` | optional Qwen wall-time budget |
| `MULTIAGENT_AGENT_MAX_TOOL_CALLS` | optional Qwen tool-call budget |

## Runtime Configuration

Important launch variables:

| Variable | Default |
| --- | --- |
| `MULTIAGENT_SESSION` | `multiagent` |
| `MULTIAGENT_ROOT` | launcher directory unless `--root` is supplied |
| `MULTIAGENT_STATE_DIR` | `$MULTIAGENT_ROOT/.multiagent` |
| `MULTIAGENT_WRITE_POLICY` | `$MULTIAGENT_ROOT/docs/write-policy.paths` |
| `MULTIAGENT_PROMPT` | this checkout's `orchestrator_prompt.md` |
| `MULTIAGENT_VERIFIER_MAX_ITERATIONS` | `3` |

The prompt path is resolved from the launcher checkout, not the target
repository. This allows one Multiagent installation to operate on another
project without copying prompt modules into it.

## Normal Workflow

The orchestrator normally performs the commands in this section. Operators use
them for inspection or deliberate manual recovery.

### 1. Record the Decision

Create a decision, record alternatives, and commit one plan:

```bash
multiagent decision init DEC-001 --title "Choose the implementation"

multiagent decision add-alternative DEC-001 \
  --plan-id PLAN-A \
  --summary "Small compatible change" \
  --proposed-by contract-scout-01 \
  --expected-outcome "Preserve behavior with minimal scope"

multiagent decision add-assumption DEC-001 \
  --assumption-id ASSUME-1 \
  --statement "The public interface remains stable" \
  --validation-method "source and test inspection"

multiagent decision commit DEC-001 \
  --selected-plan PLAN-A \
  --reason "Matches the registered contract"

multiagent decision list
multiagent decision show DEC-001
```

Decision records are durable under `$MULTIAGENT_STATE_DIR/decisions`.

### 2. Register the Contract

For tasks with API, compatibility, security, benchmark, or hidden-contract risk,
spawn a read-only scout:

```bash
SUBAGENT_CLI="${VERIFIER_CLI:-codex}" \
multiagent subagent spawn contract-scout-01 \
  --role scout \
  --instruction "Extract structured must and must-not contract rules. Do not edit."

multiagent workflow contract-register "$MULTIAGENT_WORKFLOW_ID" \
  --scout contract-scout-01
```

The supervisor seals the scout result and records its hash. Later workers and
reviewers receive the immutable original task and exact registered artifact.

### 3. Open the Implementation Gate

After an independent decision-authority review passes, bind the approved
implementation context:

```bash
multiagent workflow prepare-implementation "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DEC-001 \
  --plan-id PLAN-A \
  --decision-revision 1 \
  --implementation-context /absolute/path/to/implementation-context.md \
  --authority-review review-01-authority

multiagent workflow transition "$MULTIAGENT_WORKFLOW_ID" implementation
```

The context must contain the exact registered contract. A plan that contradicts
a registered `must-not` rule is rejected before a writer starts.

### 4. Assign and Run a Worker

Create metadata before spawning a writer:

```bash
multiagent subagent assignment-create worker-01 \
  --assignment-id IMPL-001 \
  --role exploitation \
  --decision-id DEC-001 \
  --plan-id PLAN-A \
  --branch "$(git -C "$MULTIAGENT_ROOT" branch --show-current)" \
  --owned src/,tests/

SUBAGENT_CLI="${WORKER_CLI:-claude}" \
multiagent subagent spawn worker-01 \
  --role worker \
  --own src/,tests/ \
  --assignment-id IMPL-001 \
  --workflow-id "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DEC-001 \
  --plan-id PLAN-A \
  --infra-retries 1 \
  --instruction-file /absolute/path/to/worker-instruction.md

multiagent subagent assignment-check worker-01
```

Only the supervisor-authorized writer receives temporary access to its existing
owned paths. On Linux, disjoint path-scoped writers may run concurrently;
overlapping writers are rejected. The lifecycle supervisor observes completion,
settles ownership and validation state, and applies only the explicitly
budgeted infrastructure retry. Use `multiagent status` to decide semantic next
steps such as repairing blocked work.

Update a durable checkpoint during long work:

```bash
multiagent subagent checkpoint-update worker-01 \
  --step "implementation complete; focused tests running" \
  --idempotency "rerun focused tests before acceptance" \
  --status running
```

### 5. Review the Canonical Diff

Freeze the current repository state:

```bash
multiagent snapshot --root "$MULTIAGENT_ROOT" --base HEAD --format json
```

Transition to post-implementation with the reported hash, then run read-only
scope, technical, decision-drift, and reflection reviews. Review instructions
must include the original task, registered contract, approved context, and
canonical diff. The lifecycle supervisor finalizes terminal reviewer processes
so their output can be sealed.

Record review findings and todos through `multiagent workflow` and
`multiagent subagent` commands. A changed diff invalidates previous acceptance.
An open finding returns the workflow to another pre-implementation iteration.

### 6. Complete Atomically

Inspect both gates:

```bash
multiagent workflow completion-check "$MULTIAGENT_WORKFLOW_ID"
multiagent subagent gate-check
```

Request completion:

```bash
multiagent orchestrator complete
```

The orchestrator cannot directly write `complete`. The supervisor runs the
lifecycle and technical gates under the lifecycle lock and changes the phase
only when every requirement passes.

## Findings and Repair

Verifier findings are durable state rather than prose that a later reviewer can
silently override. Inspect the gate at any time:

```bash
multiagent subagent gate-check
multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
```

A repair iteration should:

1. preserve the original task and registered contract;
2. create or reuse a bounded assignment for implicated source paths;
3. rerun the failing validation or a justified source-derived equivalent;
4. ask a fresh read-only verifier to recheck the new diff;
5. close the exact todo with sealed, hash-bound evidence.

The default verifier follow-up cap is three iterations. Override it at launch:

```bash
MULTIAGENT_VERIFIER_MAX_ITERATIONS=2 ./launch.sh --root /absolute/path/to/repo
```

## DAGs

Use a DAG when work has real dependencies. Disjoint read-only exploration may
fan out; writer publication remains serialized.

```bash
multiagent dag init feature-001 --title "Feature implementation"

multiagent dag add-node feature-001 inspect-contract \
  --agent scout-01 \
  --assignment-id SCOUT-001 \
  --role scout \
  --branch main \
  --owned src/

multiagent dag add-node feature-001 implement \
  --agent worker-01 \
  --assignment-id IMPL-001 \
  --role exploitation \
  --branch feature/work \
  --owned src/,tests/ \
  --depends-on inspect-contract

multiagent dag ready feature-001
multiagent dag status feature-001 inspect-contract done
multiagent dag show feature-001
multiagent dag blocked feature-001
```

DAG state describes readiness and dependencies. The orchestrator remains the
controller that spawns agents and records status.

## Status and Monitoring

Show agent, assignment, and workflow state:

```bash
multiagent status
multiagent subagent list
multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
```

Render a live terminal dashboard or one snapshot:

```bash
multiagent watch
multiagent watch --once
multiagent watch --interval 2 --log-lines 80
```

Inspect one subagent without writing to its pane:

```bash
multiagent subagent inspect worker-01 --lines 160
```

## Recovery

After an interrupted session, relaunch with `--resume`, then inspect the
conservative recovery plan:

```bash
multiagent subagent recover-plan
```

Possible actions include:

- `restore`: closed agent with enough durable context;
- `skip-open`: its tmux window already exists;
- `skip-finalized`: it completed or was intentionally stopped;
- `skip-blocked`: it needs an external decision;
- `skip-unknown`: state is insufficient and needs manual inspection.

Restore only after reviewing the plan:

```bash
multiagent subagent restore NAME
multiagent subagent restore-all
```

Restore creates a fresh process attempt and preserves prior transcripts and
traces. It does not overwrite the evidence used for recovery.

## Write Policy

The target repository is the default write root. Outside-root writes require a
narrow recorded approval:

```bash
multiagent policy init
multiagent policy show
multiagent policy check README.md /tmp/report-output
multiagent policy approve /tmp/report-output \
  --actor orchestrator \
  --assignment-id REPORT-001 \
  --reason "user approved report export"
```

Broad roots such as `/`, a home directory, `/tmp`, `/Users`, `/home`, `/usr`,
and `/var` are rejected by default. `--force` is reserved for an explicit user
decision. Workers must not edit `docs/write-policy.paths` directly.

## Logs and Traces

Default logs are under `$MULTIAGENT_STATE_DIR/logs`:

```text
logs/
  orchestrator.log
  NAME.log
  agents/
    ROLE/
      attempt-NNNN/
        metadata.json
        stdout.log
        stderr.log
        events.jsonl
        final-message.txt
```

File presence depends on backend capabilities and exit path. Raw output remains
the diagnostic source of truth. Mount or configure the trace directory outside
an ephemeral evaluation container when postmortem analysis is required.

## Evaluation

Evaluation is optional and separate from normal operation. No-spend adapter
checks include:

```bash
python3 -m evaluation.cli --adapter ponytail --selftest
python3 -m evaluation.cli --adapter orchestration --selftest
```

The SWE-bench Pro runner launches the same production workflow and passes its
workspace diff to the official scorer:

```bash
python3 -m evaluation.swe_bench_pro --help
```

See [evaluation/README.md](../evaluation/README.md) for dataset, image, resource,
and provenance details. Evaluation adapters do not constitute another solver or
acceptance gate.

## Tests

Run the full local contract suite:

```bash
cargo fmt --check
cargo test
bash tests/run.sh
```

On Linux, test the process and authority boundary:

```bash
bash tests/malicious-orchestrator.sh
```

The Qwen live smoke test is opt-in and requires operator authentication; normal
tests use fake executables and do not require network access.

## Troubleshooting

### The orchestrator cannot edit the target

This is expected. Only a supervisor-authorized writer may modify assigned paths.
Create an implementation assignment and spawn a writer instead of opening a raw
tmux pane.

### A writer cannot start

Check the workflow phase, decision/plan IDs, implementation-context hash,
assignment paths, existing writer lease, and configured backend executable:

```bash
multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
multiagent subagent assignment-show NAME
multiagent agent backend-info "${WORKER_CLI:-claude}"
```

### Completion is rejected

Run both checks and inspect open findings/todos or stale diff evidence:

```bash
multiagent workflow completion-check "$MULTIAGENT_WORKFLOW_ID"
multiagent subagent gate-check
```

Do not edit lifecycle state manually. Repair the failed condition and obtain a
fresh sealed review for the current diff.

### A reviewer or orchestrator disconnects

A broken client connection should not stop the authority supervisor. Inspect
the role status and start a bounded replacement if necessary. A replacement may
narrow runtime scope but must receive the same original task and registered
contract.

### The tmux session disappeared

Relaunch with `--resume`, run `multiagent subagent recover-plan`, and restore
only entries classified as recoverable.
