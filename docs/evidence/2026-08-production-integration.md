# Initial production integration: recorded deployment evidence

Reviewed on September 15, 2026. The operational observations below are from
August 22–27, 2026; this documentation review did not query the live deployment
or rerun the integration test.

## Summary

The historical deployment records support three completed milestones:

- prod-mcp was configured to trust the supervisor through an immutable AWS KMS
  key ARN, an explicit key ID, and a matching region.
- A deployed multiagent session read actual testnet Loki logs through prod-mcp.
- The session retained its request, successful operation receipt, and trace
  artifacts in its scoped S3 prefix.

These observations correct the previously unchecked setup items in the
[canonical TODO](../TODO.md#production-mcp-integration-status). They establish
an internally deployed integration with real operational data, not a simulated
Grafana response. They do not establish current availability, organic adoption,
or completion of every later architecture requirement.

## Key pinning

The deployment catalog read on August 22 at 10:29:59 UTC contained a non-empty
kmsKeys entry with:

- keyArn: an immutable AWS KMS key resource ARN, not an alias;
- kid: the configured supervisor permit-verification key ID;
- region: the same region as the key ARN.

The recorded verifier implementation resolves the configured key through
GetPublicKey at startup, checks the immutable ARN and region, and imports the
public key for permit verification. Pinning here means selecting an immutable
KMS key resource; it does not mean checking a PEM public key into this repository.

An August 27 deployment record also contains the same key ARN, key ID, and
region in the recorded prod-mcp Helm values. The real Grafana receipt below
comes from the intervening deployed integration. The cloud account, full key
ARN, key material, and authentication values are intentionally omitted here.

## Real Grafana/Loki execution

Session: grafana-e2e-20260823-v28.

The session report records discovery of 13 Loki label names and 35 namespace
values, followed by retrieval of real logs selected from the testnet environment.
The deployment report identifies multiagent Helm revision 45.

Selected fields from the retained operation receipt:

    operationId: op-41f65b2358d1353e01a384f335a8e9af
    actionId: ops-1787477155582-1630
    requestedOperation.id: grafana.read
    state: succeeded
    executor: fixed-command
    acceptedAt: 2026-08-23T09:25:56.022Z
    updatedAt: 2026-08-23T09:25:56.654Z

The receipt includes a permit digest and returned Loki content. Its recorded
result contains real cluster-autoscaler log entries. This note omits service
identifiers, private endpoints, raw logs, and the complete permit/request.

At that point the integration worked on the internal path. The contemporaneous
report still listed public DNS/Ingress work as pending; this run should not be
described as evidence that the public reverse-proxy path was ready.

## S3 persistence

The August 23 object listing confirms artifacts beneath the session prefix,
including these relative keys:

    production/sessions/grafana-e2e-20260823-v28/logs/transcript-index.json
    production/sessions/grafana-e2e-20260823-v28/operations/ops-1787477155582-1630/request.json
    production/sessions/grafana-e2e-20260823-v28/operations/ops-1787477155582-1630/receipt.json

The bucket name is omitted. The listing establishes observed persistence of the
session artifacts; it does not prove retention policy, rollback detection, or
complete trace coverage for every execution.

## Source provenance

This is an operator-readable summary derived from retained private deployment
records, not a publicly downloadable or independently reproduced test bundle.
The source is the Codex deployment session
01a02145-afcc-7601-8e7e-d078dc1b0296, started on August 20, 2026.

The following one-based JSONL record locations identify the observations:

| Record | Observation time (UTC) | Evidence |
| --- | --- | --- |
| 6338 | August 22, 10:29:59 | Deployment catalog with the configured KMS key pin |
| 17685 | August 23, 09:30:40 | Session report with discovery results, operation identifiers, Helm revision and artifact locations |
| 18090 | August 23, 09:55:40 | Tool output containing the retained successful Grafana operation receipt |
| 18100 | August 23, 09:56:03 | Tool output listing persisted S3 request, receipt and trace artifacts |
| 41354 | August 27, 08:04:18 | Deployment record retaining the configured KMS pin in Helm values |

The two raw tool-output records used for receipt and S3 verification have these
SHA-256 digests, computed over each original JSONL line including its newline:

    record 18090: 9e1dc7e77b7e29fa961cb02a99cf673589a139b896743bb5b43fa58d840fe4db
    record 18100: 408d3b2a27d9ca19e9cb10d6936882eb5ada038dc57d8d3b9408043b22217f4a

These digests help an authorized reviewer match the retained records; they are
not signatures or independent attestations of the deployment outcome. No local
user filesystem paths or private cloud object URLs are required by this note.

Later public evidence in [PR #87](https://github.com/areshand/multiagent/pull/87),
merged September 5, records a completed production Wiki thread, a deployed
111-repository catalog, and S3 trace export. It supports closing the old
repository-provisioning setup item; it is not a rerun of the Grafana scenario.

## What remains open

The [current architecture acceptance path](../architecture/system-architecture.md#required-end-to-end-acceptance-path)
also requires the independent Logger to receive trace commitments, expose chain
and signed checkpoint evidence, and retain continuity after restart. Those
requirements postdate this Grafana run. A complete current-deployment acceptance
run remains unchecked in the TODO, alongside the separate Logger rollback
detection and durable-delivery backlog items.

This update does not change the historical benchmark interpretation: 36/50
remains a tuned cumulative aggregate, not a reproducible single run.

