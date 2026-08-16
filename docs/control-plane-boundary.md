# Control-Plane Boundary

`multiagent` is the single command surface. The source-checkout `launch.sh`
builds or locates the Rust binary and execs `multiagent launch`. Packaged
releases install the binary directly.

Rust owns production decisions and durable state:

- repository snapshots and diff hashes;
- decision ledgers, workflow DAGs, and implementation lifecycle transitions;
- write-policy checks and approvals;
- assignments, checkpoints, and Git worktree metadata;
- findings, repair TODOs, resolution and closure evidence;
- durable reviewer findings, which cannot be replaced by a later pass on the
  same candidate without first entering the repair loop;
- validation leases and bounded validation subprocesses;
- launch configuration, tmux subprocess orchestration, status, watch, and
  recovery behavior.

There is no production shell control plane. Rust invokes tmux as a normal child
process for session/window operations and terminal capture. Rust does not
allocate or emulate a PTY; tmux continues to own terminal lifecycle and
interactive process semantics. This keeps PTY behavior without preserving shell
implementations.

In the production Linux-container boundary, tmux runs as the read-only
orchestrator UID. A raw tmux window therefore cannot acquire repository writes.
Worker/reviewer transitions use the Rust binary's narrowly gated
`role-agent-exec` entrypoint: it accepts only a persisted named headless coding
agent, validates the configured root-owned agent binary, and starts the shared
Rust runner in a dedicated process group under the role's UID. The runner then
executes the recorded Codex, Claude, or Qwen Code backend through argv and stdin.
A minimal wait-only parent retains no workflow discretion;
it exists solely to forward pane termination to the complete role process tree.
`subagent kill` waits for that boundary to close, preventing detached or late
worker output from modifying the workspace after cancellation. The setuid
privilege gate drops privilege for every other command, including generic
`role-exec`, so bypassing the high-level CLI cannot create an arbitrary writer
shell. Lifecycle enforcement is also derived from the orchestrator's real UID,
not solely from its mutable environment. Before the privileged bridge starts a
writer it revalidates the assignment against the live workflow phase and
approved implementation context; setting
`MULTIAGENT_LIFECYCLE_ENFORCEMENT=0` cannot reopen a completed workflow.

Headless runs retain raw stdout/stderr, normalized JSONL events, provider session
identity when available, the final message, and the exit/cancellation reason
under `MULTIAGENT_LOG_DIR/agents`. Each invocation receives an immutable
`attempt-NNNN` directory and `latest` points to the newest attempt, so restore
does not overwrite the trace it relies on. This directory may be mounted outside
an evaluation container so evidence survives task teardown.

Python under `evaluation/` is limited to benchmark adapters, status readers,
and provenance. SWE Bench adapters launch the production workflow and pass the
current workspace diff to the official scorer. They neither derive a second
acceptance decision nor perform production state transitions.

The important benefit is not command rendering or startup speed. A single
locked writer makes overlap checks, duplicate detection, lifecycle gates,
atomic publication, and child exit-code propagation consistent across all
entry points. This eliminates time-of-check/time-of-use races that separate
shell and Python writers could otherwise introduce.
