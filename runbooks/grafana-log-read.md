# Grafana Log Read

## Metadata

- Runbook ID: `observability.investigation`
- Version: `1.1.0`
- Prod MCP operation: `grafana.read`

## Goal

Read production logs through Grafana for a bounded investigation. This runbook
does not authorize mutation of Grafana, Loki, Kubernetes, or the target service.

## Procedure

1. Identify the target environment, cluster, namespace, and service from the original goal.
2. If the original goal does not provide verified Loki labels, use `list-loki-label-names` and then `list-loki-label-values` through prod-mcp. Treat only returned names and values as discovered evidence.
3. Construct the narrowest LogQL query that satisfies the original goal using those verified labels.
4. Set `action` to `query-loki-logs`, `direction` to `backward`, and `datasourceUid` to the approved Loki datasource.
5. Set `lookbackMinutes` no higher than 120 and `limit` no higher than 100 for discovery and query requests.
6. Materialize each generic prod-mcp request and submit it for independent pre-execution review.
7. Execute only after the reviewer accepts the exact goal, runbook, target, operation, and parameters.
8. Persist every discovery/query action ID and receipt for independent post-execution review.

## Required request parameters

- `action`
- `datasourceUid`
- `direction`
- `limit`
- `logql`
- `lookbackMinutes`

Label discovery additionally requires `action`, `datasourceUid`, `lookbackMinutes`, and `limit`; `list-loki-label-values` also requires `labelName`.

## Stop conditions

- The original goal does not identify a bounded investigation.
- The requested target is broader than the original goal.
- The query requires more than 120 minutes of history or more than 100 results.
- The operation would write data or change service state.
- The reviewer or prod-mcp rejects the request.
## Reviewed role continuation

Use the provider-neutral lifecycle in
`prompts/playbooks/reviewed-ops-cycle.md` for every immutable request. This
runbook defines Grafana operations and limits; it does not redefine agent
spawning, independent review, binding, restoration, or receipt handling.
