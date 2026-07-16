#!/usr/bin/env bash
set -euo pipefail

: "${PILOT_WORKTREE:?pilot runner must set PILOT_WORKTREE}"
: "${PILOT_CELL_DIR:?pilot runner must set PILOT_CELL_DIR}"
: "${PILOT_PROMPT_FILE:?pilot runner must set PILOT_PROMPT_FILE}"

CODEX_BIN="${CODEX_BIN:-codex}"
command -v "$CODEX_BIN" >/dev/null 2>&1 || {
  echo "missing Codex CLI: $CODEX_BIN" >&2
  exit 127
}

exec "$CODEX_BIN" exec \
  --cd "$PILOT_WORKTREE" \
  --skip-git-repo-check \
  --dangerously-bypass-approvals-and-sandbox \
  --output-last-message "$PILOT_CELL_DIR/solver-last-message.txt" \
  - < "$PILOT_PROMPT_FILE"
