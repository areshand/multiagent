# Control-Plane Boundary

`bin/multiagent` is the single user-facing command surface. The source-checkout
launcher builds and executes the Rust binary; packaged releases can set
`MULTIAGENT_BIN` to a prebuilt binary.

Rust owns production decisions and durable state:

- repository snapshots and diff hashes;
- decision ledgers, workflow DAGs, and implementation lifecycle transitions;
- write-policy checks and approvals;
- assignments, checkpoints, and Git worktree metadata;
- findings, repair TODOs, resolution and closure evidence;
- validation leases and bounded validation subprocesses.

Shell is an external runtime adapter for tmux session/window operations,
terminal capture, and recovery interaction. The Rust CLI dispatches these
adapters for `launch`, `status`, `watch`, and tmux-oriented `subagent` commands.
Rust does not allocate or emulate a PTY; tmux continues to own terminal
lifecycle and interactive process semantics.

Python is the evaluation and compatibility client. SWE Bench adapters can read
the version-1 state and evidence formats, derive benchmark-specific evidence,
and publish evaluator results. Python must not become a second writer for
production control-plane state. Temporary legacy entry points are guarded by
`MULTIAGENT_USE_LEGACY_*` environment variables and exist for parity diagnosis,
not as the normal execution path.

The important benefit is not command rendering or startup speed. A single
locked writer makes overlap checks, duplicate detection, lifecycle gates,
atomic publication, and child exit-code propagation consistent across all
entry points. This eliminates time-of-check/time-of-use races that separate
shell and Python writers could otherwise introduce.
