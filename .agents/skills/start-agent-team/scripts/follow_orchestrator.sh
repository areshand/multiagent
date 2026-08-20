#!/usr/bin/env bash
set -euo pipefail

SESSION=""
ROOT=""

usage() {
  cat <<'USAGE'
Usage: follow_orchestrator.sh --session NAME --root DIR

Attach a read-only tmux client to the orchestrator window. Run this helper in
a PTY-backed Codex terminal session so the live pane can be opened in the
Codex desktop UI. Keystrokes cannot control or resize the tmux session.
USAGE
}

die() {
  echo "follow-orchestrator: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION="${2:-}"
      shift 2
      ;;
    --root)
      ROOT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -n "$SESSION" ]] || die "--session NAME is required"
[[ -n "$ROOT" ]] || die "--root DIR is required"
[[ -d "$ROOT" ]] || die "root does not exist or is not a directory: $ROOT"
command -v tmux >/dev/null 2>&1 || die "missing required command: tmux"
tmux has-session -t "$SESSION" 2>/dev/null || die "missing tmux session: $SESSION"
tmux list-windows -t "$SESSION" -F '#W' | grep -Fxq orchestrator || \
  die "session has no orchestrator window: $SESSION"

if [[ -z "${TERM:-}" || "${TERM:-}" == "dumb" ]]; then
  if infocmp xterm-256color >/dev/null 2>&1; then
    export TERM=xterm-256color
  else
    die "terminal does not support a live tmux view"
  fi
fi

ROOT="$(cd "$ROOT" && pwd -P)"
pane_root="$(tmux display-message -p -t "$SESSION:orchestrator" '#{pane_current_path}')"
[[ -d "$pane_root" ]] || die "orchestrator pane path is not a directory: $pane_root"
pane_root="$(cd "$pane_root" && pwd -P)"
[[ "$pane_root" == "$ROOT" ]] || \
  die "orchestrator root mismatch: expected $ROOT, found $pane_root"

exec tmux attach-session -r -f ignore-size -t "$SESSION:orchestrator"
