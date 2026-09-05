# Multiagent terminal client

The control server exposes authenticated durable-thread APIs. The repo-owned
terminal client is the only human interface. The control server has no browser
UI; its root route returns service metadata as JSON.

## Use

From this directory:

```bash
npm run client -- --server https://agent.example login operator
npm run client --
```

Running without a command opens a persistent, Claude Code-style terminal without
enumerating server threads. `/list` shows only threads created by this local
client profile; `/open THREAD_ID` opens an explicitly known thread without adding
it to that local list. A list number may be used after `/list`. Use
`/new REPOSITORY [TITLE]` to create a thread and `/help` to see the complete
interactive command set. The server
assigns both thread IDs and execution-session IDs. After a message starts an
execution session, the client streams the orchestrator terminal without locking
the prompt. Additional ordinary input is durably appended and delivered as a
follow-up to that open thread's active orchestrator. Use `/wait` when you want
to stop entering commands until the current execution replies. While a thread
is open, the client maintains an authenticated WebSocket to receive conversation
events, thread state, heartbeats, and bounded subagent status. A separate
session WebSocket carries live orchestrator terminal output only while an
execution is active. The interactive TTY reserves a small bottom pane for each
subagent's state, role, and current progress as a compact graph rooted at the
orchestrator. Before the first delegation, the graph labels the orchestrator
`planning` and says that no agents have been delegated yet; it does not imply
that a separate discovery operation is running. The stable `› ` input area
remains available while agents work;
asynchronous output redraws it without discarding partially typed follow-up
text. When an execution finishes, the pane keeps a concise result summary,
wrapped to at most three terminal lines, showing the latest public outcome, and
labels the orchestrator `complete` instead of reducing the result to `idle`.
Bounded clarification responses are shown as questions and wait for ordinary
follow-up input. HTTP event replay repairs gaps after a disconnect and
reconstructs that summary when a thread is reopened.

Pending Slack repair proposals are shown before the normal prompt in a clearly
labelled review window. Enter `yes` to bind the exact review question to a fresh
human-authorized execution session, or `no` to reject it and permanently close
that thread to further messages. Use `/reviews` to refresh the pending queue.

The first command securely prompts for the password. For a non-interactive
caller, provide the password on stdin. Do not put it in a command argument:

```bash
printf '%s' "$MULTIAGENT_LOGIN_PASSWORD" | \
  npm run client -- --server https://agent.example login operator
```

The login session defaults to
`~/.config/multiagent/client-session.json` and is written with mode `0600`.
Override it with `--session-file` or `MULTIAGENT_CLIENT_SESSION_FILE`.
Locally created thread IDs are kept separately in the adjacent mode-`0600`
`client-session.json.threads.json` file. It contains no authentication cookie.

## Commands

The following non-interactive commands remain available for agents, scripts,
and debugging:

```text
connect [THREAD_ID]
reviews list [--status pending|approved|rejected|all]
reviews decide REVIEW_ID yes|no
repositories list
threads list  # only threads created by this local client profile
threads show THREAD_ID
threads create --repository NAME [--title TITLE] (--message TEXT | --message-file PATH)
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

Run the client contract tests with:

```bash
npm test
```

The tests use an injected HTTP transport rather than a simulated agent runtime.
Production end-to-end acceptance still requires the real
deployed gateway, session worker, runbooks, `prod-mcp`, KMS, and trace export.
