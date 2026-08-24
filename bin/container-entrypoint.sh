#!/usr/bin/env bash
set -euo pipefail
if [[ "$(id -u)" -ne 10000 ]]; then
  echo "multiagent container requires trusted control uid 10000" >&2
  exit 1
fi
export HOME="${HOME:-/var/lib/multiagent/home}"
export CODEX_HOME="${CODEX_HOME:-/var/lib/multiagent/codex}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/var/lib/multiagent/claude}"
export MULTIAGENT_STATE_DIR="${MULTIAGENT_STATE_DIR:-/var/lib/multiagent/state}"
export MULTIAGENT_REPOSITORY_ROOT="${MULTIAGENT_REPOSITORY_ROOT:-/var/lib/multiagent/repositories}"
if [[ "${MULTIAGENT_CONTROL_MODE:-local}" == "gateway" ]]; then
  mkdir -p "$HOME" "$MULTIAGENT_STATE_DIR" "$MULTIAGENT_REPOSITORY_ROOT"
  exec node /opt/multiagent/control-server/src/server.mjs
fi
/opt/multiagent/bin/multiagent container-bootstrap
mkdir -p "$HOME"
if [[ -n "${MULTIAGENT_STATE_S3_URI:-}" && ! -f "$MULTIAGENT_STATE_DIR/control-server/sessions.json" ]]; then
  aws s3 sync "$MULTIAGENT_STATE_S3_URI" "$MULTIAGENT_STATE_DIR" --only-show-errors || true
fi
if [[ -n "${MULTIAGENT_BOOTSTRAP_REPOSITORY_URL:-}" ]]; then
  repository_name="${MULTIAGENT_BOOTSTRAP_REPOSITORY_NAME:-multiagent}"
  if [[ ! "$repository_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    echo "invalid MULTIAGENT_BOOTSTRAP_REPOSITORY_NAME" >&2
    exit 1
  fi
  repository_path="$MULTIAGENT_REPOSITORY_ROOT/$repository_name"
  if [[ ! -d "$repository_path/.git" ]]; then
    if [[ -e "$repository_path" ]]; then
      echo "repository bootstrap target exists but is not a git repository: $repository_path" >&2
      exit 1
    fi
    umask 0002
    git clone -- "$MULTIAGENT_BOOTSTRAP_REPOSITORY_URL" "$repository_path"
  fi
fi
exec node /opt/multiagent/control-server/src/server.mjs
