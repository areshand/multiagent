# Composing Codex CLI and Claude CLI as verifier/worker agents for SWE-bench-style tasks

## Status and scope

This note describes a local orchestration pattern in which Codex CLI coordinates
or verifies work and Claude CLI performs bounded implementation assignments. The
roles are configurable: either CLI can be an orchestrator, worker, named
subagent, or verifier. The useful property is role separation, not a claim that
one model is intrinsically better at a role.

The repository contains an implementation of this pattern, including tmux-based
process management, owned-path assignments, durable findings and todos, final
Git-diff snapshots, and verifier evidence bound to the final diff. It also
contains a production-native SWE-bench-style runner. This note does **not**
report a benchmark score. Scores are time-, dataset-, model-, toolchain-, and
configuration-sensitive; only a complete, reproducible run and its official
scorer output can support a score claim.

## Intended outcome

For each task, produce a candidate patch in the actual task repository, then
accept it only when independent verification covers the exact patch being
submitted. The runner should preserve enough evidence to answer:

1. Which repository and base revision were used?
2. Which agents ran, in which roles, with what declared permissions?
3. What files and commands did they touch?
4. Which public validations ran, and what were their exit codes?
5. Does the verifier evidence refer to the exact final diff?
6. Was the official benchmark scorer run without exposing hidden evidence to
   the agents?

A run that merely starts both CLIs, exercises tmux, or produces a patch proves
infrastructure operation. It does not prove task correctness.

## Setup

### Prerequisites

- A Git checkout of the target task repository.
- Rust 1.98/Cargo, `tmux`, and Python 3.8 or newer.
- Installed and authenticated Codex and Claude CLIs.
- An isolated environment for benchmark tasks, preferably a disposable
  container or VM with network and credential exposure explicitly controlled.
- The language toolchains and dependencies required by the target repository.

CLI flags and authentication behavior change over time. Check `codex --help`,
`claude --help`, and each CLI's current authentication documentation before an
unattended run. Do not treat the command lines below as a durable security
interface.

### Repository configuration

The launcher accepts these role selectors:

```bash
ORCHESTRATOR_CLI=codex
WORKER_CLI=claude
SUBAGENT_CLI=claude
VERIFIER_CLI=codex
```

The current launcher uses bypass-permission modes for both CLIs. That is
appropriate only inside a separately enforced sandbox. A production runner
should prefer least-privilege CLI settings and must treat OS/container policy as
the authoritative boundary. Prompt instructions and CLI permission modes are
defense in depth, not containment.

Start a clean local session against the real target checkout:

```bash
MULTIAGENT_ROOT=/absolute/path/to/task-repository \
ORCHESTRATOR_CLI=codex \
WORKER_CLI=claude \
VERIFIER_CLI=codex \
./launch.sh --session swe-task --no-attach
```

Use `--resume` only for deliberate recovery from persisted state. A clean run
must not silently import conclusions or evidence from an earlier attempt.

Before launching, record the task source, base commit, container image digest,
CLI versions, model selections when available, environment policy, and whether
network access is enabled. Verify that the production orchestrator and solver,
not a compatibility scaffold or evaluator-side stand-in, are the processes
that will edit the task repository.

### Role contract

The orchestrator decomposes the issue into non-overlapping owned paths and
routes implementation, review, and repair. A worker may edit only its assigned
paths and reports exact validation commands and outcomes. A verifier is
read-only: it inspects the candidate diff, checks public acceptance clauses,
runs relevant visible validation, and emits findings rather than silently
repairing the patch.

The default composition is useful because the worker and verifier have separate
contexts and tool histories. Stronger independence requires separate processes,
read-only verifier filesystem permissions, no inherited worker transcript, and
no shared mutable state except declared artifacts.

## Execution and evidence flow

1. Capture the base commit and a clean-worktree declaration.
2. Extract a public contract from issue text, visible source, tests, docs,
   callers, schemas, fixtures, and runtime behavior.
3. Assign disjoint owned paths to workers. Record any approved exception.
4. Record process lifecycle and normalized tool events without secrets or raw
   hidden-test data.
5. Materialize a candidate patch in the real task checkout.
6. Capture `git diff HEAD --binary --ignore-submodules=all --` and compute its
   SHA-256 digest.
7. Run build and behavior verification against that snapshot. Record command,
   working directory, start/end time, exit code, and bounded output references.
8. Turn blocking verifier findings into durable todos. A repair worker may
   change the patch; if it does, invalidate prior acceptance and repeat the
   snapshot and verification steps.
9. Reject completion while blocking todos remain or evidence names a different
   diff hash.
10. Submit the patch to the benchmark's official scorer. Keep hidden tests and
    scorer-only metadata outside every agent context.

This repository implements the snapshot primitive in
[`../runtime/src/snapshot.rs`](../runtime/src/snapshot.rs) and durable hash-bound
finding/TODO gate integration in
[`../runtime/src/subagent.rs`](../runtime/src/subagent.rs). Benchmark
adapters do not repeat these checks before submitting a workspace.

## Improvements over a single unconstrained agent loop

### Exact-patch verification

An agent can correctly report a passing test and then change the patch. Binding
verification to the final binary Git diff makes that stale acceptance
detectable. Any source change after verification changes the digest and forces a
new check.

### Findings become state, not prose

Verifier objections are easy to lose in long transcripts. Persisting a finding,
converting accepted blockers to todos, and requiring verifier recheck before
closure gives repair work an auditable lifecycle.

### Ownership limits interference

Disjoint file ownership reduces concurrent edits to the same subsystem. An
explicit outside-path approval record makes scope expansion visible. Ownership
does not replace Git isolation, but it gives the orchestrator a machine-checkable
contract to enforce before accepting work.

### Validation is scheduled as work

Compilation, targeted tests, behavior probes, and final regression checks are
first-class DAG nodes rather than an afterthought in the final response. This
allows independent implementation work to proceed concurrently while shared or
expensive validators remain serialized.

### Heterogeneous failure modes are useful

Different CLIs may interpret a requirement, inspect a repository, or fail a
tool call differently. A verifier with a separate context can catch omissions
that a worker has normalized during implementation. This is diversity of
execution path, not evidence of statistical independence.

## Why orchestration helps

SWE-bench-style tasks combine repository discovery, contract inference,
implementation, testing, and submission packaging. These activities have
different permissions and concurrency constraints. Orchestration helps by:

- parallelizing independent discovery and implementation paths;
- making dependencies and owned paths explicit;
- reserving a read-only role for adversarial review;
- bounding repair attempts and stopping repeated no-diff exploration;
- invalidating stale evidence when the patch changes;
- preserving artifacts that distinguish solver behavior from runner behavior;
- providing a single gate where scope, validation, and unresolved findings are
  checked before submission.

The benefit is largest on tasks with separable subsystems or multiple public
contracts. Small, tightly coupled fixes may cost more to coordinate than a
single-agent run.

## Common failures and mitigations

### Wrong system under test

**Failure:** The evaluator invokes a scaffold, proxy, or simplified adapter
while the intended production orchestrator never runs.

**Mitigation:** Record the solver entrypoint and image digest, attest the baked
source revision, and include a startup assertion that the expected production
modules exist inside the task environment.

### Authentication or bootstrap failure

**Failure:** A CLI is missing, unauthenticated, starts an interactive setup
flow, or cannot use its credential in the container.

**Mitigation:** Run a non-mutating preflight, install credentials only at
runtime, restrict their filesystem permissions, and scrub them on every exit
path. A successful preflight is not a solved task.

### Permission bypass becomes the sandbox

**Failure:** A `dangerously-*` CLI flag is assumed to provide isolation.

**Mitigation:** Enforce writable roots, network policy, process limits, secret
mounts, and teardown outside the agent process. Capture effective permission
evidence, not only requested flags.

### Overlapping workers corrupt or overwrite work

**Failure:** Two workers edit the same path or one worker expands scope without
handoff.

**Mitigation:** Use separate worktrees where practical, declared owned paths,
an assignment check, and explicit handoff or approval records.

### Stale or self-authored verification

**Failure:** Validation predates the final patch, or the implementation worker
declares its own patch accepted.

**Mitigation:** Compute the final-diff hash after implementation, run a separate
read-only verifier, and reject evidence whose subject hash differs. Record
worker tests as useful evidence, but not as the verifier gate.

### Tool-call and terminal protocol drift

**Failure:** CLI output, prompt readiness detection, command syntax, or exit
markers change and the orchestrator mistakes a failed process for progress.

**Mitigation:** Version adapters, normalize lifecycle events, preserve raw logs
as bounded artifacts, test cancellation and timeout paths, and fail closed on
unknown terminal states.

### Validation is unavailable or misleading

**Failure:** A required toolchain is absent, a test is flaky, the selected test
does not cover the changed behavior, or untrusted test code runs in the host
evaluator.

**Mitigation:** Bake toolchains into isolated images, record skipped checks with
concrete reasons, quarantine retries in the evidence model, and execute
agent-produced code only inside a sandbox. The official scorer remains
authoritative.

### Orchestration churn

**Failure:** Agents repeatedly explore, spawn replacements, or rerun expensive
validation without producing a diff or a new source-derived finding.

**Mitigation:** Use no-diff and stale-diff checkpoints, one validation lease per
package, bounded verifier/repair iterations, and a terminal blocked state with a
specific reason.

### Benchmark contamination

**Failure:** Hidden tests, row identity, expected failures, prior score data, or
fixture-specific answers enter an agent prompt or durable memory.

**Mitigation:** Separate public agent evidence from scorer-only evidence,
sanitize metadata, use clean state per task, and audit trace fields before
publication.

## Limitations

- A passing verifier gate cannot prove hidden-test correctness.
- SHA-256 binds evidence to diff bytes, not to the base commit, environment, or
  dependency graph. A complete subject identity must include those values too.
- Agent separation does not imply independent reasoning when models share
  training data, prompts, or prior artifacts.
- More agents increase token cost, wall-clock coordination, failure surface,
  and trace volume.
- Path ownership is weaker than kernel-enforced filesystem isolation.
- Command exit code zero does not prove that the command is relevant or that
  all acceptance clauses were tested.
- Traces can leak source, secrets, personal paths, credentials, or benchmark
  metadata unless fields are minimized and redacted.
- Network-enabled agents make reproducibility and supply-chain attribution
  harder.
- Benchmark conclusions do not automatically transfer to production software
  work, other repositories, or later CLI/model versions.

## Evidence boundaries

Keep the following claims separate:

| Claim | Minimum supporting evidence | What it does not prove |
| --- | --- | --- |
| Runner started | Process event, version, image and entrypoint | Agent edited the real task or solved it |
| Patch produced | Base commit, final diff, diff hash | Patch is correct |
| Public validation passed | Exact command, environment reference, exit code, output artifact, subject hash | Hidden tests pass |
| Verifier accepted | Independent verifier identity, read-only policy evidence, covered clauses, subject hash | Verifier was complete or unbiased |
| Official task resolved | Official scorer result for that task and run | Aggregate benchmark performance |
| Aggregate score | Complete run manifest, official per-task results, aggregation method, exclusions and failures | Future or differently configured performance |

Do not infer a current score from historical commits, partial shards, selected
examples, public probes, reference reports, or an older model name in a default
configuration. Report partial runs as partial. Report infrastructure failures
separately from attempted task failures. Publish enough metadata to reproduce a
claim without publishing secrets or hidden evaluator content.

## Recommended run artifact

A portable run manifest should include:

```json
{
  "schema_version": "1",
  "task": {"source": "...", "id": "...", "base_commit": "..."},
  "runtime": {"image_digest": "...", "network_policy": "..."},
  "agents": [
    {"id": "worker-01", "adapter": "claude-cli", "role": "worker"},
    {"id": "verifier-01", "adapter": "codex-cli", "role": "verifier"}
  ],
  "patch": {"diff_sha256": "...", "artifact": "final.patch"},
  "validation": [
    {"command": "...", "exit_code": 0, "subject_diff_sha256": "..."}
  ],
  "gate": {"accepted": true, "open_blocking_findings": 0},
  "official_score": {"status": "not_run"}
}
```

Use content-addressed artifact references for large logs. Redact secrets before
hashing a publishable trace, or retain a private raw trace and a separately
hashed sanitized trace with an explicit transformation record.

## Proposed upstream work

Self-contained proposal and maintainer-feedback drafts are under
[`upstream/`](upstream/). They deliberately ask maintainers to confirm the
extension point and artifact conventions before code is written.
