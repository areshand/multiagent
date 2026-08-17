#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MULTIAGENT="${MULTIAGENT_BIN:-$ROOT/target/debug/multiagent}"
QWEN_BIN="${QWEN_BIN:-qwen}"

if ! command -v "$QWEN_BIN" >/dev/null 2>&1; then
  echo "Qwen Code executable not found: $QWEN_BIN" >&2
  exit 2
fi

if [[ ! -x "$MULTIAGENT" ]]; then
  cargo build --manifest-path "$ROOT/Cargo.toml"
fi

SMOKE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/multiagent-qwen-smoke.XXXXXX")"
trap 'rm -rf "$SMOKE_ROOT"' EXIT
WORKSPACE="$SMOKE_ROOT/workspace"
mkdir -p "$WORKSPACE"
printf 'immutable fixture\n' >"$WORKSPACE/input.txt"
BEFORE="$(shasum -a 256 "$WORKSPACE/input.txt" | awk '{print $1}')"

printf '%s\n' \
  'Read input.txt. Do not modify any file. Reply with exactly READ_ONLY_OK.' \
  >"$SMOKE_ROOT/read-only.prompt"
QWEN_BIN="$QWEN_BIN" \
MULTIAGENT_AGENT_TIMEOUT_SECONDS="${MULTIAGENT_AGENT_TIMEOUT_SECONDS:-300}" \
  "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$WORKSPACE" \
  --prompt-file "$SMOKE_ROOT/read-only.prompt" \
  --final-output "$SMOKE_ROOT/read-only.final" \
  --trace-dir "$SMOKE_ROOT/traces/read-only" \
  --access read-only

AFTER="$(shasum -a 256 "$WORKSPACE/input.txt" | awk '{print $1}')"
[[ "$BEFORE" == "$AFTER" ]]
grep -Fq 'READ_ONLY_OK' "$SMOKE_ROOT/read-only.final"

printf '%s\n' \
  'Create output.txt with exactly the single line WRITE_OK, then reply with exactly WRITE_DONE.' \
  >"$SMOKE_ROOT/write.prompt"
QWEN_BIN="$QWEN_BIN" \
MULTIAGENT_AGENT_TIMEOUT_SECONDS="${MULTIAGENT_AGENT_TIMEOUT_SECONDS:-300}" \
  "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$WORKSPACE" \
  --prompt-file "$SMOKE_ROOT/write.prompt" \
  --final-output "$SMOKE_ROOT/write.final" \
  --trace-dir "$SMOKE_ROOT/traces/write" \
  --access workspace-write

[[ "$(cat "$WORKSPACE/output.txt")" == "WRITE_OK" ]]
grep -Fq 'WRITE_DONE' "$SMOKE_ROOT/write.final"
echo "Qwen Code live read-only and workspace-write smoke passed"
