#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"

usage() {
  cat <<'USAGE'
Usage:
  bin/harness.sh evaluate ACTION_FILE
  bin/harness.sh dispatch ACTION_FILE
  bin/harness.sh assess NAME

Evaluates a structured harness action before executing it.

Action files are simple key=value manifests. Supported action types:

  type=assignment_check
  name=worker-01-docs

  type=assess_agent
  name=worker-01-docs

  type=kill_agent
  name=worker-01-docs

  type=finalize_agent
  name=worker-01-docs

  type=approve_write
  path=/tmp/report
  actor=orchestrator
  assignment_id=docs-001
  reason=user approved report export
  approved=false

Policy decisions:
  ALLOW             dispatch may execute
  DENY              action must not execute
  REQUIRE_APPROVAL  action needs explicit user approval first
  TERMINATE         capture and terminate the agent/session path
USAGE
}

die() {
  echo "harness: $*" >&2
  exit 1
}

read_action_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

value_or_empty() {
  local file="$1"
  local key="$2"
  read_action_value "$file" "$key" 2>/dev/null || true
}

validate_action_file() {
  local file="$1"
  [[ -f "$file" ]] || die "missing action file: $file"
  if grep -Ev '^([A-Za-z_][A-Za-z0-9_]*=.*|[[:space:]]*#.*|[[:space:]]*)$' "$file" >/dev/null; then
    die "invalid action file syntax: $file"
  fi
}

validate_name() {
  local name="$1"
  [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid agent name: $name"
  [[ "$name" != "orchestrator" ]] || die "orchestrator is not an agent target"
}

validate_observed_name() {
  local name="$1"
  [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid observed name: $name"
}

decision() {
  local value="$1"
  local reason="$2"
  local executor="${3:-}"
  printf 'decision\t%s\n' "$value"
  printf 'reason\t%s\n' "$reason"
  if [[ -n "$executor" ]]; then
    printf 'executor\t%s\n' "$executor"
  fi
}

window_exists() {
  local name="$1"
  command -v tmux >/dev/null 2>&1 || return 1
  tmux list-windows -t "${MULTIAGENT_SESSION:-multiagent}" -F '#W' 2>/dev/null | grep -Fx -- "$name" >/dev/null 2>&1
}

capture_observed_agent() {
  local name="$1"
  if window_exists "$name"; then
    tmux capture-pane -t "${MULTIAGENT_SESSION:-multiagent}:$name" -p -S -300 2>/dev/null || true
  elif [[ -f "$STATE_DIR/subagents/$name/current.txt" ]]; then
    cat "$STATE_DIR/subagents/$name/current.txt"
  else
    true
  fi
}

classify_capture() {
  local name="$1"
  local capture="$2"
  if grep -Eiq '(rm[[:space:]]+-rf[[:space:]]+/|git[[:space:]]+push|gh[[:space:]]+pr[[:space:]]+create|submit PR|open PR|send external|exfiltrat|secret|credential|delete production|production database)' <<<"$capture"; then
    printf 'kill\n'
  elif grep -Eiq '\b(blocked|need input|waiting for|cannot proceed)\b' <<<"$capture"; then
    printf 'block\n'
  elif grep -Eiq '\b(done|complete|completed|final status|finished)\b' <<<"$capture"; then
    printf 'verify\n'
  elif [[ "$name" == "orchestrator" && -n "$capture" ]]; then
    printf 'inspect\n'
  elif window_exists "$name"; then
    printf 'continue\n'
  elif [[ -n "$capture" ]]; then
    printf 'inspect\n'
  else
    printf 'unknown\n'
  fi
}

assess_agent() {
  local name="$1"
  validate_observed_name "$name"

  local capture recommendation reason check_output
  capture="$(capture_observed_agent "$name")"
  recommendation="$(classify_capture "$name" "$capture")"
  reason="capture-classification"

  if [[ "$name" != "orchestrator" && -f "$STATE_DIR/assignments/$name/assignment.env" ]]; then
    if ! check_output="$(MULTIAGENT_ROOT="$ROOT" MULTIAGENT_STATE_DIR="$STATE_DIR" "$TOOL_ROOT/bin/subagent.sh" assignment-check "$name" 2>&1)"; then
      recommendation="kill"
      reason="$(awk -F'\t' '/^reject\t/ { print $2; exit }' <<<"$check_output")"
      [[ -n "$reason" ]] || reason="assignment-check-failed"
    elif [[ "$recommendation" == "continue" ]]; then
      reason="assignment-check-accepted"
    fi
  elif [[ "$name" != "orchestrator" && "$recommendation" == "continue" ]]; then
    recommendation="inspect"
    reason="missing-assignment-metadata"
  fi

  printf 'assessment\t%s\t%s\n' "$name" "$recommendation"
  printf 'reason\t%s\n' "$reason"
}

evaluate_action() {
  local file="${1:-}"
  [[ -n "$file" ]] || die "evaluate requires ACTION_FILE"
  validate_action_file "$file"

  local type name path actor assignment_id reason approved
  type="$(value_or_empty "$file" type)"
  name="$(value_or_empty "$file" name)"
  path="$(value_or_empty "$file" path)"
  actor="$(value_or_empty "$file" actor)"
  assignment_id="$(value_or_empty "$file" assignment_id)"
  reason="$(value_or_empty "$file" reason)"
  approved="$(value_or_empty "$file" approved)"

  case "$type" in
    assess_agent)
      [[ -n "$name" ]] || die "assess_agent requires name"
      validate_observed_name "$name"
      decision "ALLOW" "external observation action" "bin/harness.sh assess $name"
      ;;
    assignment_check)
      [[ -n "$name" ]] || die "assignment_check requires name"
      validate_name "$name"
      decision "ALLOW" "acceptance check action" "bin/subagent.sh assignment-check $name"
      ;;
    kill_agent)
      [[ -n "$name" ]] || die "kill_agent requires name"
      validate_name "$name"
      decision "ALLOW" "harness lifecycle termination" "bin/subagent.sh kill $name"
      ;;
    finalize_agent)
      [[ -n "$name" ]] || die "finalize_agent requires name"
      validate_name "$name"
      decision "ALLOW" "harness lifecycle finalization" "bin/subagent.sh finalize $name"
      ;;
    approve_write)
      [[ -n "$path" ]] || die "approve_write requires path"
      [[ -n "$actor" ]] || die "approve_write requires actor"
      [[ -n "$assignment_id" ]] || die "approve_write requires assignment_id"
      [[ -n "$reason" ]] || die "approve_write requires reason"
      if [[ "$approved" == "true" ]]; then
        decision "ALLOW" "approval recorded in action" "bin/write-policy.sh approve $path --actor $actor --assignment-id $assignment_id --reason $reason"
      else
        decision "REQUIRE_APPROVAL" "outside write approval must come from user"
      fi
      ;;
    send_instruction)
      decision "DENY" "direct instruction dispatch is not implemented in this harness entrypoint"
      ;;
    terminate_session)
      decision "TERMINATE" "session termination requested"
      ;;
    "")
      die "action file requires type"
      ;;
    *)
      decision "DENY" "unsupported action type: $type"
      ;;
  esac
}

decision_value() {
  awk -F'\t' '$1 == "decision" { print $2; found=1; exit } END { exit found ? 0 : 1 }'
}

dispatch_action() {
  local file="${1:-}"
  [[ -n "$file" ]] || die "dispatch requires ACTION_FILE"

  local evaluation result type name path actor assignment_id reason force_args=()
  evaluation="$(evaluate_action "$file")"
  printf '%s\n' "$evaluation"
  result="$(decision_value <<<"$evaluation")"

  case "$result" in
    ALLOW)
      type="$(value_or_empty "$file" type)"
      name="$(value_or_empty "$file" name)"
      path="$(value_or_empty "$file" path)"
      actor="$(value_or_empty "$file" actor)"
      assignment_id="$(value_or_empty "$file" assignment_id)"
      reason="$(value_or_empty "$file" reason)"
      case "$type" in
        assess_agent)
          assess_agent "$name"
          ;;
        assignment_check)
          "$TOOL_ROOT/bin/subagent.sh" assignment-check "$name"
          ;;
        kill_agent)
          "$TOOL_ROOT/bin/subagent.sh" kill "$name"
          ;;
        finalize_agent)
          "$TOOL_ROOT/bin/subagent.sh" finalize "$name"
          ;;
        approve_write)
          if [[ "$(value_or_empty "$file" force)" == "true" ]]; then
            force_args=(--force)
          fi
          "$TOOL_ROOT/bin/write-policy.sh" approve "$path" --actor "$actor" --assignment-id "$assignment_id" --reason "$reason" "${force_args[@]}"
          ;;
      esac
      ;;
    DENY)
      return 10
      ;;
    REQUIRE_APPROVAL)
      return 20
      ;;
    TERMINATE)
      return 30
      ;;
    *)
      die "unknown policy decision: $result"
      ;;
  esac
}

cmd="${1:-}"
case "$cmd" in
  evaluate)
    shift
    evaluate_action "$@"
    ;;
  dispatch)
    shift
    dispatch_action "$@"
    ;;
  assess)
    shift
    assess_agent "$@"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
