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

The design should remain general. These initial operations must not become
hard-coded orchestration logic.

## System context

```text
Terminal client and authenticated user
              |
              v
Control server, durable thread store, and session gateway
              |
              | appends to one durable thread and creates an execution session
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
                                              v
                                             S3

Supervisor -- bearer token + KMS-signed permit --> prod-mcp
                                                    |
                                                    +--> Grafana/Loki
                                                    +--> Kubernetes
                                                    +--> AWS services/accounts

InternalServices owns all Kubernetes, IAM, KMS, secret, ingress, DNS, and
storage configuration shown above.
```

## Component ownership

| Component | Owns | Must not own or know |
| --- | --- | --- |
| Terminal client | User login, local session-cookie storage, interactive durable-thread conversation, scriptable commands, result presentation | Runbook implementation, KMS signing, production credentials |
| Control server | Treating the authenticated client caller as the user, durable thread ownership and public history, execution-session creation, message transport, event replay, trace-derived context, result streaming | Provider lifecycle logic, agent/model turn storage, Grafana procedures, operation IDs, runbook steps, production credentials |
| Supervisor | One session's authority, role bootstrap, role confinement, privileged-request mediation, KMS signing | Service-specific operational procedures |
| Orchestrator | Goal decomposition, role routing, workflow coordination | Grafana/Loki knowledge, concrete production operations, `prod-mcp` parameters, provider-specific prompts |
| Ops agent | Reading a selected Markdown runbook, planning and requesting its steps, reporting evidence | Deployment secrets, KMS private authority, infrastructure provisioning |
| Ops reviewer | Comparing the proposed or observed operation with the authorized goal and runbook | Production credentials, independent execution authority |
| Other role agents | Their assigned reasoning or implementation role | Supervisor authority and unrelated role capabilities |
| `prod-mcp` | Authentication verification, signed-permit validation, operation schemas, target allowlists, execution, receipts | Multiagent workflow orchestration and model-provider behavior |
| `InternalServices` | Images, deployments, secrets, IAM, KMS, service accounts, endpoints, ingress, DNS, certificates, S3 trace export | Agent reasoning and runbook procedures |
| Markdown runbooks | Human-readable operational procedure, operation version, allowed phase progression | Credentials and environment-specific secrets |

The deployment may also place a trusted repository-preparation init container
in front of a session runtime. That init container is not an agent and is not
part of the orchestrator's production-operation path.

## Accepted architecture decisions

### AD-001: The authenticated client user is the authorizing user

The terminal client authenticates the human user and submits that user's intent
to the control server. It stores only the resulting session cookie in a local
mode-`0600` file. Interactive mode presents durable thread conversation events;
explicit subcommands emit thread state as JSON for automation. The control
server records the authenticated actor and approval time. It must not convert
authorization into hidden prompt text or ask each role agent to authenticate
the caller again.

The control server may have high authority because access to it is already
restricted to authenticated users. That authority remains attributable to the
authenticated user and session.

The HTTP API is the client contract. There is no browser client and therefore no
second thread/session state machine. The unauthenticated root route returns only
JSON service metadata; health and readiness use their dedicated JSON routes.
Human interaction and agent-driven testing use the same terminal client and API,
preventing client-only ID or lifecycle behavior from drifting from the server
contract.

The terminal-client implementation lives in the top-level `client/` package.
The `control-server/` package contains no client source or executable, and the
control-server container image excludes `client/`. This filesystem and package
boundary prevents the independently distributed caller from importing trusted
server internals; the public HTTP API is their only integration surface.

### AD-002: There is one supervisor per execution session

Every execution session has its own supervisor authority and role process tree.
A shared supervisor across execution sessions would mix authority, failures,
and audit evidence. A durable user thread may contain multiple sequential
execution sessions, each with a fresh supervisor.

The target deployment separates the long-lived control gateway from dedicated
session runtimes. A session runtime may be implemented as a Kubernetes Pod or
Job. Moving to this target must preserve the existing bootstrap model in which
the supervisor creates role processes and confines them after creation.

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

### AD-014: Threads outlive execution sessions

A thread is the durable, user-owned task and conversation shown by the client.
An execution session is one isolated runtime instance created to make progress
on that thread. The control server assigns both thread and execution-session IDs
and owns thread authorization, a small append-only
user-visible manifest, context checkpoints, S3 trace references, and the mapping
to sequential execution sessions. Detailed model and agent histories remain in
the session traces already exported to S3; the control server does not duplicate
or reinterpret provider-native conversation storage.

Only one execution session may hold the active fenced lease for a thread. A
follow-up after a session finishes creates a new session ID, Pod or Job,
supervisor, orchestrator, role agents, provider sessions, writable workspace,
reviewer decisions, and permits. No prior agent is revived. The new orchestrator
receives bounded context derived from public messages, final reports,
checkpoints, and verified S3 trace references, not the previous session's
credentials, permits, unbounded raw trace, provider home, or writable filesystem.

User messages are durably and idempotently appended before acknowledgement.
When a thread still has a live execution session, the gateway forwards each
newly appended follow-up through the session-scoped worker channel and advances
the inbox acknowledgement only after that worker accepts the supervisor-resume
request. Once an execution session has finished, the next follow-up creates the
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

`multiagent` owns the thread manifest and single-writer lifecycle semantics.
`InternalServices` provisions the gateway PVC, versioned S3 backup, IAM,
encryption, endpoints, and retention configuration. With one gateway writer,
atomic local manifest replacement is sufficient; a distributed database is
required only if the gateway is later scaled to multiple writers. Automatic S3
restore is forbidden while session Pods can write the same prefix; recovery is
operator-gated until the gateway has a distinct read identity or manifests have
an independently verified integrity signature.

### AD-015: Session fences do not revoke issued permits

The active thread lease controls which execution session may append
authoritative thread state and issue new production permits. Losing that lease
prevents new issuance and fenced writes, but it does not revoke a permit that
was validly issued earlier.

An issued permit remains valid until it is consumed or reaches its encoded
expiry. `prod-mcp` verifies its signature, bearer authentication, attribution,
operation bounds, nonce or operation identity, and expiry without consulting
the current thread lease. A later execution session may issue its own permits,
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

Reviewer approval is bound to the task, intent, history, runbook content,
runbook context, operation, parameters, target, actor, and expiry through
digests in the permit.

### AD-007: `prod-mcp` is the production execution boundary

One centrally deployed `prod-mcp` serves the supported accounts. It executes
only fixed, versioned operations against explicit target and parameter
allowlists. Initial operations include Grafana/Loki reads, service log reads,
and allowed service restarts.

`prod-mcp` must not expose a general shell, arbitrary Kubernetes command, or
unbounded Grafana proxy. Supporting another AWS account or cluster requires a
deployment-managed role and target allowlist, not agent-provided credentials.

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

### AD-012: Public ingress is deployment-managed

The control server is reached through a deployment-managed reverse proxy and
load balancer. Route53, certificates, DNS validation, listener rules, hostname,
and mount path belong to `InternalServices`.

An `agent.<approved-domain>` hostname is acceptable when the account and DNS
zone are configured. Source repositories must not assume that hostname exists.
Ingress is not considered complete until DNS resolution, TLS, routing, and
application health are verified together.

### AD-013: Contracts and runbooks are versioned and digest-bound

Every operation and runbook has an explicit version. A semantic change to a
request schema, allowed behavior, or runbook procedure requires a version
change. Permits and receipts bind the exact runbook content digest and contract
fields.

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
passing evidence bound to the exact artifact. Reviewers receive only the goal,
their role instructions, and the immutable artifacts needed for their review.

Provider and model selection remain deployment-owned. The orchestrator chooses
roles and dependencies, not provider credentials, model names, or prices.

## End-to-end request flow

1. The terminal client authenticates a user and appends a goal or follow-up to a thread.
2. The control server records the actor, durably appends the user event, and
   routes it to the active execution session or creates a fresh one.
3. The execution-session supervisor bootstraps the orchestrator and confined
   role agents with bounded thread context.
4. The orchestrator delegates production work without encoding the procedure.
5. The ops agent selects and reads the exact versioned Markdown runbook.
6. The ops agent proposes the next operation and supplies runbook evidence.
7. The ops reviewer checks the proposal against the user goal and runbook.
8. The supervisor creates a short-lived permit containing all required digests,
   target information, approvals, authority-proxy data, and expiry.
9. AWS KMS signs the permit under the supervisor's deployment-provided role.
10. The supervisor calls `prod-mcp` with the bearer token and signed permit.
11. `prod-mcp` authenticates transport, verifies the signature and permit,
    applies operation and target allowlists, then executes the operation.
12. `prod-mcp` returns a digest-bound receipt and appropriately classified
    output.
13. The reviewer and supervisor evaluate the result before another runbook
    phase or operation is allowed.
14. The trace sidecar persists session evidence to S3.
15. The session runtime delivers its bounded result to the gateway with its
    session-scoped token, and the gateway persists it before finalization.
16. The control server returns or streams user-safe progress and results to the terminal client.

## Deployment topology

`multiagent` and `prod-mcp` are separate deployments with separate secrets,
service accounts, IAM permissions, health checks, and rollout lifecycles.

The desired production topology is:

| Workload | Lifetime | Network exposure | Credentials |
| --- | --- | --- | --- |
| Control server | Long-lived, one writer | Reverse proxy or approved private ingress | Client/session authentication only |
| Session runtime | One per execution session | Private | Model keys as needed, supervisor KMS and `prod-mcp` client authority |
| Trace sidecar | Same lifetime as session | S3 egress | Narrow S3 write role |
| `prod-mcp` | Long-lived central service | Private service endpoint | Grafana token and narrow cross-account execution roles |

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
| Ops reviewer | Goal-alignment criteria, runbook-phase verification, rejection behavior | Independent execution instructions and credentials |
| Other role prompts | Role-specific reasoning boundaries | Production operations unrelated to the role |

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
supervisor, and infrastructure belongs to `InternalServices`.

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
12. Health and readiness endpoints report each integration accurately.

A test that only proves image startup, simulated execution, a log emitter, or a
mock Grafana response does not satisfy this acceptance path.

## Known target-state work

The following items are compatible with the accepted architecture but may not
yet be fully implemented:

- Add stale runtime cleanup, artifact materialization, and child-process reaping
  around the file-backed thread manifest and existing S3 trace lifecycle.
- Separate gateway and session S3 identities or add independently verified
  manifest integrity before enabling automatic S3 bootstrap.
- Harden filesystem operations against descriptor-relative path and race
  attacks where pathname policy is insufficient.
- Complete cross-account Route53, ACM validation, load balancer routing, and
  health verification in deployment code.
- Move from shared contract fixtures toward one canonical generated or
  machine-validated permit schema.
- Strengthen deterministic assignment/result binding and immutable
  pre-execution evidence.
- Enforce provider-native tool capability restrictions where operating-system
  confinement cannot express the required boundary.
- Define retention, redaction, migration, and replay policy for historical S3
  evidence.

Items must remain in the tracked security or architecture backlog until their
implementation and end-to-end behavior are complete. Remove a TODO only when
the enforcing code, deployment configuration, and relevant evidence all exist.
