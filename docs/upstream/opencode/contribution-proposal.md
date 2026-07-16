# Proposal: capability-based external agent adapter for `anomalyco/opencode`

## Summary

Define a small, experimental process adapter that lets opencode-compatible
orchestration invoke an external CLI agent without parsing its interactive
terminal UI. The adapter owns request/response translation, cancellation,
bounded logs, capability declarations, and normalized evidence. Vendor-specific
commands remain plugins or local configuration.

The contribution targets the current `anomalyco/opencode` repository, not the
archived `opencode-ai/opencode` repository. It should start with protocol and
conformance tests. Maintainers should confirm whether the adapter belongs in
opencode core, its SDK/plugin
surface, or a separate ecosystem package before code is written.

## Goals

- Make an external agent replaceable without changing orchestration logic.
- Separate requested capabilities from runtime-enforced permissions.
- Preserve process and tool evidence for review and benchmark runners.
- Support noninteractive execution, cancellation, timeouts, and terminal error
  classification.
- Avoid dependencies on Codex- or Claude-specific transcript text.

## Minimal adapter contract

### Capability manifest

```json
{
  "schema_version": "1",
  "adapter": "example-cli",
  "modes": ["worker", "verifier"],
  "input": ["prompt_file", "workspace", "artifact_dir"],
  "capabilities": ["read_workspace", "write_owned_paths", "run_commands"],
  "stream": "jsonl"
}
```

The manifest describes what the adapter can request. The host runtime returns a
separate effective-policy record; the two must never be conflated.

### Start request

The host sends a JSON file or stdin object containing run/agent IDs, role,
workspace, public instructions, owned paths, resource limits, policy reference,
and artifact directory. Secrets are passed through the host's existing secret
mechanism, not serialized in the request.

### Event stream

The child emits JSONL envelopes with schema version, sequence, event type, and
payload. Initial event types should be limited to:

- `ready`
- `progress`
- `tool_observation`
- `validation_result`
- `finding`
- `artifact`
- `result`
- `error`

Unknown optional events are preserved or ignored according to negotiated schema
rules. A terminal `result` includes status and patch/artifact references, not a
claim of benchmark correctness.

### Lifecycle

The host owns working directory, environment allowlist, stdin closure, timeout,
interrupt, termination grace period, and process-tree cleanup. Cancellation has
a structured reason and must produce a terminal host event even if the child
does not cooperate.

## Role behavior

A worker can receive writable owned paths. A verifier receives a read-only
snapshot and returns findings and validation evidence. Role-specific policy is
enforced by the host runtime; prompt text and adapter self-reporting are not
sufficient.

Adapters may translate native CLI output into normalized events, but should
retain a bounded raw-log artifact for diagnostics. Conformance tests must use
fake executables rather than requiring a particular vendor CLI.

## Error model

Use stable reason codes such as:

- `spawn_failed`
- `not_ready`
- `authentication_required`
- `protocol_error`
- `timeout`
- `cancelled`
- `policy_denied`
- `process_failed`
- `result_missing`

Human-readable diagnostics are supplementary. Orchestration must not infer
success from terminal prose or a zero process exit when the required structured
result is absent.

## Security model

- The host validates canonical workspace and artifact paths.
- Environment variables are allowlisted; values are never echoed by default.
- Child output is size-limited and treated as untrusted.
- Shell command construction uses argument arrays in the implementation
  language, not string concatenation.
- Effective filesystem, network, subprocess, and resource policy is recorded by
  the enforcing runtime.
- Tool observations are evidence of what the child reported, not proof of
  enforcement.

## Conformance suite

Provide fixture adapters for:

- successful result with ordered events and artifact digest;
- slow readiness, timeout, cancellation, and ignored interrupt;
- malformed JSON, duplicate sequence, oversized line, and unknown event;
- zero exit without result and nonzero exit with diagnostic;
- attempted path escape and forbidden environment access;
- verifier write attempt under read-only policy;
- secret-like fixture values redacted from logs.

The suite should run offline and expose a reusable adapter test helper.

## Non-goals

- A universal semantic representation of every agent tool call.
- Bundling external CLIs, models, or credentials.
- Guaranteeing sandboxing from protocol compliance alone.
- Replacing native opencode providers or interactive UI.
- Benchmark score reporting.

## Acceptance criteria

- No behavior change without explicit external-adapter configuration.
- Protocol types/schema and lifecycle are documented and versioned.
- A fake adapter completes a worker request and returns an artifact reference.
- Cancellation cleans up the fixture process tree.
- Malformed or missing terminal results fail closed with stable reason codes.
- Requested capabilities and effective permissions are separate records.
- Conformance tests cover path validation, output bounds, and redaction.

## Open design questions

- Is a provider/plugin, ACP-compatible boundary, SDK package, or standalone
  bridge the preferred ecosystem surface?
- Is there an existing opencode event envelope the adapter should reuse?
- Which process and sandbox primitives are stable public APIs?
- Should adapters be discovered by configuration, executable manifest, or
  package registration?
- What schema-version compatibility window should be supported?

## Related opencode work

The upstream audit found multiple subagent issues but no exact external-CLI
adapter and evidence-gate duplicate. Those issues may define delegation UX,
session lineage, or native subagent behavior that this adapter should reuse.
This proposal differs by focusing on a process interoperability boundary,
host-enforced capabilities, cancellation, normalized evidence, and fake-process
conformance tests. It does not propose another native subagent implementation.

Recheck the current issue set and extension APIs before posting or coding.
