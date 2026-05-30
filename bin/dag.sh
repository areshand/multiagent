#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"

usage() {
  cat <<'USAGE'
Usage:
  bin/dag.sh init WORKFLOW_ID --title TEXT [--owner NAME]
  bin/dag.sh add-node WORKFLOW_ID NODE_ID --agent NAME --assignment-id ID --role ROLE --branch BRANCH --owned PATH[,PATH...] [--depends-on NODE[,NODE...]] [--status STATUS] [--decision-id ID] [--plan-id ID]
  bin/dag.sh status WORKFLOW_ID NODE_ID STATUS [--reason TEXT]
  bin/dag.sh ready WORKFLOW_ID
  bin/dag.sh blocked WORKFLOW_ID
  bin/dag.sh show WORKFLOW_ID
  bin/dag.sh list

Manages durable workflow DAGs for orchestrator-generated task coordination.

Workflow records are stored under $MULTIAGENT_STATE_DIR/workflows/WORKFLOW_ID with:
  workflow.env      - Workflow metadata (title, owner, status, timestamps)
  nodes.tsv         - Node definitions with their properties
  edges.tsv         - Dependency relationships between nodes
  events.log        - Timestamped events in the workflow lifecycle

Node statuses: pending, ready, running, blocked, done, failed, skipped
Dependencies are satisfied only when upstream nodes are done (unless skipped).

Node lifecycle:
  1. pending    - Initial state when node is added
  2. ready      - All dependencies satisfied, ready to run
  3. running    - Currently being executed
  4. done       - Successfully completed
  5. failed     - Execution failed
  6. blocked    - Cannot run due to failed dependencies
  7. skipped    - Skipped due to conditions

Commands compute which nodes are ready based on dependency status and detect:
- Duplicate workflow IDs
- Duplicate node IDs within a workflow
- Missing dependencies
- Invalid statuses
- Dependency cycles
USAGE
}

die() {
  echo "dag: $*" >&2
  exit 1
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

validate_workflow_id() {
  local workflow_id="$1"
  [[ "$workflow_id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid workflow ID: $workflow_id"
}

validate_node_id() {
  local node_id="$1"
  [[ "$node_id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid node ID: $node_id"
}

validate_status() {
  local status="$1"
  case "$status" in
    pending|ready|running|blocked|done|failed|skipped)
      ;;
    *)
      die "invalid status: $status (expected pending|ready|running|blocked|done|failed|skipped)"
      ;;
  esac
}

reject_newline() {
  local label="$1"
  local value="$2"
  [[ "$value" != *$'\n'* ]] || die "$label may not contain newlines"
}

workflow_dir() {
  printf '%s/workflows/%s\n' "$STATE_DIR" "$1"
}

workflow_meta_file() {
  printf '%s/workflow.env\n' "$(workflow_dir "$1")"
}

nodes_file() {
  printf '%s/nodes.tsv\n' "$(workflow_dir "$1")"
}

edges_file() {
  printf '%s/edges.tsv\n' "$(workflow_dir "$1")"
}

events_file() {
  printf '%s/events.log\n' "$(workflow_dir "$1")"
}

workflow_exists() {
  local workflow_id="$1"
  [[ -f "$(workflow_meta_file "$workflow_id")" ]]
}

log_event() {
  local workflow_id="$1"
  local event="$2"
  local file
  file="$(events_file "$workflow_id")"
  mkdir -p "$(dirname "$file")"
  printf '%s\t%s\n' "$(timestamp)" "$event" >>"$file"
}

get_workflow_value() {
  local workflow_id="$1"
  local key="$2"
  local file
  file="$(workflow_meta_file "$workflow_id")"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

node_exists() {
  local workflow_id="$1"
  local node_id="$2"
  local file
  file="$(nodes_file "$workflow_id")"
  [[ -f "$file" ]] && awk -F'\t' -v node_id="$node_id" 'NR > 1 && $1 == node_id { found=1; exit } END { exit !found }' "$file"
}

get_node_status() {
  local workflow_id="$1"
  local node_id="$2"
  local file
  file="$(nodes_file "$workflow_id")"
  [[ -f "$file" ]] && awk -F'\t' -v node_id="$node_id" 'NR > 1 && $1 == node_id { print $7; exit 0 } END { exit 1 }' "$file"
}

get_node_dependencies() {
  local workflow_id="$1"
  local node_id="$2"
  local file
  file="$(edges_file "$workflow_id")"
  [[ -f "$file" ]] && awk -F'\t' -v node_id="$node_id" 'NR > 1 && $2 == node_id { print $1 }' "$file" | sort | uniq
}

# Check for dependency cycles using DFS
check_cycles() {
  local workflow_id="$1"
  local edges_file
  edges_file="$(edges_file "$workflow_id")"
  [[ -f "$edges_file" ]] || return 0

  # Create adjacency list in a temp file
  local temp_adj temp_visited temp_rec_stack
  temp_adj="$(mktemp)"
  temp_visited="$(mktemp)"
  temp_rec_stack="$(mktemp)"

  # Extract edges (from -> to)
  awk -F'\t' 'NR > 1 { print $1 "\t" $2 }' "$edges_file" > "$temp_adj"

  # Get all unique nodes
  local nodes
  nodes=($(awk -F'\t' 'NR > 1 { print $1; print $2 }' "$temp_adj" | sort | uniq))

  # DFS cycle detection function (implemented via temp files for bash compatibility)
  local has_cycle=0
  for node in "${nodes[@]}"; do
    if ! grep -q "^$node$" "$temp_visited" 2>/dev/null; then
      if dfs_cycle_check "$node" "$temp_adj" "$temp_visited" "$temp_rec_stack"; then
        has_cycle=1
        break
      fi
    fi
  done

  rm -f "$temp_adj" "$temp_visited" "$temp_rec_stack"
  return $has_cycle
}

dfs_cycle_check() {
  local node="$1"
  local adj_file="$2"
  local visited_file="$3"
  local rec_stack_file="$4"

  # Mark as visited and add to recursion stack
  echo "$node" >> "$visited_file"
  echo "$node" >> "$rec_stack_file"

  # Check all neighbors
  local neighbors
  neighbors=($(awk -F'\t' -v from="$node" '$1 == from { print $2 }' "$adj_file"))

  for neighbor in "${neighbors[@]}"; do
    # If neighbor not visited, recurse
    if ! grep -q "^$neighbor$" "$visited_file" 2>/dev/null; then
      if dfs_cycle_check "$neighbor" "$adj_file" "$visited_file" "$rec_stack_file"; then
        return 0  # Cycle found
      fi
    # If neighbor is in recursion stack, we found a cycle
    elif grep -q "^$neighbor$" "$rec_stack_file" 2>/dev/null; then
      return 0  # Cycle found
    fi
  done

  # Remove from recursion stack
  grep -v "^$node$" "$rec_stack_file" > "$rec_stack_file.tmp" 2>/dev/null || touch "$rec_stack_file.tmp"
  mv "$rec_stack_file.tmp" "$rec_stack_file"

  return 1  # No cycle
}

init_workflow() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "init requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"
  shift

  local title="" owner=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --title)
        title="${2:-}"
        shift 2
        ;;
      --owner)
        owner="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done

  [[ -n "$title" ]] || die "init requires --title"
  reject_newline "--title" "$title"
  reject_newline "--owner" "$owner"

  if workflow_exists "$workflow_id"; then
    die "workflow already exists: $workflow_id"
  fi

  local dir file
  dir="$(workflow_dir "$workflow_id")"
  file="$(workflow_meta_file "$workflow_id")"
  mkdir -p "$dir"

  cat >"$file" <<EOF
workflow_id=$workflow_id
title=$title
owner=$owner
status=active
created_at=$(timestamp)
EOF

  # Initialize empty TSV files with headers
  printf 'node_id\tagent\tassignment_id\trole\tbranch\towned_paths\tstatus\tdecision_id\tplan_id\tadded_at\n' >"$(nodes_file "$workflow_id")"
  printf 'from_node\tto_node\tadded_at\n' >"$(edges_file "$workflow_id")"

  log_event "$workflow_id" "workflow_created\ttitle=$title\towner=$owner"
  printf 'workflow created\t%s\t%s\n' "$workflow_id" "$title"
}

add_node() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "add-node requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"
  shift

  local node_id="${1:-}"
  [[ -n "$node_id" ]] || die "add-node requires NODE_ID"
  validate_node_id "$node_id"
  shift

  local agent="" assignment_id="" role="" branch="" owned_paths="" depends_on="" status="pending" decision_id="" plan_id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent)
        agent="${2:-}"
        shift 2
        ;;
      --assignment-id)
        assignment_id="${2:-}"
        shift 2
        ;;
      --role)
        role="${2:-}"
        shift 2
        ;;
      --branch)
        branch="${2:-}"
        shift 2
        ;;
      --owned)
        owned_paths="${2:-}"
        shift 2
        ;;
      --depends-on)
        depends_on="${2:-}"
        shift 2
        ;;
      --status)
        status="${2:-}"
        shift 2
        ;;
      --decision-id)
        decision_id="${2:-}"
        shift 2
        ;;
      --plan-id)
        plan_id="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done

  [[ -n "$agent" ]] || die "add-node requires --agent"
  [[ -n "$assignment_id" ]] || die "add-node requires --assignment-id"
  [[ -n "$role" ]] || die "add-node requires --role"
  [[ -n "$branch" ]] || die "add-node requires --branch"
  [[ -n "$owned_paths" ]] || die "add-node requires --owned"

  validate_status "$status"
  reject_newline "--agent" "$agent"
  reject_newline "--assignment-id" "$assignment_id"
  reject_newline "--role" "$role"
  reject_newline "--branch" "$branch"
  reject_newline "--owned" "$owned_paths"
  reject_newline "--depends-on" "$depends_on"
  reject_newline "--decision-id" "$decision_id"
  reject_newline "--plan-id" "$plan_id"

  workflow_exists "$workflow_id" || die "workflow does not exist: $workflow_id"

  # Check if node_id already exists
  if node_exists "$workflow_id" "$node_id"; then
    die "node ID already exists: $node_id"
  fi

  # Validate dependencies exist
  if [[ -n "$depends_on" ]]; then
    IFS=',' read -ra deps <<< "$depends_on"
    for dep in "${deps[@]}"; do
      dep="$(printf '%s' "$dep" | xargs)"  # trim whitespace
      if ! node_exists "$workflow_id" "$dep"; then
        die "dependency does not exist: $dep"
      fi
    done
  fi

  # Add node to nodes.tsv
  local nodes_f
  nodes_f="$(nodes_file "$workflow_id")"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$node_id" "$agent" "$assignment_id" "$role" "$branch" "$owned_paths" "$status" "$decision_id" "$plan_id" "$(timestamp)" >>"$nodes_f"

  # Add edges to edges.tsv
  local edges_f
  edges_f="$(edges_file "$workflow_id")"
  if [[ -n "$depends_on" ]]; then
    IFS=',' read -ra deps <<< "$depends_on"
    for dep in "${deps[@]}"; do
      dep="$(printf '%s' "$dep" | xargs)"  # trim whitespace
      printf '%s\t%s\t%s\n' "$dep" "$node_id" "$(timestamp)" >>"$edges_f"
    done
  fi

  # Check for dependency cycles after adding edges
  if ! check_cycles "$workflow_id"; then
    die "dependency cycle detected"
  fi

  log_event "$workflow_id" "node_added\tnode_id=$node_id\tagent=$agent\tassignment_id=$assignment_id\tstatus=$status\tdepends_on=$depends_on"
  printf 'node added\t%s\t%s\t%s\n' "$workflow_id" "$node_id" "$agent"
}

update_status() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "status requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"

  local node_id="${2:-}"
  [[ -n "$node_id" ]] || die "status requires NODE_ID"
  validate_node_id "$node_id"

  local new_status="${3:-}"
  [[ -n "$new_status" ]] || die "status requires STATUS"
  validate_status "$new_status"
  shift 3

  local reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --reason)
        reason="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown option: $1"
        ;;
    esac
  done

  reject_newline "--reason" "$reason"

  workflow_exists "$workflow_id" || die "workflow does not exist: $workflow_id"

  if ! node_exists "$workflow_id" "$node_id"; then
    die "node does not exist: $node_id"
  fi

  # Update node status in nodes.tsv
  local nodes_f temp_f
  nodes_f="$(nodes_file "$workflow_id")"
  temp_f="$(mktemp)"

  awk -F'\t' -v node_id="$node_id" -v new_status="$new_status" 'BEGIN { OFS="\t" } NR == 1 { print } NR > 1 && $1 == node_id { $7 = new_status; print } NR > 1 && $1 != node_id { print }' \
    "$nodes_f" > "$temp_f"
  mv "$temp_f" "$nodes_f"

  log_event "$workflow_id" "status_updated\tnode_id=$node_id\tstatus=$new_status\treason=$reason"
  printf 'status updated\t%s\t%s\t%s\n' "$workflow_id" "$node_id" "$new_status"
}

list_ready_nodes() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "ready requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"

  workflow_exists "$workflow_id" || die "workflow does not exist: $workflow_id"

  local nodes_f edges_f
  nodes_f="$(nodes_file "$workflow_id")"
  edges_f="$(edges_file "$workflow_id")"

  printf 'READY_NODES\n'

  # For each node with status pending, check if all dependencies are done
  while IFS=$'\t' read -r node_id agent assignment_id role branch owned_paths status decision_id plan_id added_at; do
    if [[ "$status" == "pending" ]]; then
      # Get dependencies for this node using inline awk instead of function call
      local deps_output
      deps_output=$(awk -F'\t' -v node_id="$node_id" 'NR > 1 && $2 == node_id { print $1 }' "$edges_f" | sort | uniq)

      if [[ -z "$deps_output" ]]; then
        # No dependencies - node is ready
        printf '%s\n' "$node_id"
      else
        # Check each dependency
        local all_deps_done=1
        while read -r dep; do
          [[ -n "$dep" ]] || continue
          local dep_status
          dep_status=$(awk -F'\t' -v node_id="$dep" 'NR > 1 && $1 == node_id { print $7 }' "$nodes_f")
          if [[ "$dep_status" != "done" && "$dep_status" != "skipped" ]]; then
            all_deps_done=0
            break
          fi
        done <<< "$deps_output"

        if [[ $all_deps_done -eq 1 ]]; then
          printf '%s\n' "$node_id"
        fi
      fi
    fi
  done < <(awk -F'\t' 'NR > 1' "$nodes_f")
}

list_blocked_nodes() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "blocked requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"

  workflow_exists "$workflow_id" || die "workflow does not exist: $workflow_id"

  local nodes_f edges_f
  nodes_f="$(nodes_file "$workflow_id")"
  edges_f="$(edges_file "$workflow_id")"

  printf 'BLOCKED_NODES\tREASON\n'

  # For each node with status pending or ready, check if any dependencies are failed
  while IFS=$'\t' read -r node_id agent assignment_id role branch owned_paths status decision_id plan_id added_at; do
    if [[ "$status" == "pending" || "$status" == "ready" ]]; then
      # Get dependencies for this node using inline awk
      local deps_output blocked_reason=""
      deps_output=$(awk -F'\t' -v node_id="$node_id" 'NR > 1 && $2 == node_id { print $1 }' "$edges_f" | sort | uniq)

      if [[ -n "$deps_output" ]]; then
        # Check each dependency for failure
        while read -r dep; do
          [[ -n "$dep" ]] || continue
          local dep_status
          dep_status=$(awk -F'\t' -v node_id="$dep" 'NR > 1 && $1 == node_id { print $7 }' "$nodes_f")
          if [[ "$dep_status" == "failed" ]]; then
            blocked_reason="dependency $dep failed"
            break
          fi
        done <<< "$deps_output"

        if [[ -n "$blocked_reason" ]]; then
          printf '%s\t%s\n' "$node_id" "$blocked_reason"
        fi
      fi
    fi
  done < <(awk -F'\t' 'NR > 1' "$nodes_f")
}

show_workflow() {
  local workflow_id="${1:-}"
  [[ -n "$workflow_id" ]] || die "show requires WORKFLOW_ID"
  validate_workflow_id "$workflow_id"

  workflow_exists "$workflow_id" || die "workflow does not exist: $workflow_id"

  local meta nodes edges events
  meta="$(workflow_meta_file "$workflow_id")"
  nodes="$(nodes_file "$workflow_id")"
  edges="$(edges_file "$workflow_id")"
  events="$(events_file "$workflow_id")"

  printf 'Workflow: %s\n' "$workflow_id"
  printf '=%.0s' {1..50}
  printf '\n'

  printf '\nMetadata:\n'
  cat "$meta"

  printf '\nNodes:\n'
  if [[ -s "$nodes" ]] && [[ $(wc -l <"$nodes") -gt 1 ]]; then
    cat "$nodes"
  else
    printf '(none)\n'
  fi

  printf '\nDependencies:\n'
  if [[ -s "$edges" ]] && [[ $(wc -l <"$edges") -gt 1 ]]; then
    cat "$edges"
  else
    printf '(none)\n'
  fi

  printf '\nEvents:\n'
  if [[ -s "$events" ]]; then
    cat "$events"
  else
    printf '(none)\n'
  fi
}

list_workflows() {
  local base="$STATE_DIR/workflows"
  printf 'WORKFLOW_ID\tSTATUS\tTITLE\tOWNER\tCREATED_AT\n'
  [[ -d "$base" ]] || return 0

  local dir workflow_id status title owner created_at
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    workflow_id="$(basename "$dir")"

    if [[ -f "$(workflow_meta_file "$workflow_id")" ]]; then
      status="$(get_workflow_value "$workflow_id" status || printf 'unknown')"
      title="$(get_workflow_value "$workflow_id" title || printf '')"
      owner="$(get_workflow_value "$workflow_id" owner || printf '')"
      created_at="$(get_workflow_value "$workflow_id" created_at || printf '')"
      printf '%s\t%s\t%s\t%s\t%s\n' "$workflow_id" "$status" "$title" "$owner" "$created_at"
    fi
  done
}

cmd="${1:-}"
case "$cmd" in
  init)
    shift
    init_workflow "$@"
    ;;
  add-node)
    shift
    add_node "$@"
    ;;
  status)
    shift
    update_status "$@"
    ;;
  ready)
    shift
    list_ready_nodes "$@"
    ;;
  blocked)
    shift
    list_blocked_nodes "$@"
    ;;
  show)
    shift
    show_workflow "$@"
    ;;
  list)
    shift
    list_workflows "$@"
    ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    [[ -n "$cmd" ]] && die "unknown command: $cmd"
    usage
    exit 1
    ;;
esac