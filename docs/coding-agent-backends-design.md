# Refactoring Design: Coding-Agent Backend Boundary

Status: Implemented; Codex regression gate passed

Related product requirements: [Pluggable Coding-Agent Backends](coding-agent-backends-prd.md)

## Design Summary

Extract provider-specific process construction and output decoding from
`runtime.rs` into three small Rust backends. Keep one shared process supervisor
for role isolation, tmux integration, cancellation, trace persistence, and
durable workflow state.

```text
workflow / role state machine
            |
            v
      AgentBackend registry
       /       |       \
   Codex     Claude    Qwen Code
       \       |       /
            v
 shared process + role sandbox supervisor
            |
            v
 raw logs + normalized events + final result
```

The backend describes how to invoke an existing coding agent. It never decides
whether the process may write, whether verification passed, or whether the
workflow may advance.

## Current Boundary

`build_cli_command` currently combines backend selection, shell rendering,
prompt delivery, final-message capture, and Codex-specific sandbox flags. Its
callers also own the durable assignment and role lifecycle.

The refactor separates these concerns:

| Concern | Owner |
| --- | --- |
| Workflow phase and role | Rust workflow state machine |
| Writable roots and UID | Rust role sandbox |
| Process group, timeout, cancellation | Shared process supervisor |
| tmux window and terminal capture | Existing tmux integration |
| Executable, arguments, input/output protocol | Agent backend |
| Provider event decoding | Agent backend |
| Raw/normalized trace persistence | Shared trace sink |
| Correctness and acceptance | Verifier and workflow gate |
| Benchmark scoring | Official benchmark runner |

## Core Types

The first extraction should remain synchronous and use the standard library so
it does not require an async runtime merely to construct commands.

```rust
pub enum AgentBackendId {
    Codex,
    Claude,
    Qwen,
}

pub struct AgentRequest {
    pub role: String,
    pub cwd: PathBuf,
    pub prompt: Vec<u8>,
    pub access: RoleAccess,
    pub final_output: PathBuf,
    pub trace_dir: PathBuf,
    pub resume_session: Option<String>,
}

pub struct CommandSpec {
    pub program: PathBuf,
    pub args: Vec<OsString>,
    pub cwd: PathBuf,
    pub env: BTreeMap<OsString, OsString>,
    pub stdin: InputSpec,
}

pub struct AgentCapabilities {
    pub structured_events: bool,
    pub native_resume: bool,
    pub usage_events: bool,
    pub interactive: bool,
}

pub trait AgentBackend {
    fn id(&self) -> AgentBackendId;
    fn capabilities(&self) -> AgentCapabilities;
    fn preflight(&self) -> Result<BackendVersion, AgentError>;
    fn command(&self, request: &AgentRequest) -> Result<CommandSpec, AgentError>;
}
```

Provider JSON formats currently share enough structure that decoding and final
result selection are implemented once in the runner. A provider-specific
decoder should be added to the trait only when a real backend cannot be
normalized without it.

`CommandSpec` is argv-based. Shell text is rendered only at the existing tmux or
privilege-bridge boundary, using one audited escaping function. Prompt contents
are delivered through stdin or a supervisor-created file and are never inserted
into a command substitution.

## Normalized Result and Trace

The common event schema stays deliberately small:

```rust
pub enum AgentEvent {
    Started { session_id: Option<String> },
    Text { text: String },
    ToolStarted { id: String, name: String },
    ToolFinished { id: String, success: bool },
    Usage { input_tokens: u64, output_tokens: u64 },
    Completed { final_message: String },
    Diagnostic { level: Level, message: String },
}
```

Backends may omit optional event types. The shared trace sink always stores:

- metadata with backend name/version and workflow correlation identifiers;
- raw stdout and stderr without lossy rewriting;
- normalized JSONL events when decoding is available;
- process exit, timeout, signal, and cancellation reason;
- the final-message artifact. The workflow-level SWE trace archive separately
  binds the submitted diff and official row identity.

Raw logs remain the diagnostic source of truth. Normalized events are an index,
not a replacement, so adding a decoder cannot discard provider data.

## Backend Mapping

### Codex

- Headless: `codex exec` with prompt on stdin.
- Final result: retain `--output-last-message` during the behavior-preserving
  extraction.
- Structured events: adopt `--json` only in a separate trace change, because it
  changes stdout semantics.
- Access flags: selected from role access, while Linux continues to rely on the
  inherited outer Landlock/UID boundary where nested Codex sandboxing is not
  available.

### Claude Code

- Headless execution becomes the default backend contract instead of depending
  on interactive command rendering.
- Structured stream output is decoded when enabled; otherwise raw output and
  exit status still produce a valid result.
- Provider permission bypass is allowed only inside the outer role sandbox.

### Qwen Code

- Use the Qwen Code agent's headless mode and `stream-json` output.
- Map native session identifiers to `resume_session` when requested.
- Use provider approval bypass only after the supervisor has installed the role
  sandbox.
- Model/provider configuration remains Qwen Code configuration. It is not added
  to Multiagent's workflow state machine.
- Interactive/PTY integration is deferred; Qwen v1 is headless only.

## Capability Policy

Required workflow behavior cannot depend on an optional capability. For
example, generic recovery may start a new process with persisted task context;
native resume is used only when explicitly requested and supported. A request
for native resume on an unsupported backend fails with a typed error rather
than silently starting a new conversation.

The registry owns backend lookup:

```text
codex  -> CodexBackend
claude -> ClaudeBackend
qwen   -> QwenBackend
```

There is no dynamic plugin ABI in v1. A Rust trait and static registry are the
simplest sufficient extension point for three bundled process backends.

## Security Invariants

1. `AgentRequest.access` is derived from persisted role state, never from agent
   output or mutable provider configuration.
2. The backend cannot add writable roots, change UID, disable lifecycle checks,
   or mark verification complete.
3. Approval-bypass flags are rejected unless the shared supervisor confirms an
   outer isolation boundary for the role.
4. Executable paths are operator configuration. They are validated during
   preflight and are not accepted from task prompts.
5. Arguments and environment metadata are logged with credential values
   redacted. Credentials are not passed as argv.
6. Cancellation terminates the complete process group before the role is
   finalized, regardless of backend behavior.

## File Layout

The first implementation intentionally stays in `src/agent.rs`: three short
command builders, one registry, one runner, and one trace normalizer. Split it
into `process`, `trace`, and provider modules only when independent ownership or
compile-time boundaries justify the extra files.

Initially, tmux and privileged role execution may remain in `runtime.rs` and
consume `CommandSpec`. Moving them is optional cleanup after contract parity;
it is not required to add Qwen Code safely.

## Refactoring Sequence

1. Add core types and extract `CodexBackend` without changing generated
   commands. Lock behavior with golden argv tests.
2. Extract `ClaudeBackend`; keep existing configuration aliases.
3. Route both through the shared process/result path and run the complete test
   suite. This is the behavior-preserving checkpoint.
4. Add fake-executable integration tests for events, non-zero exit, timeout,
   cancellation, access, and trace persistence.
5. Add `QwenBackend`, capability preflight, configuration, and documentation.
6. Run opt-in live Qwen smoke tests in read-only and workspace-write roles.
7. Rerun the first ten SWE-Bench rows with Codex and compare each previously
   solved row to the stored baseline before enabling the refactor by default.
8. Remove the old provider branches only after parity evidence is retained.

Steps 1 through 5 and the old provider-branch removal are implemented. The
Codex first-ten regression gate passed at 6/10 with all five baseline successes
retained. The live Qwen check remains an explicit operator-authenticated rollout
gate.

## Test Plan

### Unit

- Exact `CommandSpec` for every backend and access mode.
- Prompt bytes never appear in rendered command text.
- Version/preflight parsing and missing executable errors.
- Event decoding with partial, malformed, unknown, and out-of-order lines.
- Final result selection when the final event is missing or the process exits
  non-zero.
- Capability mismatch errors.
- Credential redaction.

### Integration

- Fake agents read stdin, emit fixture events, write a candidate file, and exit
  with controlled statuses.
- Read-only roles cannot modify the repository even when the fake agent tries.
- Writer cancellation kills descendants and prevents late writes.
- Raw and normalized traces survive process/container completion in the
  configured external trace directory.
- Existing Codex and Claude spawn, wait, restore, verifier, and lifecycle tests
  remain green.

### Regression evaluation

The Codex first-ten SWE-Bench run is the migration regression gate. Compare by
row, not only aggregate score. Any previously solved row that becomes unresolved
blocks rollout until trace analysis attributes and resolves the regression.
Qwen Code receives a separate exploratory result set because agent quality is
not adapter parity.

## Rollback

Backend selection remains behind the existing role CLI configuration. Codex is
the default, so a rollout can disable `qwen` without changing persisted workflow
state. Rollback selects the Codex backend; it does not restore the removed
provider-specific command branches.

## Validation Result

The first-ten Codex run produced the following official row outcomes:

| Row | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Baseline | fail | pass | pass | pass | pass | fail | pass | fail | fail | fail |
| Refactor | fail | pass | pass | pass | pass | fail | pass | pass | fail | fail |

This is a 6/10 aggregate result, up from 5/10, with no loss among previously
solved rows. The failed rows were solver-output failures rather than adapter
scoring decisions: generated-file pollution (0), incomplete compatibility
coverage (5), uncaught Go compile errors (8), and an empty diff (9).

## References

- [Qwen Code headless mode](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)
- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-reference)
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/)
