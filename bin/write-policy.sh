#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
POLICY_FILE="${MULTIAGENT_WRITE_POLICY:-$ROOT/docs/write-policy.paths}"

usage() {
  cat <<'USAGE'
Usage:
  bin/write-policy.sh init
  bin/write-policy.sh show
  bin/write-policy.sh check PATH [...]
  bin/write-policy.sh approve PATH --actor ACTOR --assignment-id ID --reason TEXT [--force]

Repo write guardrail helper.

By default, writes are allowed only inside $MULTIAGENT_ROOT. Paths outside that
root are allowed only when they are approved in the repo-local policy file:

  $MULTIAGENT_WRITE_POLICY, default $MULTIAGENT_ROOT/docs/write-policy.paths

Approvals are structured audit records. This helper evaluates policy and
updates the allowlist. It does not sandbox Codex; workers still need to follow
the policy before writing.
USAGE
}

die() {
  echo "write-policy: $*" >&2
  exit 1
}

canonical_root() {
  local path="$1"
  mkdir -p "$path"
  (cd "$path" && pwd -P)
}

canonical_path() {
  local path="$1"
  local input rest parent base

  if [[ "$path" = /* ]]; then
    input="$path"
  else
    input="$(pwd)/$path"
  fi

  if [[ -e "$input" ]]; then
    if [[ -d "$input" ]]; then
      (cd "$input" && pwd -P)
    else
      parent="$(dirname "$input")"
      base="$(basename "$input")"
      printf '%s/%s\n' "$(cd "$parent" && pwd -P)" "$base"
    fi
    return
  fi

  rest=""
  parent="$input"
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
    printf '%s/%s\n' "$(cd "$parent" && pwd -P)" "$rest"
  else
    printf '/%s\n' "$rest"
  fi
}

inside_path() {
  local path="$1"
  local root="$2"
  [[ "$path" == "$root" || "$path" == "$root/"* ]]
}

ensure_policy_dir() {
  mkdir -p "$(dirname "$POLICY_FILE")"
}

init_policy() {
  ensure_policy_dir
  if [[ -f "$POLICY_FILE" ]]; then
    return 0
  fi

  cat >"$POLICY_FILE" <<'POLICY'
# Multiagent repo write policy
#
# Default allowed write root is $MULTIAGENT_ROOT for the launched session.
# Orchestrator-owned: workers should not edit this file directly.
# Add approvals only with:
#   bin/write-policy.sh approve PATH --actor ACTOR --assignment-id ID --reason TEXT [--force]
#
# Records are TSV:
#   approval<TAB>timestamp<TAB>actor<TAB>assignment_id<TAB>requested_path<TAB>canonical_path<TAB>reason<TAB>force
# Blank lines and comments are ignored. Legacy bare absolute path lines are read
# for compatibility but new approvals must be structured records.
POLICY
}

approved_paths() {
  [[ -f "$POLICY_FILE" ]] || return 0
  local line type timestamp actor assignment_id requested canonical reason force
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] || continue

    if [[ "$line" == approval$'\t'* ]]; then
      IFS=$'\t' read -r type timestamp actor assignment_id requested canonical reason force <<<"$line"
      [[ "$type" == "approval" && -n "${canonical:-}" ]] || continue
      printf '%s\n' "$(canonical_path "$canonical")"
    else
      printf '%s\n' "$(canonical_path "$line")"
    fi
  done <"$POLICY_FILE"
}

path_allowed() {
  local path="$1"
  local root="$2"
  local approved

  if inside_path "$path" "$root"; then
    return 0
  fi

  while IFS= read -r approved; do
    [[ -n "$approved" ]] || continue
    if inside_path "$path" "$approved"; then
      return 0
    fi
  done < <(approved_paths)

  return 1
}

show_policy() {
  init_policy
  local root
  root="$(canonical_root "$ROOT")"

  printf 'Default write root: %s\n' "$root"
  printf 'Policy file: %s\n' "$POLICY_FILE"
  printf 'Approved outside write roots:\n'

  local any=0 approved
  while IFS= read -r approved; do
    [[ -n "$approved" ]] || continue
    if ! inside_path "$approved" "$root"; then
      printf '  %s\n' "$approved"
      any=1
    fi
  done < <(approved_paths)

  if [[ "$any" -eq 0 ]]; then
    printf '  (none)\n'
  fi
}

check_paths() {
  [[ $# -gt 0 ]] || die "check requires at least one PATH"
  init_policy

  local root path canonical failed=0
  root="$(canonical_root "$ROOT")"

  for path in "$@"; do
    canonical="$(canonical_path "$path")"
    if path_allowed "$canonical" "$root"; then
      printf 'allowed\t%s\n' "$canonical"
    else
      printf 'denied\t%s\n' "$canonical"
      failed=1
    fi
  done

  return "$failed"
}

approve_path() {
  local path="${1:-}"
  [[ -n "$path" ]] || die "approve requires PATH"
  shift || true

  local actor="" assignment_id="" reason="" force=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --actor)
        actor="${2:-}"
        shift 2
        ;;
      --assignment-id)
        assignment_id="${2:-}"
        shift 2
        ;;
      --reason)
        reason="${2:-}"
        shift 2
        ;;
      --force)
        force=1
        shift
        ;;
      *)
        die "unknown approve argument: $1"
        ;;
    esac
  done

  [[ -n "$actor" ]] || die "approve requires --actor ACTOR"
  [[ -n "$assignment_id" ]] || die "approve requires --assignment-id ID"
  [[ -n "$reason" ]] || die "approve requires --reason TEXT"

  init_policy

  local root canonical existing
  root="$(canonical_root "$ROOT")"
  canonical="$(canonical_path "$path")"

  if inside_path "$canonical" "$root"; then
    printf 'already allowed by default root: %s\n' "$canonical"
    return 0
  fi

  while IFS= read -r existing; do
    [[ -n "$existing" ]] || continue
    if [[ "$existing" == "$canonical" ]]; then
      printf 'already approved: %s\n' "$canonical"
      return 0
    fi
  done < <(approved_paths)

  if is_broad_approval "$canonical" "$root" && [[ "$force" -eq 0 ]]; then
    die "refusing broad outside approval without --force: $canonical"
  fi

  printf 'approval\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$(timestamp)" "$actor" "$assignment_id" "$path" "$canonical" "$reason" "$force" >>"$POLICY_FILE"
  if [[ "$force" -eq 1 ]]; then
    printf 'approved outside write root: %s (forced)\n' "$canonical"
  else
    printf 'approved outside write root: %s\n' "$canonical"
  fi
}

timestamp() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

is_broad_approval() {
  local canonical="$1"
  local root="$2"
  local home="${HOME:-}"
  local repo_parent
  repo_parent="$(dirname "$root")"

  case "$canonical" in
    /|/tmp|/private/tmp|/var/tmp|/Users|/home|/opt|/usr|/var|/private|/Applications)
      return 0
      ;;
  esac

  [[ -n "$home" && "$canonical" == "$home" ]] && return 0
  [[ "$canonical" == "$repo_parent" ]] && return 0

  return 1
}

cmd="${1:-}"
case "$cmd" in
  init)
    shift
    [[ $# -eq 0 ]] || die "init takes no arguments"
    init_policy
    ;;
  show)
    shift
    [[ $# -eq 0 ]] || die "show takes no arguments"
    show_policy
    ;;
  check)
    shift
    check_paths "$@"
    ;;
  approve)
    shift
    [[ $# -ge 1 ]] || die "approve requires PATH"
    approve_path "$@"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
