#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

MOCK_BIN="$TMPDIR/bin"
mkdir -p "$MOCK_BIN" "$TMPDIR/captures" "$TMPDIR/state"

cat >"$MOCK_BIN/tmux" <<'TMUX'
#!/usr/bin/env bash
set -euo pipefail

windows_file="${MOCK_TMUX_WINDOWS:?}"
captures_dir="${MOCK_TMUX_CAPTURES:?}"
log_file="${MOCK_TMUX_LOG:?}"

cmd="${1:-}"
shift || true

window_name_from_target() {
  local target="$1"
  printf '%s\n' "${target#*:}"
}

case "$cmd" in
  has-session)
    exit 0
    ;;
  list-windows)
    while IFS= read -r window; do
      [[ -n "$window" ]] || continue
      printf '%s\n' "$window"
    done <"$windows_file"
    ;;
  new-window)
    name=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -n)
          name="$2"
          shift 2
          ;;
        *)
          shift
          ;;
      esac
    done
    printf '%s\n' "$name" >>"$windows_file"
    printf 'new-window %s\n' "$name" >>"$log_file"
    ;;
  capture-pane)
    target=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -t)
          target="$2"
          shift 2
          ;;
        *)
          shift
          ;;
      esac
    done
    name="$(window_name_from_target "$target")"
    cat "$captures_dir/$name.txt"
    ;;
  send-keys)
    target=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -t)
          target="$2"
          shift 2
          ;;
        *)
          printf 'send-key %s %s\n' "$target" "$1" >>"$log_file"
          shift
          ;;
      esac
    done
    ;;
  kill-window)
    target=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -t)
          target="$2"
          shift 2
          ;;
        *)
          shift
          ;;
      esac
    done
    name="$(window_name_from_target "$target")"
    grep -Fvx -- "$name" "$windows_file" >"$windows_file.next" || true
    mv "$windows_file.next" "$windows_file"
    printf 'kill-window %s\n' "$name" >>"$log_file"
    ;;
  *)
    echo "unexpected tmux command: $cmd" >&2
    exit 64
    ;;
esac
TMUX
chmod +x "$MOCK_BIN/tmux"

export PATH="$MOCK_BIN:$PATH"
export MOCK_TMUX_WINDOWS="$TMPDIR/windows"
export MOCK_TMUX_CAPTURES="$TMPDIR/captures"
export MOCK_TMUX_LOG="$TMPDIR/tmux.log"
export MULTIAGENT_SESSION="test-session"
export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_STATE_DIR="$TMPDIR/state"
export CODEX_BIN="true"

printf 'orchestrator\n' >"$MOCK_TMUX_WINDOWS"
printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
printf 'Worker progress: editing README\n' >"$MOCK_TMUX_CAPTURES/worker-01-docs.txt"

assert_file_contains() {
  local file="$1"
  local expected="$2"
  if ! grep -Fq -- "$expected" "$file"; then
    echo "expected $file to contain: $expected" >&2
    echo "--- $file ---" >&2
    cat "$file" >&2
    exit 1
  fi
}

"$ROOT/bin/subagent.sh" spawn subagent-watch --instruction "Watch builds"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-watch"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "Codex prompt ready"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-watch Watch builds"

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_output="$("$ROOT/bin/subagent.sh" poll subagent-watch)"
[[ "$poll_output" == $'subagent-watch\trunning' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/transcript.log" "Progress update: still running"

printf 'worker-01-docs\n' >>"$MOCK_TMUX_WINDOWS"
status_output="$("$ROOT/bin/status.sh")"
[[ "$status_output" == *$'worker\tworker-01-docs\tbusy\topen\tWorker progress: editing README\t-'* ]]
[[ "$status_output" == *$'subagent\tsubagent-watch\trunning\topen\tProgress update: still running\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-watch"* ]]
if grep -Fq $'\torchestrator\t' <<<"$status_output"; then
  echo "expected status output to exclude orchestrator" >&2
  echo "$status_output" >&2
  exit 1
fi

printf 'Final status: completed\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
finalize_output="$("$ROOT/bin/subagent.sh" finalize subagent-watch)"
[[ "$finalize_output" == "finalized subagent-watch" ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "finalized"
if grep -Fqx -- "subagent-watch" "$MOCK_TMUX_WINDOWS"; then
  echo "expected finalize to close the subagent window" >&2
  exit 1
fi

inspect_output="$("$ROOT/bin/subagent.sh" inspect subagent-watch --lines 5)"
[[ "$inspect_output" == *"Final status: completed"* ]]

echo "tests passed"
