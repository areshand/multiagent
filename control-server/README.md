# Multiagent client CLI

The control server exposes authenticated durable-thread APIs. The repo-owned
terminal client is the only human interface. The control server has no browser
UI; its root route returns service metadata as JSON.

## Use

From this directory:

```bash
npm run client -- --server https://agent.example login operator
npm run client --
```

Running without a command opens a persistent, Claude Code-style terminal. It
lists durable threads, accepts `/open THREAD_ID` or a list number, and sends
ordinary input to the open thread. Use `/new THREAD_ID REPOSITORY` to create a
thread and `/help` to see the complete interactive command set. The server,
not the user, creates execution-session IDs.

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

The following non-interactive commands remain available for agents, scripts,
and debugging:

```text
connect [THREAD_ID]
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

Non-interactive commands emit formatted JSON. `threads watch` emits newline-delimited
JSON events so scripts and agents can consume the stream incrementally. A
thread ID is never reused as an execution-session ID; the server creates and
returns execution sessions when a message needs a fresh runtime.

## Development

Run the client and server contract tests with:

```bash
npm test
```

The tests use an injected HTTP transport rather than a simulated agent runtime.
Production end-to-end acceptance still requires the real
deployed gateway, session worker, runbooks, `prod-mcp`, KMS, and trace export.
