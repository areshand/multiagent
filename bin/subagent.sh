#!/usr/bin/env bash
set -euo pipefail

SESSION="${MULTIAGENT_SESSION:-multiagent}"
ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"
CODEX_BIN="${CODEX_BIN:-codex}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
WORKER_CLI="${WORKER_CLI:-claude}"
SUBAGENT_CLI="${SUBAGENT_CLI:-$WORKER_CLI}"
VERIFIER_CLI="${VERIFIER_CLI:-codex}"

usage() {
  cat <<'USAGE'
Usage:
  bin/subagent.sh spawn NAME [--instruction TEXT]
  bin/subagent.sh list
  bin/subagent.sh assignment-create NAME --assignment-id ID --branch BRANCH --owned PATH[,PATH...] [--status STATUS] [--start-commit COMMIT] [--role exploitation|exploration|reflection|architecture|qa|verifier] [--decision-id DECISION_ID] [--plan-id PLAN_ID] [--workflow-id WORKFLOW_ID] [--node-id NODE_ID] [--depends-on NODE[,NODE...]]
  bin/subagent.sh assignment-show NAME
  bin/subagent.sh assignment-status NAME STATUS
  bin/subagent.sh assignment-check NAME
  bin/subagent.sh checkpoint-update NAME --step TEXT [--blocker TEXT] [--idempotency TEXT] [--last-commit COMMIT] [--status STATUS]
  bin/subagent.sh checkpoint-show NAME
  bin/subagent.sh worktree-create NAME [--branch BRANCH] [--path PATH]
  bin/subagent.sh worktree-show NAME
  bin/subagent.sh worktree-remove NAME [--force]
  bin/subagent.sh health-check NAME
  bin/subagent.sh poll NAME
  bin/subagent.sh inspect NAME [--lines N]
  bin/subagent.sh recover-plan
  bin/subagent.sh restore NAME [--force]
  bin/subagent.sh restore-all
  bin/subagent.sh finalize NAME [--keep-window]
  bin/subagent.sh kill NAME

Manages named long-running subagents in tmux and persists their captured
context under $MULTIAGENT_STATE_DIR/subagents/NAME.

Subagents inherit $MULTIAGENT_WRITE_POLICY, defaulting to
$MULTIAGENT_ROOT/docs/write-policy.paths. They are expected to check planned
writes with bin/write-policy.sh before writing outside $MULTIAGENT_ROOT.

CLI selection:
  WORKER_CLI defaults to claude. SUBAGENT_CLI defaults to WORKER_CLI.
  VERIFIER_CLI defaults to codex; pass SUBAGENT_CLI="$VERIFIER_CLI" when
  using generic subagent spawning for verifier windows.
  Supported values are codex and claude. Codex uses --cd,
  --dangerously-bypass-approvals-and-sandbox, and --no-alt-screen. Claude uses
  --dangerously-skip-permissions from the target directory.
USAGE
}

die() {
  echo "subagent: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

normalize_cli() {
  case "$1" in
    codex|claude)
      printf '%s\n' "$1"
      ;;
    *)
      die "unsupported CLI '$1' (expected codex or claude)"
      ;;
  esac
}

cli_bin() {
  case "$1" in
    codex) printf '%s\n' "$CODEX_BIN" ;;
    claude) printf '%s\n' "$CLAUDE_BIN" ;;
    *) die "unsupported CLI '$1' (expected codex or claude)" ;;
  esac
}

build_cli_command() {
  local cli="$1"
  local cwd="$2"
  local bin
  bin="$(cli_bin "$cli")"
  case "$cli" in
    codex)
      printf "%q --cd %q --dangerously-bypass-approvals-and-sandbox --no-alt-screen" "$bin" "$cwd"
      ;;
    claude)
      printf "%q --dangerously-skip-permissions" "$bin"
      ;;
    *)
      die "unsupported CLI '$cli' (expected codex or claude)"
      ;;
  esac
}

read_subagent_meta_value() {
  local name="$1"
  local key="$2"
  local file
  file="$(subagent_dir "$name")/meta.env"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
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

assignment_dir() {
  printf '%s/assignments/%s\n' "$STATE_DIR" "$1"
}

assignment_meta_file() {
  printf '%s/assignment.env\n' "$(assignment_dir "$1")"
}

assignment_owned_file() {
  printf '%s/owned-paths\n' "$(assignment_dir "$1")"
}

assignment_status_file() {
  printf '%s/status\n' "$(assignment_dir "$1")"
}

checkpoint_file() {
  printf '%s/checkpoint.env\n' "$(assignment_dir "$1")"
}

worktree_meta_file() {
  printf '%s/worktrees/%s.env\n' "$STATE_DIR" "$1"
}

default_worktree_path() {
  printf '%s/worktrees/%s\n' "$STATE_DIR" "$1"
}

WORKER_CLI="$(normalize_cli "$WORKER_CLI")"
SUBAGENT_CLI="$(normalize_cli "$SUBAGENT_CLI")"
VERIFIER_CLI="$(normalize_cli "$VERIFIER_CLI")"

set_status() {
  local name="$1"
  local status="$2"
  mkdir -p "$(subagent_dir "$name")"
  printf '%s\n' "$status" >"$(status_file "$name")"
}

get_status() {
  local name="$1"
  if [[ -f "$(status_file "$name")" ]]; then
    tr -d '\n' <"$(status_file "$name")"
  else
    printf 'unknown\n'
  fi
}

read_assignment_value() {
  local name="$1"
  local key="$2"
  local file
  file="$(assignment_meta_file "$name")"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

read_checkpoint_value() {
  local name="$1"
  local key="$2"
  local file
  file="$(checkpoint_file "$name")"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

reject_newline() {
  local label="$1"
  local value="$2"
  [[ "$value" != *$'\n'* ]] || die "$label may not contain newlines"
}

set_assignment_status() {
  local name="$1"
  local status="$2"
  [[ -f "$(assignment_meta_file "$name")" ]] || die "no assignment for agent: $name"
  printf '%s\n' "$status" >"$(assignment_status_file "$name")"
}

get_assignment_status() {
  local name="$1"
  if [[ -f "$(assignment_status_file "$name")" ]]; then
    tr -d '\n' <"$(assignment_status_file "$name")"
  else
    printf 'unknown\n'
  fi
}

normalize_repo_path() {
  local path="$1"
  local root canonical rel
  root="$(cd "$ROOT" && pwd -P)"
  if [[ "$path" = /* ]]; then
    canonical="$path"
  else
    canonical="$root/$path"
  fi

  if [[ -e "$canonical" ]]; then
    canonical="$(cd "$(dirname "$canonical")" && pwd -P)/$(basename "$canonical")"
  else
    local rest="" parent="$canonical" base
    while [[ ! -e "$parent" ]]; do
      base="$(basename "$parent")"
      if [[ -n "$rest" ]]; then
        rest="$base/$rest"
      else
        rest="$base"
      fi
      parent="$(dirname "$parent")"
      [[ "$parent" != "/" ]] || break
    done
    if [[ -e "$parent" ]]; then
      canonical="$(cd "$parent" && pwd -P)/$rest"
    fi
  fi

  [[ "$canonical" == "$root" || "$canonical" == "$root/"* ]] || die "assigned path is outside MULTIAGENT_ROOT: $path"
  rel="${canonical#"$root"/}"
  rel="${rel#./}"
  rel="${rel%/}"
  [[ -n "$rel" && "$rel" != "$root" ]] || die "assigned path may not be the whole repo root"
  printf '%s\n' "$rel"
}

path_in_assignment() {
  local changed="$1"
  local owned
  while IFS= read -r owned; do
    [[ -n "$owned" ]] || continue
    if [[ "$changed" == "$owned" || "$changed" == "$owned/"* ]]; then
      return 0
    fi
  done
  return 1
}

assignment_create() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "assignment-create requires NAME"
  validate_name "$name"
  shift

  local assignment_id="" branch="" owned_csv="" status="assigned" start_commit="" role="exploitation" decision_id="" plan_id="" workflow_id="" node_id="" depends_on=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --assignment-id)
        assignment_id="${2:-}"
        shift 2
        ;;
      --branch)
        branch="${2:-}"
        shift 2
        ;;
      --owned)
        owned_csv="${2:-}"
        shift 2
        ;;
      --status)
        status="${2:-}"
        shift 2
        ;;
      --start-commit)
        start_commit="${2:-}"
        shift 2
        ;;
      --role)
        role="${2:-}"
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
      --workflow-id)
        workflow_id="${2:-}"
        shift 2
        ;;
      --node-id)
        node_id="${2:-}"
        shift 2
        ;;
      --depends-on)
        depends_on="${2:-}"
        shift 2
        ;;
      *)
        die "unknown assignment-create argument: $1"
        ;;
    esac
  done

  [[ -n "$assignment_id" ]] || die "assignment-create requires --assignment-id ID"
  [[ -n "$branch" ]] || die "assignment-create requires --branch BRANCH"
  [[ -n "$owned_csv" ]] || die "assignment-create requires --owned PATH[,PATH...]"
  case "$role" in
    exploitation|exploration|reflection|architecture|qa|verifier)
      ;;
    *)
      die "invalid role '$role' (expected exploitation|exploration|reflection|architecture|qa|verifier)"
      ;;
  esac
  if [[ -z "$start_commit" ]]; then
    start_commit="$(git -C "$ROOT" rev-parse HEAD)"
  else
    git -C "$ROOT" rev-parse --verify "$start_commit^{commit}" >/dev/null || die "invalid start commit: $start_commit"
    start_commit="$(git -C "$ROOT" rev-parse "$start_commit^{commit}")"
  fi

  local dir owned_file item normalized
  dir="$(assignment_dir "$name")"
  mkdir -p "$dir"
  owned_file="$(assignment_owned_file "$name")"
  : >"$owned_file"
  IFS=',' read -ra owned_items <<<"$owned_csv"
  for item in "${owned_items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [[ -n "$item" ]] || continue
    normalized="$(normalize_repo_path "$item")"
    grep -Fx -- "$normalized" "$owned_file" >/dev/null 2>&1 || printf '%s\n' "$normalized" >>"$owned_file"
  done
  [[ -s "$owned_file" ]] || die "assignment must own at least one path"

  cat >"$(assignment_meta_file "$name")" <<EOF
agent_name=$name
assignment_id=$assignment_id
branch=$branch
start_commit=$start_commit
created_at=$(timestamp)
root=$ROOT
worker_cli=$WORKER_CLI
subagent_cli=$SUBAGENT_CLI
verifier_cli=$VERIFIER_CLI
role=$role
decision_id=$decision_id
plan_id=$plan_id
workflow_id=$workflow_id
node_id=$node_id
depends_on=$depends_on
EOF
  set_assignment_status "$name" "$status"
  printf 'assignment created\t%s\t%s\t%s\n' "$name" "$assignment_id" "$branch"
}

assignment_show() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "assignment-show requires NAME"
  validate_name "$name"
  [[ -f "$(assignment_meta_file "$name")" ]] || die "no assignment for agent: $name"

  cat "$(assignment_meta_file "$name")"
  printf 'status=%s\n' "$(get_assignment_status "$name")"
  if [[ -f "$(checkpoint_file "$name")" ]]; then
    printf 'checkpoint=\n'
    sed 's/^/  /' "$(checkpoint_file "$name")"
  fi
  printf 'owned_paths=\n'
  sed 's/^/  /' "$(assignment_owned_file "$name")"
}

assignment_status() {
  local name="${1:-}"
  local status="${2:-}"
  [[ -n "$name" && -n "$status" ]] || die "assignment-status requires NAME STATUS"
  validate_name "$name"
  set_assignment_status "$name" "$status"
  printf 'assignment status\t%s\t%s\n' "$name" "$status"
}

assignment_changed_files() {
  local workdir="$1"
  local start_commit="$2"
  {
    git -C "$workdir" diff --name-only "$start_commit"..HEAD
    git -C "$workdir" diff --name-only
    git -C "$workdir" diff --name-only --cached
    git -C "$workdir" ls-files --others --exclude-standard
  } | sed '/^$/d' | sort -u
}

assignment_workdir() {
  local name="$1"
  local worktree_path
  worktree_path="$(read_assignment_value "$name" worktree_path || true)"
  if [[ -n "$worktree_path" && ( -d "$worktree_path/.git" || -f "$worktree_path/.git" ) ]]; then
    printf '%s\n' "$worktree_path"
  else
    printf '%s\n' "$ROOT"
  fi
}

assignment_check() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "assignment-check requires NAME"
  validate_name "$name"
  [[ -f "$(assignment_meta_file "$name")" ]] || die "no assignment for agent: $name"

  local expected_branch start_commit current_branch owned_file workdir failed=0 changed
  expected_branch="$(read_assignment_value "$name" branch)"
  start_commit="$(read_assignment_value "$name" start_commit)"
  owned_file="$(assignment_owned_file "$name")"
  workdir="$(assignment_workdir "$name")"
  current_branch="$(git -C "$workdir" rev-parse --abbrev-ref HEAD)"

  printf 'assignment\t%s\t%s\n' "$name" "$(read_assignment_value "$name" assignment_id)"
  printf 'workdir\t%s\n' "$workdir"
  printf 'branch\t%s\t%s\n' "$expected_branch" "$current_branch"
  if [[ "$current_branch" != "$expected_branch" ]]; then
    printf 'reject\tbranch-mismatch\texpected=%s\tactual=%s\n' "$expected_branch" "$current_branch"
    failed=1
  fi

  local any=0
  while IFS= read -r changed; do
    [[ -n "$changed" ]] || continue
    any=1
    if path_in_assignment "$changed" <"$owned_file"; then
      printf 'ok\t%s\n' "$changed"
    else
      printf 'reject\toutside-owned-path\t%s\n' "$changed"
      failed=1
    fi
  done < <(assignment_changed_files "$workdir" "$start_commit")

  if [[ "$any" -eq 0 ]]; then
    printf 'ok\tno-changes\n'
  fi

  if [[ "$failed" -eq 0 ]]; then
    printf 'accepted\t%s\n' "$name"
  fi
  return "$failed"
}

checkpoint_update() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "checkpoint-update requires NAME"
  validate_name "$name"
  shift
  [[ -f "$(assignment_meta_file "$name")" ]] || die "no assignment for agent: $name"

  local step="" blocker="" idempotency="" last_commit="" status=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --step)
        step="${2:-}"
        shift 2
        ;;
      --blocker)
        blocker="${2:-}"
        shift 2
        ;;
      --idempotency)
        idempotency="${2:-}"
        shift 2
        ;;
      --last-commit)
        last_commit="${2:-}"
        shift 2
        ;;
      --status)
        status="${2:-}"
        shift 2
        ;;
      *)
        die "unknown checkpoint-update argument: $1"
        ;;
    esac
  done

  [[ -n "$step" ]] || die "checkpoint-update requires --step TEXT"
  if [[ -z "$last_commit" ]]; then
    last_commit="$(git -C "$ROOT" rev-parse HEAD)"
  else
    git -C "$ROOT" rev-parse --verify "$last_commit^{commit}" >/dev/null || die "invalid last commit: $last_commit"
    last_commit="$(git -C "$ROOT" rev-parse "$last_commit^{commit}")"
  fi
  if [[ -z "$status" ]]; then
    if [[ -n "$blocker" ]]; then
      status="blocked"
    else
      status="$(get_assignment_status "$name")"
    fi
  fi

  reject_newline "--step" "$step"
  reject_newline "--blocker" "$blocker"
  reject_newline "--idempotency" "$idempotency"
  reject_newline "--status" "$status"

  local file
  file="$(checkpoint_file "$name")"
  mkdir -p "$(dirname "$file")"
  cat >"$file" <<EOF
agent_name=$name
assignment_id=$(read_assignment_value "$name" assignment_id)
branch=$(read_assignment_value "$name" branch)
owned_paths_file=$(assignment_owned_file "$name")
last_commit=$last_commit
completed_step=$step
blocker=$blocker
idempotency=$idempotency
status=$status
role=$(read_assignment_value "$name" role || printf 'exploitation')
decision_id=$(read_assignment_value "$name" decision_id || true)
plan_id=$(read_assignment_value "$name" plan_id || true)
workflow_id=$(read_assignment_value "$name" workflow_id || true)
node_id=$(read_assignment_value "$name" node_id || true)
depends_on=$(read_assignment_value "$name" depends_on || true)
updated_at=$(timestamp)
EOF
  set_assignment_status "$name" "$status"
  set_status "$name" "$status"
  printf 'checkpoint updated\t%s\t%s\n' "$name" "$status"
}

checkpoint_show() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "checkpoint-show requires NAME"
  validate_name "$name"
  [[ -f "$(checkpoint_file "$name")" ]] || die "no checkpoint for agent: $name"
  cat "$(checkpoint_file "$name")"
}

worktree_create() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "worktree-create requires NAME"
  validate_name "$name"
  shift

  local branch="" path=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --branch)
        branch="${2:-}"
        shift 2
        ;;
      --path)
        path="${2:-}"
        shift 2
        ;;
      *)
        die "unknown worktree-create argument: $1"
        ;;
    esac
  done
  if [[ -z "$branch" && -f "$(assignment_meta_file "$name")" ]]; then
    branch="$(read_assignment_value "$name" branch)"
  fi
  [[ -n "$branch" ]] || die "worktree-create requires --branch BRANCH or assignment metadata"
  [[ -n "$path" ]] || path="$(default_worktree_path "$name")"

  mkdir -p "$(dirname "$path")" "$(dirname "$(worktree_meta_file "$name")")"
  if [[ ! -d "$path/.git" && ! -f "$path/.git" ]]; then
    if git -C "$ROOT" show-ref --verify --quiet "refs/heads/$branch"; then
      git -C "$ROOT" worktree add "$path" "$branch"
    else
      git -C "$ROOT" worktree add -b "$branch" "$path" HEAD
    fi
  fi

  cat >"$(worktree_meta_file "$name")" <<EOF
agent_name=$name
branch=$branch
path=$path
created_at=$(timestamp)
root=$ROOT
EOF
  if [[ -f "$(assignment_meta_file "$name")" ]] && ! grep -q '^worktree_path=' "$(assignment_meta_file "$name")"; then
    printf 'worktree_path=%s\n' "$path" >>"$(assignment_meta_file "$name")"
  fi
  printf 'worktree created\t%s\t%s\t%s\n' "$name" "$branch" "$path"
}

worktree_show() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "worktree-show requires NAME"
  validate_name "$name"
  [[ -f "$(worktree_meta_file "$name")" ]] || die "no worktree metadata for agent: $name"
  cat "$(worktree_meta_file "$name")"
}

worktree_remove() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "worktree-remove requires NAME"
  validate_name "$name"
  shift

  local force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force)
        force=1
        shift
        ;;
      *)
        die "unknown worktree-remove argument: $1"
        ;;
    esac
  done

  local meta path args=()
  meta="$(worktree_meta_file "$name")"
  [[ -f "$meta" ]] || die "no worktree metadata for agent: $name"
  path="$(awk -F= '$1 == "path" { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$meta")"
  [[ "$force" -eq 1 ]] && args+=(--force)
  git -C "$ROOT" worktree remove "${args[@]}" "$path"
  rm -f "$meta"
  printf 'worktree removed\t%s\t%s\n' "$name" "$path"
}

window_exists() {
  local name="$1"
  command -v tmux >/dev/null 2>&1 || return 1
  tmux list-windows -t "$SESSION" -F '#W' 2>/dev/null | grep -Fx -- "$name" >/dev/null 2>&1
}

capture_agent_for_health() {
  local name="$1"
  if window_exists "$name"; then
    tmux capture-pane -t "$SESSION:$name" -p -S -300 2>/dev/null || true
  elif [[ -f "$(subagent_dir "$name")/current.txt" ]]; then
    cat "$(subagent_dir "$name")/current.txt"
  else
    true
  fi
}

classify_health_capture() {
  local name="$1"
  local capture="$2"
  if grep -Eiq '(rm[[:space:]]+-rf[[:space:]]+/|git[[:space:]]+push|gh[[:space:]]+pr[[:space:]]+create|submit PR|open PR|send external|exfiltrat|secret|credential|write outside|outside assigned|outside-owned-path|delete production|production database)' <<<"$capture"; then
    printf 'unsafe\n'
  elif grep -Eiq '\b(blocked|need input|waiting for|cannot proceed)\b' <<<"$capture"; then
    printf 'blocked\n'
  elif grep -Eiq '\b(done|complete|completed|final status|finished)\b' <<<"$capture"; then
    printf 'done\n'
  elif grep -Eiq '(not authenticated|authentication required|login required|setup required|trust this folder|do you trust)' <<<"$capture"; then
    printf 'blocked\n'
  elif window_exists "$name"; then
    printf 'working\n'
  elif [[ -n "$capture" ]]; then
    printf 'stale\n'
  else
    printf 'unknown\n'
  fi
}

health_action_for_status() {
  local status="$1"
  case "$status" in
    working) printf 'continue\n' ;;
    done) printf 'verify\n' ;;
    blocked) printf 'resolve-blocker\n' ;;
    misaligned|unsafe) printf 'kill-or-reassign\n' ;;
    stale|unknown) printf 'inspect\n' ;;
    *) printf 'inspect\n' ;;
  esac
}

health_check() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "health-check requires NAME"
  validate_name "$name"

  local capture status reason action check_output
  capture="$(capture_agent_for_health "$name")"
  status="$(classify_health_capture "$name" "$capture")"
  reason="capture-status-$status"

  if [[ -f "$(assignment_meta_file "$name")" ]]; then
    if ! check_output="$(assignment_check "$name" 2>&1)"; then
      if [[ "$status" != "unsafe" ]]; then
        status="misaligned"
        reason="$(awk -F'\t' '/^reject\t/ { print $2; exit }' <<<"$check_output")"
        [[ -n "$reason" ]] || reason="assignment-check-failed"
      fi
    elif [[ "$status" == "working" ]]; then
      reason="assignment-check-accepted"
    fi
  elif [[ "$status" == "working" || "$status" == "done" ]]; then
    status="unknown"
    reason="missing-assignment-metadata"
  fi

  action="$(health_action_for_status "$status")"
  printf 'health\t%s\t%s\n' "$name" "$status"
  printf 'action\t%s\n' "$action"
  printf 'reason\t%s\n' "$reason"
}

readiness_state() {
  local text="$1"
  if grep -Eiq '(not authenticated|authentication required|login required|sign in|setup required|api key required|failed to authenticate|claude login|log in to claude|not logged in|select theme|choose your setup|trust this folder|do you trust|press enter to continue)' <<<"$text"; then
    printf 'blocked\n'
  elif grep -Eiq '(codex prompt ready|claude prompt ready|prompt ready|restored codex prompt ready|restored claude prompt ready|what can i help|ready for input|type your message|claude code.*ready|bypass permissions mode|dangerously-skip-permissions)' <<<"$text"; then
    printf 'ready\n'
  else
    printf 'waiting\n'
  fi
}

wait_for_ready() {
  local name="$1"
  local attempts="${MULTIAGENT_READY_ATTEMPTS:-20}"
  local delay="${MULTIAGENT_READY_DELAY:-0.5}"
  local capture="" state i
  for ((i = 1; i <= attempts; i++)); do
    if capture="$(tmux capture-pane -t "$SESSION:$name" -p -S -200 2>&1)"; then
      state="$(readiness_state "$capture")"
      if [[ "$state" == "ready" ]]; then
        printf '%s\n' "$capture" >"$(subagent_dir "$name")/current.txt"
        return 0
      fi
      if [[ "$state" == "blocked" ]]; then
        printf '%s\n' "$capture" >"$(subagent_dir "$name")/last-error.txt"
        return 2
      fi
    fi
    sleep "$delay"
  done
  printf '%s\n' "${capture:-no capture available}" >"$(subagent_dir "$name")/last-error.txt"
  return 1
}

deliver_instruction() {
  local name="$1"
  local instruction="$2"
  local dir
  dir="$(subagent_dir "$name")"
  mkdir -p "$dir"
  if ! wait_for_ready "$name"; then
    set_status "$name" "delivery-blocked"
    die "subagent window is not ready for instruction delivery: $name; see $dir/last-error.txt"
  fi
  tmux send-keys -t "$SESSION:$name" "$instruction" Enter
  capture_subagent "$name" || true
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
  local cli bin
  cli="$SUBAGENT_CLI"
  bin="$(cli_bin "$cli")"
  require_cmd "$bin"
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
cli=$cli
cli_bin=$bin
created_at=$(timestamp)
EOF
  set_status "$name" "starting"

  local command
  printf -v command "cd %q && export MULTIAGENT_SESSION=%q MULTIAGENT_ROOT=%q MULTIAGENT_STATE_DIR=%q MULTIAGENT_WRITE_POLICY=%q MULTIAGENT_SUBAGENT_NAME=%q WORKER_CLI=%q SUBAGENT_CLI=%q VERIFIER_CLI=%q && %s" \
    "$ROOT" "$SESSION" "$ROOT" "$STATE_DIR" "$POLICY_FILE" "$name" "$WORKER_CLI" "$cli" "$VERIFIER_CLI" "$(build_cli_command "$cli" "$ROOT")"
  tmux new-window -d -t "$SESSION" -n "$name" "$command"
  set_status "$name" "running"

  capture_subagent "$name" || true
  if [[ -n "$instruction" ]]; then
    deliver_instruction "$name" "$instruction"
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

has_recovery_context() {
  local name="$1"
  local dir
  dir="$(subagent_dir "$name")"
  [[ -s "$dir/current.txt" || -s "$dir/transcript.log" ]]
}

recovery_text() {
  local name="$1"
  local dir
  dir="$(subagent_dir "$name")"

  {
    if [[ -s "$dir/current.txt" ]]; then
      printf 'Current pane tail:\n'
      tail -n 80 "$dir/current.txt"
    fi
    if [[ -s "$dir/transcript.log" ]]; then
      printf '\nTranscript tail:\n'
      tail -n 120 "$dir/transcript.log"
    fi
  } | tail -n 180
}

classify_recovery() {
  local name="$1"
  validate_name "$name"

  local dir status lowered current transcript combined action reason window checkpoint_status checkpoint_blocker
  dir="$(subagent_dir "$name")"
  status="$(get_status "$name")"
  lowered="$(printf '%s' "$status" | tr '[:upper:]' '[:lower:]')"
  current="$dir/current.txt"
  transcript="$dir/transcript.log"
  window="closed"

  if window_exists "$name"; then
    window="open"
    action="skip-open"
    reason="tmux-window-already-open"
  elif [[ ! -d "$dir" ]]; then
    action="skip-unknown"
    reason="missing-state-dir"
  elif [[ "$lowered" =~ ^(finalized|done|complete|completed)$ ]]; then
    action="skip-finalized"
    reason="status-$lowered"
  elif [[ "$lowered" =~ ^(killed|stopped|cancelled|canceled)$ ]]; then
    action="skip-finalized"
    reason="intentionally-stopped-$lowered"
  elif [[ -f "$(checkpoint_file "$name")" ]]; then
    checkpoint_status="$(read_checkpoint_value "$name" status || true)"
    checkpoint_blocker="$(read_checkpoint_value "$name" blocker || true)"
    checkpoint_status="$(printf '%s' "$checkpoint_status" | tr '[:upper:]' '[:lower:]')"
    if [[ -n "$checkpoint_blocker" || "$checkpoint_status" == "blocked" ]]; then
      action="skip-blocked"
      reason="checkpoint-blocked"
    elif [[ "$checkpoint_status" =~ ^(done|complete|completed|finalized)$ ]]; then
      action="skip-finalized"
      reason="checkpoint-$checkpoint_status"
    elif ! has_recovery_context "$name"; then
      action="skip-unknown"
      reason="checkpoint-without-captured-context"
    else
      action="restore"
      reason="checkpoint-resumable"
    fi
  else
    combined=""
    [[ -f "$current" ]] && combined="$combined"$'\n'"$(tail -n 120 "$current")"
    [[ -f "$transcript" ]] && combined="$combined"$'\n'"$(tail -n 160 "$transcript")"

    if [[ "$lowered" == "blocked" ]] || grep -Eiq '\b(blocked|need input|waiting for|cannot proceed)\b' <<<"$combined"; then
      action="skip-blocked"
      reason="requires-orchestrator-decision"
    elif grep -Eiq '\b(done|complete|completed|final status|finished)\b' <<<"$combined"; then
      action="skip-finalized"
      reason="context-looks-final"
    elif ! has_recovery_context "$name"; then
      action="skip-unknown"
      reason="no-current-or-transcript"
    elif [[ "$lowered" =~ ^(running|starting|exited|missing|restoring|unknown)$ ]]; then
      action="restore"
      reason="closed-with-recoverable-context"
    else
      action="skip-unknown"
      reason="unrecognized-status-$lowered"
    fi
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$name" "$action" "$reason" "$status" "$window" "$dir"
}

recover_plan() {
  local base="$STATE_DIR/subagents"
  printf 'NAME\tACTION\tREASON\tSTATUS\tWINDOW\tSTATE_DIR\n'
  [[ -d "$base" ]] || return 0

  local dir name
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"
    classify_recovery "$name"
  done
}

restore_instruction() {
  local name="$1"
  local prior_status="$2"
  local dir="$3"
  local context
  context="$(recovery_text "$name")"

  cat <<EOF
You are a restored long-running subagent.

Restoration details:
- Subagent name: $name
- Prior persisted status: $prior_status
- Persisted state directory: $dir
- This is a fresh tmux window after an orchestrator/session recovery.
- Do not delete, overwrite, or reset prior memory in the state directory.
- Read the prior context below, continue only if the assignment is still valid, and report progress/final status in this tmux window.
- If the prior state shows completion, intentional stop, stale instructions, or a blocker that needs orchestrator/user input, stop and state what you need instead of guessing.

Concise prior context:
$(printf '%s\n' "$context")
EOF
}

restore_subagent() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "restore requires NAME"
  validate_name "$name"
  shift

  local force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --force)
        force=1
        shift
        ;;
      *)
        die "unknown restore argument: $1"
        ;;
    esac
  done

  require_cmd tmux
  local cli bin
  cli="$(read_subagent_meta_value "$name" cli || printf '%s\n' "$SUBAGENT_CLI")"
  cli="$(normalize_cli "$cli")"
  bin="$(cli_bin "$cli")"
  require_cmd "$bin"
  tmux has-session -t "$SESSION" 2>/dev/null || die "missing tmux session: $SESSION"

  local dir plan action reason prior_status window
  dir="$(subagent_dir "$name")"
  [[ -d "$dir" ]] || die "no persisted subagent state: $name"

  plan="$(classify_recovery "$name")"
  IFS=$'\t' read -r _ action reason prior_status window _ <<<"$plan"
  if [[ "$action" != "restore" && "$force" -eq 0 ]]; then
    die "refusing to restore $name: $action ($reason); use --force only after an explicit orchestrator/user decision"
  fi
  [[ "$window" != "open" ]] || die "subagent window already exists: $name"
  has_recovery_context "$name" || die "no captured context to restore: $name"

  local instruction command
  instruction="$(restore_instruction "$name" "$prior_status" "$dir")"
  printf '%s\n' "$(timestamp) prior_status=$prior_status action=$action reason=$reason force=$force cli=$cli" >>"$dir/restore_events.log"
  {
    printf '\n----- restore seed %s -----\n' "$(timestamp)"
    printf '%s\n' "$instruction"
  } >>"$dir/transcript.log"
  set_status "$name" "restoring"

  printf -v command "cd %q && export MULTIAGENT_SESSION=%q MULTIAGENT_ROOT=%q MULTIAGENT_STATE_DIR=%q MULTIAGENT_WRITE_POLICY=%q MULTIAGENT_SUBAGENT_NAME=%q MULTIAGENT_SUBAGENT_RESTORED=1 WORKER_CLI=%q SUBAGENT_CLI=%q VERIFIER_CLI=%q && %s" \
    "$ROOT" "$SESSION" "$ROOT" "$STATE_DIR" "$POLICY_FILE" "$name" "$WORKER_CLI" "$cli" "$VERIFIER_CLI" "$(build_cli_command "$cli" "$ROOT")"
  tmux new-window -d -t "$SESSION" -n "$name" "$command"
  set_status "$name" "running"
  deliver_instruction "$name" "$instruction"

  printf 'restored %s\n' "$name"
}

restore_all() {
  local base="$STATE_DIR/subagents"
  [[ -d "$base" ]] || return 0

  local dir name plan action restored=0 skipped=0
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    name="$(basename "$dir")"
    plan="$(classify_recovery "$name")"
    IFS=$'\t' read -r _ action _ _ _ _ <<<"$plan"
    if [[ "$action" == "restore" ]]; then
      restore_subagent "$name"
      restored=$((restored + 1))
    else
      printf 'skipped %s\t%s\n' "$name" "$action"
      skipped=$((skipped + 1))
    fi
  done
  printf 'restore-all complete: restored=%s skipped=%s\n' "$restored" "$skipped"
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
  assignment-create)
    shift
    assignment_create "$@"
    ;;
  assignment-show)
    shift
    assignment_show "$@"
    ;;
  assignment-status)
    shift
    assignment_status "$@"
    ;;
  assignment-check)
    shift
    assignment_check "$@"
    ;;
  health-check)
    shift
    health_check "$@"
    ;;
  checkpoint-update)
    shift
    checkpoint_update "$@"
    ;;
  checkpoint-show)
    shift
    checkpoint_show "$@"
    ;;
  worktree-create)
    shift
    worktree_create "$@"
    ;;
  worktree-show)
    shift
    worktree_show "$@"
    ;;
  worktree-remove)
    shift
    worktree_remove "$@"
    ;;
  poll)
    shift
    poll_subagent "$@"
    ;;
  inspect)
    shift
    inspect_subagent "$@"
    ;;
  recover-plan)
    shift
    recover_plan "$@"
    ;;
  restore)
    shift
    restore_subagent "$@"
    ;;
  restore-all)
    shift
    restore_all "$@"
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
