# Audit Logger service

`audit-log` is the independently deployed, single-writer authority for
structural audit history. Agents, supervisors, reviewers, runtime sidecars,
and `prod-mcp` are producers; none of them can choose an authoritative
sequence number or parent hash.

The service owns:

- authenticated, event-type- and session-scoped ingestion;
- canonical event encoding and serialized SQLite WAL appends;
- replay-safe event IDs and per-session hash chains;
- Ed25519-signed periodic checkpoints;
- startup and on-demand chain verification;
- durable retries for optional non-authoritative JSONL projections;
- health, readiness, integrity, and Prometheus metrics endpoints.

It does not interpret agent reasoning, runbooks, reviewer policy, or production
operations. It has no model credentials, production credentials, or permit
signing authority. It never approves a workflow transition and does not return
an append result that a producer can use as an authorization artifact.

`POST /v1/events` returns `204 No Content` after a new or exactly idempotent
append. That empty HTTP response is transport acknowledgement only. Supervisor
workflow gates do not consume it. Producer integrations must retain and retry
undelivered events through a local outbox or deployment-owned durable queue and
alert on an excessive backlog; an Audit Logger outage does not transfer
workflow authority to the logger.

## Run locally

Node.js 24 or newer is required. Generate an Ed25519 key and a producer token:

```bash
openssl genpkey -algorithm ED25519 -out /tmp/audit-logger.pem
token="$(openssl rand -hex 32)"
node audit-log/bin/hash-token.mjs "$token"
```

Create a deployment-owned client file using the printed digest:

```json
{
  "clients": [
    {
      "id": "local-producer",
      "tokenSha256": "sha256:...",
      "permissions": ["append", "read", "verify"],
      "eventTypes": ["*"],
      "sessions": ["*"]
    }
  ]
}
```

Then start the service:

```bash
AUDIT_LOG_SIGNING_KEY_FILE=/tmp/audit-logger.pem \
AUDIT_LOG_CLIENTS_FILE=/tmp/audit-clients.json \
AUDIT_LOG_DATABASE=/tmp/audit-log.sqlite \
AUDIT_LOG_LOGGER_ID=audit-logger-local \
npm start --prefix audit-log
```

Submit an event with the generic producer client:

```bash
printf '%s\n' '{
  "eventId":"event-abc",
  "sessionId":"session-123",
  "eventType":"reviewer.verdict",
  "payloadDigest":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "artifactReferences":[]
}' | AUDIT_LOG_URL=http://127.0.0.1:8090 \
    AUDIT_LOG_BEARER_TOKEN_FILE=/tmp/audit-producer-token \
    node audit-log/bin/submit-event.mjs
```

The trace exporter should continue sending bulk trace files directly to S3.
After a successful export it submits a small `trace.artifact_exported` event
whose payload and artifact digests commit the exported bytes and storage
reference to the authoritative chain.

The bundled helper streams the local file through SHA-256 without loading the
trace body into memory or sending it to the logger:

```bash
AUDIT_LOG_URL=http://audit-log:8090 \
AUDIT_LOG_BEARER_TOKEN_FILE=/run/secrets/audit-log/token \
node audit-log/bin/submit-trace-commitment.mjs \
  --event-id trace-export-session-123 \
  --session-id session-123 \
  --file /traces/session-123.tar.gz \
  --storage-reference s3://audit-bucket/session-123.tar.gz \
  --media-type application/gzip
```

## API

- `POST /v1/events`
- `GET /v1/logs/{logId}/head`
- `GET /v1/logs/{logId}/entries?after=0&limit=100`
- `GET /v1/logs/{logId}/checkpoints?after=0&limit=100`
- `GET /v1/checkpoints/{checkpointId}`
- `GET /v1/public-key`
- `POST /v1/verify`
- `GET /healthz`
- `GET /readyz`
- `GET /metrics` (requires a client with `read` permission)

All `/v1` requests except `/v1/public-key` require a bearer token configured
in the deployment-owned clients file. The service stores only token digests.
Use distinct producer tokens and narrow `eventTypes` and `sessions` rules.

An event is limited to 64 KiB by default. `artifactReferences` contain metadata
only; the logger is deliberately not a blob proxy.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUDIT_LOG_DATABASE` | `/var/lib/audit-log/ledger.sqlite` | Dedicated authoritative SQLite database |
| `AUDIT_LOG_SIGNING_KEY_FILE` | required | Mounted Ed25519 PKCS#8 private key |
| `AUDIT_LOG_CLIENTS_FILE` | required | Mounted producer/read-client authorization file |
| `AUDIT_LOG_LOGGER_ID` | required | Stable deployment identity placed in signed checkpoints |
| `AUDIT_LOG_SIGNING_KEY_ID` | `audit-log-signing-key` | Public signing-key identifier |
| `AUDIT_LOG_CHECKPOINT_INTERVAL` | `100` | Entries between signed checkpoints per log |
| `AUDIT_LOG_MAX_EVENT_BYTES` | `65536` | Maximum ingestion body size |
| `AUDIT_LOG_PROJECTION_DIR` | unset | Optional non-authoritative JSONL projection directory |
| `AUDIT_LOG_PROJECTION_INTERVAL_MS` | `1000` | Durable projection retry worker interval |
| `PORT` | `8090` | HTTP port |

Signing keys, producer tokens, concrete storage, workload identities, network
policy, and retention remain deployment-owned.
