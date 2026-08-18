# Decisions

This document records the durable design decisions behind the current
implementation. It describes chosen boundaries and their consequences, not a
future product backlog.

## Compose Coding Agents Instead of Reimplementing One

**Decision:** Multiagent coordinates existing coding-agent CLIs. It does not
implement a shared model API, tool loop, context manager, or autonomous coding
agent.

**Why:** Codex, Claude Code, and Qwen Code already own model interaction, tool
use, context, and provider-specific behavior. The missing capability is a
provider-neutral control plane for role assignment, isolation, evidence, and
acceptance.

**Consequence:** Agent behavior and solution quality can differ by backend.
Multiagent standardizes process and evidence contracts, not reasoning.

## One Rust Production Control Plane

**Decision:** Rust owns every production state transition and subprocess
operation. `launch.sh` remains only as a source-checkout compatibility entrypoint
that builds or locates the binary and execs `multiagent launch`.

**Why:** Separate shell, Python, and Rust writers created inconsistent locking,
exit propagation, and time-of-check/time-of-use behavior.

**Consequence:** Shell scripts may bootstrap or test the system, and Python may
run evaluations, but neither is a second workflow implementation.

## Keep Tmux and Do Not Reimplement PTYs

**Decision:** Rust invokes tmux as a child process; tmux continues to own PTYs,
interactive terminal behavior, panes, and session persistence.

**Why:** PTY emulation is not part of the orchestration problem and would add a
large compatibility surface without improving authority or evidence.

**Consequence:** Live sessions require tmux. Headless backend execution remains
available inside role processes.

## Use a Small Static Agent Backend Contract

**Decision:** `codex`, `claude`, and `qwen` are statically registered Rust
backends. Each backend supplies executable/argv construction, prompt delivery,
capabilities, and output normalization to one shared process supervisor.

**Why:** A static registry is sufficient for the bundled CLIs and keeps
provider-specific flags out of workflow and authorization code. A dynamic
plugin ABI would add versioning and trust problems before a concrete need
exists.

**Consequences:**

- headless single-task execution, exit status, cancellation, raw logs, and a
  final message are the common minimum;
- structured events, usage reporting, native resume, and interactive support
  are declared capabilities rather than assumed features;
- requesting an unsupported capability fails explicitly;
- adding a backend must not modify workflow authority or writable roots.

## Put Authority in a Separate Supervisor Process

**Decision:** On production Linux, a small authority supervisor owns protected
workflow state, one-time launch authorizations, the writer lease, and sealed
reviewer evidence. Peer credentials and persisted role metadata determine which
typed operations a process may request.

**Why:** Prompt instructions cannot reliably prevent an orchestrator from
writing files or declaring success. The restriction must exist below the agent
CLI and tmux session.

**Consequence:** The orchestrator can inspect the repository and every agent's
state, create work requests, and ask for completion, but it cannot directly edit
the target or protected state. A bypassed high-level CLI still runs under the
same non-writing Unix identity.

The tmux-owning identity also hosts a fixed Rust lifecycle reconciler. That
process may observe and close panes, but protected assignment and lease changes
still cross the supervisor's typed authority socket. This avoids granting the
shared worker/reviewer group access to the tmux control socket.

## Keep Topology Adaptive and Writing Path-Scoped

**Decision:** The orchestrator chooses the task graph, worker count, and each
worker's responsibility. The supervisor does not encode a preferred topology.
It admits write-capable workers only when their durable assignments own
non-overlapping paths and the lifecycle gate is open.

On Linux kernels with Landlock, disjoint writers may run concurrently under
per-process write allowlists. If that isolation is unavailable, the supervisor
falls back to a single active writer because shared Unix ownership cannot
safely distinguish two processes using the writer UID.

**Why:** Task decomposition is semantic and belongs to the orchestrator;
non-overlap, lifecycle readiness, and isolation capability are mechanical and
belong to the supervisor. A hard-coded worker count makes simple work expensive
and parallelizable work unnecessarily slow.

**Consequence:** Multiagent imposes no fixed worker count or responsibility
catalog. Available isolation and actual path conflicts determine concurrency,
while completion still evaluates one canonical diff.

## Bind Semantics and Reviews to Immutable Evidence

**Decision:** The original task, registered contract artifact, approved
implementation context, candidate diff, and reviewer results are stored with
SHA-256 bindings. Contract scouts emit structured positive and negative rules.
Workers and reviewers receive the immutable original task and exact registered
contract, not an orchestrator paraphrase.

**Why:** A mutable checklist lets an orchestrator silently narrow the request or
reinterpret a failed implementation as acceptable.

**Consequences:**

- a plan contradicting a registered `must-not` rule cannot open the writer gate;
- replacement reviewers may narrow runtime scope but not semantic scope;
- changing the diff invalidates prior hash-bound acceptance;
- sealed reviewer identity proves who produced evidence, not that the semantic
  judgment was correct.

## Make Completion Supervisor-Owned and Atomic

**Decision:** Direct lifecycle transitions to `complete` are rejected.
`multiagent orchestrator complete` asks the supervisor to run lifecycle and
technical gates under the lifecycle lock; only then may the supervisor write
`phase=complete`.

**Why:** Marking completion before running gates leaves a visible completed
state even when verification subsequently fails.

**Consequence:** Completion either publishes one fully checked state or leaves
the workflow in its prior phase. Open findings and todos route another bounded
implementation iteration.

## Preserve Raw and Normalized Traces

**Decision:** Every agent attempt gets an immutable trace directory containing
metadata, raw stdout/stderr, normalized JSONL when available, final output, and
exit/cancellation state. Evaluation may mount the trace root outside the task
container.

**Why:** Normalization is useful for analysis but can lose provider detail; raw
logs remain the source of truth. External storage is required for postmortems
after task-container teardown.

## Keep Evaluation Outside the Acceptance Boundary

**Decision:** Evaluation adapters prepare task containers, launch the production
workflow, collect its workspace diff and traces, and hand the patch to the
official benchmark verifier. They do not repeat scoring or invent additional
submission validation.

**Why:** A second eval-side solver or acceptance layer can make infrastructure
look successful while measuring a different system.

**Consequence:** Official verifier feedback may be used for explicitly labeled
engineering regression work, but a patch repaired from that feedback is not
reported as a clean one-shot benchmark result.

## Non-Goals

- identical output across coding-agent backends;
- proving that reviewer judgment is semantically correct;
- replacing human review for consequential changes;
- implementing PTYs, a terminal UI, a model gateway, or a dynamic plugin ABI;
- allowing an agent backend to grant permissions or advance lifecycle state.
