# Proposal: optional verifier-gated external runner for SWE-agent

## Summary

Add an experimental runner path that can execute a worker through an external
CLI adapter, preserve a normalized task trace, and optionally require a
read-only verifier decision bound to the exact final Git diff before handing the
patch to the existing benchmark scorer. Keep the scorer and default SWE-agent
trajectory unchanged.

This proposal is intentionally adapter-level. Maintainers should confirm the
supported runner/trajectory extension point before implementation.

## Motivation

Multi-process agent experiments are difficult to compare when orchestration,
patch generation, validation, and scoring are collapsed into one transcript.
A small runner contract would make it possible to distinguish:

- whether the worker ran and produced a patch;
- which public checks ran against which patch;
- whether a separate verifier accepted the final patch;
- whether the existing benchmark scorer resolved the task.

It would not imply that external CLIs are trusted or officially supported.

## Minimal scope

1. One opt-in external worker adapter implementing the same task input and
   patch output boundary as an existing runner path.
2. One normalized, versioned trace written alongside existing run artifacts.
3. One optional verifier hook that receives a read-only checkout plus the public
   task text and emits a structured decision for the final diff hash.
4. One submission gate that fails closed on verifier timeout, malformed output,
   open blocking findings, or a mismatched diff hash.
5. One offline regression harness using fake worker/verifier processes. No live
   model, credentials, network, or benchmark dataset download in unit tests.

## Proposed lifecycle

1. Runner records task ID, base commit, environment/image identity, adapter
   version, and effective policy reference.
2. Worker receives public task inputs and a writable task checkout.
3. Runner captures the binary Git diff and computes SHA-256 over the exact diff
   bytes.
4. If enabled, verifier receives a read-only view of that snapshot. It returns
   covered public clauses, validation command results, blocking findings, and
   `subject_diff_sha256`.
5. Runner recaptures the diff. Any change invalidates the verifier result.
6. Gate records accepted/rejected/error without translating a runner error into
   an unresolved benchmark task.
7. Existing SWE-agent patch packaging and benchmark scoring continue unchanged.

## Trace boundary

Suggested events are `run_started`, `agent_started`, `tool_observed`,
`validation_finished`, `patch_captured`, `verifier_finished`, `gate_finished`,
and `run_finished`. Each event has a schema version, monotonic sequence number,
timestamp, task/run IDs, actor and role, and a payload.

Do not include credentials, environment values, hidden tests, scorer-only
metadata, or unrestricted command output. Store large logs as artifacts with
digest, size, media type, and redaction state. Retain the native SWE-agent
trajectory as the source of detailed agent interaction; the normalized trace is
an interoperability summary, not a replacement.

## Verifier result

```json
{
  "schema_version": "1",
  "subject_diff_sha256": "...",
  "decision": "accept",
  "public_clauses": [{"id": "issue-1", "status": "covered"}],
  "validations": [{"command": "...", "exit_code": 0}],
  "findings": []
}
```

`accept` is valid only when the hash matches, all required fields parse, no
blocking finding is open, and the verifier process completed within policy. The
result is public-evidence acceptance, not a replacement for official scoring.

## Regression harness

Use deterministic fixture repositories and executable fakes to cover:

- worker success with a non-empty patch;
- worker no-patch, timeout, cancellation, and malformed result;
- verifier accept, reject, timeout, and malformed JSON;
- patch mutation after verifier acceptance;
- nonzero validation exit code;
- trace redaction and deterministic event ordering;
- gate-disabled compatibility with the current runner output.

Golden files should cover only the normalized schema. Assertions against
terminal prose would make the adapter brittle.

## Compatibility and security

- Feature flag or explicit runner selection; default behavior is unchanged.
- External commands run only in the isolation boundary already approved by
  SWE-agent maintainers. The adapter must not claim a CLI permission flag is a
  sandbox.
- Verifier filesystem access is read-only by enforcement, not prompt alone.
- Runner and scorer artifacts remain separate so hidden evidence cannot flow
  back into agent prompts.
- Secret values and raw environment dumps are prohibited from normalized
  traces.

## Non-goals

- Shipping or authenticating Codex or Claude CLIs.
- Selecting models or recommending a worker/verifier pairing.
- Changing SWE-agent's default agent loop or benchmark score semantics.
- Publishing benchmark scores.
- Defining a universal tool-call schema.

## Acceptance criteria

- Existing runner tests and default trajectories remain byte-for-byte or
  semantically unchanged, as maintainers prefer.
- A fake external worker can produce a patch through the supported runner API.
- The normalized trace validates against a versioned schema.
- Verifier acceptance is rejected after any diff mutation.
- Timeouts and malformed outputs have distinct runner error states.
- Hidden/scorer-only data is absent from worker and verifier inputs in tests.
- Documentation labels the verifier gate as optional public-evidence checking,
  not official task resolution.

## Open design questions

- Which existing runner and trajectory abstractions should own this extension?
- Should the trace be JSONL, an existing SWE-agent event type, or both?
- Is a generic post-run hook preferable to a named verifier concept?
- Which sandbox abstraction can enforce verifier read-only access?
- Where should adapter conformance tests live?

## Upstream issue shape

The pre-code issue draft follows SWE-agent's audited feature-request fields,
`Describe the feature` and `Potential Solutions`. The proposal stays behind a
maintainer decision because the correct runner/trajectory extension point is an
upstream ownership question.
