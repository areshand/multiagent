#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
DEFAULT_ROOT="/Users/bowu/projects/multiagent"
ROOT="${MULTIAGENT_ROOT:-$DEFAULT_ROOT}"
PROMPT_FILE="$ROOT/orchestrator_prompt.md"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"
CODEX_BIN="${CODEX_BIN:-codex}"
ATTACH=1

usage() {
  cat <<'USAGE'
Usage: ./launch.sh [--session NAME] [--root DIR] [--attach|--no-attach]

Starts a tmux multi-agent session with one window:
  - orchestrator: Codex commander that spawns and manages workers

Environment:
  MULTIAGENT_SESSION  Default tmux session name
  MULTIAGENT_ROOT     Default project root, default: /Users/bowu/projects/multiagent
  MULTIAGENT_STATE_DIR Persisted subagent state, default: $MULTIAGENT_ROOT/.multiagent
  MULTIAGENT_WRITE_POLICY Repo write policy, default: $MULTIAGENT_ROOT/docs/write-policy.paths
  CODEX_BIN           Codex CLI command, default: codex
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --session)
      SESSION="$2"
      shift 2
      ;;
    --root)
      ROOT="$(cd "$2" && pwd)"
      PROMPT_FILE="$ROOT/orchestrator_prompt.md"
      POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"
      shift 2
      ;;
    --attach)
      ATTACH=1
      shift
      ;;
    --no-attach)
      ATTACH=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_cmd tmux
require_cmd "$CODEX_BIN"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Missing orchestrator prompt: $PROMPT_FILE" >&2
  exit 1
fi

if [[ ! -x "$ROOT/bin/write-policy.sh" ]]; then
  echo "Missing write policy helper: $ROOT/bin/write-policy.sh" >&2
  exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION" >&2
  echo "Attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

export MULTIAGENT_SESSION="$SESSION"
export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_PROMPT="$PROMPT_FILE"
export MULTIAGENT_STATE_DIR="$STATE_DIR"
export MULTIAGENT_WRITE_POLICY="$POLICY_FILE"

mkdir -p "$STATE_DIR/subagents" "$STATE_DIR/assignments"
"$ROOT/bin/write-policy.sh" init

ORCHESTRATOR_BOOTSTRAP="$(
  cat <<EOF
cd '$ROOT'
export MULTIAGENT_SESSION='$SESSION'
export MULTIAGENT_ROOT='$ROOT'
export MULTIAGENT_PROMPT='$PROMPT_FILE'
export MULTIAGENT_STATE_DIR='$STATE_DIR'
export MULTIAGENT_WRITE_POLICY='$POLICY_FILE'
$CODEX_BIN --cd '$ROOT' --dangerously-bypass-approvals-and-sandbox --no-alt-screen "\$(cat '$PROMPT_FILE')"
EOF
)"

tmux new-session -d -s "$SESSION" -n orchestrator "$ORCHESTRATOR_BOOTSTRAP"
tmux select-window -t "$SESSION:orchestrator"

echo "Started tmux session: $SESSION"
echo "Attach with: tmux attach -t $SESSION"
echo "Subagent state: $STATE_DIR"
echo "Write policy:"
"$ROOT/bin/write-policy.sh" show

if [[ "$ATTACH" -eq 1 ]]; then
  tmux attach -t "$SESSION"
fi
