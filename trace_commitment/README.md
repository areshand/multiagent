# Trace commitment delivery

The deployment's existing trace exporter remains responsible for staging,
delta detection, retry, and uploading trace bodies. After a successful sync,
`python3 -m trace_commitment.trace_commitment` hashes that stable staged tree,
writes a deterministic commitment manifest as a separate object in the same S3
location, and durably retries a bounded `trace.artifact_exported` Logger event.

The Logger receives only the manifest digest, size, media type, and S3
reference. The event and delivered marker are also separate metadata-only
objects beside the existing export. Deterministic IDs preserve idempotency after
an uncertain acknowledgement or process restart.

Required environment is `TRACE_COMMITMENT_SOURCE`, `TRACE_EXPORT_DESTINATION`,
`TRACE_SESSION_ID`, `LOGGER_URL`, and `LOGGER_TOKEN_FILE`.
`TRACE_COMMITMENT_WORK_DIR` and `TRACE_EXPORT_STATUS_FILE` are optional. Logger
delivery updates `loggerOk`, `loggerPending`, and Logger timestamps/errors in
the existing status file without changing its S3 export `ok` field.
