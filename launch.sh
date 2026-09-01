#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
export MULTIAGENT_FRAMEWORK_ROOT="${MULTIAGENT_FRAMEWORK_ROOT:-$SCRIPT_DIR}"

if [[ -n "${MULTIAGENT_BIN:-}" ]]; then
  exec "$MULTIAGENT_BIN" launch "$@"
fi

for candidate in \
  "$SCRIPT_DIR/bin/multiagent" \
  "$SCRIPT_DIR/target/release/multiagent" \
  "$SCRIPT_DIR/target/debug/multiagent"
do
  if [[ -x "$candidate" ]]; then
    exec "$candidate" launch "$@"
  fi
done

command -v cargo >/dev/null 2>&1 || {
  echo "launch: Rust binary is not built and cargo is unavailable" >&2
  exit 1
}

exec cargo run --quiet --manifest-path "$SCRIPT_DIR/Cargo.toml" --package multiagent -- launch "$@"
