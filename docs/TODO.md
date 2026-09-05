# Project TODO

This is the canonical backlog for deferred repository work. Architecture
decisions remain in `docs/architecture/system-architecture.md`; this file tracks
work that is accepted by those decisions but not yet implemented or proven.

Add newly discovered deferred work here in the same pull request that identifies
it. Keep an item open until its implementation, deployment integration when
applicable, and relevant evidence are complete.

## Architecture Target-State Work

- [ ] Add stale runtime cleanup, artifact materialization, and child-process
  reaping around the file-backed thread manifest and existing S3 trace
  lifecycle.
- [ ] Separate gateway and session S3 identities or add independently verified
  manifest integrity before enabling automatic S3 bootstrap.
- [ ] Harden filesystem operations against descriptor-relative path and race
  attacks where pathname policy is insufficient.
- [ ] Complete cross-account Route53, ACM validation, load balancer routing,
  and health verification in deployment code.
- [ ] Move from shared contract fixtures toward one canonical generated or
  machine-validated permit schema.
- [ ] Strengthen deterministic assignment/result binding and immutable
  pre-execution evidence.
- [ ] Enforce provider-native tool capability restrictions where
  operating-system confinement cannot express the required boundary.
- [ ] Define retention, redaction, migration, and replay policy for historical
  S3 evidence.
- [ ] Complete Logger storage rollback detection: publish signed checkpoints to
  an independently retained witness, allow verification against the last
  trusted checkpoint, require scheduled checkpoints during replay, and add
  restart tests for newline-aligned tail truncation and missing checkpoints.
  This does not require producer-assigned sequence numbers; the Logger remains
  the sole sequencer.
- [ ] Integrate producer outboxes or a deployment-owned durable queue so Logger
  delivery retries independently and backlog alerts are testable.
- [ ] Add deployment-owned Loki/OpenTelemetry projections if operational demand
  justifies them; these must remain derived from the authoritative ledger.

Security-specific deferred work that predates this canonical backlog remains
detailed in [Known Security TODOs](known-security-todos.md). New security TODOs
must also be represented or linked from this file.

## Slack Hangout Diagnosis And Human Review

- [ ] Replace static deployment-injected Slack diagnosis target metadata with a
  Wiki-backed resolver that maps alert evidence such as hostnames and service
  names to repository and operational targets, cites the mapping source, and
  fails closed when the mapping is ambiguous.
- [ ] Provision the Slack app and Events API callback in `InternalServices`,
  inject the signing secret, immutable Hangout channel ID, internal delivery
  token, Slack diagnosis repository, durable ingress volume, and `production-e2e`
  review owner, and project the session `authority-scope` secret into the runtime
  Job template.
- [ ] Pass the deployed acceptance paths in `slack-ingress/README.md`: a real
  Hangout message must create exactly one diagnosis-only thread, `no` must
  permanently close its continuation path, and `yes` must start a fresh
  human-authorized session linked to the original thread and exact review
  question. Preserve evidence that no repair operation ran before approval.
- [ ] Add production queue observability, bounded exponential retry, corrupt
  event quarantine, retention, and recovery exercises before running more than
  one Slack ingress replica or relying on it as a paging system.

## Organizational Wiki MVP

- [x] Seed exactly the 111 repositories in the deployed multiagent catalog with
  a summary and detailed description; non-empty repositories also require an
  exact resolved commit and source digests, while verified-empty repositories
  must be marked explicitly and contain no fabricated evidence.
- [x] Build and deploy the private Wiki query image with a separate versioned
  bucket and read-only Mountpoint S3 volume owned by `InternalServices`.
- [x] Deploy a multiagent runtime image containing `wiki-query` and the direct
  non-mutating read/materialize supervisor path.
- [x] From a thread initially targeting `multiagent`, ask for the
  `InternalServices` architecture and prove the result identifies
  `MoveIndustries/InternalServices`, cites its committed sources, and records a
  real Wiki query in the session trace.
- [ ] After the MVP, implement the singleton steward, retrieval-gap processing,
  regression evals, and its separate Wiki-write/trace-read identity.

## Production MCP Joint Test

- [x] Activate the existing Linux UID and Landlock boundary in the production container.
- [x] Prevent role processes from inheriting AWS, KMS, prod-mcp, and GitHub credentials.
- [x] Add supervisor-owned KMS permit signing for generic runbook operations with a sealed independent reviewer gate.
- [x] Configure the multiagent StatefulSet with the correct runtime variables and credential group.
- [ ] Pin the immutable multiagent KMS public key in prod-mcp.
- [ ] Persist multiagent traces to a scoped S3 prefix.
- [ ] Provision an approved repository for task execution.
- [x] Pass the local malicious-agent boundary and joint contract tests.
- [ ] Pass the deployed testnet Grafana log-read joint test.

## OSS Positioning And Adoption

Do not try to contribute the whole framework to another OSS project first. A
large orchestration and evaluation change is too expensive for most maintainers
to review as one contribution.

Position the project as:

> This is not another coding agent. It is an orchestration layer that composes
> existing coding agents, runs them in parallel roles, verifies their work, and
> improves SWE-style task reliability.

### Reference Implementation

- [x] Keep `areshand/multiagent` as the reference implementation.
- [x] Document an exact benchmark command and commit.
- [x] Publish a concise result table, relevant log facts, and failure analysis,
  while labeling the historical `36/50` as a tuned cumulative aggregate rather
  than a reproducible single run.
- [x] Add an architecture diagram and a three-minute demonstration.
- [x] Prioritize orchestration, evaluation, and runtime rigor over UI work.
- [x] Preserve the benchmark denominator by passing every normally completed
  solver workspace to the official verifier. Keep only launch, process,
  timeout, and workspace-collection failures fail-closed.

### Adapter Submission Boundary

- [x] Keep only public metadata sanitization, runtime bootstrap, lifecycle
  observation, workspace materialization, and runner transport in the adapter.
- [x] Remove adapter-owned issue-coverage, provenance, history, build, parser,
  UI, Go-package, and evidence-marker acceptance gates.
- [x] Treat terminal status as a diagnostic stop signal rather than submission
  permission.
- [x] Submit blocked, internally timed-out, and markerless runs whenever the
  adapter can still complete a reliable workspace handoff.
- [x] Leave patch correctness exclusively to the official SWE-bench verifier.

### Native Solver Import Model

- [x] Add `evaluation/native_solver/__init__.py` and treat the native solver as
  one package in local tests and baked task containers.
- [x] Launch the production solver with
  `python3 -m evaluation.native_solver.solve_swe_prod` from
  `/opt/multiagent` instead of executing `solve_swe_prod.py` by file path.
- [x] Remove the repeated relative-import/top-level-import fallbacks
  (`try/except ImportError`) from all native solver modules.
- [x] Replace `from ... import *` with explicit symbol imports or module-qualified
  references so dependencies and cycles are visible.
- [x] Do not catch broad `ImportError` around module loading; internal dependency
  failures must preserve their original traceback instead of triggering an
  alternate import path.
- [x] Refactor `evaluation/native_solver/swe_prod_lifecycle.py` to use one
  explicit package import path: remove its repeated `try/except ImportError`
  blocks and wildcard imports, keep dependencies named or module-qualified,
  and add a regression test proving an import-time dependency failure surfaces
  the original exception instead of silently selecting a fallback path.
- [x] Add tests for package import, module entrypoint execution, and the exact
  baked-container command.

### Small Upstream Contributions

- [x] Propose a benchmark runner, trace/evidence format, verifier gate, or
  regression harness to SWE-agent or OpenHands.
- [x] Propose a minimal external coding-agent worker adapter to opencode or the
  Claude/Codex CLI ecosystem.
- [x] Extract diff-hash binding, tool-execution audit, or permission/evidence
  gates for relevant security and runtime projects. The focused OpenHands SDK
  draft PR links tool spans to persisted action/observation events without
  requiring adoption of this framework.
- [x] Keep each upstream contribution independently reviewable and mergeable.

### Technical Note

- [x] Write a short technical note titled approximately *Composing Codex CLI
  and Claude CLI as verifier/worker agents for SWE-bench-style tasks*.
- [x] Include the benchmark setup, improvements, failures, and evidence for why
  orchestration helped.

### Getting Started

- [x] Provide one command that runs a small local demonstration in about five
  minutes.
- [x] Keep the full benchmark and 20 GB container workflow as an advanced path,
  not the first proof a new user must run.

### Upstream Discovery

- [x] Open design or feedback issues before sending upstream code.
- [x] Ask maintainers whether a minimal external-agent adapter or evidence-gate
  contribution fits their project.
- [x] Avoid leading with the complete multiagent framework implementation.

Suggested issue framing:

> I built an external-agent orchestration layer that runs existing CLIs as
> workers and verifiers. Would a minimal adapter or evidence-gate contribution
> be useful here?

### Internal Validation

- [ ] Present the system internally as a benchmark harness and orchestration
  experiment, not as a tool that every team should immediately adopt.
- [ ] Find one team willing to run it on 5-10 real bugs or internal tasks.
- [ ] Use those results to validate reliability and workflow fit before broader
  promotion.

Evidence rules for checking these boxes (see
[the pilot request one-pager](docs/internal-pilot-request.md)):

- The presentation item requires a record that the pitch was actually delivered
  internally, with date and audience. Having presentation material in the repo
  is not enough.
- The willing-team item requires a named sponsor and team, the agreed 5-10 task
  list, and named independent reviewers. A prepared ask is not a commitment.
- The results item requires a completed pilot run directory: validated
  manifest, per-cell `evidence.json`, filled `review.json` files from
  independent reviewers, the generated summary, and checksums. Fixture tests
  and mocked drivers do not count as pilot results.

### OSS Readiness

- [x] Add an explicit open-source license.
- [x] Make the README concise and clearly state the project positioning.
- [x] Add a reproducible benchmark section with exact commands and a compact,
  relocatable provenance validator built on reusable framework primitives.
- [x] Complete these credibility basics before significant external promotion.
