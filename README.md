# Multiagent

Multiagent is a Rust control plane for coordinating existing coding agents. It
does not implement another coding agent or model loop. It runs Codex, Claude
Code, and Qwen Code in explicit roles, records durable workflow state, and
accepts work only when reviewer evidence matches the exact final Git diff.

## Stateful control server

The container image runs a same-origin web UI and authenticated WebSocket gateway as PID 1. Each task owns an isolated tmux orchestrator session. Paused, completed, and archived tasks retain their workflow state and terminal transcript without retaining an active tmux process; resuming reconstructs the session from that state.

The production container runs its authenticated control server as trusted UID 10000. A root-owned setuid launcher accepts privileged bootstrap and session launch only from that UID. The orchestrator, writer, readers, and authority supervisor run as fixed UIDs 10001 through 10004. Only the setuid-gated `role-agent-exec` path may launch a registered role process; Landlock and Unix ownership enforce its filesystem boundary. The tmux environment is allowlisted so KMS, prod-mcp, GitHub, and AWS workload credentials remain available only to the authority supervisor and trusted control process.

Users are configured in a mounted JSON file. Passwords must be scrypt hashes, never plaintext:

```bash
node bin/hash-password.mjs operator
```

The mounted file has this shape:

```json
{
  "sessionSecret": "at-least-32-random-characters",
  "users": [
    {"username": "operator", "passwordHash": "scrypt$16384$8$1$..."}
  ]
}
```

Task repositories must be provisioned as Git worktrees below `MULTIAGENT_REPOSITORY_ROOT`. Repository provisioning is owned by the deployment rather than the control server, so restarting the UI never fetches or mutates source checkouts.

Important container variables:

- `MULTIAGENT_USERS_FILE`: mounted login configuration, default `/run/secrets/multiagent/users.json`.
- `MULTIAGENT_REPOSITORY_ROOT`: deployment-provisioned Git worktrees available for new tasks.
- `MULTIAGENT_IDLE_TIMEOUT_SECONDS`: inactivity period after which a running task is checkpointed and paused.
- `MULTIAGENT_STATE_S3_URI`: S3 prefix used for recovery snapshots.
- `MULTIAGENT_PUBLIC_URL`: canonical HTTPS origin accepted for browser and WebSocket requests.
- `PROD_MCP_URL`: internal MCP endpoint used only by the authority supervisor.
- `MULTIAGENT_KMS_KEY_ID`: AWS KMS P-256 key alias or ARN used only by the authority supervisor.

The PVC mounted at `/var/lib/multiagent` is the primary store for repositories, CLI conversation history, checkpoints, and session metadata. Final reports, a bounded terminal tail, and the transcript index live under each task's existing `logs` trace root. They reference immutable agent event traces instead of duplicating full transcripts. S3 is the durable recovery and inspection copy.

## Requirements

From a source checkout you need Rust 1.75+, Cargo, Bash, Git, and tmux. Install
and authenticate at least one supported coding-agent CLI. Python 3.8+ is used
only by evaluation and evidence-analysis tools, not the production control
plane.

## Quick Start

Run:

```bash
./launch.sh --session multiagent --root /absolute/path/to/target-repo
```

`launch.sh` is only a compatibility bootstrap. It locates or builds the Rust
binary and immediately executes:

```bash
multiagent launch --session multiagent --root /absolute/path/to/target-repo
```

Launches are clean by default. Resume durable state after an interrupted run
with:

```bash
./launch.sh --resume --session multiagent --root /absolute/path/to/target-repo
```

The default role backends are Codex for orchestration and verification and
Claude Code for workers. To use one backend for every role:

```bash
ORCHESTRATOR_CLI=codex \
WORKER_CLI=codex \
SUBAGENT_CLI=codex \
VERIFIER_CLI=codex \
./launch.sh --root /absolute/path/to/target-repo
```

Supported backend names are `codex`, `claude`, and `qwen`.

## What Runs

```mermaid
flowchart LR
    U["Task"] --> O["Read-only orchestrator"]
    O --> D["Decision + contract"]
    D --> W["Path-scoped writer"]
    W --> S["Canonical Git snapshot"]
    S --> V["Read-only reviewers"]
    V --> G{"Supervisor gates pass?"}
    G -- no --> D
    G -- yes --> C["Atomic completion"]
```

The Rust binary owns decisions, workflow phases, assignments, snapshots,
findings, todos, reviewer evidence, process lifecycle, status, and recovery.
Tmux owns PTYs and interactive terminal lifecycle. Python is restricted to
evaluation and provenance; it does not implement a second production workflow
or acceptance gate.

On production Linux, separate Unix identities isolate the orchestrator, the
single active writer, read-only agents, and the authority supervisor. The
orchestrator can read worker and reviewer state but cannot write the target
repository or protected lifecycle state. Completion is a request to the
supervisor, which checks every gate under the lifecycle lock before changing
the phase to `complete`.

## Common Commands

```bash
multiagent status
multiagent watch
multiagent decision list
multiagent workflow status "$MULTIAGENT_WORKFLOW_ID"
multiagent subagent list
multiagent subagent gate-check
multiagent orchestrator complete
```

Normally the orchestrator issues lifecycle and subagent commands. Operators use
the status, watch, recovery, and inspection commands to supervise a run.

## Documentation

- [Decisions](docs/decisions.md) — why the control plane and backend boundary
  have this shape.
- [Architecture](docs/architecture.md) — components, authority boundaries,
  lifecycle, state, and evaluation boundary.
- [Getting started and operations](docs/getting-started.md) — configuration,
  normal operation, decisions, agents, recovery, traces, and troubleshooting.

## Test

```bash
cargo test
bash tests/run.sh
```

Linux authority-boundary coverage is exercised by:

```bash
bash tests/malicious-orchestrator.sh
```
