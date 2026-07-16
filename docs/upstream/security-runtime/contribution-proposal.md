# Proposal: portable security and runtime evidence gates for agent runners

## Summary

Add an opt-in submission-gate library or runner module with four independently
testable checks:

1. final-diff identity and stale-evidence rejection;
2. normalized tool/process audit completeness;
3. requested-versus-effective permission evidence;
4. unresolved finding and validation policy enforcement.

The module should consume artifacts rather than vendor transcripts and return
structured decisions. It can be adopted by SWE-agent, OpenHands, opencode
integrations, or other benchmark runners without adopting a particular
orchestrator.

Maintainers should decide whether these checks belong together and which
existing policy/artifact abstractions to reuse before implementation.

This proposal may complement OpenHands #14590 (durable backend) and #13781
(trust verification), but does not duplicate either: storage and generalized
trust are out of scope. It also complements opencode subagent work without
changing native subagent semantics. Its unit of contribution is a deterministic
artifact gate and runtime-evidence contract.

## Threat model

The gate addresses accidental or adversarial acceptance when:

- verification ran against an earlier patch;
- a worker modified files outside declared scope;
- a verifier that was intended to be read-only could write;
- a child process failed but terminal prose looked successful;
- required tool or validation evidence is missing;
- secrets or evaluator-only data appear in publishable traces;
- open blocking findings are lost in transcript text.

It does not contain a malicious process by itself. OS/container isolation,
network policy, secret handling, and resource controls remain external runtime
responsibilities.

## Subject identity

Diff hash alone is insufficient for reproducibility. Define a canonical subject
record:

```json
{
  "schema_version": "1",
  "base_commit": "full object id",
  "diff_sha256": "sha256 of exact binary git diff bytes",
  "repository": "stable non-secret identifier",
  "runtime_image_digest": "optional immutable digest",
  "dependency_lock_digests": []
}
```

The minimal gate binds validation and verifier decisions to `base_commit` and
`diff_sha256`. Environment fields support stronger reproducibility but should
be policy-selectable because not every runner has immutable images.

## Gate 1: diff-hash binding

- Capture `git diff <base> --binary --ignore-submodules=all --` as bytes.
- Hash exactly those bytes and store the patch as an artifact.
- Require every accepting validation/verifier record to name the subject.
- Recapture immediately before submission; reject on mismatch.
- Record empty-patch policy explicitly.

The implementation must avoid reserializing or line-ending-normalizing the diff
between hashing and submission.

## Gate 2: tool and process audit

Require a minimal host-observed lifecycle:

- process spawn identity and adapter version;
- start/ready/terminal timestamps and status;
- working-directory and policy references;
- bounded command/tool observations with actor and sequence;
- validation command, exit code, duration, and output artifact digest;
- timeout/cancellation/process-tree cleanup outcome;
- patch capture and changed paths.

Child-reported events are labeled `reported`; runtime-observed events are
labeled `observed` or `enforced`. Missing optional detail can be `unknown`, but a
policy must state which unknowns block acceptance.

## Gate 3: permission evidence

Represent three separate objects:

- `requested`: capabilities the adapter asks for;
- `configured`: policy the runner intended to apply;
- `effective`: evidence emitted by the enforcing runtime.

At minimum, cover writable roots, read-only roots, network mode, secret mounts,
subprocess policy, user identity, and resource limits. Prompt instructions and
CLI flags may be recorded as configuration but cannot satisfy `effective`
evidence.

For a verifier role, default policy requires a read-only repository snapshot
and a separate writable artifact directory. A regression fixture must attempt a
write and show enforcement failure.

## Gate 4: validation and finding closure

- Require configured build/behavior records for changed production code.
- Preserve each blocking finding as structured state.
- Permit closure only by an allowed actor with resolution evidence bound to the
  current subject.
- Reject acceptance with open blockers, failed required validation, or an
  unrecognized terminal state.
- Keep `runner_error`, `policy_rejected`, and `task_unresolved` distinct.

Policy should declare required checks; the library should not guess relevant
tests from filenames as its only signal.

## Decision schema

```json
{
  "schema_version": "1",
  "decision": "rejected",
  "subject": {"base_commit": "...", "diff_sha256": "..."},
  "checks": [
    {"id": "diff-current", "status": "passed", "evidence": ["artifact:..."]},
    {"id": "verifier-read-only", "status": "failed", "reason": "write allowed"}
  ],
  "reason_codes": ["effective_permission_mismatch"]
}
```

Decisions are deterministic for a fixed policy and artifact set. Human prose is
diagnostic only.

## Regression harness

Use a temporary Git fixture and fake child processes to test:

- binary diff hashing, rename, deletion, untracked-file policy, and line endings;
- mutation between validation and submission;
- forged child-reported permissions versus host-observed policy;
- verifier write attempt and separate artifact-directory write;
- missing, duplicate, out-of-order, and oversized tool events;
- failed command represented as success in prose;
- open, resolved, dismissed, and stale finding evidence;
- timeout and descendant-process cleanup;
- trace redaction and evaluator-only field exclusion;
- deterministic reason codes and forward-compatible schema parsing.

Property tests are appropriate for event ordering and subject mutation. Golden
tests should be limited to stable schemas, not full logs.

## Rollout

1. Land schemas, reason codes, and fake-process fixtures behind an experimental
   namespace.
2. Add report-only mode that never blocks submission.
3. Compare report-only decisions with current runner outcomes and resolve false
   positives using public artifacts only.
4. Offer fail-closed mode per runner after maintainers approve required checks.

No historical benchmark result should be rescored as though the new evidence
had existed. Missing historical evidence remains unknown.

## Non-goals

- Replacing container or OS sandboxing.
- Inspecting model reasoning or proving agent honesty.
- Inferring hidden-test outcomes.
- Mandating a vendor CLI or orchestrator.
- Retrofitting score claims to incomplete historical traces.

## Acceptance criteria

- Schemas and reason codes are versioned and documented.
- Gate decisions are deterministic from policy plus artifacts.
- Any final-diff mutation invalidates earlier accepting evidence.
- Requested/configured permissions cannot satisfy effective-policy checks.
- The verifier write-denial fixture passes on a supported runtime.
- Runner, policy, and task outcomes remain distinct in API and reports.
- Secret/evaluator-only fixtures are absent from sanitized output.
- Report-only mode has no effect on existing submission behavior.

## Open design questions

- Should these be one gate package or composable checks in existing policy APIs?
- What host evidence can each supported runtime provide reliably?
- How should untracked files and submodules enter subject identity?
- Which fields are mandatory for local runs versus publishable benchmark runs?
- What artifact retention and redaction policy is acceptable upstream?
