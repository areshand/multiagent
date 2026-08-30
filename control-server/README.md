# Multiagent control server

The control server owns authenticated HTTP/WebSocket APIs, durable Threads,
and execution Session lifecycle. It has no browser UI and does not contain the
terminal client implementation. The standalone client lives in `../client` and
communicates only through the public HTTP API.

The public API supports thread creation and individually authorized lookup by
thread ID; it does not expose server-wide thread discovery. `GET /api/threads`
returns `404`. Deployment diagnostics inside the control-server Pod inspect the
internal thread manifest/store directly rather than using an HTTP endpoint.
The legacy `GET /api/sessions` collection excludes every thread-backed execution,
so it cannot be used to recover the removed thread collection indirectly.

## Development

```bash
npm ci
npm test
npm start
```

The GitHub-backed end-to-end contracts remain here because they exercise the
deployed control server and execution runtime:

```bash
npm run test:e2e:github
npm run test:e2e:thread
```
