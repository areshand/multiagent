#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

usage() {
  cat <<'USAGE'
Usage:
  bin/orchestrator.sh complete

Runs the normal-path completion gates for the active orchestrated workflow.
USAGE
}

complete_run() {
  if [[ "${MULTIAGENT_LIFECYCLE_ENFORCEMENT:-0}" == "1" ]]; then
    local workflow_id="${MULTIAGENT_WORKFLOW_ID:-}"
    [[ -n "$workflow_id" ]] || {
      echo "orchestrator: lifecycle enforcement requires MULTIAGENT_WORKFLOW_ID" >&2
      exit 1
    }
    MULTIAGENT_STATE_DIR="$STATE_DIR" "$SCRIPT_DIR/workflow.sh" completion-check "$workflow_id" >/dev/null
    local phase
    phase="$(MULTIAGENT_STATE_DIR="$STATE_DIR" "$SCRIPT_DIR/workflow.sh" value "$workflow_id" phase)"
    if [[ "$phase" != "complete" ]]; then
      echo "orchestrator: workflow must transition to complete before run completion (current: $phase)" >&2
      exit 1
    fi
  fi

  MULTIAGENT_ROOT="$ROOT" MULTIAGENT_STATE_DIR="$STATE_DIR" "$SCRIPT_DIR/subagent.sh" gate-check >/dev/null
  printf 'run completed\t%s\n' "${MULTIAGENT_RUN_ID:-${MULTIAGENT_WORKFLOW_ID:-unknown}}"
}

case "${1:-}" in
  complete)
    shift
    [[ $# -eq 0 ]] || { usage >&2; exit 2; }
    complete_run
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    echo "orchestrator: unknown command: $1" >&2
    usage >&2
    exit 2
    ;;
esac
