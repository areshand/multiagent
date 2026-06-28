#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"

die() {
  echo "status: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

subagent_dir() {
  printf '%s/subagents/%s\n' "$STATE_DIR" "$1"
}

is_subagent() {
  [[ -d "$(subagent_dir "$1")" ]]
}

assignment_dir() {
  printf '%s/assignments/%s\n' "$STATE_DIR" "$1"
}

assignment_meta_file() {
  printf '%s/assignment.env\n' "$(assignment_dir "$1")"
}

read_assignment_value() {
  local name="$1"
  local key="$2"
  local file
  file="$(assignment_meta_file "$name")"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

read_assignment_value_or_dash() {
  local name="$1"
  local key="$2"
  local value
  value="$(read_assignment_value "$name" "$key" 2>/dev/null || true)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' '-'
  fi
}

capture_window() {
  local name="$1"
  tmux capture-pane -t "$SESSION:$name" -p -S -300 2>/dev/null || true
}

classify_capture() {
  local capture="$1"

  if grep -Eiq '\b(blocked|need input|waiting for|cannot proceed)\b' <<<"$capture"; then
    printf 'blocked\n'
  elif grep -Eiq '\b(done|complete|completed|final status|finished)\b' <<<"$capture"; then
    printf 'done\n'
  elif grep -Eiq '(│|>) *$|codex.*[?]' <<<"$capture"; then
    printf 'idle\n'
  elif [[ -n "$capture" ]]; then
    printf 'busy\n'
  else
    printf 'unknown\n'
  fi
}

last_progress_line() {
  local source="$1"

  if [[ -f "$source" ]]; then
    awk 'NF { line=$0 } END { print line }' "$source"
  else
    awk 'NF { line=$0 } END { print line }' <<<"$source"
  fi
}

read_status_file() {
  local name="$1"
  local file
  file="$(subagent_dir "$name")/status"
  if [[ -f "$file" ]]; then
    tr -d '\n' <"$file"
  else
    printf 'unknown'
  fi
}

health_value() {
  local name="$1"
  local key="$2"
  "$ROOT/bin/subagent.sh" health-check "$name" 2>/dev/null | awk -F'\t' -v key="$key" '
    $1 == key {
      if (key == "health") {
        print $3
      } else {
        print $2
      }
      found=1
      exit
    }
    END { exit found ? 0 : 1 }
  '
}

health_value_or_dash() {
  local name="$1"
  local key="$2"
  local value
  value="$(health_value "$name" "$key" 2>/dev/null || true)"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
  else
    printf '%s' '-'
  fi
}

list_window_names() {
  tmux list-windows -t "$SESSION" -F '#W'
}

print_row() {
  local type="$1"
  local name="$2"
  local status="$3"
  local window="$4"
  local progress="$5"
  local state="$6"
  local role="$7"
  local decision_id="$8"
  local plan_id="$9"
  local workflow_id="${10}"
  local node_id="${11}"
  local health="${12}"
  local action="${13}"
  local reason="${14}"

  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$type" "$name" "$status" "$window" "$progress" "$state" "$role" "$decision_id" "$plan_id" "$workflow_id" "$node_id" "$health" "$action" "$reason"
}

main() {
  require_cmd tmux
  tmux has-session -t "$SESSION" 2>/dev/null || die "missing tmux session: $SESSION"

  local windows
  windows="$(list_window_names)"

  printf 'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID\tWORKFLOW_ID\tNODE_ID\tHEALTH\tACTION\tREASON\n'

  local name status progress state role decision_id plan_id workflow_id node_id health action reason
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    [[ "$name" != "orchestrator" ]] || continue
    is_subagent "$name" && continue

    local capture
    capture="$(capture_window "$name")"
    status="$(classify_capture "$capture")"
    progress="$(last_progress_line "$capture")"

    # Try to read assignment metadata for workers, use "-" if not available
    role="$(read_assignment_value_or_dash "$name" role)"
    decision_id="$(read_assignment_value_or_dash "$name" decision_id)"
    plan_id="$(read_assignment_value_or_dash "$name" plan_id)"
    workflow_id="$(read_assignment_value_or_dash "$name" workflow_id)"
    node_id="$(read_assignment_value_or_dash "$name" node_id)"
    health="$(health_value_or_dash "$name" health)"
    action="$(health_value_or_dash "$name" action)"
    reason="$(health_value_or_dash "$name" reason)"

    print_row "worker" "$name" "$status" "open" "$progress" "-" "$role" "$decision_id" "$plan_id" "$workflow_id" "$node_id" "$health" "$action" "$reason"
  done <<<"$windows"

  local base="$STATE_DIR/subagents"
  [[ -d "$base" ]] || return 0

  local dir window
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"
    state="$(subagent_dir "$name")"

    if grep -Fx -- "$name" <<<"$windows" >/dev/null 2>&1; then
      window="open"
      "$ROOT/bin/subagent.sh" poll "$name" >/dev/null || true
    else
      window="closed"
    fi

    status="$(read_status_file "$name")"
    progress="$(last_progress_line "$state/current.txt")"

    # Try to read assignment metadata for subagents, use "-" if not available
    role="$(read_assignment_value_or_dash "$name" role)"
    decision_id="$(read_assignment_value_or_dash "$name" decision_id)"
    plan_id="$(read_assignment_value_or_dash "$name" plan_id)"
    workflow_id="$(read_assignment_value_or_dash "$name" workflow_id)"
    node_id="$(read_assignment_value_or_dash "$name" node_id)"
    health="$(health_value_or_dash "$name" health)"
    action="$(health_value_or_dash "$name" action)"
    reason="$(health_value_or_dash "$name" reason)"

    print_row "subagent" "$name" "$status" "$window" "$progress" "$state" "$role" "$decision_id" "$plan_id" "$workflow_id" "$node_id" "$health" "$action" "$reason"
  done
}

main "$@"
