# Logger service

`logger` is the independently deployed Rust single-writer authority for
structural history. Producers submit bounded event commitments; only this
service assigns authoritative sequence numbers and parent hashes.

It provides scoped bearer authentication, canonical JSON encoding, a durable
fsynced append-only JSONL ledger, exact event-id idempotency, per-session hash chains,
Ed25519-signed periodic checkpoints, startup/on-demand verification, and
optional retrying JSONL projections. It does not interpret reasoning, approve
workflow transitions, hold production credentials, or issue operation permits.

`POST /v1/events` returns `204 No Content` for a new or exactly idempotent
append. The response is a transport acknowledgement, never an authorization
artifact. Producers must retain and retry undelivered events.

## Run locally

Generate an Ed25519 key and token, then put the printed digest in a client file:

```bash
openssl genpkey -algorithm ED25519 -out /tmp/logger.pem
token="$(openssl rand -hex 32)"
cargo run -p multiagent-logger -- hash-token "$token"
```

```json
{"clients":[{"id":"local-producer","tokenSha256":"sha256:...","permissions":["append","read","verify"],"eventTypes":["*"],"sessions":["*"]}]}
```

```bash
LOGGER_SIGNING_KEY_FILE=/tmp/logger.pem \
LOGGER_CLIENTS_FILE=/tmp/logger-clients.json \
LOGGER_LEDGER_FILE=/tmp/logger.jsonl \
LOGGER_ID=logger-local \
cargo run -p multiagent-logger -- serve
```

Submit a JSON event on stdin:

```bash
cargo run -p multiagent-logger -- submit-event \
  --url http://127.0.0.1:8090 --token-file /tmp/logger-token < event.json
```

Commit a trace artifact without sending the artifact body:

```bash
cargo run -p multiagent-logger -- submit-trace-commitment \
  --url http://logger:8090 --token-file /run/secrets/logger/token \
  --event-id trace-export-session-123 --session-id session-123 \
  --file /traces/session-123.tar.gz \
  --storage-reference s3://audit-bucket/session-123.tar.gz \
  --media-type application/gzip
```

## API and configuration

The API exposes event append, log heads/entries/checkpoints, the public key,
verification, health/readiness, and authenticated metrics endpoints. Event
bodies are limited to 64 KiB by default; artifact references are metadata only.

| Variable | Default |
| --- | --- |
| `LOGGER_LEDGER_FILE` | `/var/lib/logger/ledger.jsonl` |
| `LOGGER_SIGNING_KEY_FILE` | required |
| `LOGGER_CLIENTS_FILE` | required |
| `LOGGER_ID` | required |
| `LOGGER_SIGNING_KEY_ID` | `logger-signing-key` |
| `LOGGER_CHECKPOINT_INTERVAL` | `100` |
| `LOGGER_MAX_EVENT_BYTES` | `65536` |
| `LOGGER_PROJECTION_DIR` | unset |
| `LOGGER_PROJECTION_INTERVAL_MS` | `1000` |
| `PORT` | `8090` |

Signing keys, client tokens, storage, network policy, retention, and workload
identity remain deployment-owned.
