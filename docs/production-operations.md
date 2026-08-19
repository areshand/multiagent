# Production operations integration

The ephemeral operations agent owns the runbook workflow. Reviewers approve each proposed operation against the runbook and past history. The supervisor is the independent signing and submission boundary, and `prod-mcp` is the stateless signed-operation enforcement boundary.

## Flow

1. The orchestrator instantiates an ephemeral read-only runbook agent.
2. The agent selects one certified runbook and proposes one `OperationRequestV1`; it cannot contact production or sign permits.
3. Independent reviewers inspect the runbook, prior operation history, current observations, target, parameters, and deviation risk, then produce SHA-256-bound approval evidence.
4. The OS-isolated supervisor assigns an operations role bound to the exact agent and task.
5. The supervisor validates the operation request against that assignment and signs a five-minute ES256 permit through AWS KMS, Vault Transit, or a development-only local key.
6. At the agent's execution request, the supervisor calls `operations_preview`, then `operations_execute`, over an OIDC-authenticated MCP connection.
7. `prod-mcp` independently revalidates the signature, role, reviewer approvals, target allowlist, runbook binding, and fixed operation schema and returns a durable receipt.
8. The agent interprets the receipt, verifies the requested postcondition, and reports or escalates the outcome.

An agent changing its JSON role does nothing: the supervisor refuses to sign a mismatch, and `prod-mcp` refuses any altered signed payload.

Reviewer approval is sealed evidence, not caller-supplied metadata. Before signing, the supervisor requires each approval hash to match `reviewer-evidence/<reviewer>/last-message.txt` and requires that sealed message to contain the exact `prod-ops-review:` marker for the action ID, task ID, runbook, operation, runbook context hash, and history hash.

## Supervisor commands

```bash
multiagent prod-ops validate --request operation-request.json

multiagent prod-ops role-assign \
  --agent agent-ephemeral-123 \
  --role runbook-operator \
  --task-id task-123 \
  --expires-at 2026-08-18T20:10:00Z

multiagent prod-ops role-revoke --agent agent-ephemeral-123

multiagent prod-ops permit-issue --request operation-request.json --output permit.jws
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
