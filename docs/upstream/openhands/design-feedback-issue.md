# Issue draft: design feedback on external worker and verifier runtime hooks

## Proposed title

Design feedback: experimental external worker process with hash-bound verifier gate

## Problem / Use Case

Multi-process benchmark experiments currently need an explicit boundary between
an external worker, runtime-observed evidence, final patch capture, optional
public verification, and the authoritative evaluator. I would like to confirm
the correct OpenHands architecture before implementing that boundary.

The use case is an opt-in benchmark run in which OpenHands launches an external
worker process inside its approved runtime, captures the final patch and
normalized evidence, and optionally runs a separate read-only verifier. The
verifier result would name SHA-256 of the exact final binary Git diff; any later
mutation invalidates acceptance. The existing OpenHands agent paths, event
stream, runtime isolation, and evaluator remain authoritative.

Related issues do not appear to be exact duplicates. #14590 addresses a durable
backend; this request does not introduce one, though a durable backend could
store its artifacts. #13781 addresses trust verification; this request is
narrower runtime evidence and exact-patch gating, not a general trust system.

## Proposed Solution

- One experimental external-worker extension point with declared capabilities.
- Lifecycle, validation, patch, verifier, and gate evidence represented by
  native events or a small linked sidecar, as maintainers prefer.
- One optional post-run read-only verifier policy.
- Deterministic fake-process tests for success, error, timeout, cancellation,
  rejection, and stale diff evidence.

Acceptance criteria for a first PR:

- The feature is opt-in and existing benchmark behavior is unchanged.
- A fake worker runs in the approved test runtime and yields a captured patch.
- Timeout, cancellation, malformed output, and no-patch outcomes are distinct.
- A fake verifier cannot write to its checkout and can accept only the current
  final-diff hash.
- Structured evidence is schema-tested and excludes secrets/evaluator-only
  fields.
- Documentation states that verifier acceptance is not official task resolution.

## Alternatives Considered

- Keep the integration in a separate repository using only stable OpenHands
  APIs.
- Implement a generic post-run policy hook with no named verifier feature.
- Emit only native OpenHands events, or only a sidecar artifact, depending on
  event compatibility guarantees.
- Use an existing Agent or Runtime abstraction directly with no new core type.

I would follow the maintainers' preferred extension point and avoid a parallel
runtime abstraction.

## Priority

Low/experimental. This is interoperability and evidence quality work, not a
blocker for existing OpenHands agents or evaluations.

## Scope

Small first PR: interfaces, fake executables, schema/event mapping, and offline
conformance tests. A concrete third-party CLI adapter would be a separate PR.

Out of scope:

- No third-party CLI distribution or authentication code.
- No default-agent or evaluator behavior changes.
- No universal action protocol.
- No hidden evaluator data in child-process input.
- No benchmark score claims.

## Area

Benchmark/evaluation runtime, agent integration, event/artifact evidence, and
sandbox policy. Maintainer guidance is needed on the primary owning area.

## Technical Details

The runtime would own process launch, timeout, cancellation, bounded logs,
policy enforcement, and final binary Git diff capture. The verifier would
receive a read-only snapshot and public task evidence only. A structured result
would include decision, blocking findings, validation outcomes, and
`subject_diff_sha256`; the runner would recapture the diff immediately before
submission and reject stale evidence.

Questions for maintainers:

1. Which abstraction should own this: Agent, Runtime, event consumer,
   evaluation hook, or an external integration?
2. Can existing events represent this evidence without introducing a new
   sidecar schema?
3. What is the supported way to enforce a read-only verifier workspace?
4. Should verifier gating be a generic post-run policy rather than a named
   feature?
5. Would maintainers prefer the conformance fixture before any concrete CLI
   adapter?

If this belongs outside OpenHands core, guidance on the narrowest supported
integration API would be sufficient.
