# Trace exporter

`python3 -m trace_exporter.trace_exporter` copies regular trace files to the
deployment-owned S3 trace bucket, preserves a content-addressed object for each
digest, and writes a bounded metadata-only Logger event to an S3 outbox. It
replays outbox objects until Logger acknowledges them, then writes an immutable
delivered marker. Deterministic event IDs make a retry after an uncertain
acknowledgement safe against Logger's exact idempotency contract.

Required environment:

| Variable | Purpose |
| --- | --- |
| `TRACE_SOURCE` | Read-only trace tree |
| `TRACE_BUCKET` | Deployment-owned S3 bucket |
| `TRACE_PREFIX` | Conventional and content-addressed trace prefix |
| `TRACE_SESSION_ID` | Logger session/log ID |
| `TRACE_OUTBOX_PREFIX` | Metadata-only durable outbox prefix |
| `TRACE_DELIVERED_PREFIX` | Durable delivery-marker prefix |
| `LOGGER_URL` | Private Logger base URL |
| `LOGGER_TOKEN_FILE` | Trace-commitment-only bearer token file |

`TRACE_EXPORT_WORK_DIR`, `TRACE_EXPORT_STATUS_FILE`, and
`TRACE_EXPORT_INTERVAL_SECONDS` are optional. The status contract keeps S3
success in `ok` and reports Logger delivery separately through `loggerOk`,
`loggerPending`, and Logger timestamps/errors. Consumers must never use Logger
availability or acknowledgement to grant or deny workflow transitions.
