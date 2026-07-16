# Proposal: benchmark runtime hook for external workers and verifier evidence in OpenHands

## Summary

Introduce an opt-in benchmark-runtime integration that launches an external
worker process through an approved OpenHands execution boundary, maps lifecycle
and validation results into versioned events, and optionally applies a
read-only verifier gate bound to the exact final Git diff. Existing OpenHands
agents, event streams, sandboxes, and benchmark evaluators remain the default.

Maintainers should select the extension point and decide whether these events
belong in the native event stream or a sidecar artifact before implementation.

## Motivation

OpenHands already separates runtime execution from evaluation concerns. An
external-process adapter can reuse that isolation while making heterogeneous
worker/verifier experiments inspectable. The important outcome is not support
for a particular vendor CLI; it is a stable boundary between task input,
runtime actions, final patch, public verification, and official evaluation.

## Minimal scope

1. An experimental external-worker runtime or agent adapter with explicit
   capability declaration.
2. A translation layer for process lifecycle, bounded tool observations,
   validation outcomes, and patch capture into a versioned evidence artifact.
3. An optional post-run verifier policy that runs with a read-only workspace and
   no evaluator-only inputs.
4. A final gate requiring the verifier's subject hash to match the recaptured
   binary Git diff.
5. Deterministic conformance tests using fake executables in the existing test
   runtime; no model credentials or network.

## Proposed boundary

Input to the worker should contain only public task text, checkout location,
base revision, allowed capabilities, resource limits, and artifact locations.
Output should be a terminal status plus patch reference. The OpenHands runtime,
not the child process, owns cancellation, timeout, log collection, resource
policy, and final patch capture.

The verifier receives the public contract, read-only repository snapshot,
final-diff hash, and references to approved worker evidence. It must not receive
hidden tests, expected failures, evaluator parsing rules, or credentials.

## Event mapping

Prefer native OpenHands events if they can express the following without
flattening important semantics:

- adapter/process start and terminal state;
- declared and effective capability/policy reference;
- validation command, working directory, exit code, duration, and artifact;
- final patch digest and changed paths;
- verifier decision, findings, and subject digest;
- gate result and reason code.

If native events are not a stable public interchange boundary, write a compact
JSONL sidecar linked from the benchmark run. Large or sensitive logs remain
separate, content-addressed, and redacted.

## Gate semantics

The gate returns one of `accepted`, `rejected`, or `runner_error`. It accepts
only a well-formed verifier result for the current diff, with no open blocking
findings and required public validations successful. Any patch mutation,
verifier timeout, cancellation, malformed result, or policy-evidence failure is
not accepted.

`accepted` means the configured public-evidence policy passed. It does not mean
the benchmark task is resolved; the existing evaluator determines that.

## Regression harness

Fixture cases should cover:

- successful patch production and event translation;
- no-patch, child crash, timeout, and cancellation;
- invalid event/result schema and oversized output;
- verifier accept/reject/error;
- final patch mutation after acceptance;
- denied write attempt from a read-only verifier;
- scorer-only metadata exclusion;
- feature-off compatibility with current benchmark output.

The harness should assert structured events and artifacts, not vendor terminal
text.

## Security and privacy

- Execute child CLIs only through the OpenHands sandbox/runtime abstraction
  approved by maintainers.
- Inject credentials at runtime through existing secret mechanisms and exclude
  them from images, prompts, events, and artifacts.
- Make network, mounts, writable roots, subprocess, and resource policy
  explicit in evidence.
- Treat child-reported tool use as untrusted observations. Runtime enforcement
  remains authoritative.
- Keep hidden evaluator state in a separate process/data boundary.

## Non-goals

- Bundling Codex CLI, Claude CLI, or their authentication flows.
- Replacing OpenHands agents, events, runtime, or evaluators.
- Making verifier use mandatory.
- Standardizing all agent actions across ecosystems.
- Publishing or comparing benchmark scores.

## Acceptance criteria

- Feature-off behavior and evaluator semantics remain unchanged.
- A fake external worker runs inside the approved test runtime and produces a
  captured patch.
- Events or sidecar records validate against a versioned schema.
- Runtime timeout/cancellation is reflected in a distinct terminal state.
- The verifier cannot mutate the task checkout in the regression fixture.
- A changed final diff invalidates earlier acceptance.
- Tests prove evaluator-only fields do not enter worker/verifier requests.

## Open design questions

- Should this be an Agent, Runtime, EventStream consumer, evaluation hook, or
  external integration package?
- Which existing event types cover lifecycle and validation evidence?
- Can the current sandbox expose a provably read-only snapshot to the verifier?
- Should policy evidence be stored in the event stream or artifact metadata?
- What backward-compatibility guarantees apply to benchmark artifacts?

## Related OpenHands work

- OpenHands #14590 concerns a durable backend. Durable run state may eventually
  store these artifacts, but this proposal is narrower: an opt-in external
  worker boundary, normalized evidence, and exact-patch verifier gating. It does
  not propose a new durable backend.
- OpenHands #13781 concerns trust verification. This proposal can consume or
  complement a trust mechanism, but focuses on runtime-observed permissions,
  public validation evidence, and stale-diff rejection rather than establishing
  actor or artifact trust generally.

No exact duplicate was found in the upstream audit. Issue state and overlap
should be rechecked before posting.

## Upstream issue shape

The pre-code issue draft follows the audited OpenHands feature template:
Problem/Use Case, Proposed Solution, Alternatives, Priority, Scope, Area, and
Technical Details.
