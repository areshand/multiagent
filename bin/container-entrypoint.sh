#!/usr/bin/env bash
set -euo pipefail
export HOME="${HOME:-/var/lib/multiagent/home}"
export CODEX_HOME="${CODEX_HOME:-/var/lib/multiagent/codex}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/var/lib/multiagent/claude}"
export MULTIAGENT_STATE_DIR="${MULTIAGENT_STATE_DIR:-/var/lib/multiagent/state}"
export MULTIAGENT_REPOSITORY_ROOT="${MULTIAGENT_REPOSITORY_ROOT:-/var/lib/multiagent/repositories}"
mkdir -p "$HOME" "$CODEX_HOME" "$CLAUDE_CONFIG_DIR" "$MULTIAGENT_STATE_DIR" "$MULTIAGENT_REPOSITORY_ROOT"
if [[ -n "${MULTIAGENT_STATE_S3_URI:-}" && ! -f "$MULTIAGENT_STATE_DIR/control-server/sessions.json" ]]; then
  aws s3 sync "$MULTIAGENT_STATE_S3_URI" "$MULTIAGENT_STATE_DIR" --only-show-errors || true
fi
exec node /opt/multiagent/control-server/src/server.mjs
