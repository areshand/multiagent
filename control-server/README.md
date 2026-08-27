# Multiagent client CLI

The control server exposes authenticated durable-thread APIs. The repo-owned
CLI is the primary human and agent-facing client; the root web page is only a
minimal reachability and command-discovery surface.

## Use

From this directory:

```bash
npm run client -- --server https://agent.example login operator
npm run client -- threads list
npm run client -- threads show THREAD_ID
npm run client -- threads watch THREAD_ID
```

The first command securely prompts for the password. For a non-interactive
caller, provide the password on stdin. Do not put it in a command argument:

```bash
printf '%s' "$MULTIAGENT_LOGIN_PASSWORD" | \
  npm run client -- --server https://agent.example login operator
```

The login session defaults to
`~/.config/multiagent/client-session.json` and is written with mode `0600`.
Override it with `--session-file` or `MULTIAGENT_CLIENT_SESSION_FILE`.

## Commands

```text
repositories list
threads list
threads show THREAD_ID
threads create THREAD_ID --repository NAME [--title TITLE] (--message TEXT | --message-file PATH)
threads send THREAD_ID (--message TEXT | --message-file PATH)
threads watch THREAD_ID [--after SEQUENCE] [--once]
sessions list THREAD_ID
legacy list
legacy report SESSION_ID
whoami
logout
```

Normal commands emit formatted JSON. `threads watch` emits newline-delimited
JSON events so scripts and agents can consume the stream incrementally. A
thread ID is never reused as an execution-session ID; the server creates and
returns execution sessions when a message needs a fresh runtime.

## Development

Run the client and server contract tests with:

```bash
npm test
```

The tests use an injected HTTP transport rather than a browser or a simulated
agent runtime. Production end-to-end acceptance still requires the real
deployed gateway, session worker, runbooks, `prod-mcp`, KMS, and trace export.
