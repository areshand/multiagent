#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  bin/prompt-bundle.sh --orchestrator PATH --lifecycle PATH --output PATH

Builds the canonical initial orchestrator prompt from the role prompt and the
mandatory implementation lifecycle playbook.
USAGE
}

die() {
  echo "prompt-bundle: $*" >&2
  exit 1
}

orchestrator=""
lifecycle=""
output=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --orchestrator)
      orchestrator="${2:-}"
      shift 2
      ;;
    --lifecycle)
      lifecycle="${2:-}"
      shift 2
      ;;
    --output)
      output="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -f "$orchestrator" ]] || die "orchestrator prompt not found: $orchestrator"
[[ -f "$lifecycle" ]] || die "lifecycle prompt not found: $lifecycle"
[[ -n "$output" ]] || die "--output is required"

mkdir -p "$(dirname "$output")"
tmp="$(mktemp "$(dirname "$output")/.orchestrator-prompt.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
{
  printf '%s\n\n' '----- BEGIN ORCHESTRATOR ROLE -----'
  cat "$orchestrator"
  printf '\n%s\n\n' '----- END ORCHESTRATOR ROLE -----'
  printf '%s\n\n' '----- BEGIN MANDATORY IMPLEMENTATION LIFECYCLE -----'
  cat "$lifecycle"
  printf '\n%s\n' '----- END MANDATORY IMPLEMENTATION LIFECYCLE -----'
} >"$tmp"
mv "$tmp" "$output"
trap - EXIT
printf 'prompt bundle built\t%s\n' "$output"
