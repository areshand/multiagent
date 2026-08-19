# Production operations integration

Multiagent remains the reasoning and proposal layer. `prod-mcp` is the independent enforcement and execution layer.

## Flow

1. The orchestrator instantiates an ephemeral read-only runbook agent.
2. The agent proposes one `ActionManifestV1`; it cannot contact production or sign permits.
3. Independent reviewers produce evidence bound by SHA-256.
4. The OS-isolated supervisor assigns an operations role bound to the exact agent and task.
5. The supervisor validates the manifest against that assignment and signs a five-minute ES256 permit through AWS KMS, Vault Transit, or a development-only local key.
6. The supervisor calls `operations_preview`, then `operations_execute`, over an OIDC-authenticated MCP connection.
7. `prod-mcp` independently revalidates the signature, role, reviews, target allowlist, and fixed runbook schema and returns a durable receipt.

An agent changing its JSON role does nothing: the supervisor refuses to sign a mismatch, and `prod-mcp` refuses any altered signed payload.

## Supervisor commands

```bash
multiagent prod-ops validate --manifest action.json

multiagent prod-ops role-assign \
  --agent agent-ephemeral-123 \
  --role runbook-operator \
  --task-id task-123 \
  --expires-at 2026-08-18T20:10:00Z

multiagent prod-ops role-revoke --agent agent-ephemeral-123

multiagent prod-ops permit-issue --manifest action.json --output permit.jws
multiagent prod-ops submit --permit permit.jws
```

The last three commands require root or the configured supervisor UID. Regular orchestrator and agent UIDs are denied mechanically.

## Signing backends

Common settings:

```bash
MULTIAGENT_PROD_OPS_SIGNER=aws-kms
MULTIAGENT_PROD_OPS_KEY_ID=alias/prod-mcp-supervisor
```

AWS KMS uses the `Sign` API through the AWS CLI with `ECDSA_SHA_256`; key bytes never leave KMS.

Vault Transit uses:

```bash
MULTIAGENT_PROD_OPS_SIGNER=vault-transit
MULTIAGENT_PROD_OPS_KEY_ID=vault-prod-mcp-v1
MULTIAGENT_PROD_OPS_VAULT_ADDR=https://vault.internal
MULTIAGENT_PROD_OPS_VAULT_MOUNT=transit
MULTIAGENT_PROD_OPS_VAULT_KEY=prod-mcp-supervisor
MULTIAGENT_PROD_OPS_VAULT_TOKEN_FILE=/run/secrets/vault-token
```

The token file must be readable only by the supervisor identity. The local file backend is compiled out by default; it requires the `insecure-dev-signer` feature plus `MULTIAGENT_PROD_OPS_DEVELOPMENT=1`.

MCP submission requires `MULTIAGENT_PROD_MCP_URL` and a supervisor-only `MULTIAGENT_PROD_MCP_TOKEN_FILE`. Agents never receive this bearer token.
