#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"
CODEX_BIN="${CODEX_BIN:-codex}"

usage() {
  cat <<'USAGE'
Usage:
  bin/subagent.sh spawn NAME [--instruction TEXT]
  bin/subagent.sh list
  bin/subagent.sh poll NAME
  bin/subagent.sh inspect NAME [--lines N]
  bin/subagent.sh finalize NAME [--keep-window]
  bin/subagent.sh kill NAME

Manages named long-running subagents in tmux and persists their captured
context under $MULTIAGENT_STATE_DIR/subagents/NAME.

Subagents inherit $MULTIAGENT_WRITE_POLICY, defaulting to
$MULTIAGENT_ROOT/docs/write-policy.paths. They are expected to check planned
writes with bin/write-policy.sh before writing outside $MULTIAGENT_ROOT.
USAGE
}

die() {
  echo "subagent: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

validate_name() {
  local name="$1"
  [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid subagent name: $name"
  [[ "$name" != "orchestrator" ]] || die "reserved subagent name: $name"
}

subagent_dir() {
  printf '%s/subagents/%s\n' "$STATE_DIR" "$1"
}

status_file() {
  printf '%s/status\n' "$(subagent_dir "$1")"
}

set_status() {
  local name="$1"
  local status="$2"
  mkdir -p "$(subagent_dir "$name")"
  printf '%s\n' "$status" >"$(status_file "$name")"
}

get_status() {
  local name="$1"
  if [[ -f "$(status_file "$name")" ]]; then
    cat "$(status_file "$name")"
  else
    printf 'unknown\n'
  fi
}

window_exists() {
  local name="$1"
  command -v tmux >/dev/null 2>&1 || return 1
  tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -Fx -- "$name" >/dev/null 2>&1
}

capture_subagent() {
  local name="$1"
  local dir
  dir="$(subagent_dir "$name")"
  mkdir -p "$dir"

  local capture
  if ! capture="$(tmux capture-pane -t "$SESSION:$name" -p -S -1000 2>&1)"; then
    printf '%s\n' "$capture" >"$dir/last-error.txt"
    return 1
  fi

  printf '%s\n' "$capture" >"$dir/current.txt"
  {
    printf '\n----- capture %s -----\n' "$(timestamp)"
    printf '%s\n' "$capture"
  } >>"$dir/transcript.log"
}

infer_status() {
  local name="$1"
  local current
  current="$(subagent_dir "$name")/current.txt"
  if [[ ! -f "$current" ]]; then
    printf 'unknown\n'
    return
  fi

  if grep -Eiq '\b(blocked|need input|waiting for|cannot proceed)\b' "$current"; then
    printf 'blocked\n'
  elif grep -Eiq '\b(done|complete|completed|final status|finished)\b' "$current"; then
    printf 'done\n'
  elif window_exists "$name"; then
    printf 'running\n'
  else
    printf 'exited\n'
  fi
}

spawn_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "spawn requires NAME"
  validate_name "$name"
  shift

  local instruction=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instruction)
        instruction="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown spawn argument: $1"
        ;;
    esac
  done

  require_cmd tmux
  require_cmd "$CODEX_BIN"
  tmux has-session -t "$SESSION" 2>/dev/null || die "missing tmux session: $SESSION"
  window_exists "$name" && die "subagent window already exists: $name"

  local dir
  dir="$(subagent_dir "$name")"
  mkdir -p "$dir"
  cat >"$dir/meta.env" <<EOF
name=$name
session=$SESSION
root=$ROOT
write_policy=$POLICY_FILE
created_at=$(timestamp)
EOF
  set_status "$name" "starting"

  local command
  printf -v command "cd %q && export MULTIAGENT_SESSION=%q MULTIAGENT_ROOT=%q MULTIAGENT_STATE_DIR=%q MULTIAGENT_WRITE_POLICY=%q MULTIAGENT_SUBAGENT_NAME=%q && %q --cd %q --dangerously-bypass-approvals-and-sandbox --no-alt-screen" \
    "$ROOT" "$SESSION" "$ROOT" "$STATE_DIR" "$POLICY_FILE" "$name" "$CODEX_BIN" "$ROOT"
  tmux new-window -t "$SESSION" -n "$name" "$command"
  set_status "$name" "running"

  capture_subagent "$name" || true
  if [[ -n "$instruction" ]]; then
    tmux send-keys -t "$SESSION:$name" "$instruction" Enter
    capture_subagent "$name" || true
  fi

  printf 'spawned %s\n' "$name"
}

list_subagents() {
  local base="$STATE_DIR/subagents"
  [[ -d "$base" ]] || return 0

  local dir name status window
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"
    status="$(get_status "$name")"
    if window_exists "$name"; then
      window="open"
    else
      window="closed"
    fi
    printf '%s\t%s\t%s\n' "$name" "$status" "$window"
  done
}

poll_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "poll requires NAME"
  validate_name "$name"
  require_cmd tmux

  if capture_subagent "$name"; then
    local status
    status="$(infer_status "$name")"
    set_status "$name" "$status"
    printf '%s\t%s\n' "$name" "$status"
  else
    set_status "$name" "missing"
    die "could not capture subagent: $name"
  fi
}

inspect_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "inspect requires NAME"
  validate_name "$name"
  shift

  local lines=120
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lines)
        lines="${2:-120}"
        shift 2
        ;;
      *)
        die "unknown inspect argument: $1"
        ;;
    esac
  done

  if window_exists "$name"; then
    capture_subagent "$name" || true
  fi

  local current
  current="$(subagent_dir "$name")/current.txt"
  [[ -f "$current" ]] || die "no captured output for subagent: $name"
  tail -n "$lines" "$current"
}

finalize_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "finalize requires NAME"
  validate_name "$name"
  shift

  local keep_window=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --keep-window)
        keep_window=1
        shift
        ;;
      *)
        die "unknown finalize argument: $1"
        ;;
    esac
  done

  if window_exists "$name"; then
    capture_subagent "$name" || true
    if [[ "$keep_window" -eq 0 ]]; then
      tmux kill-window -t "$SESSION:$name"
    fi
  fi
  set_status "$name" "finalized"
  printf '%s\n' "$(timestamp)" >"$(subagent_dir "$name")/finalized_at"
  printf 'finalized %s\n' "$name"
}

kill_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "kill requires NAME"
  validate_name "$name"
  require_cmd tmux

  if window_exists "$name"; then
    capture_subagent "$name" || true
    tmux kill-window -t "$SESSION:$name"
  fi
  set_status "$name" "killed"
  printf 'killed %s\n' "$name"
}

cmd="${1:-}"
case "$cmd" in
  spawn)
    shift
    spawn_subagent "$@"
    ;;
  list)
    shift
    list_subagents "$@"
    ;;
  poll)
    shift
    poll_subagent "$@"
    ;;
  inspect)
    shift
    inspect_subagent "$@"
    ;;
  finalize)
    shift
    finalize_subagent "$@"
    ;;
  kill)
    shift
    kill_subagent "$@"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
