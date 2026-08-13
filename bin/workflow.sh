#!/usr/bin/env bash
set -euo pipefail

ROOT="${MULTIAGENT_ROOT:-$(pwd)}"
STATE_DIR="${MULTIAGENT_STATE_DIR:-$ROOT/.multiagent}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
FRAMEWORK_ROOT="${MULTIAGENT_FRAMEWORK_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd -P)}"

export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_STATE_DIR="$STATE_DIR"
export PYTHONPATH="$FRAMEWORK_ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec python3 -m multiagent_framework.workflow "$@"
