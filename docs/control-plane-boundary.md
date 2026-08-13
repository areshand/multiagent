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
- validation leases and bounded validation subprocesses;
- launch configuration, tmux subprocess orchestration, status, watch, and
  recovery behavior.

There is no production shell control plane. Rust invokes tmux as a normal child
process for session/window operations and terminal capture. Rust does not
allocate or emulate a PTY; tmux continues to own terminal lifecycle and
interactive process semantics. This keeps PTY behavior without preserving shell
implementations.

Python is the evaluation client and reusable evidence-analysis layer. SWE Bench
adapters read version-1 state and evidence, derive benchmark-specific evidence,
and publish evaluator results. Whenever evaluation needs a production state
transition, it invokes the Rust binary instead of implementing a second writer.

The important benefit is not command rendering or startup speed. A single
locked writer makes overlap checks, duplicate detection, lifecycle gates,
atomic publication, and child exit-code propagation consistent across all
entry points. This eliminates time-of-check/time-of-use races that separate
shell and Python writers could otherwise introduce.
