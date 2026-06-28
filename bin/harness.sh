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

Evaluates a structured harness action before executing it.

Action files are simple key=value manifests. Supported action types:

  type=assignment_check
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
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
