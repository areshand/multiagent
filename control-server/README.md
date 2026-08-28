# Multiagent control server

The control server owns authenticated HTTP/WebSocket APIs, durable Threads,
and execution Session lifecycle. It has no browser UI and does not contain the
terminal client implementation. The standalone client lives in `../client` and
communicates only through the public HTTP API.

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
