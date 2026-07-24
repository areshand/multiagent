#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
LOG_DIR="${MULTIAGENT_LOG_DIR:-$STATE_DIR/logs}"
INTERVAL="${MULTIAGENT_WATCH_INTERVAL:-5}"
LOG_LINES="${MULTIAGENT_WATCH_LOG_LINES:-40}"
ONCE=0

usage() {
  cat <<'USAGE'
Usage: bin/watch.sh [--once] [--interval SECONDS] [--log-lines N]

Renders a compact multiagent dashboard for Codex desktop. It combines
assignment/subagent status, workflow DAG summaries, blocked nodes, and the
orchestrator pane log written by launch.sh.

Environment:
  MULTIAGENT_SESSION           tmux session name, default: multiagent
  MULTIAGENT_ROOT              project root, default: current directory
  MULTIAGENT_STATE_DIR         state root, default: $MULTIAGENT_ROOT/.multiagent
  MULTIAGENT_LOG_DIR           pane log directory, default: $STATE_DIR/logs
  MULTIAGENT_WATCH_INTERVAL    refresh interval, default: 5
  MULTIAGENT_WATCH_LOG_LINES   orchestrator tail lines, default: 40
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)
      ONCE=1
      shift
      ;;
    --interval)
      INTERVAL="${2:-}"
      shift 2
      ;;
    --log-lines)
      LOG_LINES="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "watch: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "watch: --interval must be a positive integer" >&2
  exit 2
fi
if ! [[ "$LOG_LINES" =~ ^[0-9]+$ ]]; then
  echo "watch: --log-lines must be a non-negative integer" >&2
  exit 2
fi

status_text() {
  if "$ROOT/bin/status.sh" 2>/dev/null; then
    return 0
  fi
  printf 'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID\tWORKFLOW_ID\tNODE_ID\n'
}

render_status_summary() {
  awk -F'\t' '
    NR == 1 { next }
    $3 != "" { count[$3]++; seen=1 }
    END {
      if (!seen) {
        print "none\t0"
        next
      }
      for (status in count) {
        print status "\t" count[status]
      }
    }
  ' | sort
}

render_workers() {
  awk -F'\t' '
    NR == 1 { next }
    {
      seen=1
      progress=$5
      if (length(progress) > 90) {
        progress=substr(progress, 1, 87) "..."
      }
      printf "%-9s %-28s %-10s %-7s %s\n", $1, $2, $3, $4, progress
    }
    END {
      if (!seen) {
        print "none"
      }
    }
  '
}

render_blocked_agents() {
  awk -F'\t' '
    NR == 1 { next }
    tolower($3) ~ /blocked|delivery-blocked/ {
      seen=1
      progress=$5
      if (length(progress) > 110) {
        progress=substr(progress, 1, 107) "..."
      }
      printf "%-28s %-16s %s\n", $2, $3, progress
    }
    END {
      if (!seen) {
        print "none"
      }
    }
  '
}

render_dag_summary() {
  local base="$STATE_DIR/workflows"
  if [[ ! -d "$base" ]]; then
    printf 'No workflows found.\n'
    return
  fi

  local any=0 dir workflow nodes
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    workflow="$(basename "$dir")"
    nodes="$dir/nodes.tsv"
    [[ -f "$nodes" ]] || continue
    any=1
    printf '%s\n' "$workflow"
    awk -F'\t' '
      NR == 1 { next }
      $7 != "" { count[$7]++ }
      END {
        for (status in count) {
          printf "  %-8s %s\n", status, count[status]
        }
      }
    ' "$nodes" | sort
  done
  [[ "$any" -eq 1 ]] || printf 'No workflows found.\n'
}

render_blocked_dag_nodes() {
  local base="$STATE_DIR/workflows"
  if [[ ! -d "$base" ]]; then
    printf 'none\n'
    return
  fi

  local any=0 dir workflow nodes
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    workflow="$(basename "$dir")"
    nodes="$dir/nodes.tsv"
    [[ -f "$nodes" ]] || continue
    while IFS=$'\t' read -r node_id agent assignment_id role branch owned_paths status decision_id plan_id added_at; do
      [[ "$node_id" != "node_id" ]] || continue
      if [[ "$status" == "blocked" || "$status" == "failed" ]]; then
        any=1
        printf '%s\t%s\t%s\t%s\n' "$workflow" "$node_id" "$status" "$agent"
      fi
    done <"$nodes"
  done
  [[ "$any" -eq 1 ]] || printf 'none\n'
}

render_once() {
  local now status_snapshot orchestrator_log
  now="$(date '+%Y-%m-%d %H:%M:%S')"
  status_snapshot="$(status_text)"
  orchestrator_log="$LOG_DIR/orchestrator.log"

  printf 'Multiagent Dashboard\n'
  printf 'Session: %s  Root: %s\n' "$SESSION" "$ROOT"
  printf 'State: %s\n' "$STATE_DIR"
  printf 'Logs: %s\n' "$LOG_DIR"
  printf 'Updated: %s\n\n' "$now"

  printf 'Agent Status Summary\n'
  printf '%s\n' "$status_snapshot" | render_status_summary

  printf '\nAgents\n'
  printf '%s\n' "$status_snapshot" | render_workers

  printf '\nBlocked Agents\n'
  printf '%s\n' "$status_snapshot" | render_blocked_agents

  printf '\nDAG Summary\n'
  render_dag_summary

  printf '\nBlocked DAG Nodes\n'
  render_blocked_dag_nodes

  printf '\nOrchestrator Tail\n'
  if [[ -f "$orchestrator_log" && "$LOG_LINES" -gt 0 ]]; then
    tail -n "$LOG_LINES" "$orchestrator_log"
  elif [[ "$LOG_LINES" -eq 0 ]]; then
    printf '(disabled)\n'
  else
    printf 'No orchestrator log yet. Start with ./launch.sh or pipe the pane manually with tmux pipe-pane.\n'
  fi
}

while true; do
  if [[ "$ONCE" -eq 0 ]]; then
    printf '\033[H\033[2J'
  fi
  render_once
  [[ "$ONCE" -eq 0 ]] || break
  sleep "$INTERVAL"
done
