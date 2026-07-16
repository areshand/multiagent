# Issue draft: design feedback on an optional external runner and verifier gate

## Proposed title

Design feedback: opt-in external worker runner with hash-bound verifier evidence

## Describe the feature

I would like maintainer guidance before writing code for a small experimental
runner extension.

The intended feature is an opt-in path to run a worker through an external CLI/process,
capture its final patch and a normalized evidence trace, then optionally run a
separate read-only verifier. Verifier acceptance would be bound to SHA-256 of
the exact final binary Git diff. The existing SWE-agent trajectory, patch
packaging, and benchmark scorer would remain authoritative and unchanged.

The minimal scope is:

- One opt-in external worker adapter at a maintainer-approved runner boundary.
- One versioned summary trace with lifecycle, patch, validation, and verifier
  events; native trajectories remain unchanged.
- One optional post-run verifier hook with structured accept/reject output.
- Fail-closed checks for malformed output, timeout, open blocking findings, and
  final-diff hash mismatch.
- Offline tests using deterministic fake processes and fixture repositories.

Explicit non-goals:

- No bundled third-party CLI or credentials.
- No default agent-loop changes.
- No scorer or benchmark metric changes.
- No benchmark score claims.
- No hidden-test or scorer-only data in agent inputs or traces.

Acceptance criteria for a first PR:

- Default runs and existing artifacts are unchanged when the feature is off.
- A fake worker produces a patch through the approved extension point.
- A fake verifier can accept or reject only the exact captured diff hash.
- Mutation after verification, timeout, and malformed output are regression
  tested and fail closed.
- Trace-schema validation and secret-redaction tests pass offline.
- Documentation clearly separates runner evidence from official scoring.

## Potential Solutions

The preferred solution is a generic external worker adapter plus an optional
post-run policy hook. A first PR would contain only the interface, fixture
processes, trace schema, and conformance tests. A concrete CLI mapping would be
a separate follow-up.

Questions to resolve before choosing that solution:

1. Is this appropriate for SWE-agent core, an experimental package, or a
   separate integration repository?
2. Which runner/trajectory interface is the supported extension point?
3. Would an existing event format be preferred over a small JSONL summary?
4. Should the verifier be modeled generically as a post-run policy hook?
5. What isolation abstraction should enforce a read-only verifier checkout?

Alternatives are a standalone integration repository, a benchmark-only wrapper,
or a generic post-run hook with no named verifier concept. I would follow the
maintainers' preferred boundary rather than add a parallel runner abstraction.
