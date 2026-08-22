# Kubernetes Service Recovery

## Metadata

- Runbook ID: `k8s.service-recovery`
- Version: `1.0.0`
- Prod MCP operations: `k8s.read-logs`, `k8s.restart-deployment`
- Operation version: `1.0.0`

## Goal

Inspect bounded Kubernetes logs and, only when the original goal explicitly
authorizes it, restart one allowlisted deployment and wait for the signed
replica count to become ready.

## Diagnose phase

1. Set the runbook phase to `diagnose` and operation to `k8s.read-logs`.
2. Use `sinceSeconds` from 60 through 7200 and `tailLines` from 1 through 1000.
3. Set `previous` only when the original goal requires logs from a prior container.
4. Submit the exact request for independent review before execution.

## Restart phase

1. Continue only when the original goal explicitly authorizes a restart of the exact target deployment.
2. Set the runbook phase to `restart` and operation to `k8s.restart-deployment`.
3. Include a user-authorized `changeTicket`, a concrete `reason`, `expectedReplicaCount`, and `waitForReadySeconds` from 30 through 600.
4. Submit the exact restart request for a new independent review; diagnosis approval cannot be reused.
5. Execute once and persist the receipt. Do not retry an unknown or failed operation automatically.

## Stop conditions

- The cluster, namespace, or deployment differs from the original goal.
- The target or operation is not allowlisted by prod-mcp.
- The request exceeds the bounded log or readiness limits.
- A restart was not explicitly authorized by the caller.
- The reviewer or prod-mcp rejects the request.
