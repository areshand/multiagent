# Issue draft: design feedback on diff, audit, and permission evidence gates

## Proposed title

Design feedback: composable final-diff, tool-audit, and effective-permission gates

## Body

I would like feedback before implementing a small set of portable runner gates
for external or multi-process agents.

The problem is stale or ambiguous acceptance evidence: validation may refer to
an earlier patch, a read-only verifier may only be read-only by prompt, child
process prose may hide a failed lifecycle, and requested CLI permissions may be
mistaken for runtime enforcement.

Related work should be reused rather than displaced. OpenHands #14590 concerns
durable storage and #13781 concerns trust verification; this draft is limited to
deterministic artifact checks and runtime-observed evidence. Existing opencode
subagent issues concern adjacent delegation behavior, while this gate is
independent of how native subagents are implemented. No exact duplicate was
found in the upstream audit, but issue state must be rechecked before posting.

### Minimal proposed scope

- Canonical subject identity using base commit plus SHA-256 of exact binary Git
  diff bytes.
- A host-observed process/tool audit summary with bounded artifact references.
- Separate requested, configured, and effective permission records.
- A composable gate for current-diff validation and unresolved blocking
  findings.
- Report-only mode plus offline fake-process and temporary-Git fixtures.

### Proposed invariants

- Any patch mutation invalidates prior acceptance.
- Child-reported capability or permission text cannot prove enforcement.
- A verifier role must have host-enforced read-only repository access.
- Missing/malformed evidence fails according to explicit policy, never terminal
  prose.
- Runner errors, policy rejection, and benchmark task outcomes remain distinct.

### Non-goals

- No replacement for OS/container isolation.
- No model-vendor integration.
- No hidden-test inference or scorer changes.
- No retroactive benchmark score claims from incomplete traces.
- No mandatory blocking behavior in the first contribution.

### Questions

1. Should these checks be independent modules in an existing policy API or a
   small gate package?
2. Which current runtime/artifact types should be reused?
3. What can the runtime attest reliably for writable roots, network, secrets,
   subprocesses, and resource limits?
4. Should untracked files and submodule state be part of the initial subject
   identity?
5. Is report-only rollout acceptable before any fail-closed integration?

### Acceptance criteria for a first PR

- Versioned schemas and stable reason codes only; report-only by default.
- Deterministic decisions from a fixed policy and artifact set.
- Tests reject stale diff evidence and forged effective-permission claims.
- A supported-runtime fixture denies verifier repository writes while allowing
  writes to a separate artifact directory.
- Timeout, process failure, open finding, and task-unresolved states remain
  distinguishable.
- Redaction tests exclude credentials and evaluator-only fixture fields.

If maintainers prefer smaller changes, the first PR can contain only subject
identity, stale-diff tests, and the policy interfaces needed for later checks.
