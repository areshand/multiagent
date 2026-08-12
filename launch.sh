#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_ROOT="$SCRIPT_DIR"
ROOT="${MULTIAGENT_ROOT:-$DEFAULT_ROOT}"
PROMPT_FILE="${MULTIAGENT_PROMPT:-$SCRIPT_DIR/orchestrator_prompt.md}"
LIFECYCLE_PROMPT="${MULTIAGENT_LIFECYCLE_PROMPT:-$SCRIPT_DIR/prompts/playbooks/implementation-lifecycle.md}"
PROMPT_MODULE_ROOT="${MULTIAGENT_PROMPT_MODULE_ROOT:-$SCRIPT_DIR}"
CODEX_BIN="${CODEX_BIN:-codex}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
ORCHESTRATOR_CLI="${ORCHESTRATOR_CLI:-codex}"
WORKER_CLI="${WORKER_CLI:-claude}"
SUBAGENT_CLI="${SUBAGENT_CLI:-$WORKER_CLI}"
VERIFIER_CLI="${VERIFIER_CLI:-codex}"
VERIFIER_MAX_ITERATIONS="${MULTIAGENT_VERIFIER_MAX_ITERATIONS:-3}"
MULTIAGENT_RUN_ID="${MULTIAGENT_RUN_ID:-run_$(date -u +%Y%m%dT%H%M%SZ)_$$}"
MULTIAGENT_WORKFLOW_ID="${MULTIAGENT_WORKFLOW_ID:-}"
MULTIAGENT_LIFECYCLE_ENFORCEMENT="${MULTIAGENT_LIFECYCLE_ENFORCEMENT:-1}"
ATTACH=1
RESUME=0

usage() {
  cat <<'USAGE'
Usage: ./launch.sh [--session NAME] [--root DIR] [--resume] [--attach|--no-attach]

Starts a tmux multi-agent session with one window:
  - orchestrator: Codex commander that spawns and manages workers

Requirements:
  - tmux
  - Python 3.8 or newer (standard library only)
  - the selected orchestrator CLI (Codex or Claude)

By default the orchestrator starts clean and does not inspect recovery state.
Pass --resume to allow the orchestrator to inspect recovery state and consider
restoring/resuming persisted subagents.

Environment:
  MULTIAGENT_SESSION  Default tmux session name
  MULTIAGENT_ROOT     Default project root, default: launcher directory
  MULTIAGENT_RESUME   Launch mode exported by this script: 0 clean, 1 resume
  MULTIAGENT_STATE_DIR Persisted subagent state, default: $MULTIAGENT_ROOT/.multiagent
  MULTIAGENT_LOG_DIR   tmux pane logs, default: $MULTIAGENT_STATE_DIR/logs
  MULTIAGENT_WRITE_POLICY Repo write policy, default: $MULTIAGENT_ROOT/docs/write-policy.paths
  MULTIAGENT_VERIFIER_MAX_ITERATIONS Verifier escalation threshold, default: 3
  MULTIAGENT_PROMPT   Orchestrator prompt, default: <launcher directory>/orchestrator_prompt.md
  MULTIAGENT_LIFECYCLE_PROMPT Mandatory lifecycle prompt, default: <launcher directory>/prompts/playbooks/implementation-lifecycle.md
  MULTIAGENT_WORKFLOW_ID Durable lifecycle workflow ID, default: current run ID
  MULTIAGENT_LIFECYCLE_ENFORCEMENT Gate normal implementation spawn/completion paths, default: 1
  MULTIAGENT_PROMPT_MODULE_ROOT Directory containing prompts/, default: launcher directory
  MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER Require accepted verifier evidence for the exact source diff, default: 1
  ORCHESTRATOR_CLI  Orchestrator CLI, default: codex
  WORKER_CLI        Worker CLI, default: claude
  SUBAGENT_CLI      Named subagent CLI, default: $WORKER_CLI
  VERIFIER_CLI      Verifier CLI, default: codex
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
LOG_DIR="${MULTIAGENT_LOG_DIR:-$STATE_DIR/logs}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"
ACTIVE_WORKFLOW_FILE="$STATE_DIR/runtime_state/active-workflow-id"
if [[ "$RESUME" -eq 1 && -z "$MULTIAGENT_WORKFLOW_ID" && -f "$ACTIVE_WORKFLOW_FILE" ]]; then
  MULTIAGENT_WORKFLOW_ID="$(tr -d '\r\n' <"$ACTIVE_WORKFLOW_FILE")"
fi
[[ -n "$MULTIAGENT_WORKFLOW_ID" ]] || MULTIAGENT_WORKFLOW_ID="$MULTIAGENT_RUN_ID"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

require_python_runtime() {
  require_cmd python3
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
    echo "Python 3.8 or newer is required (found: $(python3 --version 2>&1))" >&2
    exit 1
  fi
}

pipe_log() {
  local window="$1"
  local log_file="$LOG_DIR/$window.log"
  mkdir -p "$LOG_DIR"
  touch "$log_file"
  tmux pipe-pane -o -t "$SESSION:$window" "cat >> $(printf '%q' "$log_file")"
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
      if [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" ]]; then
        if [[ -n "$prompt_file" ]]; then
          printf "%q exec --cd %q --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --output-last-message %q - < %q; rc=\$?; printf '\\n[multiagent codex exec exited rc=%%s]\\n' \$rc; sleep infinity" "$bin" "$cwd" "$STATE_DIR/orchestrator-last-message.txt" "$prompt_file"
        else
          printf "%q exec --cd %q --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox; rc=\$?; printf '\\n[multiagent codex exec exited rc=%%s]\\n' \$rc; sleep infinity" "$bin" "$cwd"
        fi
        return
      fi
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
WORKER_CLI="$(normalize_cli "$WORKER_CLI")"
SUBAGENT_CLI="$(normalize_cli "$SUBAGENT_CLI")"
VERIFIER_CLI="$(normalize_cli "$VERIFIER_CLI")"
if ! [[ "$VERIFIER_MAX_ITERATIONS" =~ ^[1-9][0-9]*$ ]]; then
  echo "MULTIAGENT_VERIFIER_MAX_ITERATIONS must be a positive integer" >&2
  exit 2
fi
require_python_runtime
require_cmd tmux
require_cmd "$(cli_bin "$ORCHESTRATOR_CLI")"

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "Missing orchestrator prompt: $PROMPT_FILE" >&2
  exit 1
fi

if [[ ! -f "$LIFECYCLE_PROMPT" ]]; then
  echo "Missing implementation lifecycle prompt: $LIFECYCLE_PROMPT" >&2
  exit 1
fi

case "$MULTIAGENT_LIFECYCLE_ENFORCEMENT" in
  0|1) ;;
  *)
    echo "MULTIAGENT_LIFECYCLE_ENFORCEMENT must be 0 or 1" >&2
    exit 2
    ;;
esac

for helper in "$SCRIPT_DIR/bin/prompt-bundle.sh" "$SCRIPT_DIR/bin/workflow.sh"; do
  if [[ ! -x "$helper" ]]; then
    echo "Missing lifecycle helper: $helper" >&2
    exit 1
  fi
done

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
export MULTIAGENT_LIFECYCLE_PROMPT="$LIFECYCLE_PROMPT"
export MULTIAGENT_PROMPT_MODULE_ROOT="$PROMPT_MODULE_ROOT"
export MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER="${MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER:-1}"
export MULTIAGENT_STATE_DIR="$STATE_DIR"
export MULTIAGENT_LOG_DIR="$LOG_DIR"
export MULTIAGENT_WRITE_POLICY="$POLICY_FILE"
export MULTIAGENT_VERIFIER_MAX_ITERATIONS="$VERIFIER_MAX_ITERATIONS"
export MULTIAGENT_RUN_ID
export MULTIAGENT_WORKFLOW_ID
export MULTIAGENT_LIFECYCLE_ENFORCEMENT
export ORCHESTRATOR_CLI
export WORKER_CLI
export SUBAGENT_CLI
export VERIFIER_CLI
export CODEX_BIN
export CLAUDE_BIN
export MULTIAGENT_CODEX_EXEC="${MULTIAGENT_CODEX_EXEC:-0}"
export MULTIAGENT_EXTRA_PATH="${MULTIAGENT_EXTRA_PATH:-}"
export PATH

mkdir -p "$STATE_DIR/subagents" "$STATE_DIR/assignments" "$STATE_DIR/worktrees" "$STATE_DIR/runtime_state" "$LOG_DIR"
"$SCRIPT_DIR/bin/write-policy.sh" init
PROMPT_BUNDLE="$STATE_DIR/runtime_state/orchestrator-prompt-bundle.md"
"$SCRIPT_DIR/bin/prompt-bundle.sh" \
  --orchestrator "$PROMPT_FILE" \
  --lifecycle "$LIFECYCLE_PROMPT" \
  --output "$PROMPT_BUNDLE" >/dev/null
python3 - "$PROMPT_FILE" "$LIFECYCLE_PROMPT" "$PROMPT_BUNDLE" >"$STATE_DIR/runtime_state/prompt-sha256.tsv" <<'PY'
import hashlib
import sys
from pathlib import Path

for value in sys.argv[1:]:
    path = Path(value)
    print(f"{hashlib.sha256(path.read_bytes()).hexdigest()}\t{path}")
PY
"$SCRIPT_DIR/bin/workflow.sh" init-or-resume "$MULTIAGENT_WORKFLOW_ID" --resume "$RESUME" >/dev/null
printf '%s\n' "$MULTIAGENT_WORKFLOW_ID" >"$ACTIVE_WORKFLOW_FILE"
export MULTIAGENT_PROMPT="$PROMPT_BUNDLE"
if [[ "$RESUME" -eq 1 ]]; then
  RESUME_LABEL="resume"
else
  RESUME_LABEL="clean"
fi

ORCHESTRATOR_BOOTSTRAP_SCRIPT="$STATE_DIR/orchestrator-bootstrap.sh"
{
  printf '#!/usr/bin/env bash\n'
  printf 'cd %q\n' "$ROOT"
  printf 'export MULTIAGENT_SESSION=%q\n' "$SESSION"
  printf 'export MULTIAGENT_ROOT=%q\n' "$ROOT"
  printf 'export MULTIAGENT_RESUME=%q\n' "$RESUME"
  printf 'export MULTIAGENT_PROMPT=%q\n' "$PROMPT_BUNDLE"
  printf 'export MULTIAGENT_LIFECYCLE_PROMPT=%q\n' "$LIFECYCLE_PROMPT"
  printf 'export MULTIAGENT_PROMPT_MODULE_ROOT=%q\n' "$PROMPT_MODULE_ROOT"
  printf 'export MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=%q\n' "${MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER:-1}"
  printf 'export MULTIAGENT_STATE_DIR=%q\n' "$STATE_DIR"
  printf 'export MULTIAGENT_LOG_DIR=%q\n' "$LOG_DIR"
  printf 'export MULTIAGENT_WRITE_POLICY=%q\n' "$POLICY_FILE"
  printf 'export MULTIAGENT_VERIFIER_MAX_ITERATIONS=%q\n' "$VERIFIER_MAX_ITERATIONS"
  printf 'export MULTIAGENT_RUN_ID=%q\n' "$MULTIAGENT_RUN_ID"
  printf 'export MULTIAGENT_WORKFLOW_ID=%q\n' "$MULTIAGENT_WORKFLOW_ID"
  printf 'export MULTIAGENT_LIFECYCLE_ENFORCEMENT=%q\n' "$MULTIAGENT_LIFECYCLE_ENFORCEMENT"
  printf 'export ORCHESTRATOR_CLI=%q\n' "$ORCHESTRATOR_CLI"
  printf 'export WORKER_CLI=%q\n' "$WORKER_CLI"
  printf 'export SUBAGENT_CLI=%q\n' "$SUBAGENT_CLI"
  printf 'export VERIFIER_CLI=%q\n' "$VERIFIER_CLI"
  printf 'export CODEX_BIN=%q\n' "$CODEX_BIN"
  printf 'export CLAUDE_BIN=%q\n' "$CLAUDE_BIN"
  printf 'export MULTIAGENT_CODEX_EXEC=%q\n' "$MULTIAGENT_CODEX_EXEC"
  printf 'export MULTIAGENT_EXTRA_PATH=%q\n' "$MULTIAGENT_EXTRA_PATH"
  printf 'export PATH=%q\n' "$PATH"
  printf 'printf %q %q %q\n' 'Multiagent launch mode: MULTIAGENT_RESUME=%s (%s)\n' "$RESUME" "$RESUME_LABEL"
  build_cli_command "$ORCHESTRATOR_CLI" "$ROOT" "$PROMPT_BUNDLE"
  printf '\n'
} > "$ORCHESTRATOR_BOOTSTRAP_SCRIPT"
chmod 700 "$ORCHESTRATOR_BOOTSTRAP_SCRIPT"

tmux new-session -d -s "$SESSION" -n orchestrator "bash $(printf '%q' "$ORCHESTRATOR_BOOTSTRAP_SCRIPT")"
tmux select-window -t "$SESSION:orchestrator"
pipe_log orchestrator

echo "Started tmux session: $SESSION"
echo "Attach with: tmux attach -t $SESSION"
echo "Resume mode: $RESUME"
echo "Workflow ID: $MULTIAGENT_WORKFLOW_ID"
echo "Lifecycle enforcement: $MULTIAGENT_LIFECYCLE_ENFORCEMENT"
echo "Prompt bundle: $PROMPT_BUNDLE"
echo "Subagent state: $STATE_DIR"
echo "Logs: $LOG_DIR"
echo "Dashboard: MULTIAGENT_SESSION=$(printf '%q' "$SESSION") MULTIAGENT_ROOT=$(printf '%q' "$ROOT") $SCRIPT_DIR/bin/watch.sh"
echo "Verifier max iterations: $VERIFIER_MAX_ITERATIONS"
echo "Worker CLI: $WORKER_CLI"
echo "Subagent CLI: $SUBAGENT_CLI"
echo "Verifier CLI: $VERIFIER_CLI"
echo "Write policy:"
"$SCRIPT_DIR/bin/write-policy.sh" show

if [[ "$ATTACH" -eq 1 ]]; then
  tmux attach -t "$SESSION"
fi
