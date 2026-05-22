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
  bin/write-policy.sh approve PATH

Repo write guardrail helper.

By default, writes are allowed only inside $MULTIAGENT_ROOT. Paths outside that
root are allowed only when they are listed in the repo-local policy file:

  $MULTIAGENT_WRITE_POLICY, default $MULTIAGENT_ROOT/docs/write-policy.paths

This helper evaluates policy and updates the allowlist. It does not sandbox
Codex; workers still need to follow the policy before writing.
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
# Add one approved outside write root per line, using absolute paths.
# Blank lines and comments are ignored.
POLICY
}

approved_paths() {
  [[ -f "$POLICY_FILE" ]] || return 0
  while IFS= read -r line; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" ]] || continue
    printf '%s\n' "$(canonical_path "$line")"
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

  printf '%s\n' "$canonical" >>"$POLICY_FILE"
  printf 'approved outside write root: %s\n' "$canonical"
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
    [[ $# -eq 1 ]] || die "approve requires exactly one PATH"
    approve_path "$1"
    ;;
  -h|--help|"")
    usage
    ;;
  *)
    die "unknown command: $cmd"
    ;;
esac
