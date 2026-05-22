#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$SCRIPT_DIR"
ROOT="${MULTIAGENT_ROOT:-$DEFAULT_ROOT}"
PROMPT_FILE="${MULTIAGENT_PROMPT:-$SCRIPT_DIR/orchestrator_prompt.md}"
CODEX_BIN="${CODEX_BIN:-codex}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
ORCHESTRATOR_CLI="${ORCHESTRATOR_CLI:-codex}"
ATTACH=1
RESUME=0

usage() {
  cat <<'USAGE'
Usage: ./launch.sh [--session NAME] [--root DIR] [--resume] [--attach|--no-attach]

Starts a tmux multi-agent session with one window:
  - orchestrator: Codex commander that spawns and manages workers

By default the orchestrator starts clean and does not inspect recovery state.
Pass --resume to allow the orchestrator to inspect recovery state and consider
restoring/resuming persisted subagents.

Environment:
  MULTIAGENT_SESSION  Default tmux session name
  MULTIAGENT_ROOT     Default project root, default: launcher directory
  MULTIAGENT_RESUME   Launch mode exported by this script: 0 clean, 1 resume
  MULTIAGENT_STATE_DIR Persisted subagent state, default: $MULTIAGENT_ROOT/.multiagent
  MULTIAGENT_WRITE_POLICY Repo write policy, default: $MULTIAGENT_ROOT/docs/write-policy.paths
  MULTIAGENT_PROMPT   Orchestrator prompt, default: <launcher directory>/orchestrator_prompt.md
  ORCHESTRATOR_CLI  Orchestrator CLI, default: codex
  CODEX_BIN           Codex CLI command, default: codex
  CLAUDE_BIN          Claude CLI command, default: claude
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
    --resume)
      RESUME=1
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

STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

normalize_cli() {
  case "$1" in
    codex|claude)
      printf '%s\n' "$1"
      ;;
    *)
      echo "Unsupported CLI '$1' (expected codex or claude)" >&2
      exit 2
      ;;
  esac
}

cli_bin() {
  case "$1" in
    codex) printf '%s\n' "$CODEX_BIN" ;;
    claude) printf '%s\n' "$CLAUDE_BIN" ;;
  esac
}

build_cli_command() {
  local cli="$1"
  local cwd="$2"
  local prompt_file="${3:-}"
  local bin
  bin="$(cli_bin "$cli")"
  case "$cli" in
    codex)
      if [[ -n "$prompt_file" ]]; then
        printf "%q --cd %q --dangerously-bypass-approvals-and-sandbox --no-alt-screen \"\$(cat %q)\"" "$bin" "$cwd" "$prompt_file"
      else
        printf "%q --cd %q --dangerously-bypass-approvals-and-sandbox --no-alt-screen" "$bin" "$cwd"
      fi
      ;;
    claude)
      if [[ -n "$prompt_file" ]]; then
        printf "%q --dangerously-skip-permissions \"\$(cat %q)\"" "$bin" "$prompt_file"
      else
        printf "%q --dangerously-skip-permissions" "$bin"
      fi
      ;;
  esac
}

ORCHESTRATOR_CLI="$(normalize_cli "$ORCHESTRATOR_CLI")"
require_cmd tmux
require_cmd "$(cli_bin "$ORCHESTRATOR_CLI")"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Missing orchestrator prompt: $PROMPT_FILE" >&2
  exit 1
fi

if [[ ! -x "$SCRIPT_DIR/bin/write-policy.sh" ]]; then
  echo "Missing write policy helper: $SCRIPT_DIR/bin/write-policy.sh" >&2
  exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "tmux session already exists: $SESSION" >&2
  echo "Attach with: tmux attach -t $SESSION" >&2
  exit 1
fi

export MULTIAGENT_SESSION="$SESSION"
export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_RESUME="$RESUME"
export MULTIAGENT_PROMPT="$PROMPT_FILE"
export MULTIAGENT_STATE_DIR="$STATE_DIR"
export MULTIAGENT_WRITE_POLICY="$POLICY_FILE"
export ORCHESTRATOR_CLI

mkdir -p "$STATE_DIR/subagents" "$STATE_DIR/assignments" "$STATE_DIR/worktrees"
"$SCRIPT_DIR/bin/write-policy.sh" init
if [[ "$RESUME" -eq 1 ]]; then
  RESUME_LABEL="resume"
else
  RESUME_LABEL="clean"
fi

ORCHESTRATOR_BOOTSTRAP="$(
  cat <<EOF
cd '$ROOT'
export MULTIAGENT_SESSION='$SESSION'
export MULTIAGENT_ROOT='$ROOT'
export MULTIAGENT_RESUME='$RESUME'
export MULTIAGENT_PROMPT='$PROMPT_FILE'
export MULTIAGENT_STATE_DIR='$STATE_DIR'
export MULTIAGENT_WRITE_POLICY='$POLICY_FILE'
export ORCHESTRATOR_CLI='$ORCHESTRATOR_CLI'
printf 'Multiagent launch mode: MULTIAGENT_RESUME=%s (%s)\n' '$RESUME' '$RESUME_LABEL'
$(build_cli_command "$ORCHESTRATOR_CLI" "$ROOT" "$PROMPT_FILE")
EOF
)"

tmux new-session -d -s "$SESSION" -n orchestrator "$ORCHESTRATOR_BOOTSTRAP"
tmux select-window -t "$SESSION:orchestrator"

echo "Started tmux session: $SESSION"
echo "Attach with: tmux attach -t $SESSION"
echo "Resume mode: $RESUME"
echo "Subagent state: $STATE_DIR"
echo "Write policy:"
"$SCRIPT_DIR/bin/write-policy.sh" show

if [[ "$ATTACH" -eq 1 ]]; then
  tmux attach -t "$SESSION"
fi
