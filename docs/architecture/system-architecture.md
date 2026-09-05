# Multiagent Production Operations Architecture

## Purpose

This document is the canonical architecture contract for the production
multiagent system and its integration with `prod-mcp` and `InternalServices`.
It records the design decisions, trust boundaries, component ownership, and
engineering principles that future changes must preserve.

This document takes precedence over historical design notes, stale prompt
instructions, and implementation shortcuts. A pull request that intentionally
changes one of these decisions must update this document and explain the
tradeoff.

## Intended outcome

The system lets an authenticated terminal-client user start a multiagent session that
can investigate production systems and perform approved operations through
versioned Markdown runbooks. One supervisor controls each session. Specialized
agents perform their roles inside that session, an operations reviewer checks
goal alignment, and `prod-mcp` is the controlled boundary through which
production reads and mutations execute.

The initial supported production capabilities are:

- Read Grafana and Loki data using the deployment-provided Grafana service
  account token.
- Read service and Kubernetes logs without requiring a separate log emitter.
- Restart an explicitly allowed Kubernetes service, including the keyless
  server where configured.
- Queue a static Move security audit for the exact reviewed head SHA of a pull
  request accessible to the deployment GitHub App.

The design should remain general. These initial operations must not become
hard-coded orchestration logic.

## System context

```text
Slack Hangout channel -- Events API --> Slack ingress adapter
                                             |
                                             | signed, filtered, durable event
                                             v
                                     Internal control API
                                             |
                                             v
Terminal client and authenticated user
              |
              v
Control server (HTTP/auth/WebSocket gateway)
              |
              v
Thread (durable task and session lifecycle)
              |
              | appends to one durable thread and creates a Session
              v
Multiagent session runtime
  +-----------------------------------------------+
  | Supervisor                                    |
  |   |                                           |
  |   +--> Orchestrator                           |
  |   +--> Ops agent                              |
  |   +--> Ops reviewer                           |
  |   +--> Other role-confined agents             |
  |                                               |
  | Trace collector sidecar ------------------+   |
  +-------------------------------------------|---+
                                              +-----------> S3
                                              |
                                              v
                                      Logger service
                                              ^
                                              |
Control server / supervisor / reviewers ------+

Session agents -- bounded query client --> Wiki service
                                          |
                                          +--> dedicated Wiki S3 Files volume

Supervisor -- bearer token + KMS-signed permit --> prod-mcp
                                                    |
                                                    +--> Grafana/Loki
                                                    +--> Kubernetes
                                                    +--> AWS services/accounts
                                                    |
                                                    +--> Logger service

InternalServices owns all Kubernetes, IAM, KMS, secret, ingress, DNS, and
storage configuration shown above.
```

## Component ownership

| Component | Owns | Must not own or know |
| --- | --- | --- |
| Terminal client | User login, local session-cookie storage, a separate local index of thread IDs created by that client profile, interactive durable-thread conversation, scriptable commands, result presentation | Server-wide thread discovery, runbook implementation, KMS signing, production credentials |
| Slack ingress adapter | Slack request-signature verification, configured channel-ID filtering, fast acknowledgement, durable event deduplication and retry, bounded event normalization | Human authority, session workflow, repository selection, production procedures or credentials |
| Control server | HTTP authentication and admission, bounded internally authenticated alert-event admission, WebSocket and message transport, execution-platform adapters, trace-derived result transport | Durable thread state transitions, provider lifecycle logic, agent/model turn storage, Grafana procedures, operation IDs, runbook steps, production credentials |
| Thread | Durable user-owned task state, public history, sequential session lifecycle and fencing, context projection, human-review queue and decisions, and result projection | HTTP authentication or transport, Kubernetes/tmux implementation details, model-provider lifecycle, production procedures or credentials |
| Supervisor | One session's authority, role bootstrap, role confinement, privileged-request mediation, KMS signing | Service-specific operational procedures |
| Orchestrator | Goal decomposition, role routing, workflow coordination | Grafana/Loki knowledge, concrete production operations, `prod-mcp` parameters, provider-specific prompts |
| Ops agent | Reading a selected Markdown runbook, planning and requesting its steps, reporting evidence | Deployment secrets, KMS private authority, infrastructure provisioning |
| Ops reviewer | Independently reconstructing evidence and comparing the proposed or observed operation with the authorized goal and runbook | Production credentials, mutating production authority, or authority to widen the reviewed scope |
| Other role agents | Their assigned reasoning or implementation role, including requests for bounded non-mutating external reads or repository materialization | Supervisor credentials, direct production execution, and unrelated role capabilities |
| `prod-mcp` | Authentication verification, signed-permit validation, operation schemas, target allowlists, execution, receipts | Multiagent workflow orchestration and model-provider behavior |
| Logger | Authenticated structural event ingestion, authoritative ordering, canonical encoding, hash-chain construction, replay prevention, signed periodic checkpoints, ledger verification, and non-authoritative audit projections | Semantic review, workflow progression, runbook interpretation, model credentials, production credentials, permit issuance, or production execution |
| Wiki service and on-demand maintenance workflow | Bounded cited retrieval from canonical Markdown and source-backed, reviewable catalog patch proposals created only when requested | Session or production-operation authority, concrete deployment configuration, repository cloning inside the Wiki service, trace-bucket access, direct publication authority, or treating user or agent text as factual evidence |
| `InternalServices` | Images, deployments, secrets, IAM, KMS, service accounts, endpoints, ingress, DNS, certificates, S3 trace export, Wiki storage and identities, and distribution of deployment-specific Markdown runbook artifacts | Agent reasoning, procedure logic embedded in deployment code, and environment-specific secrets inside runbooks |
| Markdown runbooks | Human-readable operational procedure, runbook version, operation IDs, allowed phase progression | Operation contract versions, credentials, and environment-specific secrets |

The deployment may also place a trusted repository-preparation init container
in front of a session runtime. That init container is not an agent and is not
part of the orchestrator's production-operation path.

## Repository component layout

Executable components and deployment integration surfaces have explicit
top-level ownership boundaries:

- `client/` owns the terminal client package.
- `control-server/` owns the authenticated HTTP and WebSocket gateway package
  and deployment-specific execution adapters.
- `thread/` owns the transport-independent durable `Thread` model and
  its mapping to sequential sessions. For the MVP it is hosted in
  the control-server process and StatefulSet; this package boundary does not
  create another network service.
- `slack-ingress/` owns the independently deployed Slack Events adapter and durable delivery queue.
- `runtime/` owns the Rust session runtime, supervisor, and role-confinement
  package.
- `logger/` owns the independent single-writer Logger executable,
  canonical event contract, append-only JSONL ledger, integrity checks, and
  producer client utilities.
- `wiki-service/` owns the data-free LLM Wiki engine, personal-vault maintenance
  workflows, independent Markdown query adapter, agent client, deterministic
  organization-catalog seeding contract, and on-demand maintenance guidance.
- `docker/` owns component image definitions and container entrypoints, but not
  deployment secrets or environment-specific configuration.
- `gitops/` documents the application-to-deployment contract. Concrete GitOps
  resources, identities, endpoints, storage, and secrets remain owned by the
  separate `InternalServices` repository.

Portable prompts, contracts, and runbook examples remain shared framework
artifacts at the repository root. Directory placement must not be interpreted
as authority: the component ownership table and accepted architecture decisions
remain controlling.

## Accepted architecture decisions

### AD-001: The authenticated client user is the authorizing user

The terminal client authenticates the human user and submits that user's intent
to the control server. Its authentication file stores only the resulting session
cookie. A separate mode-`0600` local index records only the thread IDs created by
that exact server-and-user client profile; it contains no credential or server-wide
discovery result. Interactive mode presents durable thread conversation events;
explicit subcommands emit thread state as JSON for automation. The control
server records the authenticated actor and approval time. It must not convert
authorization into hidden prompt text or ask each role agent to authenticate
the caller again.

The control server may have high authority because access to it is already
restricted to authenticated users. That authority remains attributable to the
authenticated user and session.

The client does not enumerate threads on startup. `/list` and the scriptable
thread-list command resolve only IDs from the local client index through
individually authorized thread lookups. A caller may explicitly open a known
thread ID, but doing so does not add it to the local-created index. Server-wide
thread collection listing is not an HTTP API. Pod-local deployment diagnostics
inspect the control server's internal thread manifest/store directly.

The HTTP API is the client contract. There is no browser client and therefore no
second thread/session state machine. The unauthenticated root route returns only
JSON service metadata; health and readiness use their dedicated JSON routes.
Human interaction and agent-driven testing use the same terminal client and API,
preventing client-only ID or lifecycle behavior from drifting from the server
contract.

The terminal-client implementation lives in the top-level `client/` package.
The `control-server/` package contains no client source or executable, the
session runtime implementation lives in the top-level `runtime/` package, and
the control-server container image excludes `client/`. These filesystem and
package boundaries prevent the independently distributed caller from importing
trusted server internals; the public HTTP API is its only integration surface.

### AD-019: Slack alerts trigger observe sessions; humans authorize bounded user sessions

A deployment may subscribe a dedicated Slack ingress adapter to one or more
deployment-allowlisted on-call channel IDs. The adapter verifies Slack's timestamped
request signatures over raw request bodies, rejects stale or unapproved events,
durably deduplicates by Slack event ID, acknowledges Slack without waiting for
agent execution, and retries delivery to a narrow internal control-server endpoint.
The Slack signing secret and separate internal delivery token are deployment-
injected files. The control server does not hold the Slack signing secret, and
the adapter receives no client cookie, model, repository, KMS, `prod-mcp`, or
production credential.

A Slack message is untrusted incident evidence, not authenticated human intent.
The internal event endpoint maps it to a durable thread owned by one deployment-
configured terminal reviewer and the deployment-selected
`MULTIAGENT_SLACK_REPOSITORY`, while attributing its initial execution to a
distinct Slack integration actor. That first execution has the mechanical
`observe` authority scope: the supervisor rejects workspace-write,
implementation-worker launches, operation publication, and operation execution.
It may use only the bounded read interfaces needed to gather evidence. An
observe execution that can answer the user completes directly without an
independent model review because its filesystem and operation boundaries
mechanically prevent mutation.

The deployment may also inject bounded, non-secret operational discovery
metadata through `MULTIAGENT_SLACK_DIAGNOSIS_CONTEXT`. The control server passes
that text to the session in a distinct trusted-deployment-context block outside
the untrusted Slack message. This is routing evidence only: it may identify an
allowlisted read-only provider target or datasource, but it grants no operation,
repair, permit, or mutation authority. `InternalServices` owns the concrete
values; the orchestrator and Slack adapter do not encode provider-specific
configuration.

If observation identifies no repair, the session completes with its bounded
evidence-backed result. If repair is proposed, the supervisor-owned
`request-review` route ends the observe execution and the Thread
atomically persists a pending review item bound to the exact source session,
question event, question digest, thread, owner, requested effects, and repository
paths. While that review is pending, ordinary follow-up cannot bypass it.

Only the configured owner authenticated through the terminal client may decide
the review. Approval appends a human-attributed authorization event containing
the exact reviewed question and digest, then creates a fresh isolated `user`
Session with bounded prior-thread context. Its first Execution carries an
immutable grant containing only the effects requested by the proposal:
`source-write`, `reviewed-ops`, or both. Source-write is restricted to the
reviewed repository-relative paths. Reviewed-ops permits entry into the existing
independent reviewer, runbook, signed-permit, target-allowlist, receipt, and
`prod-mcp` flow; it is not direct production authority and cannot bypass those
checks. The approved Session cannot request broader effects.

Approval never revives the observe agents, filesystem, credentials, or permits.
Rejecting the review creates no session and mechanically closes the thread to
further continuation. Both decisions are idempotent and durable. Provider-
specific workspace IDs, channel IDs, app identities, callback hostname, secrets,
storage, and network policy remain `InternalServices` configuration.

### AD-002: There is one supervisor per session

Every session has its own supervisor authority and role process tree.
A shared supervisor across sessions would mix authority, failures,
and audit evidence. A durable user thread may contain multiple sequential
sessions, each with a fresh supervisor.

A Session contains the existing orchestrator loop. One pass through that loop is
a runtime-owned `Execution`: a small, runtime-local authority step describing the
effects available to that pass. An Execution is not a Thread entity,
Pod, Job, provider session, or second supervisor. Advancing from a read-only
Execution to a bounded mutation Execution keeps the same Session, supervisor,
orchestrator, workspace, and trace. The runtime persists only the active bounded
effect state needed for mechanical enforcement and recovery.

The target deployment separates the long-lived control gateway from dedicated
session runtimes. A session runtime may be implemented as a Kubernetes Pod or
Job. Moving to this target must preserve the existing bootstrap model in which
the supervisor creates role processes and confines them after creation.
The orchestrator and role processes run with the thread-selected repository as
their working tree; session state and trace directories remain separate and
must not replace the repository working directory.
Headless orchestrators do not accept terminal-style live input. A follow-up
therefore remains in the same session but is delivered by a native
resume, and incomplete lifecycle passes are retried by the session worker with
a deployment-bounded automatic-resume limit. Each native resume restates the
authenticated original task and treats the latest follow-up as additive unless
the user explicitly replaces earlier scope, so transport recovery cannot erase
unfinished thread requirements.

A fresh headless Session also receives the bounded authenticated original
task in its initial model envelope. The same task is persisted as a
supervisor-bound artifact and digest; prompt delivery is context, not a new
source of authorization, and grants no authority beyond the authenticated
request text.

### AD-016: Deployment repository preparation is isolated from agent authority

`InternalServices` may mount a deployment-owned GitHub App credential into a
trusted repository-preparation init container for a session Job. The init
container may use that credential only to discover the installation for the
catalog-selected GitHub repository, mint a short-lived token restricted to
that one repository and `contents:read`, clone the repository into the
session's empty workspace, remove its temporary authentication helper, and
exit before the agent runtime starts.

The long-lived App credential and short-lived installation token must not be
mounted into or passed to the control gateway, session worker, supervisor,
orchestrator, role agents, trace exporter, or model harnesses. The repository
catalog and credential source are deployment configuration owned by
`InternalServices`; the control server only validates the selected catalog
entry and substitutes its bounded clone configuration into the deployment-
owned Job template. The gateway's Kubernetes role must not grant read access
to namespace Secrets. Repository contents remain untrusted session input.

This bootstrap exception does not grant agents a general GitHub credential and
does not replace `prod-mcp` for agent-requested GitHub reads, materialization,
publishing, or other production operations governed by a runbook and signed
permit.

### AD-014: Threads outlive sessions

A thread is the durable, user-owned task and conversation shown by the client.
A Session is one isolated runtime instance created to make progress on that
thread. A Session may run multiple sequential Executions inside its existing
orchestrator loop. Thread assigns Thread and Session IDs and owns
thread authorization, a small append-only user-visible manifest, context
checkpoints, S3 trace references, review transitions, and the mapping from a
Thread to sequential Sessions. It does not assign or persist Execution IDs. The
control server is the authenticated HTTP and WebSocket gateway and supplies
execution-platform adapters to Thread. Detailed model and agent
histories remain in the session traces already exported to S3; neither component
duplicates or reinterprets provider-native conversation storage.

Only one Session may hold the active fenced lease for a Thread. A follow-up after
a Session finishes creates a new Session ID, Pod or Job,
supervisor, orchestrator, role agents, provider sessions, writable workspace,
reviewer decisions, and permits. No prior agent is revived. The new orchestrator
receives bounded context derived from public messages, final reports,
checkpoints, and verified S3 trace references, not the previous session's
credentials, permits, unbounded raw trace, provider home, or writable filesystem.

Each session reaches exactly one sealed terminal outcome:
`succeeded`, `failed`, or `review_requested`. Route-specific safety checks still
decide whether the supervisor may seal that outcome, but they do not create
parallel session state machines. A completed operation receipt whose canonical
`outcome.disposition` is `failed` or `blocked` seals the session as `failed`.
The reviewed-ops runtime returns such a terminal operation result directly to
the orchestrator and must not automatically restore the failed ops sub-agent.
Only an explicit orchestrator decision may start a distinct retry attempt.
Human review seals the current session as `review_requested`; an approval
creates a fresh session, and a rejection closes continuation.

Execution transitions are not Session terminal outcomes. Every direct
authenticated `user` Session starts with a read-only Execution. If the request
requires source or reviewed-operations effects, the orchestrator submits exact
paths and/or `reviewed-ops`; the Supervisor validates the request and activates
one bounded next Execution in the same Session. The orchestrator remains
read-only, and only confined workers or the reviewed-ops path consume effects.
The active Execution cannot widen its own effect set.

User messages are durably and idempotently appended before acknowledgement.
When a thread still has a live session, the gateway forwards each
newly appended follow-up through the session-scoped worker channel and advances
the inbox acknowledgement only after that worker accepts the supervisor-resume
request. Once a session has finished, the next follow-up creates the
fresh isolated session described above.
The public manifest is the client conversation source of truth, while S3
session traces are the detailed audit and context-recovery source. The HTTP
event API provides replay with stable event IDs and thread-local sequence
numbers. While a thread is open, the interactive client maintains an
authenticated, read-only thread WebSocket for live events, thread state,
heartbeats, and bounded structured subagent-status snapshots. It reconnects
with its last sequence cursor and uses HTTP replay to repair gaps. A separate
session-scoped WebSocket presents raw orchestrator terminal output only while
that execution is active. Terminal output and subagent status are presentation
data; raw orchestrator stdout is never streamed into a new model context
without bounded deterministic projection.

Before a completed session runtime exits, it sends its bounded final report to
the control gateway through a deployment-provided endpoint using a
gateway-issued, session-scoped bearer token. Delivery is retried for a bounded
period so a gateway restart does not lose the public result. The gateway
persists the report before projecting the assistant event and finalizing the
session. This protocol contains no S3 location or provider credential;
deployment-managed trace export remains the independent audit path.

The worker report separates its bounded caller message from lifecycle and trace
metadata. The gateway projects that explicit message for both completed and
interrupted executions, while retaining only session-scoped trace references
in the public transcript. If a worker subagent-status endpoint stops responding,
the gateway reconciles the deployment-owned session record once and retries
only when reconciliation identifies a different live worker address. A
completed or stopped worker is not presented as an unavailable running worker.

The typed workflow context exposes one bounded `resultCandidate.path` under
session state so the orchestrator can hand a caller result to the supervisor
without writing into supervisor-owned workflow directories. The supervisor
validates and canonically persists that result before any public projection.
External-only completion normally requires a successful reviewed receipt, but
may instead terminate with an honest structural blocker when at least one
reviewed receipt is classified `blocked` and no receipt is classified `failed`.
An executor failure without a success remains fail-closed.

The `thread/` component owns the thread manifest and single-writer
lifecycle semantics. It is initially linked into the single control-server
process, so the deployment topology and one-writer assumption do not change.
`InternalServices` provisions the gateway PVC, versioned S3 backup, IAM,
encryption, endpoints, and retention configuration. With one gateway writer,
atomic local manifest replacement is sufficient; a distributed database is
required only if the gateway is later scaled to multiple writers. Automatic S3
restore is forbidden while session Pods can write the same prefix; recovery is
operator-gated until the gateway has a distinct read identity or manifests have
an independently verified integrity signature.

### AD-015: Session fences do not revoke issued permits

The active thread lease controls which session may append
authoritative thread state and issue new production permits. Losing that lease
prevents new issuance and fenced writes, but it does not revoke a permit that
was validly issued earlier.

An issued permit remains valid until it is consumed or reaches its encoded
expiry. `prod-mcp` verifies its signature, bearer authentication, attribution,
operation bounds, nonce or operation identity, and expiry without consulting
the current thread lease. A later session may issue its own permits,
so short-lived permits from sequential sessions may overlap. Each remains
attributable to its original thread, session, authorizing user event, reviewer
decision, and operation. Replay protection prevents a one-shot permit from
executing the same operation twice.

### AD-003: Role boundaries are enforced after bootstrap

The supervisor creates agents with explicit roles and then applies Linux
process, identity, environment, filesystem, and Landlock restrictions where
supported. Role prompts explain responsibility, but prompts are not the sole
security boundary.

Agents must receive only the capabilities and environment necessary for their
role. A role must not gain credentials merely because its prompt says not to
misuse them.

Shared role prompts contain only cross-cutting responsibility and safety
guidance. Language-, framework-, interface-, and scenario-specific instructions
belong in the registered contract or bounded task assignment. Specialized roles
receive a smaller prompt when the generic role prompt contains knowledge they do
not need. This least-knowledge rule reduces ambiguity and latency; mechanical
runtime controls remain the authority boundary.

### AD-004: The orchestrator routes; it does not operate production

The orchestrator can recognize that a request requires production operations
and delegate it to the ops agent and ops reviewer. It must not know the exact
operation, Grafana query, Kubernetes command, `prod-mcp` request shape, or
runbook procedure.

The only production-related orchestrator rule should be a generic delegation
boundary. A section such as `Production runbook operations` is incorrect if it
contains operational details. Those details belong to the ops role, reviewer
role, Markdown runbooks, or `prod-mcp`.

### AD-005: The ops agent follows Markdown runbooks

The ops agent is a general runbook executor. It does not receive a hard-coded
catalog of Grafana, log, restart, or provider-specific behavior in its prompt.
It reads an exact, versioned Markdown runbook and proposes or requests the
operations required by that runbook.

The ops agent has logical authority to carry out the authorized task, but it
does not directly hold transport credentials or KMS signing authority. The
supervisor mediates the privileged call.

### AD-006: The ops reviewer verifies deviation from the goal

The reviewer independently checks that the requested action is consistent with
the authenticated user's goal, the selected runbook, the current runbook phase,
and collected evidence. The reviewer does not replace deterministic policy in
`prod-mcp` and cannot expand an operation beyond the server allowlist.

The reviewer is not limited to evidence selected by the proposing agent. It
may read all session traces and immutable artifacts and may request fresh,
bounded read-only evidence through `prod-mcp`. Reviewer evidence requests use
the same supervisor-mediated ops execution command and `prod-mcp`
`operations_execute` surface as other operations; there is no parallel
evidence-read authority. The reviewer-specific operating-system identity is
preserved across the supervisor boundary. Before signing, the supervisor
mechanically binds the request to the same session, task, goal, runbook, and
target, requires the delegated observer subject to be the live reviewer, and
requires `prod-mcp` to advertise `access=read` and `mutation=false`. The
reviewer never receives transport credentials or KMS authority, and the shared
path cannot issue a mutating permit for a reviewer.

Reviewer approval is bound to the task, intent, history, runbook content,
runbook context, operation, parameters, target, actor, and expiry through
digests in the permit.

If independent reconstruction cannot establish that the next action is within
the authorized contract, the system uses a Simplex-style fallback: it issues no
next operation permit, persists a supervisor-verified human-review request,
ends the session in `human-review-required` state, and asks the user
one bounded question. This is the same terminal authority pattern used when a
decision-authority review detects a user-owned scope or risk choice. A later
user answer starts a new session; model prose alone cannot clear the
pending human boundary in the completed session.

### AD-007: `prod-mcp` is the production execution boundary

One centrally deployed `prod-mcp` serves the supported accounts. It executes
only fixed, versioned operations against explicit target and parameter
allowlists. Initial operations include Grafana/Loki reads, service log reads,
and allowed service restarts.

`prod-mcp` must not expose a general shell, arbitrary Kubernetes command, or
unbounded Grafana proxy. Supporting another AWS account or cluster requires a
deployment-managed role and target allowlist, not agent-provided credentials.

### AD-020: Confined agents may directly request non-mutating external evidence

Every confined session role may ask its supervisor to execute an operation that
the live `prod-mcp` capability catalog classifies as `access=read` or
`access=materialize`, `mutation=false`, and requiring no approval role. This
path does not require an ops agent, independent reviewer, or human approval.
The supervisor authenticates the direct caller, binds the request to the
session and authenticated task, signs the bounded permit, persists the receipt,
and retains all transport, KMS, provider, and repository credentials.

This is a distinct authority operation, not a relaxation of generic
`ops execute`. It mechanically rejects write/execute capabilities, mutations,
approval-bearing operations, arbitrary URLs, caller-selected filesystem
destinations, and provider options outside the advertised schema and target
policy. The requester receives only bounded evidence or a credential-free local
artifact.

For `github.clone`, `prod-mcp` owns repository eligibility and its authenticated
Git smart-HTTP proxy. The supervisor owns the clone process and materializes the
repository only into a session-scoped path before returning that path and the
resolved commit identity to the requesting agent. No role agent receives the
bearer token, signed permit, GitHub App token, or authenticated clone URL.

### AD-017: PR audit execution is separated from the production operation boundary

`prod-mcp` may queue one fixed Move-audit event after verifying the signed
repository, pull-request number, expected head SHA, mode, runbook, and reviews.
It must not run the model, execute target code, accept a workflow name or URL,
or hold audit-model behavior. Repository eligibility is the GitHub App
installation's access set, queried through GitHub; it is not a caller-provided
or prompt-maintained list.

The versioned audit workflow in `move-ai-audit-prototype` owns audit-model
instructions and report production. Its preparation job mints a token scoped
to only the requested repository, revalidates the PR head, and exports a
credential-free tracked-source snapshot. The model-credentialed audit job has
no target-repository token and performs source-only analysis without executing
target code or using network/RAG tools. A separate publication job has the
target-repository token but no model credential and posts only the bounded
report. The workflow independently validates all dispatch fields so direct
GitHub invocation cannot widen the signed prod-mcp contract.

The audit workflow's GitHub App must be the same installation authority used
by `prod-mcp`, or have an access set no broader than it. Deployment automation
must provision both App and model secrets; neither credential appears in
source, dispatch payloads, artifacts, reports, or prod-mcp receipts.

### AD-008: `prod-mcp` uses two independent request protections

Every privileged request requires both:

1. A bearer token accepted by the `prod-mcp` transport.
2. A valid signature produced by the supervisor's configured AWS KMS key.

`prod-mcp` verifies the corresponding KMS public key and validates the signed
permit. Caller OIDC authentication is not currently required. OIDC may be added
later, but it must not silently replace either existing protection without an
explicit architecture decision.

The signed permit and receipt include an authority-proxy description that
identifies the supervisor subject, deployment credential source, AWS KMS
signing backend, and bearer-token transport authentication.

### AD-009: Grafana access uses the Grafana service account token

Grafana and Loki reads use the same direct service-token access pattern used by
the existing Grafana tooling. The architecture does not require a log-emitter
component. `prod-mcp` constrains data-source IDs, query ranges, label names,
targets, result size, and response handling.

The token is a deployment secret. It must never appear in source, prompts,
runbooks, images, test fixtures, traces, or receipts.

### AD-010: `InternalServices` owns deployment concerns

`multiagent` and `prod-mcp` source code must not know concrete OpenAI keys,
Claude keys, Grafana tokens, S3 buckets, KMS key IDs, account role ARNs,
hostnames, or cluster endpoints.

`InternalServices` supplies these values at deployment time through Kubernetes
Secrets, service accounts, IAM roles, and environment configuration. It owns
building or selecting application images and deploying `multiagent` and
`prod-mcp` as separate workloads.

Deployment-specific operational runbooks may be source-controlled as Markdown
artifacts in `InternalServices`, selected by its service catalog, and mounted
read-only into session runtimes. Terraform, Helm, catalog fields, and agent
prompts must not duplicate or interpret their procedure steps. The deployment
must preserve the exact artifact bytes whose digest is authorized by
`prod-mcp`; repository location does not weaken the Markdown runbook boundary.

The model API keys belong to the intended deployment account or project. Model
selection and economical test settings are deployment configuration rather
than hard-coded prompts.

### AD-011: Traces are collected outside agent logic

The agent harness writes structured traces to its normal local trace path. A
deployment-managed sidecar exports those traces to an S3 bucket for later
inspection and self-improvement.

Each invocation also includes a provider-neutral `usage.json` with observed
input, cached-input, output, and total token counts. The sidecar exports this
file with the rest of the trace. Model pricing and cost calculation remain
deployment concerns because prices and model mappings change independently of
the harness.

Agents do not need S3 credentials or S3-specific code. The sidecar owns upload,
retry, object naming, and status reporting. Trace export failures must be
observable without preventing the session from retaining local evidence until
the configured retention limit.

Bulk traces continue to use this path rather than passing through the Logger.
After a successful export, the sidecar submits a bounded
`trace.artifact_exported` event containing the artifact digest, storage
reference, size, and media type. The Logger commits the reference and
digest but does not fetch, interpret, or proxy the trace body.

### AD-019: One Markdown Wiki engine supports isolated local and organization deployments

`wiki-service/` is the canonical source of the reusable, data-free LLM Wiki
engine. It includes the personal-vault CLI, prompts, schemas, templates, Codex
skills, privacy checks, read-only HTTP query adapter, agent client, and
organization catalog seeder. The former standalone personal engine is
deprecated after compatibility-preserving migration into this directory. Real
personal notes, raw sources, feedback, steward state, and generated knowledge
remain in their private vault and must never be committed to the engine.

Local and organization deployments share the Markdown storage format and
operating contracts but not authority. A local Codex process may maintain an
explicitly selected writable personal vault using the engine skills and CLI. The
organizational Wiki is a separate private Kubernetes service: session agents
query it through the provider-neutral `wiki-query` client, do not mount its
volume, and receive no Wiki S3 credential. Its bounded excerpts and Markdown
citations cannot grant filesystem, network, repository, session, or
production-operation authority. Source consolidation must never be interpreted
as data consolidation or credential sharing.

Canonical knowledge remains directly auditable Markdown. Personal vaults use
the existing `LLM Wiki/index.md`, synthesized-page, Obsidian-link, graph, raw
source, and private `LLM Wiki/system/` conventions. Organization knowledge is
stored in a dedicated, versioned Wiki bucket. The read-only adapter accepts
either a corpus root containing `index.md` or a vault root containing
`LLM Wiki/index.md`; a rebuildable in-memory lexical index provides index-first
retrieval with a bounded fallback. SQLite and embeddings are not required for
the MVP. The deployment profile defaults to `organization` and continues to
require the catalog schema and generation digest; personal-vault indexes are
accepted only under an explicitly selected local `personal` profile. The
deterministic one-shot `wiki-seed` administrative command accepts
a prepared, bounded manifest and writes repository pages before publishing
`index.md` as the catalog commit marker. It performs no network or GitHub access
and is not deployed as a scheduled synchronization workload.

Repository evidence is obtained by confined session agents through the direct
supervisor-mediated `prod-mcp` read/materialize path in AD-020. The query service
reads but never mutates the mounted corpus. When a user explicitly requests
Wiki maintenance, a role-confined session agent may use a failed or weak query
as the starting point for an ad-hoc inspection. The agent requests bounded
read-only repository materialization through the supervisor, inspects an exact
commit, and produces a Markdown patch proposal with source paths and digests
plus a regression query. User and agent text may identify what to investigate,
but it is not factual evidence. The proposal has no publication authority;
reviewed publication remains an explicit deployment-owner operation that writes
pages before the catalog commit marker and then reruns the regression query.

The organizational Wiki has no scheduled trace reader, background steward, or
autonomous writer. Its service account cannot read the separate trace bucket,
and the query service receives no GitHub credential or repository clone
authority. The MVP has no per-user or per-page Wiki authorization model: the
cluster-private service exposes one shared corpus to allowed multiagent
workloads, while workload identity, network policy, storage encryption, and
bucket versioning remain deployment responsibilities owned by
`InternalServices`.

### AD-018: One independently isolated Logger advances authoritative audit history

The Logger is a long-lived service in a security domain separate from
the control server, session runtimes, reviewers, trace sidecars, and
`prod-mcp`. Many authenticated producers may submit structural audit events,
but only the Logger assigns a sequence number and previous hash, appends
an entry, and advances the authoritative per-session chain head.

For each append, the service authenticates the producer, authorizes the event
type and session, validates a bounded schema, rejects conflicting event-ID
replays, serializes each append as one canonical JSONL record, fsyncs it before
acknowledgement, hashes the entry, and then advances the in-memory head. Exact
idempotent replay is a no-op. The HTTP append endpoint returns `204 No Content`;
this is transport acknowledgement, not evidence that authorizes workflow progress. Periodic
signed checkpoints commit the current chain head. Startup and explicit
verification recompute the chain and verify checkpoint signatures; an
integrity failure makes the service unready and prevents further authoritative
appends.

The append-only ledger file and its dedicated volume are internal implementation
details of the single writer, not a database or shared organizational storage.
The service takes an exclusive process lock, rejects truncated or non-canonical
records during startup replay, and rebuilds read indexes in memory. Deployment
must run at most one active writer for a ledger volume. A cold standby has no
write authority until deployment fencing transfers ownership. Producers may
call the append API but cannot update or delete entries, choose the chain head,
or read the logger signing key.

The Logger signing identity, producer credentials, volume, network
policy, backups, retention, and concrete endpoints are deployment-owned. The
service receives no model, supervisor KMS, `prod-mcp`, Grafana, Kubernetes, or
repository credentials and cannot issue permits or perform production work.
Its verification is structural and cryptographic; independent reviewers retain
ownership of semantic correctness and scope review.

Authoritative appends complete before derived exports. Optional JSONL
projections are non-authoritative and are rebuilt atomically from the ledger;
their outage must not invalidate or block a committed append. Producers retain
and retry undelivered events through a local outbox or deployment-owned durable
queue, and delivery backlog is observable. Neither logger availability, append
acknowledgement, nor a checkpoint grants or denies a workflow transition. The
supervisor remains the sole workflow authority, and independent reviewers use
the logger's read interface, original traces, and read-only `prod-mcp` evidence
to reconstruct and evaluate behavior. A missing or inconsistent audit event is
review evidence that may cause the supervisor to request human review; the
Logger itself does not perform that semantic decision.

### AD-012: Public ingress is deployment-managed

The control server is reached through a deployment-managed reverse proxy and
load balancer. Route53, certificates, DNS validation, listener rules, hostname,
and mount path belong to `InternalServices`.

An `agent.<approved-domain>` hostname is acceptable when the account and DNS
zone are configured. Source repositories must not assume that hostname exists.
Ingress is not considered complete until DNS resolution, TLS, routing, and
application health are verified together.

### AD-013: Contracts and runbooks are versioned and digest-bound

Every operation contract and runbook has an explicit version, but each has one
owner. The exact read-only Markdown file mounted at the framework-relative path
inside a deployed session is the authoritative runbook for that session. The
runtime does not compare it with a source-tree or deployment-repository copy.
Requests, permits, and receipts bind its exact content digest and runbook
version.

`prod-mcp` is the authoritative source for an operation's current contract and
version. Runbooks name operation IDs but do not duplicate operation versions.
The requesting role obtains the version from `multiagent ops describe`; the
supervisor rechecks it against the live capability before signing and execution.
That version check remains necessary because it binds the immutable request to
the exact schema and behavior authorized by `prod-mcp`; removing the duplicate
Markdown value avoids drift without weakening the trust boundary.

A deployment may select and mount a deployment-specific runbook artifact over
the image default. Once mounted, those exact deployed bytes are the sole
procedure source of truth. `prod-mcp` policy may allow only specified runbook
IDs, versions, and digests, but no second runbook copy participates in runtime
consistency checking.

The Rust permit producer and TypeScript permit consumer must share conformance
fixtures. In the longer term, a canonical machine-readable schema should be
owned by `prod-mcp` and used to generate or validate clients so the contract is
not maintained independently in two languages.

### AD-014: Role selection is task-adaptive; review obligations are mechanical

The orchestrator constructs the smallest role dependency graph needed for the
current goal and spawns a role only when its inputs are ready. Optional roles
such as contract scouts, scope reviewers, and reflection reviewers are selected
only when their documented trigger applies. This avoids paying for agents whose
work cannot affect the result.

Quality gates are not optional routing hints. The supervisor derives and stores
review obligations from the artifacts and actions actually produced. The
orchestrator may request additional review but cannot remove a pending
obligation, and completion is denied until every applicable obligation has
passing evidence bound to the exact artifact. Reviewers receive the goal, their
role instructions, and the immutable artifacts needed for their review. They
also have read access to the session trace corpus and the supervisor-mediated,
read-only `prod-mcp` evidence path defined in AD-006, so evidence selection by
another agent is not a trust boundary.
Before implementation, the supervisor generates an immutable decision capsule
containing the workflow revision, committed decision, selected alternative,
original-task digest, and contract digest. Decision-authority evidence and the
implementation permit must bind to the same capsule digest; an orchestrator
summary cannot substitute for that binding.

Provider and model selection remain deployment-owned. The orchestrator chooses
roles and dependencies, not provider credentials, model names, or prices.
Provider-native delegation tools do not establish the supervisor-owned role,
identity, or evidence boundary. The Claude headless adapter therefore disables
its built-in `Agent` and legacy `Task` tools; delegated work must enter through
the registered `multiagent subagent` lifecycle.

The primary Session and Execution transitions are small and mechanically selected:

- Every fresh authenticated `user` Session starts with a read-only Execution. It
  may chat, query the Wiki, read code, and gather bounded external evidence. If
  reading is sufficient, it terminates `succeeded` with a direct answer and no
  independent model reviewer.
- If that authenticated request needs mutation, the orchestrator may request
  exact source paths and/or `reviewed-ops`. The Supervisor either rejects the
  request or advances the same Session to one bounded Execution. The request
  does not create a new Session, Pod, Job, supervisor, or durable Thread entity,
  and an active bounded Execution cannot request a wider effect set.
- Every Slack-triggered Session has external `observe` origin and stays
  read-only. It terminates as `succeeded` with a direct diagnosis or
  `review_requested` with one bounded proposal. It cannot request an in-session
  mutation Execution, and neither outcome requires an independent model reviewer.
- A pending review accepts only the configured owner's idempotent `yes` or `no`.
  `no` closes the thread. `yes` creates a fresh `user` Session whose initial
  Execution contains only the reviewed effect set; it never resumes or upgrades
  the completed observe Session.
- An effect-bearing Execution uses the normal source lifecycle and mechanically
  derived independent review obligations. Workspace writes are limited to the
  exact paths. Production mutation is allowed only through `reviewed-ops`, which
  still requires the runbook, independent reviewer, signed permit, target
  allowlist, receipt, Logger, and trace gates.

The older direct-response and reviewed read-only completion commands remain
compatibility routes for existing callers, not requirements for read-only
sessions. Route prose never grants authority: UID separation, Landlock,
immutable session grants, assignment ownership, diff binding, and the
supervisor completion gate enforce these transitions.

For source implementation, adaptivity happens at iteration boundaries. The
orchestrator submits one complete iteration plan containing the committed
decision, worker dependency graph, bounded ownership, and any additional
review requests. The supervisor records the plan digest, adds review
obligations derived from policy and persisted artifacts, and binds the
decision-authority capsule to that digest. Once sealed, the runtime—not the
orchestrator—advances ready nodes, launches mutually independent agents, waits,
finalizes durable evidence, freezes the candidate diff, and submits lifecycle
transitions for that iteration. The runtime may report `needs_replan`, but it
must not revise the graph or reinterpret a semantic finding. A substantive
finding, changed assumption, expanded scope, user decision, or risk change ends
the iteration and returns control to the orchestrator for a newly sealed plan.
This keeps routing adaptive between iterations and deterministic within one
authorized iteration without granting the runtime semantic decision authority.

## End-to-end request flow

1. The terminal client authenticates a user and appends a goal or follow-up to a thread.
2. The control server records the actor, durably appends the user event, and
   routes it to the active session or creates a fresh one.
3. The session supervisor bootstraps the orchestrator and confined
   role agents with bounded thread context.
4. The orchestrator delegates production work without encoding the procedure.
5. The ops agent selects and reads the exact versioned Markdown runbook.
6. The ops agent proposes the next operation and supplies runbook evidence.
7. The ops reviewer reconstructs evidence from immutable artifacts and session
   traces and, when needed, requests a same-scope read-only query through the
   supervisor and `prod-mcp`.
8. The ops reviewer checks the proposal against the user goal and runbook. If
   it cannot safely accept, the supervisor persists a human-review request,
   issues no next permit, and terminates the session with the bounded
   question.
9. The supervisor creates a short-lived permit containing all required digests,
   target information, approvals, authority-proxy data, and expiry.
10. AWS KMS signs the permit under the supervisor's deployment-provided role.
11. The supervisor calls `prod-mcp` with the bearer token and signed permit.
12. `prod-mcp` authenticates transport, verifies the signature and permit,
   applies operation and target allowlists, then executes the operation.
13. `prod-mcp` returns a digest-bound receipt and appropriately classified
   output.
14. The reviewer and supervisor evaluate the result before another runbook
   phase or operation is allowed.
15. Authenticated producers submit bounded structural events to the Logger,
   which independently advances the authoritative chain without
   participating in workflow progression.
16. The trace sidecar persists session evidence to S3 and submits the exported
   artifact commitment to the Logger without sending the trace body.
17. The session runtime delivers its bounded result to the gateway with its
   session-scoped token, and the gateway persists it before finalization.
18. The control server returns or streams user-safe progress and results to the terminal client.

### Slack-triggered diagnosis and review flow

1. Slack sends a signed message event to the dedicated adapter.
2. The adapter verifies the raw request, filters by channel ID, writes the
   normalized event to its durable queue, and acknowledges Slack.
3. The adapter retries the event against the token-authenticated internal
   gateway endpoint until the gateway durably deduplicates it.
4. The gateway creates a reviewer-owned thread and a Slack-attributed `observe`
   session in the configured Slack repository.
5. The session gathers read-only evidence and either reports its diagnosis
   directly or terminates through the bounded `request-review` route.
6. The gateway atomically completes that Session and exposes the pending
   review to only its configured terminal owner.
7. A terminal `yes` launches a fresh `user` Session whose initial Execution has
   only the proposed source paths and/or `reviewed-ops` effect; a terminal `no`
   records rejection and closes the thread without starting another Session.
8. Source changes remain path-bound, and any production mutation continues
   through the normal independent reviewer, runbook, signed permit, allowlist,
   receipt, Logger, and trace controls.

## Deployment topology

`multiagent` and `prod-mcp` are separate deployments with separate secrets,
service accounts, IAM permissions, health checks, and rollout lifecycles.

The desired production topology is:

| Workload | Lifetime | Network exposure | Credentials |
| --- | --- | --- | --- |
| Control server | Long-lived, one writer | Reverse proxy or approved private ingress | Client/session authentication only |
| Slack ingress adapter | Long-lived, one queue writer per volume | Public Slack Events callback; private gateway egress | Slack signing secret and narrow internal delivery token only |
| Session runtime | One per session | Private | Model keys as needed, supervisor KMS and `prod-mcp` client authority |
| Trace sidecar | Same lifetime as session | S3 and Logger egress | Narrow S3 write role and a trace-commitment-only Logger producer identity |
| Wiki query service | Long-lived private service | Private health, query, and in-memory refresh endpoints | Read-only Wiki volume; no trace, GitHub, or production credential |
| `prod-mcp` | Long-lived central service | Private service endpoint | Grafana token and narrow cross-account execution roles |
| Logger | Long-lived, one active writer per ledger | Private append/read endpoints | Logger signing key and producer-authentication configuration only |

Organization Wiki maintenance reuses a user-requested session and produces a
reviewable artifact; it is not a separately deployed workload and receives no
Wiki storage credential.

If session runtimes initially share a deployment with the control server, that
is a transitional implementation rather than a change to the one-supervisor-
per-session decision. The transition must not introduce a global supervisor or
share mutable authority across sessions.

## Authority and secret rules

- Never commit bearer tokens, model API keys, Grafana tokens, KMS material, AWS
  credentials, or generated secret values.
- Never pass production credentials to role agents that do not need them.
- Never write authorization into a prompt as a replacement for authenticated
  session metadata.
- Never allow an agent to choose its own AWS role, KMS key, Grafana endpoint,
  Kubernetes context, cluster, or account outside deployment allowlists.
- Never log authorization headers, model keys, Grafana tokens, full secret
  values, or unredacted sensitive service responses.
- Rotate deployment secrets independently for `multiagent` and `prod-mcp`.
- Treat permits as short-lived capabilities and receipts as immutable audit
  evidence.

## Prompt ownership rules

| Prompt | Required content | Forbidden content |
| --- | --- | --- |
| Orchestrator | Role routing, decomposition, workflow coordination, generic production delegation | Grafana queries, Loki labels, operation IDs, runbook steps, provider lifecycle, credentials |
| Ops agent | How to interpret and follow a Markdown runbook, how to report evidence and blockers | Hard-coded service procedures, deployment secrets, direct KMS use |
| Ops reviewer | Goal-alignment criteria, independent evidence reconstruction, bounded read-only evidence-request protocol, runbook-phase verification, human-review fallback | Mutating execution instructions and credentials |
| Other role prompts | Cross-cutting responsibility and safety boundaries needed by that role | Unrelated production operations, language/framework recipes, and scenario-specific procedures |

Model-provider instructions belong in deployment or provider adapters. Prompts
must not prefer Claude, OpenAI, Codex, or another provider unless a role's
portable capability contract explicitly requires a provider feature.

## Engineering principles

### Least knowledge

A component receives only the information required to perform its owned
responsibility. Knowledge duplication is an architecture smell even when the
duplicated instructions appear harmless.

### Mechanism over prompt policy

Security properties must be enforced by authentication, signatures, schemas,
IAM, process isolation, Landlock, target allowlists, and deployment policy.
Prompts communicate responsibilities but are not trusted enforcement.

### One owner per concern

Operational procedure belongs to runbooks, operation validation belongs to
`prod-mcp`, coordination belongs to the orchestrator, authority belongs to the
supervisor, structural audit history belongs to the Logger, and
infrastructure belongs to `InternalServices`.

### Exact and inspectable authorization

Every production action must be attributable to a user goal, session,
supervisor, runbook version, reviewer decision, target, operation, and bounded
time window.

### Provider neutrality

The multiagent architecture depends on role and tool capabilities, not vendor
names. Provider-specific model selection, API keys, and flags are injected at
deployment or adapter boundaries.

### Separate reasoning from execution

Agents may reason broadly within their role, but production effects occur only
through the narrow `prod-mcp` operation surface.

### Deployment-owned environment

Application repositories expose configuration interfaces. Deployment code
selects concrete endpoints, accounts, credentials, storage, and ingress.

### Version every semantic contract

Runbooks, operations, permit schemas, and receipts change explicitly. Digest
binding detects accidental or malicious drift.

### Preserve evidence by default

Authorization, reviewer decisions, requests, receipts, and traces are retained
with redaction and integrity information sufficient for incident review and
self-improvement.

### Prefer the simplest sufficient path

Use existing role confinement, Kubernetes identity, Grafana APIs, S3 sidecars,
and service deployment patterns before adding new gateways, emitters, workflow
engines, or credential protocols.

## Architecture review gate

Before opening a pull request, the author or agent must answer:

1. Which component owns the behavior being changed?
2. Does that component need every new piece of knowledge or authority?
3. Is enforcement implemented mechanically rather than only in a prompt?
4. Does the change introduce provider, account, endpoint, or operation-specific
   logic into a general component?
5. Are secrets and infrastructure values still supplied only by deployment?
6. Are operation and runbook versions updated for semantic changes?
7. Are permits, receipts, traces, and user attribution still complete?
8. Does the change preserve one supervisor per session and role confinement?
9. Does the end-to-end test exercise the real production integration rather
   than a simulator, emitter, or compatibility scaffold?
10. If an architecture decision changes, is this document updated in the same
    pull request?

## Required end-to-end acceptance path

The deployment is working only when a real test jointly verifies:

1. The terminal client or test caller authenticates and starts a multiagent session.
2. The control server records the correct current caller.
3. A session supervisor creates the required confined roles.
4. The orchestrator delegates a testnet log investigation to the ops role.
5. The ops agent follows a versioned Markdown runbook.
6. The reviewer approves an action that matches the authorized goal.
7. The supervisor signs the permit with the configured AWS KMS key.
8. `prod-mcp` validates both bearer authentication and the KMS signature.
9. `prod-mcp` reads actual testnet logs through Grafana/Loki service-token
   access.
10. The result and receipt return to the session and user.
11. The trace sidecar persists the complete redacted trace to S3.
12. The trace sidecar submits a digest and storage-reference commitment to the
    independently isolated Logger.
13. The Logger exposes the advanced chain and signed periodic checkpoints
    through its read interface and survives a restart without losing or forking
    history; its append response is not consumed as workflow authorization.
14. Health and readiness endpoints report each integration accurately.

A test that only proves image startup, simulated execution, a log emitter, or a
mock Grafana response does not satisfy this acceptance path.

## Known target-state work

Deferred work compatible with this architecture is tracked in the canonical
[project TODO](../TODO.md). The TODO backlog records implementation status; it
does not replace or override the ownership and trust-boundary decisions in this
document.
