# Repository Architecture Rules

Before changing prompts, roles, authorization, production operations, tracing,
session lifecycle, or deployment integration, read
`docs/architecture/system-architecture.md`.

The architecture document is the authority for component ownership and trust
boundaries. If an implementation request conflicts with it, stop and describe
the conflict before editing code. Change the architecture decision explicitly
before changing implementation behavior.

For every proposed change, answer this question:

> Does this component need this knowledge or authority to perform its owned
> responsibility?

Do not duplicate operational knowledge in a higher-level component as a
substitute for enforcement. Enforce boundaries with process isolation, IAM,
schemas, signed permits, deployment configuration, or executable policy.

In particular:

- The orchestrator may route work but must not contain production procedures.
- Production procedures belong in versioned Markdown runbooks.
- The ops agent interprets runbooks but does not own deployment credentials.
- The supervisor owns session authority and mediates privileged requests.
- `prod-mcp` owns the executable operation allowlist and parameter validation.
- `InternalServices` owns secrets, endpoints, IAM, KMS, ingress, and storage.
- Provider-specific configuration must be injected by deployment, not encoded
  in repository prompts.

Pull requests that change an architecture boundary must update
`docs/architecture/system-architecture.md` in the same pull request.

## Deferred Work Tracking

`docs/TODO.md` is the canonical repository backlog. When a change identifies
accepted but unfinished work, add a concrete unchecked item there in the same
pull request. Do not leave the only record of deferred work in review comments,
commit messages, or scattered documentation.

A TODO must state the missing outcome and enough completion evidence to remove
it. Do not add TODOs for work completed by the current change, and do not remove
or mark an item complete until its implementation, applicable deployment
integration, and relevant tests or operational evidence exist. A TODO never
substitutes for an architecture decision: changes to ownership or trust
boundaries must still update `docs/architecture/system-architecture.md`.
