#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR="$(mktemp -d)"
TMPDIR="$(cd "$TMPDIR" && pwd -P)"
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
export MULTIAGENT_WRITE_POLICY="$TMPDIR/write-policy.paths"
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

"$ROOT/bin/write-policy.sh" init
assert_file_contains "$MULTIAGENT_WRITE_POLICY" "Default allowed write root"

policy_show="$("$ROOT/bin/write-policy.sh" show)"
[[ "$policy_show" == *"Default write root: $ROOT"* ]]
[[ "$policy_show" == *"Approved outside write roots:"* ]]

policy_check_inside="$("$ROOT/bin/write-policy.sh" check "$ROOT/README.md")"
[[ "$policy_check_inside" == $'allowed\t'"$ROOT/README.md" ]]

outside_path="$TMPDIR/outside/result.txt"
policy_check_file="$TMPDIR/policy-check.out"
if "$ROOT/bin/write-policy.sh" check "$outside_path" >"$policy_check_file" 2>&1; then
  echo "expected outside path to be denied before approval" >&2
  cat "$policy_check_file" >&2
  exit 1
fi
assert_file_contains "$policy_check_file" $'denied\t'"$outside_path"

approve_output="$("$ROOT/bin/write-policy.sh" approve "$TMPDIR/outside")"
[[ "$approve_output" == $'approved outside write root: '"$TMPDIR/outside" ]]
policy_check_outside="$("$ROOT/bin/write-policy.sh" check "$outside_path")"
[[ "$policy_check_outside" == $'allowed\t'"$outside_path" ]]

"$ROOT/bin/subagent.sh" spawn subagent-watch --instruction "Watch builds"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-watch"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "Codex prompt ready"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "write_policy=$MULTIAGENT_WRITE_POLICY"
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

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-restore"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/status"
printf 'Previous progress: halfway through recovery work\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/current.txt"
printf 'Older transcript context\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log"
printf 'Restored Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-restore.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-blocked"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked/status"
printf 'Blocked: need input from orchestrator\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked/current.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-open"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-open/status"
printf 'Still active in tmux\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-open/current.txt"
printf 'subagent-open\n' >>"$MOCK_TMUX_WINDOWS"
printf 'Open subagent prompt\n' >"$MOCK_TMUX_CAPTURES/subagent-open.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-unknown"

recover_plan="$("$ROOT/bin/subagent.sh" recover-plan)"
[[ "$recover_plan" == *$'subagent-watch\tskip-finalized\tstatus-finalized\tfinalized\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-watch"* ]]
[[ "$recover_plan" == *$'subagent-restore\trestore\tclosed-with-recoverable-context\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-restore"* ]]
[[ "$recover_plan" == *$'subagent-blocked\tskip-blocked\trequires-orchestrator-decision\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked"* ]]
[[ "$recover_plan" == *$'subagent-open\tskip-open\ttmux-window-already-open\trunning\topen\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-open"* ]]
[[ "$recover_plan" == *$'subagent-unknown\tskip-unknown\tno-current-or-transcript\tunknown\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-unknown"* ]]

blocked_restore_file="$TMPDIR/blocked-restore.out"
if "$ROOT/bin/subagent.sh" restore subagent-blocked >"$blocked_restore_file" 2>&1; then
  echo "expected blocked subagent restore to require force" >&2
  cat "$blocked_restore_file" >&2
  exit 1
fi
assert_file_contains "$blocked_restore_file" "refusing to restore subagent-blocked: skip-blocked"

restore_output="$("$ROOT/bin/subagent.sh" restore subagent-restore)"
[[ "$restore_output" == "restored subagent-restore" ]]
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-restore"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/restore_events.log" "prior_status=running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "You are a restored long-running subagent."
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "Previous progress: halfway through recovery work"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-restore You are a restored long-running subagent."

restore_all_output="$("$ROOT/bin/subagent.sh" restore-all)"
[[ "$restore_all_output" == *$'skipped subagent-blocked\tskip-blocked'* ]]
[[ "$restore_all_output" == *$'skipped subagent-open\tskip-open'* ]]
[[ "$restore_all_output" == *$'skipped subagent-watch\tskip-finalized'* ]]
[[ "$restore_all_output" == *"restore-all complete: restored=0"* ]]

echo "tests passed"
