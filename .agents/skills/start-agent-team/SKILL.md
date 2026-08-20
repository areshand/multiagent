---
name: start-agent-team
description: Start and coordinate project-focused multiagent swarm teams using this repository's tmux orchestrator, and show read-only live orchestrator output in the Codex desktop UI. Use only when the user explicitly asks for an agent team, swarm team, worker team, orchestrator, or multiagent session for a coding project or repository.
---

# Start Agent Team

## Launch

Use this repository's `launch.sh` to start a tmux session with one orchestrator
window. The orchestrator creates workers and long-running subagents for the
target project.

This skill is only for tmux-backed multiagent teams. Standalone agent or
subagent requests should use native Codex UI agents instead.

Default to a clean, non-attached launch:

```bash
REPO_ROOT/launch.sh --session SESSION --root PROJECT_ROOT --no-attach
```

- Use `multiagent-<repo-name>` unless the user supplies a session name.
- Use the user's named project root, or the current git root for "this project."
- Add `--resume` only for explicit recovery.
- Keep `ORCHESTRATOR_CLI=codex` unless the user requests another backend.
- Do not overwrite an existing tmux session.

If the user supplied a task, inspect the orchestrator pane before sending it:

```bash
tmux capture-pane -t "SESSION:orchestrator" -p -S -200
tmux send-keys -t "SESSION:orchestrator" "TOP_LEVEL_TASK" Enter
```

Do not send input while the orchestrator is starting, blocked on authentication,
or busy. Keep the task high level and let the orchestrator decompose it.

## Live Codex UI view

After launch and task delivery, prefer a live read-only terminal view when the
Codex desktop terminal-opening tool is available:

1. Start the follower in PTY mode (`tty: true`) with a short yield. Keep the
   returned running terminal `session_id`.

   ```bash
   REPO_ROOT/.agents/skills/start-agent-team/scripts/follow_orchestrator.sh \
     --session SESSION --root PROJECT_ROOT
   ```

2. Open that terminal in Codex with `codex_app__open_in_codex`, using target
   `{ type: "terminal", sessionId: "SESSION_ID" }`. Prefer bottom placement
   unless the user requested another layout.
3. Tell the user the panel is a live, read-only orchestrator view. Do not mirror
   its entire transcript into commentary.

The follower validates the project root and attaches a tmux client with both
`read-only` and `ignore-size` flags. It cannot send input to or resize the
orchestrator or worker panes. Do not start it without a PTY or wait
synchronously for it to exit.

If the terminal-opening tool is unavailable, show a bounded snapshot instead:

```bash
tmux list-windows -t SESSION -F '#I:#W'
tmux capture-pane -t "SESSION:orchestrator" -p -S -220
```

Treat the orchestrator pane as the delegation boundary. Ask the orchestrator to
spawn, poll, inspect, or finalize its agents; do not drive those agents directly
unless the user explicitly requests bypassing the orchestrator.

## Safety

- Do not translate a standalone subagent request into a tmux team.
- Do not delete `PROJECT_ROOT/.multiagent`; it stores durable state.
- Do not kill or replace an existing session with the same name.
- Keep outside-root writes subject to the multiagent write policy.
