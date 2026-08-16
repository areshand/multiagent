# PRD: Pluggable Coding-Agent Backends

Status: Implemented; Qwen live-auth smoke pending

## Problem

The Rust runtime currently constructs Codex and Claude CLI commands directly in
`runtime.rs`. Adding another coding agent would add more provider-specific
branches to orchestration, permission, tracing, and lifecycle code.

We need one small backend contract that can run:

- Codex CLI;
- Claude Code;
- Qwen Code as an open-source coding agent, independently of which model or
  inference provider Qwen Code uses.

This is an agent-runtime abstraction, not a common model API and not a new
agent loop implemented by Multiagent.

## Product Goal

Let each workflow role select a supported coding-agent backend without changing
Multiagent's workflow semantics, security boundary, trace layout, or benchmark
submission behavior.

## Users and Use Cases

- Operators can compare agents on the same task and role policy.
- Developers can add a backend without editing the supervisor state machine.
- Evaluators can preserve raw and normalized traces outside task containers and
  submit the resulting workspace diff to the official benchmark scorer.

## Requirements

### Required backend contract

Every v1 backend must support:

1. A non-interactive, single-task invocation.
2. A configured working directory and prompt input that does not depend on a
   shell-specific quoting convention.
3. A final message, raw stdout/stderr, exit status, and cancellation.
4. Read-only or workspace-write execution as determined by the outer Rust
   supervisor.
5. A stable backend name, executable path, version preflight, and explicit
   failure when a requested capability is unavailable.
6. Trace correlation with workflow, role, assignment, process, and optional
   provider session identifiers.

### Provider-specific capabilities

Structured events, native session resume, usage data, interactive UI, and
provider-side sandboxing are capabilities, not assumptions. The runtime must
query the selected backend's declared capabilities and must not silently
simulate unsupported behavior.

Qwen Code v1 support uses its complete open-source coding-agent runtime. It may
connect to Qwen or another supported model provider; Multiagent does not
implement Qwen Code's tool loop.

### Supervisor invariants

- Rust remains authoritative for role assignment, workflow transitions,
  writable paths, UID isolation, timeouts, cancellation, durable state, and the
  final acceptance gate.
- Agent approval or `--yolo` flags cannot grant access beyond the outer role
  sandbox.
- An agent's success exit code or final message is not verification evidence.
- Evaluation adapters only collect the workspace result and submit it to the
  benchmark. They do not duplicate acceptance or scoring.
- Credentials are passed through the environment or provider-native stores,
  never rendered into command logs. Trace storage retains restrictive
  permissions and records any redaction performed.

## Non-goals

- Reimplementing a shared agent loop, tool registry, context manager, or model
  protocol.
- Guaranteeing identical reasoning or solution quality across agents.
- Reproducing every Codex CLI feature; parity is limited to features used by
  this repository.
- Migrating tmux or PTY behavior. Existing interactive compatibility remains;
  Qwen Code v1 only needs the headless backend contract.
- Letting an agent backend make workflow, authorization, or verification
  decisions.

## Configuration

Existing role-level CLI selection becomes backend selection. The initial names
are `codex`, `claude`, and `qwen`. Each backend has an overridable executable
path. Invalid names and missing executables fail during launch preflight.

Existing Codex and Claude environment variables remain compatible for one
deprecation cycle. Qwen Code receives an equivalent executable override without
embedding provider credentials in repository configuration.

## Acceptance Criteria

- Existing Codex and Claude launches produce equivalent commands, permissions,
  lifecycle state, final artifacts, and cancellation behavior after extraction.
- Unit tests cover command specifications and event/result normalization for all
  three backends, including malformed events, non-zero exits, timeout, and
  cancellation.
- Integration tests use fake executables to prove role access, trace persistence,
  final-message capture, and unsupported-capability failures without network
  access.
- An opt-in live smoke test completes one read-only and one workspace-write task
  with Qwen Code inside the existing supervisor boundary.
- The first ten-row SWE-Bench regression run with the Codex backend does not lose
  any row previously solved by the pre-refactor baseline. Qwen Code results are
  reported separately and are not treated as proof of Codex parity.
- `launch.sh` continues to launch the Rust workflow unchanged for existing
  callers.

## Success Measures

- Adding a fourth process-based agent requires a backend module and contract
  tests, but no changes to workflow or authorization logic.
- No provider-specific command construction remains in the workflow state
  machine.
- Every run identifies its backend and version, and retains enough raw evidence
  to diagnose a provider or adapter failure after its container exits.

## Validation Snapshot

The Codex first-ten SWE-Bench Pro regression run scored 6/10 versus the stored
5/10 baseline. All previously solved rows (1, 2, 3, 4, and 6) remained solved;
row 7 became solved. Raw workflow traces for every row were exported outside the
task containers before teardown.

Offline unit and integration coverage exercises Codex, Claude, and Qwen command
construction and Qwen process behavior. The opt-in live Qwen read/write smoke
test is implemented but remains a rollout check until an operator authenticates
Qwen Code; credentials are intentionally not bundled with this repository.
