#!/usr/bin/env bash
set -euo pipefail

: "${PILOT_HARNESS_ROOT:?pilot runner must set PILOT_HARNESS_ROOT}"
: "${PILOT_WORKTREE:?pilot runner must set PILOT_WORKTREE}"
: "${PILOT_CELL_DIR:?pilot runner must set PILOT_CELL_DIR}"
: "${PILOT_PROMPT_FILE:?pilot runner must set PILOT_PROMPT_FILE}"
: "${PILOT_TASK_ID:?pilot runner must set PILOT_TASK_ID}"
: "${PILOT_SOLVER_TIMEOUT_SECONDS:?pilot runner must set PILOT_SOLVER_TIMEOUT_SECONDS}"

command -v tmux >/dev/null 2>&1 || { echo "missing tmux" >&2; exit 127; }
command -v "${CODEX_BIN:-codex}" >/dev/null 2>&1 || { echo "missing Codex CLI" >&2; exit 127; }

safe_task="$(printf '%s' "$PILOT_TASK_ID" | tr -cd '[:alnum:]_-')"
session="pilot-${safe_task:0:24}-$$"
state_dir="$PILOT_CELL_DIR/runtime-state"
full_prompt="$PILOT_CELL_DIR/orchestrator-prompt.md"

# shellcheck disable=SC2329  # Invoked indirectly by trap.
cleanup() {
  tmux capture-pane -p -S - -t "$session:orchestrator" \
    > "$PILOT_CELL_DIR/orchestrator-pane.log" 2>/dev/null || true
  tmux kill-session -t "$session" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cp "$PILOT_HARNESS_ROOT/prompts/orchestrator.md" "$full_prompt"
cat >> "$full_prompt" <<EOF

## Internal Pilot Assignment

This is a bounded benchmark cell. Complete the task below in
\`$PILOT_WORKTREE\`, run appropriate validation, close the structured gate, give
a concise final report, and then exit. Use \`\$MULTIAGENT_BIN subagent\` whenever
the core prompt shows \`multiagent subagent\`; do not expect framework files in
the target repository. The launcher supplies the absolute Rust executable path.
Do not read sibling pilot cells or future commits.

EOF
cat "$PILOT_PROMPT_FILE" >> "$full_prompt"

export MULTIAGENT_SESSION="$session"
export MULTIAGENT_ROOT="$PILOT_WORKTREE"
export MULTIAGENT_STATE_DIR="$state_dir"
export MULTIAGENT_WRITE_POLICY="$PILOT_CELL_DIR/write-policy.paths"
export MULTIAGENT_PROMPT="$full_prompt"
export MULTIAGENT_PROMPT_MODULE_ROOT="$PILOT_HARNESS_ROOT"
export MULTIAGENT_CODEX_EXEC=1
export ORCHESTRATOR_CLI=codex
export WORKER_CLI="${PILOT_WORKER_CLI:-codex}"
export SUBAGENT_CLI="${PILOT_WORKER_CLI:-codex}"
export VERIFIER_CLI="${PILOT_VERIFIER_CLI:-codex}"

"$PILOT_HARNESS_ROOT/launch.sh" --session "$session" \
  --root "$PILOT_WORKTREE" --no-attach

deadline=$((SECONDS + PILOT_SOLVER_TIMEOUT_SECONDS))
last_message="$state_dir/orchestrator-last-message.txt"
while (( SECONDS < deadline )); do
  if [[ -s "$last_message" ]]; then
    cat "$last_message"
    exit 0
  fi
  pane="$(tmux capture-pane -p -S -20 -t "$session:orchestrator" 2>/dev/null || true)"
  if printf '%s\n' "$pane" | grep -q '\[multiagent codex exec exited rc='; then
    printf '%s\n' "$pane" >&2
    exit 1
  fi
  sleep 2
done

echo "orchestrated solver timed out after ${PILOT_SOLVER_TIMEOUT_SECONDS}s" >&2
exit 124
