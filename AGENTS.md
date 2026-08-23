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
