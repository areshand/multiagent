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
MULTIAGENT_HELPER="${MULTIAGENT_HELPER:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")}"
MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER="${MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER:-1}"
if [[ -n "${MULTIAGENT_EXTRA_PATH:-}" ]]; then
  PATH="$MULTIAGENT_EXTRA_PATH:$PATH"
  export PATH
fi
if [[ "${CODEX_BIN:-codex}" == "codex" && -n "${MULTIAGENT_EXTRA_PATH:-}" && -x "$MULTIAGENT_EXTRA_PATH/codex-bridge" ]]; then
  CODEX_BIN="$MULTIAGENT_EXTRA_PATH/codex-bridge"
fi
if [[ "${CODEX_BIN:-codex}" == "codex" && -n "${MULTIAGENT_STATE_DIR:-}" && -x "$(dirname "$MULTIAGENT_STATE_DIR")/codex-bridge" ]]; then
  CODEX_BIN="$(dirname "$MULTIAGENT_STATE_DIR")/codex-bridge"
fi

usage() {
  cat <<'USAGE'
Usage:
  bin/subagent.sh spawn NAME [--instruction TEXT | --instruction-file PATH]
  bin/subagent.sh list
  bin/subagent.sh assignment-create NAME --assignment-id ID --branch BRANCH --owned PATH[,PATH...] [--status STATUS] [--start-commit COMMIT] [--role exploitation|exploration|reflection|architecture|qa|verifier|scout] [--decision-id DECISION_ID] [--plan-id PLAN_ID] [--workflow-id WORKFLOW_ID] [--node-id NODE_ID] [--depends-on NODE[,NODE...]]
  bin/subagent.sh assignment-show NAME
  bin/subagent.sh assignment-status NAME STATUS
  bin/subagent.sh assignment-check NAME
  bin/subagent.sh checkpoint-update NAME --step TEXT [--blocker TEXT] [--idempotency TEXT] [--last-commit COMMIT] [--status STATUS]
  bin/subagent.sh checkpoint-show NAME
  bin/subagent.sh worktree-create NAME [--branch BRANCH] [--path PATH]
  bin/subagent.sh worktree-show NAME
  bin/subagent.sh worktree-remove NAME [--force]
  bin/subagent.sh finding-create FINDING_ID --severity blocking|nonblocking|warning --type TYPE --summary TEXT --evidence-json JSON --required-resolution TEXT [--affected PATH[,PATH...]]
  bin/subagent.sh finding-show FINDING_ID
  bin/subagent.sh finding-list [--severity SEVERITY] [--type TYPE]
  bin/subagent.sh todo-create TODO_ID --source-finding-id FINDING_ID --task TEXT --done-criteria TEXT [--done-criteria TEXT ...] [--required-command CMD ...] [--context TEXT | --context-file PATH] [--assigned-to NAME]
  bin/subagent.sh todo-show TODO_ID
  bin/subagent.sh todo-list [--status STATUS]
  bin/subagent.sh todo-assign TODO_ID NAME
  bin/subagent.sh todo-status TODO_ID open|assigned|resolved|reopened|closed
  bin/subagent.sh resolution-create TODO_ID --worker NAME --status resolved|blocked --validation-json JSON --why TEXT [--changed PATH[,PATH...]]
  bin/subagent.sh todo-close TODO_ID --verified-by NAME --recheck-json JSON [--notes TEXT]
  bin/subagent.sh validation-lease-acquire LEASE_ID --owner NAME --target TEXT --command TEXT [--state planned|running] [--resource-risk TEXT]
  bin/subagent.sh validation-lease-status LEASE_ID planned|running|passed|failed|timed-out|stale|released [--result-json JSON]
  bin/subagent.sh validation-lease-show LEASE_ID
  bin/subagent.sh validation-lease-list [--state STATE]
  bin/subagent.sh validation-run LEASE_ID --owner NAME --target TEXT [--resource-risk TEXT] [--timeout-seconds N] -- COMMAND [ARG ...]
  bin/subagent.sh gate-check
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
  local prompt_file="${3:-}"
  local output_file="${4:-}"
  local bin
  bin="$(cli_bin "$cli")"
  case "$cli" in
    codex)
      if [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" ]]; then
        if [[ -n "$prompt_file" ]]; then
          if [[ -n "$output_file" ]]; then
            printf "%q exec --cd %q --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox --output-last-message %q - < %q" "$bin" "$cwd" "$output_file" "$prompt_file"
          else
            printf "%q exec --cd %q --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox - < %q" "$bin" "$cwd" "$prompt_file"
          fi
        else
          printf "%q exec --cd %q --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox" "$bin" "$cwd"
        fi
        return
      fi
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

codex_exec_protocol_prelude() {
  cat <<'EOF'
## Codex Exec Tool Protocol

You are running under `codex exec` in a benchmark container. When you need to run
a shell command, emit a normal Codex shell tool call with a JSON object that
contains a `cmd` string, for example:

{"cmd":"cd /app && sed -n '1,120p' lib/example.go"}

Do not emit raw command arrays, partial JSON, or prose pretending to be a tool
call. If a tool call fails with `missing field cmd`, immediately retry the same
operation as a shell tool call whose arguments include exactly one `cmd` string.

EOF
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
  [[ "$name" != -* ]] || die "invalid subagent name: $name"
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

finding_dir() {
  printf '%s/findings/%s\n' "$STATE_DIR" "$1"
}

finding_meta_file() {
  printf '%s/finding.env\n' "$(finding_dir "$1")"
}

todo_dir() {
  printf '%s/todos/%s\n' "$STATE_DIR" "$1"
}

todo_meta_file() {
  printf '%s/todo.env\n' "$(todo_dir "$1")"
}

todo_status_file() {
  printf '%s/status\n' "$(todo_dir "$1")"
}

todo_required_commands_file() {
  printf '%s/required-commands\n' "$(todo_dir "$1")"
}

validation_lease_dir() {
  printf '%s/validation-leases/%s\n' "$STATE_DIR" "$1"
}

validation_lease_meta_file() {
  printf '%s/lease.env\n' "$(validation_lease_dir "$1")"
}

validation_lease_status_file() {
  printf '%s/status\n' "$(validation_lease_dir "$1")"
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

write_csv_lines() {
  local csv="$1"
  local file="$2"
  local item trimmed
  : >"$file"
  [[ -n "$csv" ]] || return 0
  IFS=',' read -ra items <<<"$csv"
  for item in "${items[@]}"; do
    trimmed="${item#"${item%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ -n "$trimmed" ]] || continue
    reject_newline "csv item" "$trimmed"
    grep -Fx -- "$trimmed" "$file" >/dev/null 2>&1 || printf '%s\n' "$trimmed" >>"$file"
  done
}

append_unique_line() {
  local line="$1"
  local file="$2"
  [[ -n "$line" ]] || return 0
  reject_newline "line" "$line"
  grep -Fx -- "$line" "$file" >/dev/null 2>&1 || printf '%s\n' "$line" >>"$file"
}

sha256_file() {
  local file="$1"
  require_cmd python3
  python3 -c '
import hashlib
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
' "$file"
}

set_env_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  reject_newline "$key" "$value"
  tmp="$file.tmp.$$"
  awk -F= -v key="$key" -v value="$value" '
    $1 == key { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$file" >"$tmp"
  mv "$tmp" "$file"
}

read_env_value() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 1
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; found=1 } END { exit found ? 0 : 1 }' "$file"
}

read_finding_value() {
  local finding_id="$1"
  local key="$2"
  read_env_value "$(finding_meta_file "$finding_id")" "$key"
}

read_todo_value() {
  local todo_id="$1"
  local key="$2"
  read_env_value "$(todo_meta_file "$todo_id")" "$key"
}

get_todo_status() {
  local todo_id="$1"
  if [[ -f "$(todo_status_file "$todo_id")" ]]; then
    tr -d '\n' <"$(todo_status_file "$todo_id")"
  else
    printf 'unknown\n'
  fi
}

read_validation_lease_value() {
  local lease_id="$1"
  local key="$2"
  read_env_value "$(validation_lease_meta_file "$lease_id")" "$key"
}

get_validation_lease_status() {
  local lease_id="$1"
  if [[ -f "$(validation_lease_status_file "$lease_id")" ]]; then
    tr -d '\n' <"$(validation_lease_status_file "$lease_id")"
  else
    printf 'unknown\n'
  fi
}

validate_validation_lease_status() {
  local status="$1"
  case "$status" in
    planned|running|passed|failed|timed-out|stale|released)
      ;;
    *)
      die "invalid validation lease status: $status"
      ;;
  esac
}

set_todo_status() {
  local todo_id="$1"
  local status="$2"
  case "$status" in
    open|assigned|resolved|reopened|closed)
      ;;
    *)
      die "invalid todo status: $status"
      ;;
  esac
  [[ -f "$(todo_meta_file "$todo_id")" ]] || die "no todo: $todo_id"
  printf '%s\n' "$status" >"$(todo_status_file "$todo_id")"
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

status_is_active_worker() {
  local status="$1"
  case "$status" in
    starting|running|restoring)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

reject_parallel_generic_worker_spawn() {
  local new_name="$1"
  [[ "${MULTIAGENT_ALLOW_PARALLEL_WORKERS:-0}" != "1" ]] || return 0
  [[ "$new_name" == worker-* ]] || return 0

  local base="$STATE_DIR/subagents"
  [[ -d "$base" ]] || return 0

  local dir existing status
  for dir in "$base"/worker-*; do
    [[ -d "$dir" ]] || continue
    existing="$(basename "$dir")"
    [[ "$existing" != "$new_name" ]] || continue
    status="$(get_status "$existing")"
    status_is_active_worker "$status" || continue
    window_exists "$existing" || continue
    die "active generic worker already running: existing=$existing status=$status; wait, finalize/kill it, or set MULTIAGENT_ALLOW_PARALLEL_WORKERS=1 only with explicit disjoint ownership"
  done
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
  [[ -n "$rel" && "$rel" != "." && "$rel" != "$root" ]] || die "assigned path may not be the whole repo root"
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

paths_overlap() {
  local left="$1"
  local right="$2"
  [[ "$left" == "$right" || "$left" == "$right/"* || "$right" == "$left/"* ]]
}

assignment_status_is_terminal() {
  local status="$1"
  case "$status" in
    done|completed|closed|cancelled|canceled|failed|released|skipped)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

reject_active_assignment_overlap() {
  local new_name="$1"
  local new_owned_file="$2"
  local new_role="$3"
  case "$new_role" in
    verifier|scout)
      return 0
      ;;
  esac
  local base="$STATE_DIR/assignments"
  [[ -d "$base" ]] || return 0
  local dir existing existing_status existing_role existing_owned_file new_owned existing_owned
  while IFS= read -r new_owned; do
    [[ -n "$new_owned" ]] || continue
    for dir in "$base"/*; do
      [[ -d "$dir" ]] || continue
      existing="$(basename "$dir")"
      [[ "$existing" != "$new_name" ]] || continue
      [[ -f "$(assignment_meta_file "$existing")" && -f "$(assignment_status_file "$existing")" ]] || continue
      existing_status="$(get_assignment_status "$existing")"
      assignment_status_is_terminal "$existing_status" && continue
      existing_role="$(read_assignment_value "$existing" role || printf 'exploitation')"
      case "$existing_role" in
        verifier|scout)
          continue
          ;;
      esac
      existing_owned_file="$(assignment_owned_file "$existing")"
      [[ -f "$existing_owned_file" ]] || continue
      while IFS= read -r existing_owned; do
        [[ -n "$existing_owned" ]] || continue
        if paths_overlap "$new_owned" "$existing_owned"; then
          die "active assignment owned-path overlap: new=$new_name path=$new_owned existing=$existing status=$existing_status existing_path=$existing_owned"
        fi
      done <"$existing_owned_file"
    done
  done <"$new_owned_file"
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
        if [[ -n "$owned_csv" ]]; then
          owned_csv="$owned_csv,${2:-}"
        else
          owned_csv="${2:-}"
        fi
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
    exploitation|exploration|reflection|architecture|qa|verifier|scout)
      ;;
    *)
      die "invalid role '$role' (expected exploitation|exploration|reflection|architecture|qa|verifier|scout)"
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
  reject_active_assignment_overlap "$name" "$owned_file" "$role"

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
  local start_commit="$1"
  {
    git -C "$ROOT" diff --name-only "$start_commit"..HEAD
    git -C "$ROOT" diff --name-only
    git -C "$ROOT" diff --name-only --cached
    git -C "$ROOT" ls-files --others --exclude-standard
  } | sed '/^$/d' | sort -u
}

assignment_check() {
  local name="${1:-}"
  [[ -n "$name" ]] || die "assignment-check requires NAME"
  validate_name "$name"
  [[ -f "$(assignment_meta_file "$name")" ]] || die "no assignment for agent: $name"

  local expected_branch start_commit current_branch owned_file failed=0 changed
  expected_branch="$(read_assignment_value "$name" branch)"
  start_commit="$(read_assignment_value "$name" start_commit)"
  owned_file="$(assignment_owned_file "$name")"
  current_branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"

  printf 'assignment\t%s\t%s\n' "$name" "$(read_assignment_value "$name" assignment_id)"
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
  done < <(assignment_changed_files "$start_commit")

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

readiness_state() {
  local text="$1"
  if grep -Eiq '(not authenticated|authentication required|login required|sign in|setup required|api key required|failed to authenticate|claude login|log in to claude|not logged in|select theme|choose your setup|trust this folder|do you trust|press enter to continue)' <<<"$text"; then
    printf 'blocked\n'
  elif grep -Eiq '(codex prompt ready|claude prompt ready|prompt ready|restored codex prompt ready|restored claude prompt ready|what can i help|ready for input|type your message|claude code.*ready|bypass permissions mode|dangerously-skip-permissions|use /skills to list available skills|gpt-[0-9][^[:space:]]*[[:space:]]+default[[:space:]]+.)' <<<"$text"; then
    printf 'ready\n'
  else
    printf 'waiting\n'
  fi
}

looks_blocked_report() {
  local text="$1"
  grep -Eiq '^[[:space:]]*(blocked|blocker|need input|waiting for|cannot proceed)[[:space:]:.-]' <<<"$text" \
    || grep -Eiq '^[[:space:]]*final status:[[:space:]]*(blocked|needs input|cannot proceed)\b' <<<"$text" \
    || grep -Eiq '^[[:space:]]*status:[[:space:]]*(blocked|needs input|cannot proceed)\b' <<<"$text"
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
  if [[ "$instruction" == *$'\n'* || "${#instruction}" -gt 800 ]]; then
    printf '%s\n' "$instruction" >"$dir/instruction.txt"
    instruction="Read and follow the assignment in $dir/instruction.txt. Proceed now, then report progress and final status in this window."
  fi
  tmux send-keys -t "$SESSION:$name" "$instruction"
  sleep "${MULTIAGENT_DELIVERY_SUBMIT_DELAY:-0.2}"
  tmux send-keys -t "$SESSION:$name" C-m
  sleep "${MULTIAGENT_DELIVERY_SECOND_SUBMIT_DELAY:-0.8}"
  tmux send-keys -t "$SESSION:$name" C-m
  capture_subagent "$name" || true
}

capture_subagent() {
  local name="$1"
  local dir
  dir="$(subagent_dir "$name")"
  mkdir -p "$dir"

  local capture
  if ! capture="$(tmux capture-pane -t "$SESSION:$name" -p -S -1000 2>&1)"; then
    if capture_subagent_from_durable_files "$name" "$capture"; then
      return 0
    fi
    printf '%s\n' "$capture" >"$dir/last-error.txt"
    return 1
  fi

  printf '%s\n' "$capture" >"$dir/current.txt"
  {
    printf '\n----- capture %s -----\n' "$(timestamp)"
    printf '%s\n' "$capture"
  } >>"$dir/transcript.log"
}

capture_subagent_from_durable_files() {
  local name="$1"
  local capture_error="$2"
  local dir last_message transcript current tmp
  dir="$(subagent_dir "$name")"
  last_message="$dir/last-message.txt"
  transcript="$dir/transcript.log"
  current="$dir/current.txt"
  tmp="$dir/current.txt.tmp.$$"

  [[ -s "$last_message" || -s "$transcript" ]] || return 1

  {
    printf 'tmux capture unavailable for %s; recovered durable subagent output.\n' "$name"
    printf 'tmux-capture-error: %s\n' "$capture_error"
    if [[ -s "$last_message" ]]; then
      printf '\n----- last-message.txt -----\n'
      cat "$last_message"
    fi
    if [[ -s "$transcript" ]]; then
      printf '\n----- transcript tail -----\n'
      tail -n 240 "$transcript"
    fi
  } >"$tmp"

  mv "$tmp" "$current"
  {
    printf '\n----- durable capture %s -----\n' "$(timestamp)"
    cat "$current"
  } >>"$transcript"
}

infer_status() {
  local name="$1"
  local current
  current="$(subagent_dir "$name")/current.txt"
  if [[ ! -f "$current" ]]; then
    printf 'unknown\n'
    return
  fi

  if grep -Eiq 'final status: codex exec exited rc=[1-9][0-9]*|warning: no last agent message' "$current"; then
    printf 'failed\n'
  elif looks_blocked_report "$(tail -n 160 "$current")"; then
    printf 'blocked\n'
  elif grep -Eiq '^[[:space:]]*(final status:|complete_task\b|assignment complete\b|task complete\b|finished assignment\b|work completed\b|done with\b)|Worked for [0-9]' "$current"; then
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

  local instruction="" instruction_file=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --instruction)
        instruction="${2:-}"
        shift 2
        ;;
      --instruction-file)
        instruction_file="${2:-}"
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
  if [[ -n "$instruction" && -n "$instruction_file" ]]; then
    die "spawn accepts only one of --instruction or --instruction-file"
  fi
  if [[ -n "$instruction_file" ]]; then
    [[ -f "$instruction_file" ]] || die "instruction file not found: $instruction_file"
    instruction="$(cat "$instruction_file")"
  fi

  require_cmd tmux
  local cli bin
  cli="$SUBAGENT_CLI"
  bin="$(cli_bin "$cli")"
  require_cmd "$bin"
  tmux has-session -t "$SESSION" 2>/dev/null || die "missing tmux session: $SESSION"
  window_exists "$name" && die "subagent window already exists: $name"
  reject_parallel_generic_worker_spawn "$name"
  if [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" && "$cli" == "codex" && -z "$instruction" ]]; then
    die "codex exec subagent spawn requires --instruction or --instruction-file: $name"
  fi

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
helper=$MULTIAGENT_HELPER
created_at=$(timestamp)
EOF
  set_status "$name" "starting"

  local command prompt_file output_file
  prompt_file=""
  output_file="$dir/last-message.txt"
  if [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" && "$cli" == "codex" && -n "$instruction" ]]; then
    prompt_file="$dir/instruction.txt"
    {
      codex_exec_protocol_prelude
      printf '%s\n' "$instruction"
    } >"$prompt_file"
    {
      printf '\n----- instruction %s -----\n' "$(timestamp)"
      cat "$prompt_file"
    } >>"$dir/transcript.log"
  fi
  printf -v command "cd %q && export MULTIAGENT_SESSION=%q MULTIAGENT_ROOT=%q MULTIAGENT_STATE_DIR=%q MULTIAGENT_WRITE_POLICY=%q MULTIAGENT_SUBAGENT_NAME=%q MULTIAGENT_HELPER=%q WORKER_CLI=%q SUBAGENT_CLI=%q VERIFIER_CLI=%q CODEX_BIN=%q CLAUDE_BIN=%q MULTIAGENT_CODEX_EXEC=%q PATH=%q && %s; rc=\$?; printf '\\nfinal status: codex exec exited rc=%%s\\n' \$rc; sleep infinity" \
    "$ROOT" "$SESSION" "$ROOT" "$STATE_DIR" "$POLICY_FILE" "$name" "$MULTIAGENT_HELPER" "$WORKER_CLI" "$cli" "$VERIFIER_CLI" "$CODEX_BIN" "$CLAUDE_BIN" "${MULTIAGENT_CODEX_EXEC:-0}" "$PATH" "$(build_cli_command "$cli" "$ROOT" "$prompt_file" "$output_file")"
  tmux new-window -d -t "$SESSION" -n "$name" "$command"
  set_status "$name" "running"

  capture_subagent "$name" || true
  if [[ -n "$instruction" && ! ( "${MULTIAGENT_CODEX_EXEC:-0}" == "1" && "$cli" == "codex" ) ]]; then
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

    if [[ "$lowered" == "blocked" ]] || looks_blocked_report "$combined"; then
      action="skip-blocked"
      reason="requires-orchestrator-decision"
    elif grep -Eiq '^[[:space:]]*(final status:|complete_task\b|assignment complete\b|task complete\b|finished assignment\b|work completed\b|done with\b)|Worked for [0-9]' <<<"$combined"; then
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

  local prompt_file output_file
  prompt_file=""
  output_file="$dir/last-message.txt"
  if [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" && "$cli" == "codex" ]]; then
    prompt_file="$dir/restore-instruction.txt"
    printf '%s\n' "$instruction" >"$prompt_file"
  fi
  printf -v command "cd %q && export MULTIAGENT_SESSION=%q MULTIAGENT_ROOT=%q MULTIAGENT_STATE_DIR=%q MULTIAGENT_WRITE_POLICY=%q MULTIAGENT_SUBAGENT_NAME=%q MULTIAGENT_HELPER=%q MULTIAGENT_SUBAGENT_RESTORED=1 WORKER_CLI=%q SUBAGENT_CLI=%q VERIFIER_CLI=%q CODEX_BIN=%q CLAUDE_BIN=%q MULTIAGENT_CODEX_EXEC=%q PATH=%q && %s; rc=\$?; printf '\\nfinal status: codex exec exited rc=%%s\\n' \$rc; sleep infinity" \
    "$ROOT" "$SESSION" "$ROOT" "$STATE_DIR" "$POLICY_FILE" "$name" "$MULTIAGENT_HELPER" "$WORKER_CLI" "$cli" "$VERIFIER_CLI" "$CODEX_BIN" "$CLAUDE_BIN" "${MULTIAGENT_CODEX_EXEC:-0}" "$PATH" "$(build_cli_command "$cli" "$ROOT" "$prompt_file" "$output_file")"
  tmux new-window -d -t "$SESSION" -n "$name" "$command"
  set_status "$name" "running"
  if ! [[ "${MULTIAGENT_CODEX_EXEC:-0}" == "1" && "$cli" == "codex" ]]; then
    deliver_instruction "$name" "$instruction"
  fi

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
  if [[ -f "$(assignment_meta_file "$name")" ]]; then
    set_assignment_status "$name" "done"
  fi
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
  if [[ -f "$(assignment_meta_file "$name")" ]]; then
    set_assignment_status "$name" "failed"
  fi
  printf 'killed %s\n' "$name"
}

write_finding_json() {
  local finding_id="$1"
  local dir
  dir="$(finding_dir "$finding_id")"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
meta = {}
for line in (root / "finding.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        meta[key] = value
affected_file = root / "affected-paths"
affected = [line for line in affected_file.read_text().splitlines() if line] if affected_file.exists() else []
with (root / "evidence.json").open() as fh:
    evidence = json.load(fh)
payload = {
    "id": meta["finding_id"],
    "severity": meta["severity"],
    "type": meta["type"],
    "summary": meta["summary"],
    "affected_paths": affected,
    "evidence": evidence,
    "required_resolution": meta["required_resolution"],
    "created_at": meta["created_at"],
}
(root / "finding.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
' "$dir"
}

write_todo_json() {
  local todo_id="$1"
  local dir
  dir="$(todo_dir "$todo_id")"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
status = sys.argv[2]
meta = {}
for line in (root / "todo.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        meta[key] = value
done_file = root / "done-criteria"
done_criteria = [line for line in done_file.read_text().splitlines() if line] if done_file.exists() else []
required_file = root / "required-commands"
required_commands = [line for line in required_file.read_text().splitlines() if line] if required_file.exists() else []
context_file = root / "context.txt"
context = context_file.read_text() if context_file.exists() else ""
payload = {
    "todo_id": meta["todo_id"],
    "source_finding_id": meta["source_finding_id"],
    "source_finding_hash": meta.get("source_finding_hash") or None,
    "assigned_to": meta.get("assigned_to") or None,
    "status": status,
    "task": meta["task"],
    "context": context,
    "done_criteria": done_criteria,
    "required_commands": required_commands,
    "created_at": meta["created_at"],
    "updated_at": meta.get("updated_at", meta["created_at"]),
}
(root / "todo.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
' "$dir" "$(get_todo_status "$todo_id")"
}

write_resolution_json() {
  local todo_id="$1"
  local dir
  dir="$(todo_dir "$todo_id")"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
meta = {}
for line in (root / "resolution.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        meta[key] = value
changed_file = root / "changed-paths"
changed = [line for line in changed_file.read_text().splitlines() if line] if changed_file.exists() else []
with (root / "validation.json").open() as fh:
    validation = json.load(fh)
payload = {
    "todo_id": meta["todo_id"],
    "status": meta["status"],
    "worker": meta["worker"],
    "changed_paths": changed,
    "validation": validation,
    "why_resolved": meta["why_resolved"],
    "created_at": meta["created_at"],
}
(root / "resolution.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
' "$dir"
}

write_closure_json() {
  local todo_id="$1"
  local dir
  dir="$(todo_dir "$todo_id")"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
meta = {}
for line in (root / "closure.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        meta[key] = value
with (root / "recheck.json").open() as fh:
    recheck = json.load(fh)
payload = {
    "todo_id": meta["todo_id"],
    "source_finding_id": meta["source_finding_id"],
    "source_finding_hash": meta.get("source_finding_hash") or None,
    "verified_by": meta["verified_by"],
    "recheck": recheck,
    "notes": meta.get("notes", ""),
    "created_at": meta["created_at"],
}
(root / "closure.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
' "$dir"
}

validate_finding_evidence_payload() {
  local severity="$1"
  local type="$2"
  local evidence_json="$3"
  require_cmd python3
  python3 -c '
import json
import sys

severity, finding_type, raw = sys.argv[1:4]
try:
    payload = json.loads(raw)
except Exception as exc:
    raise SystemExit(f"invalid evidence JSON: {exc}")
if not isinstance(payload, dict):
    raise SystemExit("evidence JSON must be an object")
if not payload:
    raise SystemExit("evidence JSON must be non-empty")

has_command = bool(str(payload.get("command") or payload.get("cmd") or "").strip())
has_rc = "returncode" in payload or "rc" in payload
has_source = any(
    str(payload.get(key, "")).strip()
    for key in ("source_evidence", "source_reasoning", "evidence", "stderr_excerpt", "stdout_excerpt")
)
if severity == "blocking" and not ((has_command and has_rc) or has_source):
    raise SystemExit("blocking finding evidence needs command+returncode or source evidence")
if has_rc:
    rc = payload.get("returncode", payload.get("rc"))
    try:
        int(rc)
    except Exception:
        raise SystemExit("finding evidence returncode/rc must be an integer")

command_required_types = {
    "compile_failure",
    "build_failure",
    "test_failure",
    "validation_failure",
}
if severity == "blocking" and finding_type in command_required_types and not (has_command and has_rc):
    raise SystemExit(f"{finding_type} finding evidence requires command and returncode")
' "$severity" "$type" "$evidence_json"
}

validate_resolution_payload() {
  local status="$1"
  local validation_json="$2"
  require_cmd python3
  python3 -c '
import json
import sys
status = sys.argv[1]
raw = sys.argv[2]
try:
    payload = json.loads(raw)
except Exception as exc:
    raise SystemExit(f"invalid validation JSON: {exc}")
if not isinstance(payload, list) or not payload:
    raise SystemExit("validation JSON must be a non-empty array")
for idx, item in enumerate(payload):
    if not isinstance(item, dict):
        raise SystemExit(f"validation item {idx} must be an object")
    has_command = bool(str(item.get("cmd", "")).strip())
    has_rc = "rc" in item
    has_source = any(str(item.get(key, "")).strip() for key in ("source_reasoning", "source_evidence", "evidence"))
    if not ((has_command and has_rc) or has_source):
        raise SystemExit(f"validation item {idx} needs cmd+rc or source evidence")
    if has_rc:
        try:
            rc = int(item["rc"])
        except Exception:
            raise SystemExit(f"validation item {idx} rc must be an integer")
        if status == "resolved" and rc != 0:
            raise SystemExit(f"resolved validation item {idx} has nonzero rc={rc}")
' "$status" "$validation_json"
}

json_command_strings() {
  local payload_json="$1"
  require_cmd python3
  python3 -c '
import json
import sys
payload = json.loads(sys.argv[1])
if isinstance(payload, dict):
    items = payload.get("commands") or payload.get("validation") or []
else:
    items = payload
if not isinstance(items, list):
    items = []
for item in items:
    if not isinstance(item, dict):
        continue
    rc = item.get("rc", item.get("returncode", 0))
    try:
        rc = int(rc)
    except Exception:
        continue
    if rc != 0:
        continue
    cmd = str(item.get("cmd") or item.get("command_text") or "").strip()
    if not cmd and isinstance(item.get("command"), list):
        cmd = " ".join(str(part) for part in item["command"]).strip()
    if cmd:
        print(" ".join(cmd.split()))
' "$payload_json"
}

validate_required_commands_covered() {
  local todo_id="$1"
  local label="$2"
  local payload_json="$3"
  local required_file command normalized found
  required_file="$(todo_required_commands_file "$todo_id")"
  [[ -f "$required_file" ]] || return 0
  mapfile -t covered < <(json_command_strings "$payload_json")
  while IFS= read -r command; do
    [[ -n "$command" ]] || continue
    normalized="$(printf '%s\n' "$command" | awk '{$1=$1; print}')"
    found=0
    local covered_command
    for covered_command in "${covered[@]}"; do
      if [[ "$covered_command" == "$normalized" ]]; then
        found=1
        break
      fi
    done
    if [[ "$found" -eq 0 ]]; then
      die "$label for todo $todo_id missing required command: $command"
    fi
  done <"$required_file"
}

validate_closure_payload() {
  local recheck_json="$1"
  require_cmd python3
  python3 -c '
import json
import sys
raw = sys.argv[1]
try:
    payload = json.loads(raw)
except Exception as exc:
    raise SystemExit(f"invalid recheck JSON: {exc}")
if not isinstance(payload, dict):
    raise SystemExit("recheck JSON must be an object")
if payload.get("accepted") is not True:
    raise SystemExit("recheck JSON must include accepted=true")
if not any(key in payload for key in ("finding_rechecked", "source_finding_id", "commands", "evidence", "final_diff_hash")):
    raise SystemExit("recheck JSON must name the finding, commands, evidence, or final diff hash")
commands = payload.get("commands", [])
if commands is None:
    commands = []
if not isinstance(commands, list):
    raise SystemExit("recheck commands must be an array when present")
for idx, item in enumerate(commands):
    if not isinstance(item, dict):
        raise SystemExit(f"recheck command {idx} must be an object")
    if not str(item.get("cmd", "")).strip():
        raise SystemExit(f"recheck command {idx} missing cmd")
    if "rc" not in item:
        raise SystemExit(f"recheck command {idx} missing rc")
    try:
        rc = int(item["rc"])
    except Exception:
        raise SystemExit(f"recheck command {idx} rc must be an integer")
    if rc != 0:
        raise SystemExit(f"recheck command {idx} has nonzero rc={rc}")
' "$recheck_json"
}

validate_closure_matches_todo() {
  local todo_id="$1"
  local source_finding_id="$2"
  local source_finding_hash="$3"
  local resolution_json="$4"
  local recheck_json="$5"
  require_cmd python3
  python3 -c '
import json
import sys

todo_id = sys.argv[1]
source_finding_id = sys.argv[2]
source_finding_hash = sys.argv[3]
resolution = json.loads(sys.argv[4])
recheck = json.loads(sys.argv[5])

finding_keys = [
    str(recheck.get(key, "")).strip()
    for key in ("finding_rechecked", "source_finding_id")
    if str(recheck.get(key, "")).strip()
]
if source_finding_id not in finding_keys:
    raise SystemExit(
        f"recheck JSON for todo {todo_id} must name source finding {source_finding_id}"
    )
recheck_hash = str(recheck.get("source_finding_hash", "")).strip()
if recheck_hash and recheck_hash != source_finding_hash:
    raise SystemExit(
        f"recheck JSON for todo {todo_id} must match source finding hash {source_finding_hash}"
    )

resolution_commands = {
    str(item.get("cmd", "")).strip()
    for item in resolution.get("validation", [])
    if isinstance(item, dict) and str(item.get("cmd", "")).strip() and int(item.get("rc", 0)) == 0
}
recheck_commands = {
    str(item.get("cmd", "")).strip()
    for item in recheck.get("commands", [])
    if isinstance(item, dict) and str(item.get("cmd", "")).strip() and int(item.get("rc", 1)) == 0
}
missing = sorted(resolution_commands - recheck_commands)
if missing:
    joined = ", ".join(missing)
    raise SystemExit(
        f"recheck JSON for todo {todo_id} must cover worker validation command(s): {joined}"
    )
' "$todo_id" "$source_finding_id" "$source_finding_hash" "$resolution_json" "$recheck_json"
}

finding_create() {
  local finding_id="${1:-}"
  [[ -n "$finding_id" ]] || die "finding-create requires FINDING_ID"
  validate_name "$finding_id"
  shift

  local severity="" type="" summary="" evidence_json="" required_resolution="" affected_csv=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --severity)
        severity="${2:-}"
        shift 2
        ;;
      --type)
        type="${2:-}"
        shift 2
        ;;
      --summary)
        summary="${2:-}"
        shift 2
        ;;
      --evidence-json)
        evidence_json="${2:-}"
        shift 2
        ;;
      --required-resolution)
        required_resolution="${2:-}"
        shift 2
        ;;
      --affected)
        affected_csv="${2:-}"
        shift 2
        ;;
      *)
        die "unknown finding-create argument: $1"
        ;;
    esac
  done

  case "$severity" in
    blocking|nonblocking|warning)
      ;;
    *)
      die "invalid finding severity: $severity"
      ;;
  esac
  [[ -n "$type" ]] || die "finding-create requires --type TYPE"
  [[ -n "$summary" ]] || die "finding-create requires --summary TEXT"
  [[ -n "$evidence_json" ]] || die "finding-create requires --evidence-json JSON"
  [[ -n "$required_resolution" ]] || die "finding-create requires --required-resolution TEXT"
  reject_newline "--type" "$type"
  reject_newline "--summary" "$summary"
  reject_newline "--required-resolution" "$required_resolution"
  validate_finding_evidence_payload "$severity" "$type" "$evidence_json"

  local dir
  dir="$(finding_dir "$finding_id")"
  [[ ! -e "$dir" ]] || die "finding already exists: $finding_id"
  mkdir -p "$dir"
  cat >"$(finding_meta_file "$finding_id")" <<EOF
finding_id=$finding_id
severity=$severity
type=$type
summary=$summary
required_resolution=$required_resolution
created_at=$(timestamp)
root=$ROOT
EOF
  printf '%s\n' "$evidence_json" >"$dir/evidence.json"
  write_csv_lines "$affected_csv" "$dir/affected-paths"
  write_finding_json "$finding_id"
  printf 'finding created\t%s\t%s\t%s\n' "$finding_id" "$severity" "$type"
}

finding_show() {
  local finding_id="${1:-}"
  [[ -n "$finding_id" ]] || die "finding-show requires FINDING_ID"
  validate_name "$finding_id"
  [[ -f "$(finding_dir "$finding_id")/finding.json" ]] || die "no finding: $finding_id"
  cat "$(finding_dir "$finding_id")/finding.json"
}

finding_list() {
  local severity_filter="" type_filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --severity)
        severity_filter="${2:-}"
        shift 2
        ;;
      --type)
        type_filter="${2:-}"
        shift 2
        ;;
      *)
        die "unknown finding-list argument: $1"
        ;;
    esac
  done

  local base="$STATE_DIR/findings"
  [[ -d "$base" ]] || return 0
  local dir id severity type summary
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    id="$(basename "$dir")"
    severity="$(read_finding_value "$id" severity || true)"
    type="$(read_finding_value "$id" type || true)"
    summary="$(read_finding_value "$id" summary || true)"
    [[ -z "$severity_filter" || "$severity" == "$severity_filter" ]] || continue
    [[ -z "$type_filter" || "$type" == "$type_filter" ]] || continue
    printf '%s\t%s\t%s\t%s\n' "$id" "$severity" "$type" "$summary"
  done
}

todo_create() {
  local todo_id="${1:-}"
  [[ -n "$todo_id" ]] || die "todo-create requires TODO_ID"
  validate_name "$todo_id"
  shift

  local source_finding_id="" task="" context="" context_file="" assigned_to="" done_joined="" required_commands_joined="" criterion required_command
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --source-finding-id)
        source_finding_id="${2:-}"
        shift 2
        ;;
      --task)
        task="${2:-}"
        shift 2
        ;;
      --done-criteria)
        criterion="${2:-}"
        reject_newline "--done-criteria" "$criterion"
        done_joined="${done_joined}${criterion}"$'\n'
        if [[ "$criterion" == run\ * ]]; then
          required_command="${criterion#run }"
          required_command="${required_command#"${required_command%%[![:space:]]*}"}"
          required_command="${required_command%"${required_command##*[![:space:]]}"}"
          [[ -n "$required_command" ]] && required_commands_joined="${required_commands_joined}${required_command}"$'\n'
        fi
        shift 2
        ;;
      --required-command)
        required_command="${2:-}"
        reject_newline "--required-command" "$required_command"
        [[ -n "$required_command" ]] || die "todo-create --required-command may not be empty"
        required_commands_joined="${required_commands_joined}${required_command}"$'\n'
        shift 2
        ;;
      --context)
        context="${2:-}"
        shift 2
        ;;
      --context-file)
        context_file="${2:-}"
        shift 2
        ;;
      --assigned-to)
        assigned_to="${2:-}"
        shift 2
        ;;
      *)
        die "unknown todo-create argument: $1"
        ;;
    esac
  done

  [[ -n "$source_finding_id" ]] || die "todo-create requires --source-finding-id FINDING_ID"
  validate_name "$source_finding_id"
  [[ -f "$(finding_meta_file "$source_finding_id")" ]] || die "no finding: $source_finding_id"
  [[ -n "$task" ]] || die "todo-create requires --task TEXT"
  [[ -n "$done_joined" ]] || die "todo-create requires at least one --done-criteria TEXT"
  [[ -z "$context" || -z "$context_file" ]] || die "todo-create accepts only one of --context or --context-file"
  [[ -z "$context_file" || -f "$context_file" ]] || die "context file not found: $context_file"
  reject_newline "--task" "$task"
  if [[ -n "$assigned_to" ]]; then
    validate_name "$assigned_to"
  fi

  local dir status source_finding_hash
  dir="$(todo_dir "$todo_id")"
  [[ ! -e "$dir" ]] || die "todo already exists: $todo_id"
  mkdir -p "$dir"
  status="open"
  [[ -n "$assigned_to" ]] && status="assigned"
  source_finding_hash="$(sha256_file "$(finding_dir "$source_finding_id")/finding.json")"
  cat >"$(todo_meta_file "$todo_id")" <<EOF
todo_id=$todo_id
source_finding_id=$source_finding_id
source_finding_hash=$source_finding_hash
assigned_to=$assigned_to
task=$task
created_at=$(timestamp)
updated_at=$(timestamp)
root=$ROOT
EOF
  printf '%s' "$done_joined" >"$dir/done-criteria"
  : >"$(todo_required_commands_file "$todo_id")"
  while IFS= read -r required_command; do
    append_unique_line "$required_command" "$(todo_required_commands_file "$todo_id")"
  done <<<"$required_commands_joined"
  if [[ -n "$context_file" ]]; then
    cp "$context_file" "$dir/context.txt"
  else
    printf '%s\n' "$context" >"$dir/context.txt"
  fi
  set_todo_status "$todo_id" "$status"
  write_todo_json "$todo_id"
  printf 'todo created\t%s\t%s\t%s\n' "$todo_id" "$source_finding_id" "$status"
}

todo_show() {
  local todo_id="${1:-}"
  [[ -n "$todo_id" ]] || die "todo-show requires TODO_ID"
  validate_name "$todo_id"
  [[ -f "$(todo_dir "$todo_id")/todo.json" ]] || die "no todo: $todo_id"
  write_todo_json "$todo_id"
  cat "$(todo_dir "$todo_id")/todo.json"
}

todo_list() {
  local status_filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --status)
        status_filter="${2:-}"
        shift 2
        ;;
      *)
        die "unknown todo-list argument: $1"
        ;;
    esac
  done

  local base="$STATE_DIR/todos"
  [[ -d "$base" ]] || return 0
  local dir id status source_finding_id assigned_to task
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    id="$(basename "$dir")"
    status="$(get_todo_status "$id")"
    [[ -z "$status_filter" || "$status" == "$status_filter" ]] || continue
    source_finding_id="$(read_todo_value "$id" source_finding_id || true)"
    assigned_to="$(read_todo_value "$id" assigned_to || true)"
    task="$(read_todo_value "$id" task || true)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$id" "$status" "$source_finding_id" "${assigned_to:--}" "$task"
  done
}

todo_assign() {
  local todo_id="${1:-}"
  local assigned_to="${2:-}"
  [[ -n "$todo_id" && -n "$assigned_to" ]] || die "todo-assign requires TODO_ID NAME"
  validate_name "$todo_id"
  validate_name "$assigned_to"
  [[ -f "$(todo_meta_file "$todo_id")" ]] || die "no todo: $todo_id"
  set_env_key "$(todo_meta_file "$todo_id")" assigned_to "$assigned_to"
  set_env_key "$(todo_meta_file "$todo_id")" updated_at "$(timestamp)"
  set_todo_status "$todo_id" "assigned"
  write_todo_json "$todo_id"
  printf 'todo assigned\t%s\t%s\n' "$todo_id" "$assigned_to"
}

todo_status() {
  local todo_id="${1:-}"
  local status="${2:-}"
  [[ -n "$todo_id" && -n "$status" ]] || die "todo-status requires TODO_ID STATUS"
  validate_name "$todo_id"
  [[ -f "$(todo_meta_file "$todo_id")" ]] || die "no todo: $todo_id"
  case "$status" in
    open|assigned|resolved|reopened|closed)
      ;;
    *)
      die "invalid todo status: $status"
      ;;
  esac
  set_env_key "$(todo_meta_file "$todo_id")" updated_at "$(timestamp)"
  set_todo_status "$todo_id" "$status"
  write_todo_json "$todo_id"
  printf 'todo status\t%s\t%s\n' "$todo_id" "$status"
}

resolution_create() {
  local todo_id="${1:-}"
  local legacy_mode=0 legacy_summary="" legacy_evidence=""
  if [[ -n "$todo_id" && "$todo_id" == --* ]]; then
    todo_id=""
  else
    [[ -n "$todo_id" ]] || die "resolution-create requires TODO_ID"
    validate_name "$todo_id"
    shift
  fi

  local worker="" status="" validation_json="" why="" changed_csv=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --todo)
        todo_id="${2:-}"
        legacy_mode=1
        shift 2
        ;;
      --worker)
        worker="${2:-}"
        shift 2
        ;;
      --owner)
        worker="${2:-}"
        legacy_mode=1
        shift 2
        ;;
      --status)
        status="${2:-}"
        shift 2
        ;;
      --validation-json)
        validation_json="${2:-}"
        shift 2
        ;;
      --why)
        why="${2:-}"
        shift 2
        ;;
      --summary)
        legacy_summary="${2:-}"
        [[ -z "$why" ]] && why="${2:-}"
        legacy_mode=1
        shift 2
        ;;
      --evidence)
        legacy_evidence="${2:-}"
        legacy_mode=1
        shift 2
        ;;
      --changed)
        changed_csv="${2:-}"
        shift 2
        ;;
      *)
        die "unknown resolution-create argument: $1"
        ;;
    esac
  done

  [[ -n "$todo_id" ]] || die "resolution-create requires TODO_ID"
  validate_name "$todo_id"
  if [[ "$legacy_mode" -eq 1 ]]; then
    [[ -n "$status" ]] || status="resolved"
    if [[ -z "$validation_json" && -n "$legacy_evidence" ]]; then
      validation_json="$(python3 -c '
import json
import re
import sys
text = sys.argv[1]
items = []
match = re.search(r"(go\s+test(?:\s+[^;,\n]+)*?)\s+returncode\s*=\s*(-?\d+)", text)
if match:
    items.append({"cmd": " ".join(match.group(1).split()), "rc": int(match.group(2)), "evidence": text})
else:
    items.append({"source_evidence": text})
print(json.dumps(items, separators=(",", ":")))
' "$legacy_evidence")"
    fi
    if [[ -z "$why" ]]; then
      why="${legacy_summary:-legacy resolution evidence recorded}"
    fi
  fi
  if [[ ! -f "$(todo_meta_file "$todo_id")" && "${MULTIAGENT_RESOLUTION_AUTOCREATE_TODO:-0}" == "1" ]]; then
    local auto_finding_id="auto-${todo_id}"
    if [[ ! -f "$(finding_meta_file "$auto_finding_id")" ]]; then
      local auto_evidence
      auto_evidence="$(python3 -c 'import json,sys; print(json.dumps({"source":"resolution-create-autocreate","evidence":sys.argv[1]}))' "${legacy_evidence:-$why}")"
      finding_create "$auto_finding_id" \
        --severity blocking \
        --type worker_resolution_without_registered_todo \
        --summary "Worker recorded a resolution for an unregistered todo." \
        --evidence-json "$auto_evidence" \
        --required-resolution "Create durable todo state before assigning worker repairs; verifier must close the todo after rechecking the worker resolution."
    fi >/dev/null
    todo_create "$todo_id" \
      --source-finding-id "$auto_finding_id" \
      --task "${legacy_summary:-Record and verify worker resolution evidence.}" \
      --context "${legacy_evidence:-$why}" \
      --done-criteria "worker records structured resolution evidence" \
      --done-criteria "verifier closes todo only after objective recheck" >/dev/null
  fi
  [[ -f "$(todo_meta_file "$todo_id")" ]] || die "no todo: $todo_id"
  [[ -n "$worker" ]] || die "resolution-create requires --worker NAME"
  validate_name "$worker"
  case "$status" in
    resolved|blocked)
      ;;
    *)
      die "invalid resolution status: $status"
      ;;
  esac
  [[ -n "$validation_json" ]] || die "resolution-create requires --validation-json JSON"
  [[ -n "$why" ]] || die "resolution-create requires --why TEXT"
  reject_newline "--why" "$why"
  validate_resolution_payload "$status" "$validation_json"
  if [[ "$status" == "resolved" ]]; then
    validate_required_commands_covered "$todo_id" "worker resolution" "$validation_json"
  fi

  local dir
  dir="$(todo_dir "$todo_id")"
  cat >"$dir/resolution.env" <<EOF
todo_id=$todo_id
status=$status
worker=$worker
why_resolved=$why
created_at=$(timestamp)
EOF
  printf '%s\n' "$validation_json" >"$dir/validation.json"
  write_csv_lines "$changed_csv" "$dir/changed-paths"
  write_resolution_json "$todo_id"
  if [[ "$status" == "resolved" ]]; then
    set_todo_status "$todo_id" "resolved"
  else
    set_todo_status "$todo_id" "reopened"
  fi
  set_env_key "$(todo_meta_file "$todo_id")" updated_at "$(timestamp)"
  write_todo_json "$todo_id"
  printf 'resolution recorded\t%s\t%s\t%s\n' "$todo_id" "$worker" "$status"
}

todo_close() {
  local todo_id="${1:-}"
  [[ -n "$todo_id" ]] || die "todo-close requires TODO_ID"
  validate_name "$todo_id"
  shift

  local verified_by="" recheck_json="" notes=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --verified-by)
        verified_by="${2:-}"
        shift 2
        ;;
      --recheck-json)
        recheck_json="${2:-}"
        shift 2
        ;;
      --notes)
        notes="${2:-}"
        shift 2
        ;;
      *)
        die "unknown todo-close argument: $1"
        ;;
    esac
  done

  [[ -f "$(todo_meta_file "$todo_id")" ]] || die "no todo: $todo_id"
  [[ "$(get_todo_status "$todo_id")" == "resolved" ]] || die "todo-close requires a resolved todo"
  [[ -f "$(todo_dir "$todo_id")/resolution.json" ]] || die "todo-close requires worker resolution evidence"
  [[ -n "$verified_by" ]] || die "todo-close requires --verified-by NAME"
  validate_name "$verified_by"
  [[ -n "$recheck_json" ]] || die "todo-close requires --recheck-json JSON"
  reject_newline "--notes" "$notes"
  validate_closure_payload "$recheck_json"
  validate_required_commands_covered "$todo_id" "verifier recheck" "$recheck_json"

  local source_finding_id source_finding_hash dir
  source_finding_id="$(read_todo_value "$todo_id" source_finding_id)"
  source_finding_hash="$(read_todo_value "$todo_id" source_finding_hash)"
  dir="$(todo_dir "$todo_id")"
  validate_closure_matches_todo "$todo_id" "$source_finding_id" "$source_finding_hash" "$(cat "$dir/resolution.json")" "$recheck_json"
  cat >"$dir/closure.env" <<EOF
todo_id=$todo_id
source_finding_id=$source_finding_id
source_finding_hash=$source_finding_hash
verified_by=$verified_by
notes=$notes
created_at=$(timestamp)
EOF
  printf '%s\n' "$recheck_json" >"$dir/recheck.json"
  write_closure_json "$todo_id"
  set_env_key "$(todo_meta_file "$todo_id")" updated_at "$(timestamp)"
  set_todo_status "$todo_id" "closed"
  write_todo_json "$todo_id"
  printf 'todo closed\t%s\t%s\n' "$todo_id" "$verified_by"
}

audit_closed_todo() {
  local todo_id="$1"
  local expected_final_diff_hash="${2:-}"
  local dir source_finding_id source_finding_hash current_finding_hash
  dir="$(todo_dir "$todo_id")"
  source_finding_id="$(read_todo_value "$todo_id" source_finding_id || true)"
  source_finding_hash="$(read_todo_value "$todo_id" source_finding_hash || true)"
  if [[ -z "$source_finding_id" || ! -f "$(finding_dir "$source_finding_id")/finding.json" ]]; then
    printf 'reject\tclosed-todo-missing-source-finding\ttodo=%s\tfinding=%s\n' "$todo_id" "$source_finding_id"
    return 1
  fi
  if [[ -z "$source_finding_hash" ]]; then
    printf 'reject\tclosed-todo-missing-source-finding-hash\ttodo=%s\n' "$todo_id"
    return 1
  fi
  current_finding_hash="$(sha256_file "$(finding_dir "$source_finding_id")/finding.json")"
  if [[ "$current_finding_hash" != "$source_finding_hash" ]]; then
    printf 'reject\tclosed-todo-source-finding-hash-changed\ttodo=%s\tfinding=%s\n' "$todo_id" "$source_finding_id"
    return 1
  fi
  if [[ ! -f "$dir/resolution.json" ]]; then
    printf 'reject\tclosed-todo-missing-resolution\ttodo=%s\n' "$todo_id"
    return 1
  fi
  if [[ ! -f "$dir/closure.json" ]]; then
    printf 'reject\tclosed-todo-missing-verifier-closure\ttodo=%s\n' "$todo_id"
    return 1
  fi
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
todo_id = sys.argv[2]
expected_finding_hash = sys.argv[3]
expected_final_diff_hash = sys.argv[4]
try:
    resolution = json.loads((root / "resolution.json").read_text())
    closure = json.loads((root / "closure.json").read_text())
except Exception as exc:
    print(f"reject\tclosed-todo-invalid-evidence\ttodo={todo_id}\treason={exc}")
    raise SystemExit(1)
if resolution.get("todo_id") != todo_id or resolution.get("status") != "resolved":
    print(f"reject\tclosed-todo-invalid-resolution\ttodo={todo_id}")
    raise SystemExit(1)
recheck = closure.get("recheck")
if closure.get("todo_id") != todo_id or not isinstance(recheck, dict) or recheck.get("accepted") is not True:
    print(f"reject\tclosed-todo-invalid-closure\ttodo={todo_id}")
    raise SystemExit(1)
if closure.get("source_finding_hash") != expected_finding_hash:
    print(f"reject\tclosed-todo-closure-finding-hash-mismatch\ttodo={todo_id}")
    raise SystemExit(1)
if expected_final_diff_hash and str(recheck.get("final_diff_hash", "")).lower() != expected_final_diff_hash.lower():
    print(f"reject\tclosed-todo-final-diff-hash-mismatch\ttodo={todo_id}")
    raise SystemExit(1)
source_finding_id = closure.get("source_finding_id")
if source_finding_id not in {
    str(recheck.get("finding_rechecked", "")).strip(),
    str(recheck.get("source_finding_id", "")).strip(),
}:
    print(f"reject\tclosed-todo-recheck-mismatch\ttodo={todo_id}\tfinding={source_finding_id}")
    raise SystemExit(1)
resolution_commands = {
    str(item.get("cmd", "")).strip()
    for item in resolution.get("validation", [])
    if isinstance(item, dict) and str(item.get("cmd", "")).strip() and int(item.get("rc", 0)) == 0
}
recheck_commands = {
    str(item.get("cmd", "")).strip()
    for item in recheck.get("commands", [])
    if isinstance(item, dict) and str(item.get("cmd", "")).strip() and int(item.get("rc", 1)) == 0
}
missing = sorted(resolution_commands - recheck_commands)
if missing:
    print(f"reject\tclosed-todo-recheck-missing-worker-command\ttodo={todo_id}\tcmd={missing[0]}")
    raise SystemExit(1)
' "$dir" "$todo_id" "$source_finding_hash" "$expected_final_diff_hash"
  validate_required_commands_covered "$todo_id" "closed todo resolution" "$(cat "$dir/resolution.json")" || return 1
  validate_required_commands_covered "$todo_id" "closed todo verifier recheck" "$(cat "$dir/recheck.json")" || return 1
}

write_validation_lease_json() {
  local lease_id="$1"
  local dir
  dir="$(validation_lease_dir "$lease_id")"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys
root = pathlib.Path(sys.argv[1])
status = sys.argv[2]
meta = {}
for line in (root / "lease.env").read_text().splitlines():
    if "=" in line:
        key, value = line.split("=", 1)
        meta[key] = value
result_file = root / "result.json"
result = json.loads(result_file.read_text()) if result_file.exists() else {}
payload = {
    "lease_id": meta["lease_id"],
    "owner": meta["owner"],
    "target": meta["target"],
    "command": meta["command"],
    "state": status,
    "resource_risk": meta.get("resource_risk", ""),
    "result": result,
    "created_at": meta["created_at"],
    "updated_at": meta.get("updated_at", meta["created_at"]),
}
(root / "lease.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
' "$dir" "$(get_validation_lease_status "$lease_id")"
}

validation_lease_acquire() {
  local lease_id="${1:-}"
  [[ -n "$lease_id" ]] || die "validation-lease-acquire requires LEASE_ID"
  validate_name "$lease_id"
  shift

  local owner="" target="" command="" state="running" resource_risk=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --owner)
        owner="${2:-}"
        shift 2
        ;;
      --target)
        target="${2:-}"
        shift 2
        ;;
      --command)
        command="${2:-}"
        shift 2
        ;;
      --state)
        state="${2:-}"
        shift 2
        ;;
      --resource-risk)
        resource_risk="${2:-}"
        shift 2
        ;;
      *)
        die "unknown validation-lease-acquire argument: $1"
        ;;
    esac
  done

  [[ -n "$owner" ]] || die "validation-lease-acquire requires --owner NAME"
  validate_name "$owner"
  [[ -n "$target" ]] || die "validation-lease-acquire requires --target TEXT"
  [[ -n "$command" ]] || die "validation-lease-acquire requires --command TEXT"
  reject_newline "--target" "$target"
  reject_newline "--command" "$command"
  reject_newline "--resource-risk" "$resource_risk"
  validate_validation_lease_status "$state"
  case "$state" in
    planned|running)
      ;;
    *)
      die "validation-lease-acquire state must be planned or running"
      ;;
  esac

  local base="$STATE_DIR/validation-leases"
  local existing_dir existing_id existing_target existing_state existing_owner
  if [[ -d "$base" ]]; then
    for existing_dir in "$base"/*; do
      [[ -d "$existing_dir" ]] || continue
      existing_id="$(basename "$existing_dir")"
      [[ "$existing_id" != "$lease_id" ]] || continue
      existing_target="$(read_validation_lease_value "$existing_id" target || true)"
      [[ "$existing_target" == "$target" ]] || continue
      existing_state="$(get_validation_lease_status "$existing_id")"
      case "$existing_state" in
        planned|running)
          existing_owner="$(read_validation_lease_value "$existing_id" owner || true)"
          die "validation lease conflict: target=$target lease=$existing_id owner=$existing_owner state=$existing_state"
          ;;
      esac
    done
  fi

  local dir
  dir="$(validation_lease_dir "$lease_id")"
  [[ ! -e "$dir" ]] || die "validation lease already exists: $lease_id"
  mkdir -p "$dir"
  cat >"$(validation_lease_meta_file "$lease_id")" <<EOF
lease_id=$lease_id
owner=$owner
target=$target
command=$command
resource_risk=$resource_risk
created_at=$(timestamp)
updated_at=$(timestamp)
root=$ROOT
EOF
  printf '{}\n' >"$dir/result.json"
  printf '%s\n' "$state" >"$(validation_lease_status_file "$lease_id")"
  write_validation_lease_json "$lease_id"
  printf 'validation lease acquired\t%s\t%s\t%s\n' "$lease_id" "$owner" "$state"
}

validation_lease_status() {
  local lease_id="${1:-}"
  local state="${2:-}"
  [[ -n "$lease_id" && -n "$state" ]] || die "validation-lease-status requires LEASE_ID STATUS"
  validate_name "$lease_id"
  validate_validation_lease_status "$state"
  shift 2

  local result_json=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --result-json)
        result_json="${2:-}"
        shift 2
        ;;
      *)
        die "unknown validation-lease-status argument: $1"
        ;;
    esac
  done

  [[ -f "$(validation_lease_meta_file "$lease_id")" ]] || die "no validation lease: $lease_id"
  if [[ -n "$result_json" ]]; then
    require_cmd python3
    python3 -c 'import json, sys; json.loads(sys.argv[1])' "$result_json"
    printf '%s\n' "$result_json" >"$(validation_lease_dir "$lease_id")/result.json"
  fi
  set_env_key "$(validation_lease_meta_file "$lease_id")" updated_at "$(timestamp)"
  printf '%s\n' "$state" >"$(validation_lease_status_file "$lease_id")"
  write_validation_lease_json "$lease_id"
  printf 'validation lease status\t%s\t%s\n' "$lease_id" "$state"
}

validation_lease_show() {
  local lease_id="${1:-}"
  [[ -n "$lease_id" ]] || die "validation-lease-show requires LEASE_ID"
  validate_name "$lease_id"
  [[ -f "$(validation_lease_dir "$lease_id")/lease.json" ]] || die "no validation lease: $lease_id"
  write_validation_lease_json "$lease_id"
  cat "$(validation_lease_dir "$lease_id")/lease.json"
}

validation_lease_list() {
  local state_filter=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --state)
        state_filter="${2:-}"
        validate_validation_lease_status "$state_filter"
        shift 2
        ;;
      *)
        die "unknown validation-lease-list argument: $1"
        ;;
    esac
  done

  local base="$STATE_DIR/validation-leases"
  [[ -d "$base" ]] || return 0
  local dir lease_id state owner target command
  for dir in "$base"/*; do
    [[ -d "$dir" ]] || continue
    lease_id="$(basename "$dir")"
    state="$(get_validation_lease_status "$lease_id")"
    [[ -z "$state_filter" || "$state" == "$state_filter" ]] || continue
    owner="$(read_validation_lease_value "$lease_id" owner || true)"
    target="$(read_validation_lease_value "$lease_id" target || true)"
    command="$(read_validation_lease_value "$lease_id" command || true)"
    printf '%s\t%s\t%s\t%s\t%s\n' "$lease_id" "$state" "$owner" "$target" "$command"
  done
}

validation_run_result_json() {
  local command_json="$1"
  local return_code="$2"
  local started_at="$3"
  local finished_at="$4"
  local stdout_path="$5"
  local stderr_path="$6"
  local cwd="$7"
  local timeout_seconds="$8"
  local timed_out="$9"
  require_cmd python3
  python3 -c '
import json
import pathlib
import sys

command = json.loads(sys.argv[1])
return_code = int(sys.argv[2])
started_at = sys.argv[3]
finished_at = sys.argv[4]
stdout_path = pathlib.Path(sys.argv[5])
stderr_path = pathlib.Path(sys.argv[6])
cwd = sys.argv[7]
timeout_seconds = int(sys.argv[8])
timed_out = sys.argv[9] == "1"

def tail(path):
    text = path.read_text(errors="replace") if path.exists() else ""
    return text[-4000:]

print(json.dumps({
    "command": command,
    "command_text": " ".join(command),
    "returncode": return_code,
    "cwd": cwd,
    "started_at": started_at,
    "finished_at": finished_at,
    "timeout_seconds": timeout_seconds,
    "timed_out": timed_out,
    "stdout_tail": tail(stdout_path),
    "stderr_tail": tail(stderr_path),
}, sort_keys=True))
' "$command_json" "$return_code" "$started_at" "$finished_at" "$stdout_path" "$stderr_path" "$cwd" "$timeout_seconds" "$timed_out"
}

validation_run() {
  local lease_id="${1:-}"
  [[ -n "$lease_id" ]] || die "validation-run requires LEASE_ID"
  validate_name "$lease_id"
  require_cmd python3
  shift

  local owner="" target="" resource_risk="" timeout_seconds="${MULTIAGENT_VALIDATION_TIMEOUT_SECONDS:-600}"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --owner)
        owner="${2:-}"
        shift 2
        ;;
      --target)
        target="${2:-}"
        shift 2
        ;;
      --resource-risk)
        resource_risk="${2:-}"
        shift 2
        ;;
      --timeout-seconds)
        timeout_seconds="${2:-}"
        shift 2
        ;;
      --)
        shift
        break
        ;;
      *)
        die "unknown validation-run argument before --: $1"
        ;;
    esac
  done

  [[ -n "$owner" ]] || die "validation-run requires --owner NAME"
  validate_name "$owner"
  [[ -n "$target" ]] || die "validation-run requires --target TEXT"
  [[ $# -gt 0 ]] || die "validation-run requires COMMAND after --"
  [[ -d "$ROOT" ]] || die "validation-run root does not exist: $ROOT"
  [[ "$timeout_seconds" =~ ^[0-9]+$ && "$timeout_seconds" -gt 0 ]] || die "validation-run --timeout-seconds must be a positive integer"

  local command_json command_text tmp_dir stdout_path stderr_path timeout_flag_path started_at finished_at rc result_json run_cwd timed_out
  command_json="$(python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@")"
  command_text="$(python3 -c 'import json, sys; print(" ".join(json.loads(sys.argv[1])))' "$command_json")"
  validation_lease_acquire "$lease_id" --owner "$owner" --target "$target" --command "$command_text" --state running --resource-risk "$resource_risk" >/dev/null

  tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/multiagent-validation-run.XXXXXX")"
  stdout_path="$tmp_dir/stdout"
  stderr_path="$tmp_dir/stderr"
  timeout_flag_path="$tmp_dir/timed-out"
  run_cwd="$(cd "$ROOT" && pwd -P)"
  started_at="$(timestamp)"
  set +e
  python3 - "$command_json" "$run_cwd" "$stdout_path" "$stderr_path" "$timeout_seconds" "$timeout_flag_path" <<'PY'
import json
import os
import signal
import subprocess
import sys

argv = json.loads(sys.argv[1])
cwd = sys.argv[2]
stdout_path = sys.argv[3]
stderr_path = sys.argv[4]
timeout_seconds = int(sys.argv[5])
timeout_flag_path = sys.argv[6]

with open(stdout_path, "wb") as stdout, open(stderr_path, "wb") as stderr:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    try:
        rc = proc.wait(timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        rc = 124

with open(stderr_path, "ab") as stderr:
    if timed_out:
        stderr.write(f"\nvalidation-run timed out after {timeout_seconds} seconds\n".encode())

with open(timeout_flag_path, "w", encoding="utf-8") as flag:
    flag.write("1\n" if timed_out else "0\n")
raise SystemExit(rc)
PY
  rc=$?
  timed_out="$(tr -d '\n' <"$timeout_flag_path" 2>/dev/null || printf '0')"
  set -e
  finished_at="$(timestamp)"

  cat "$stdout_path"
  cat "$stderr_path" >&2
  result_json="$(validation_run_result_json "$command_json" "$rc" "$started_at" "$finished_at" "$stdout_path" "$stderr_path" "$run_cwd" "$timeout_seconds" "$timed_out")"
  if [[ "$timed_out" -eq 1 ]]; then
    validation_lease_status "$lease_id" timed-out --result-json "$result_json" >/dev/null
  elif [[ "$rc" -eq 0 ]]; then
    validation_lease_status "$lease_id" passed --result-json "$result_json" >/dev/null
  else
    validation_lease_status "$lease_id" failed --result-json "$result_json" >/dev/null
  fi
  rm -rf "$tmp_dir"
  return "$rc"
}

current_final_diff_sha256() {
  [[ "$MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER" == "1" ]] || return 0
  require_cmd python3
  python3 - "$ROOT" "${MULTIAGENT_START_HEAD:-}" <<'PY'
import hashlib
import pathlib
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
start_head = sys.argv[2]
if not root.is_dir():
    raise SystemExit(0)
command = ["git", "diff", "--binary", "--ignore-submodules=all"]
if start_head:
    command.append(start_head)
result = subprocess.run(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
if result.returncode == 0 and result.stdout.strip():
    print(hashlib.sha256(result.stdout).hexdigest())
PY
}

verifier_evidence_matches_hash() {
  local evidence_path="$1"
  local expected_hash="$2"
  require_cmd python3
  python3 - "$evidence_path" "$expected_hash" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace").lower()
expected = sys.argv[2].lower()
compact = re.sub(r"\s+", "", text)
accepted = (
    f"final-diff-sha256={expected}" in text
    or f'"final_diff_hash":"{expected}"' in compact
    or f'"final_diff_sha256":"{expected}"' in compact
)
raise SystemExit(0 if accepted else 1)
PY
}

latest_verifier_verdict() {
  local subagents_base="$STATE_DIR/subagents"
  [[ -d "$subagents_base" ]] || return 0
  require_cmd python3
  python3 - "$subagents_base" <<'PY'
import pathlib
import re
import sys

base = pathlib.Path(sys.argv[1])
candidates = []
for path in base.glob("*/last-message.txt"):
    name = path.parent.name.lower()
    if "verifier" not in name and "review" not in name:
        continue
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        mtime = path.stat().st_mtime_ns
    except OSError:
        continue
    verdict = "MISSING"
    for line in text.splitlines()[:80]:
        if not line.strip():
            continue
        match = re.fullmatch(r"\s*(ACCEPTED|BLOCKING)\s*", line, re.IGNORECASE)
        if not match:
            match = re.fullmatch(
                r"\s*(?:verdict\s*[:=]\s*)?(ACCEPTED|BLOCKING|REJECTED)\s*",
                line,
                re.IGNORECASE,
            )
        if match:
            verdict = match.group(1).upper()
            if verdict == "REJECTED":
                verdict = "BLOCKING"
        break
    candidates.append((mtime, path.parent.name, verdict, path))

if candidates:
    _, name, verdict, path = max(candidates, key=lambda item: (item[0], str(item[3])))
    print(f"{verdict}\t{name}\t{path}")
PY
}

active_verifiers() {
  local subagents_base="$STATE_DIR/subagents"
  [[ -d "$subagents_base" ]] || return 0
  require_cmd python3
  python3 - "$subagents_base" <<'PY'
import pathlib
import sys

base = pathlib.Path(sys.argv[1])
for agent_dir in sorted(path for path in base.iterdir() if path.is_dir()):
    name = agent_dir.name.lower()
    if "verifier" not in name and "review" not in name:
        continue
    try:
        status = agent_dir.joinpath("status").read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        continue
    if status in {"running", "starting", "pending"}:
        print(f"{agent_dir.name}\t{status}")
PY
}

gate_check() {
  local failed=0
  local findings_base="$STATE_DIR/findings"
  local todos_base="$STATE_DIR/todos"
  local dir finding_id severity todo_dir_path todo_id source status found_todo
  local verifier_verdict verdict verifier_name verifier_evidence final_diff_hash active_verifier

  final_diff_hash="$(current_final_diff_sha256)"
  while IFS= read -r active_verifier; do
    [[ -n "$active_verifier" ]] || continue
    printf 'reject\tactive-verifier\t%s\n' "$active_verifier"
    failed=1
  done < <(active_verifiers)
  verifier_verdict="$(latest_verifier_verdict)"
  if [[ -n "$verifier_verdict" ]]; then
    IFS=$'\t' read -r verdict verifier_name verifier_evidence <<<"$verifier_verdict"
    if [[ "$verdict" == "BLOCKING" ]]; then
      printf 'reject\tlatest-verifier-blocking\tverifier=%s\tevidence=%s\n' "$verifier_name" "$verifier_evidence"
      failed=1
    elif [[ "$verdict" == "MISSING" ]]; then
      printf 'reject\tlatest-verifier-missing-verdict\tverifier=%s\tevidence=%s\n' "$verifier_name" "$verifier_evidence"
      failed=1
    elif [[ -n "$final_diff_hash" ]] && ! verifier_evidence_matches_hash "$verifier_evidence" "$final_diff_hash"; then
      printf 'reject\tlatest-verifier-final-diff-hash-mismatch\tverifier=%s\texpected=%s\tevidence=%s\n' "$verifier_name" "$final_diff_hash" "$verifier_evidence"
      failed=1
    fi
  elif [[ -n "$final_diff_hash" ]]; then
    printf 'reject\tmissing-verifier-acceptance\texpected=%s\n' "$final_diff_hash"
    failed=1
  fi

  if [[ -d "$findings_base" ]]; then
    for dir in "$findings_base"/*; do
      [[ -d "$dir" ]] || continue
      finding_id="$(basename "$dir")"
      severity="$(read_finding_value "$finding_id" severity || true)"
      [[ "$severity" == "blocking" ]] || continue
      found_todo=0
      if [[ -d "$todos_base" ]]; then
        for todo_dir_path in "$todos_base"/*; do
          [[ -d "$todo_dir_path" ]] || continue
          todo_id="$(basename "$todo_dir_path")"
          source="$(read_todo_value "$todo_id" source_finding_id || true)"
          [[ "$source" == "$finding_id" ]] || continue
          found_todo=1
          status="$(get_todo_status "$todo_id")"
          if [[ "$status" != "closed" ]]; then
            printf 'reject\topen-blocking-todo\tfinding=%s\ttodo=%s\tstatus=%s\n' "$finding_id" "$todo_id" "$status"
            failed=1
          fi
        done
      fi
      if [[ "$found_todo" -eq 0 ]]; then
        printf 'reject\tunqueued-blocking-finding\tfinding=%s\n' "$finding_id"
        failed=1
      fi
    done
  fi

  if [[ -d "$todos_base" ]]; then
    for todo_dir_path in "$todos_base"/*; do
      [[ -d "$todo_dir_path" ]] || continue
      todo_id="$(basename "$todo_dir_path")"
      status="$(get_todo_status "$todo_id")"
      if [[ "$status" != "closed" ]]; then
        printf 'reject\topen-todo\ttodo=%s\tstatus=%s\n' "$todo_id" "$status"
        failed=1
      elif ! audit_closed_todo "$todo_id" "$final_diff_hash"; then
        failed=1
      fi
    done
  fi

  if [[ "$failed" -eq 0 ]]; then
    printf 'accepted\tfinal-gate\n'
  fi
  return "$failed"
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
  finding-create)
    shift
    finding_create "$@"
    ;;
  finding-show)
    shift
    finding_show "$@"
    ;;
  finding-list)
    shift
    finding_list "$@"
    ;;
  todo-create)
    shift
    todo_create "$@"
    ;;
  todo-show)
    shift
    todo_show "$@"
    ;;
  todo-list)
    shift
    todo_list "$@"
    ;;
  todo-assign)
    shift
    todo_assign "$@"
    ;;
  todo-status)
    shift
    todo_status "$@"
    ;;
  resolution-create)
    shift
    resolution_create "$@"
    ;;
  todo-close)
    shift
    todo_close "$@"
    ;;
  validation-lease-acquire)
    shift
    validation_lease_acquire "$@"
    ;;
  validation-lease-status)
    shift
    validation_lease_status "$@"
    ;;
  validation-lease-show)
    shift
    validation_lease_show "$@"
    ;;
  validation-lease-list)
    shift
    validation_lease_list "$@"
    ;;
  validation-run)
    shift
    validation_run "$@"
    ;;
  gate-check)
    shift
    gate_check "$@"
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
