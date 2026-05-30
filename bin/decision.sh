#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"

usage() {
  cat <<'USAGE'
Usage:
  bin/decision.sh init DECISION_ID --title TEXT [--owner NAME]
  bin/decision.sh add-alternative DECISION_ID --plan-id PLAN_ID --summary TEXT --proposed-by AGENT [--branch BRANCH] [--assignment-name NAME] [--expected-outcome TEXT] [--risk TEXT]
  bin/decision.sh add-assumption DECISION_ID --assumption-id ID --statement TEXT [--confidence VALUE] [--validation-method TEXT] [--expected-signal TEXT]
  bin/decision.sh commit DECISION_ID --selected-plan PLAN_ID --reason TEXT [--rollback-policy TEXT] [--reflection-due TEXT]
  bin/decision.sh record-metric DECISION_ID --name NAME [--expected VALUE] [--actual VALUE]
  bin/decision.sh reflect DECISION_ID --recommendation continue|adjust|rollback|pivot --reason TEXT [--follow-up-assignment NAME]
  bin/decision.sh show DECISION_ID
  bin/decision.sh list

Manages durable organizational learning records for multi-agent decision making.

Decision records are stored under $MULTIAGENT_STATE_DIR/decisions/DECISION_ID with:
  decision.env       - Decision metadata (title, owner, status, timestamps)
  alternatives.tsv   - Alternative plans with their details
  assumptions.tsv    - Decision assumptions with validation criteria
  metrics.tsv        - Expected and actual metrics for the decision
  events.log         - Timestamped events in the decision lifecycle
  outcome.env        - Final outcome data when decision is committed

Decision lifecycle:
  1. init             - Create new decision record
  2. add-alternative  - Add alternative implementation plans
  3. add-assumption   - Add key assumptions underlying the decision
  4. commit           - Select a plan and commit to implementation
  5. record-metric    - Track expected vs actual metrics
  6. reflect          - Evaluate decision outcome and recommend next steps
USAGE
}

die() {
  echo "decision: $*" >&2
  exit 1
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

validate_decision_id() {
  local decision_id="$1"
  [[ "$decision_id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid decision ID: $decision_id"
}

validate_plan_id() {
  local plan_id="$1"
  [[ "$plan_id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid plan ID: $plan_id"
}

validate_assumption_id() {
  local assumption_id="$1"
  [[ "$assumption_id" =~ ^[A-Za-z0-9_.-]+$ ]] || die "invalid assumption ID: $assumption_id"
}

reject_newline() {
  local label="$1"
  local value="$2"
  [[ "$value" != *$'\n'* ]] || die "$label may not contain newlines"
}

decision_dir() {
  printf '%s/decisions/%s\n' "$STATE_DIR" "$1"
}

decision_meta_file() {
  printf '%s/decision.env\n' "$(decision_dir "$1")"
}

alternatives_file() {
  printf '%s/alternatives.tsv\n' "$(decision_dir "$1")"
}

assumptions_file() {
  printf '%s/assumptions.tsv\n' "$(decision_dir "$1")"
}

metrics_file() {
  printf '%s/metrics.tsv\n' "$(decision_dir "$1")"
}

events_file() {
  printf '%s/events.log\n' "$(decision_dir "$1")"
}

outcome_file() {
  printf '%s/outcome.env\n' "$(decision_dir "$1")"
}

decision_exists() {
  local decision_id="$1"
  [[ -f "$(decision_meta_file "$decision_id")" ]]
}

log_event() {
  local decision_id="$1"
  local event="$2"
  local file
  file="$(events_file "$decision_id")"
  mkdir -p "$(dirname "$file")"
  printf '%s\t%s\n' "$(timestamp)" "$event" >>"$file"
}

get_decision_value() {
  local decision_id="$1"
  local key="$2"
  local file
  file="$(decision_meta_file "$decision_id")"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

init_decision() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "init requires DECISION_ID"
  validate_decision_id "$decision_id"
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

  if decision_exists "$decision_id"; then
    die "decision already exists: $decision_id"
  fi

  local dir file
  dir="$(decision_dir "$decision_id")"
  file="$(decision_meta_file "$decision_id")"
  mkdir -p "$dir"

  cat >"$file" <<EOF
decision_id=$decision_id
title=$title
owner=$owner
status=open
created_at=$(timestamp)
EOF

  # Initialize empty TSV files with headers
  printf 'plan_id\tsummary\tproposed_by\tbranch\tassignment_name\texpected_outcome\trisk\tadded_at\n' >"$(alternatives_file "$decision_id")"
  printf 'assumption_id\tstatement\tconfidence\tvalidation_method\texpected_signal\tadded_at\n' >"$(assumptions_file "$decision_id")"
  printf 'name\texpected\tactual\trecorded_at\n' >"$(metrics_file "$decision_id")"

  log_event "$decision_id" "decision_created\ttitle=$title\towner=$owner"
  printf 'decision created\t%s\t%s\n' "$decision_id" "$title"
}

add_alternative() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "add-alternative requires DECISION_ID"
  validate_decision_id "$decision_id"
  shift

  local plan_id="" summary="" proposed_by="" branch="" assignment_name="" expected_outcome="" risk=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --plan-id)
        plan_id="${2:-}"
        shift 2
        ;;
      --summary)
        summary="${2:-}"
        shift 2
        ;;
      --proposed-by)
        proposed_by="${2:-}"
        shift 2
        ;;
      --branch)
        branch="${2:-}"
        shift 2
        ;;
      --assignment-name)
        assignment_name="${2:-}"
        shift 2
        ;;
      --expected-outcome)
        expected_outcome="${2:-}"
        shift 2
        ;;
      --risk)
        risk="${2:-}"
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

  [[ -n "$plan_id" ]] || die "add-alternative requires --plan-id"
  [[ -n "$summary" ]] || die "add-alternative requires --summary"
  [[ -n "$proposed_by" ]] || die "add-alternative requires --proposed-by"

  validate_plan_id "$plan_id"
  reject_newline "--plan-id" "$plan_id"
  reject_newline "--summary" "$summary"
  reject_newline "--proposed-by" "$proposed_by"
  reject_newline "--branch" "$branch"
  reject_newline "--assignment-name" "$assignment_name"
  reject_newline "--expected-outcome" "$expected_outcome"
  reject_newline "--risk" "$risk"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local status
  status="$(get_decision_value "$decision_id" status)"
  [[ "$status" == "open" ]] || die "cannot add alternatives to $status decision: $decision_id"

  # Check if plan_id already exists
  local file
  file="$(alternatives_file "$decision_id")"
  if awk -F'\t' -v plan_id="$plan_id" 'NR > 1 && $1 == plan_id { exit 1 }' "$file"; then
    # Plan ID doesn't exist, add it
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$plan_id" "$summary" "$proposed_by" "$branch" "$assignment_name" "$expected_outcome" "$risk" "$(timestamp)" >>"$file"
    log_event "$decision_id" "alternative_added\tplan_id=$plan_id\tproposed_by=$proposed_by"
    printf 'alternative added\t%s\t%s\t%s\n' "$decision_id" "$plan_id" "$summary"
  else
    die "plan ID already exists: $plan_id"
  fi
}

add_assumption() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "add-assumption requires DECISION_ID"
  validate_decision_id "$decision_id"
  shift

  local assumption_id="" statement="" confidence="" validation_method="" expected_signal=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --assumption-id)
        assumption_id="${2:-}"
        shift 2
        ;;
      --statement)
        statement="${2:-}"
        shift 2
        ;;
      --confidence)
        confidence="${2:-}"
        shift 2
        ;;
      --validation-method)
        validation_method="${2:-}"
        shift 2
        ;;
      --expected-signal)
        expected_signal="${2:-}"
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

  [[ -n "$assumption_id" ]] || die "add-assumption requires --assumption-id"
  [[ -n "$statement" ]] || die "add-assumption requires --statement"

  validate_assumption_id "$assumption_id"
  reject_newline "--assumption-id" "$assumption_id"
  reject_newline "--statement" "$statement"
  reject_newline "--confidence" "$confidence"
  reject_newline "--validation-method" "$validation_method"
  reject_newline "--expected-signal" "$expected_signal"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local status
  status="$(get_decision_value "$decision_id" status)"
  [[ "$status" == "open" ]] || die "cannot add assumptions to $status decision: $decision_id"

  # Check if assumption_id already exists
  local file
  file="$(assumptions_file "$decision_id")"
  if awk -F'\t' -v assumption_id="$assumption_id" 'NR > 1 && $1 == assumption_id { exit 1 }' "$file"; then
    # Assumption ID doesn't exist, add it
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$assumption_id" "$statement" "$confidence" "$validation_method" "$expected_signal" "$(timestamp)" >>"$file"
    log_event "$decision_id" "assumption_added\tassumption_id=$assumption_id"
    printf 'assumption added\t%s\t%s\t%s\n' "$decision_id" "$assumption_id" "$statement"
  else
    die "assumption ID already exists: $assumption_id"
  fi
}

commit_decision() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "commit requires DECISION_ID"
  validate_decision_id "$decision_id"
  shift

  local selected_plan="" reason="" rollback_policy="" reflection_due=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --selected-plan)
        selected_plan="${2:-}"
        shift 2
        ;;
      --reason)
        reason="${2:-}"
        shift 2
        ;;
      --rollback-policy)
        rollback_policy="${2:-}"
        shift 2
        ;;
      --reflection-due)
        reflection_due="${2:-}"
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

  [[ -n "$selected_plan" ]] || die "commit requires --selected-plan"
  [[ -n "$reason" ]] || die "commit requires --reason"

  validate_plan_id "$selected_plan"
  reject_newline "--selected-plan" "$selected_plan"
  reject_newline "--reason" "$reason"
  reject_newline "--rollback-policy" "$rollback_policy"
  reject_newline "--reflection-due" "$reflection_due"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local status
  status="$(get_decision_value "$decision_id" status)"
  [[ "$status" == "open" ]] || die "cannot commit $status decision: $decision_id"

  # Verify selected plan exists
  local file
  file="$(alternatives_file "$decision_id")"
  if ! awk -F'\t' -v plan_id="$selected_plan" 'NR > 1 && $1 == plan_id { found=1 } END { exit found ? 0 : 1 }' "$file"; then
    die "selected plan does not exist: $selected_plan"
  fi

  # Update decision status
  local meta
  meta="$(decision_meta_file "$decision_id")"
  {
    grep -v "^status=" "$meta"
    printf 'status=committed\n'
    printf 'committed_at=%s\n' "$(timestamp)"
  } >"$meta.tmp"
  mv "$meta.tmp" "$meta"

  # Create outcome record
  local outcome
  outcome="$(outcome_file "$decision_id")"
  cat >"$outcome" <<EOF
selected_plan=$selected_plan
reason=$reason
rollback_policy=$rollback_policy
reflection_due=$reflection_due
committed_at=$(timestamp)
status=implementation
EOF

  log_event "$decision_id" "decision_committed\tselected_plan=$selected_plan\treason=$reason"
  printf 'decision committed\t%s\t%s\t%s\n' "$decision_id" "$selected_plan" "$reason"
}

record_metric() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "record-metric requires DECISION_ID"
  validate_decision_id "$decision_id"
  shift

  local name="" expected="" actual=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --name)
        name="${2:-}"
        shift 2
        ;;
      --expected)
        expected="${2:-}"
        shift 2
        ;;
      --actual)
        actual="${2:-}"
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

  [[ -n "$name" ]] || die "record-metric requires --name"

  reject_newline "--name" "$name"
  reject_newline "--expected" "$expected"
  reject_newline "--actual" "$actual"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local status
  status="$(get_decision_value "$decision_id" status)"
  [[ "$status" == "committed" ]] || die "can only record metrics for committed decisions, got: $status"

  local file
  file="$(metrics_file "$decision_id")"
  printf '%s\t%s\t%s\t%s\n' "$name" "$expected" "$actual" "$(timestamp)" >>"$file"

  log_event "$decision_id" "metric_recorded\tname=$name\texpected=$expected\tactual=$actual"
  printf 'metric recorded\t%s\t%s\texpected=%s\tactual=%s\n' "$decision_id" "$name" "$expected" "$actual"
}

reflect_decision() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "reflect requires DECISION_ID"
  validate_decision_id "$decision_id"
  shift

  local recommendation="" reason="" follow_up_assignment=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --recommendation)
        recommendation="${2:-}"
        shift 2
        ;;
      --reason)
        reason="${2:-}"
        shift 2
        ;;
      --follow-up-assignment)
        follow_up_assignment="${2:-}"
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

  [[ -n "$recommendation" ]] || die "reflect requires --recommendation"
  [[ -n "$reason" ]] || die "reflect requires --reason"

  case "$recommendation" in
    continue|adjust|rollback|pivot)
      ;;
    *)
      die "invalid recommendation: $recommendation (expected continue|adjust|rollback|pivot)"
      ;;
  esac

  reject_newline "--recommendation" "$recommendation"
  reject_newline "--reason" "$reason"
  reject_newline "--follow-up-assignment" "$follow_up_assignment"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local status
  status="$(get_decision_value "$decision_id" status)"
  [[ "$status" == "committed" ]] || die "can only reflect on committed decisions, got: $status"

  # Update decision status
  local meta
  meta="$(decision_meta_file "$decision_id")"
  {
    grep -v "^status=" "$meta"
    printf 'status=reflected\n'
    printf 'reflected_at=%s\n' "$(timestamp)"
  } >"$meta.tmp"
  mv "$meta.tmp" "$meta"

  # Update outcome record
  local outcome
  outcome="$(outcome_file "$decision_id")"
  [[ -f "$outcome" ]] || die "no outcome record found: $decision_id"

  {
    cat "$outcome"
    printf 'recommendation=%s\n' "$recommendation"
    printf 'reflection_reason=%s\n' "$reason"
    printf 'follow_up_assignment=%s\n' "$follow_up_assignment"
    printf 'reflected_at=%s\n' "$(timestamp)"
    printf 'status=reflected\n'
  } >"$outcome.tmp"
  mv "$outcome.tmp" "$outcome"

  log_event "$decision_id" "decision_reflected\trecommendation=$recommendation\treason=$reason\tfollow_up=$follow_up_assignment"
  printf 'decision reflected\t%s\t%s\t%s\n' "$decision_id" "$recommendation" "$reason"
}

show_decision() {
  local decision_id="${1:-}"
  [[ -n "$decision_id" ]] || die "show requires DECISION_ID"
  validate_decision_id "$decision_id"

  decision_exists "$decision_id" || die "decision does not exist: $decision_id"

  local meta alternatives assumptions metrics events outcome
  meta="$(decision_meta_file "$decision_id")"
  alternatives="$(alternatives_file "$decision_id")"
  assumptions="$(assumptions_file "$decision_id")"
  metrics="$(metrics_file "$decision_id")"
  events="$(events_file "$decision_id")"
  outcome="$(outcome_file "$decision_id")"

  printf 'Decision: %s\n' "$decision_id"
  printf '=%.0s' {1..50}
  printf '\n'

  printf '\nMetadata:\n'
  cat "$meta"

  printf '\nAlternatives:\n'
  if [[ -s "$alternatives" ]] && [[ $(wc -l <"$alternatives") -gt 1 ]]; then
    cat "$alternatives"
  else
    printf '(none)\n'
  fi

  printf '\nAssumptions:\n'
  if [[ -s "$assumptions" ]] && [[ $(wc -l <"$assumptions") -gt 1 ]]; then
    cat "$assumptions"
  else
    printf '(none)\n'
  fi

  printf '\nMetrics:\n'
  if [[ -s "$metrics" ]] && [[ $(wc -l <"$metrics") -gt 1 ]]; then
    cat "$metrics"
  else
    printf '(none)\n'
  fi

  if [[ -f "$outcome" ]]; then
    printf '\nOutcome:\n'
    cat "$outcome"
  fi

  printf '\nEvents:\n'
  if [[ -s "$events" ]]; then
    cat "$events"
  else
    printf '(none)\n'
  fi
}

list_decisions() {
  local base="$STATE_DIR/decisions"
  printf 'DECISION_ID\tSTATUS\tTITLE\tOWNER\tCREATED_AT\n'
  [[ -d "$base" ]] || return 0

  local dir decision_id status title owner created_at
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    decision_id="$(basename "$dir")"

    if [[ -f "$(decision_meta_file "$decision_id")" ]]; then
      status="$(get_decision_value "$decision_id" status || printf 'unknown')"
      title="$(get_decision_value "$decision_id" title || printf '')"
      owner="$(get_decision_value "$decision_id" owner || printf '')"
      created_at="$(get_decision_value "$decision_id" created_at || printf '')"
      printf '%s\t%s\t%s\t%s\t%s\n' "$decision_id" "$status" "$title" "$owner" "$created_at"
    fi
  done
}

cmd="${1:-}"
case "$cmd" in
  init)
    shift
    init_decision "$@"
    ;;
  add-alternative)
    shift
    add_alternative "$@"
    ;;
  add-assumption)
    shift
    add_assumption "$@"
    ;;
  commit)
    shift
    commit_decision "$@"
    ;;
  record-metric)
    shift
    record_metric "$@"
    ;;
  reflect)
    shift
    reflect_decision "$@"
    ;;
  show)
    shift
    show_decision "$@"
    ;;
  list)
    shift
    list_decisions "$@"
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