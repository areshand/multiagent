#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="${CARGO_TARGET_DIR:-$ROOT/target}"
MULTIAGENT="$TARGET_DIR/debug/multiagent"
HOST_KERNEL="$(uname -s)"
cargo build --offline --locked --manifest-path "$ROOT/Cargo.toml" >/dev/null
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
    if [[ "${MOCK_TMUX_HAS_SESSION:-1}" -eq 1 ]]; then
      exit 0
    fi
    exit 1
    ;;
  list-windows)
    while IFS= read -r window; do
      [[ -n "$window" ]] || continue
      printf '%s\n' "$window"
    done <"$windows_file"
    ;;
  new-window)
    detached=0
    target=""
    name=""
    command=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -d)
          detached=1
          shift
          ;;
        -t)
          target="$2"
          shift 2
          ;;
        -n)
          name="$2"
          shift 2
          ;;
        *)
          command="$1"
          shift
          ;;
      esac
    done
    printf '%s\n' "$name" >>"$windows_file"
    if [[ "$detached" -eq 1 ]]; then
      printf 'new-window -d %s %s %s\n' "$target" "$name" "$command" >>"$log_file"
    else
      printf 'new-window %s %s %s\n' "$target" "$name" "$command" >>"$log_file"
    fi
    ;;
  new-session)
    name=""
    window=""
    command=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -d)
          shift
          ;;
        -s)
          name="$2"
          shift 2
          ;;
        -n)
          window="$2"
          shift 2
          ;;
        *)
          command="$1"
          shift
          ;;
      esac
    done
    printf '%s\n' "$window" >>"$windows_file"
    printf 'new-session %s %s %s\n' "$name" "$window" "$command" >>"$log_file"
    ;;
  select-window)
    printf 'select-window %s\n' "${1:-}" >>"$log_file"
    ;;
  pipe-pane)
    target=""
    pipe_command=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -o)
          shift
          ;;
        -t)
          target="$2"
          shift 2
          ;;
        *)
          pipe_command="$1"
          shift
          ;;
      esac
    done
    printf 'pipe-pane %s %s\n' "$target" "$pipe_command" >>"$log_file"
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

cat >"$MOCK_BIN/qwen" <<'QWEN'
#!/usr/bin/env bash
set -euo pipefail
if [[ " ${*:-} " == *" --version "* ]]; then
  printf 'qwen-code test-1.0\n'
  exit 0
fi
prompt="$(cat)"
if [[ -n "${QWEN_PROMPT_CAPTURE:-}" ]]; then
  printf '%s' "$prompt" >"$QWEN_PROMPT_CAPTURE"
fi
if [[ -n "${QWEN_TRY_WRITE:-}" ]]; then
  printf 'unauthorized\n' >"$QWEN_TRY_WRITE"
fi
if [[ -n "${QWEN_DESCENDANT_PID_FILE:-}" ]]; then
  sleep 30 &
  descendant_pid=$!
  printf '%s\n' "$descendant_pid" >"$QWEN_DESCENDANT_PID_FILE"
fi
if [[ -n "${QWEN_SLEEP_SECONDS:-}" ]]; then
  sleep "$QWEN_SLEEP_SECONDS"
fi
printf '%s\n' '{"type":"system","session_id":"qwen-session-1"}'
printf '%s\n' 'malformed provider line retained as raw text'
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"text","text":"Qwen working"}]}}'
printf '%s\n' '{"type":"result","result":"Qwen final result"}'
printf 'qwen diagnostic\n' >&2
exit "${QWEN_EXIT_CODE:-0}"
QWEN
chmod +x "$MOCK_BIN/qwen"

export PATH="$MOCK_BIN:$PATH"
export MOCK_TMUX_WINDOWS="$TMPDIR/windows"
export MOCK_TMUX_CAPTURES="$TMPDIR/captures"
export MOCK_TMUX_LOG="$TMPDIR/tmux.log"
export MULTIAGENT_SESSION="test-session"
export MULTIAGENT_ROOT="$ROOT"
export MULTIAGENT_STATE_DIR="$TMPDIR/state"
export MULTIAGENT_WRITE_POLICY="$TMPDIR/write-policy.paths"
export MULTIAGENT_READY_ATTEMPTS=1
export MULTIAGENT_READY_DELAY=0
export CODEX_BIN="true"
export CLAUDE_BIN="true"
export QWEN_BIN="$MOCK_BIN/qwen"
export ORCHESTRATOR_CLI="codex"
export WORKER_CLI="claude"
export SUBAGENT_CLI="claude"
export VERIFIER_CLI="codex"
export MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=0

printf 'orchestrator\n' >"$MOCK_TMUX_WINDOWS"
printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
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

AGENT_RUN_DIR="$TMPDIR/agent-run"
mkdir -p "$AGENT_RUN_DIR/work"
printf 'prompt payload with spaces and '\''quotes'\''\n' >"$AGENT_RUN_DIR/prompt.txt"
QWEN_PROMPT_CAPTURE="$AGENT_RUN_DIR/prompt-captured.txt" \
  "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$AGENT_RUN_DIR/work" \
  --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
  --final-output "$AGENT_RUN_DIR/final.txt" \
  --trace-dir "$AGENT_RUN_DIR/trace" \
  --access read-only >"$AGENT_RUN_DIR/forwarded.out" 2>"$AGENT_RUN_DIR/forwarded.err"
AGENT_TRACE="$AGENT_RUN_DIR/trace/$(tr -d '\r\n' <"$AGENT_RUN_DIR/trace/latest")"
assert_file_contains "$AGENT_RUN_DIR/prompt-captured.txt" "prompt payload with spaces and 'quotes'"
assert_file_contains "$AGENT_RUN_DIR/final.txt" "Qwen final result"
assert_file_contains "$AGENT_TRACE/raw-stdout.log" "malformed provider line retained as raw text"
assert_file_contains "$AGENT_TRACE/raw-stderr.log" "qwen diagnostic"
assert_file_contains "$AGENT_TRACE/events.jsonl" '"backend":"qwen"'
assert_file_contains "$AGENT_TRACE/events.jsonl" '"raw_type":"result"'
assert_file_contains "$AGENT_TRACE/session-id" "qwen-session-1"
assert_file_contains "$AGENT_TRACE/metadata.json" '"version": "qwen-code test-1.0"'
assert_file_contains "$AGENT_TRACE/exit.json" '"success": true'
"$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$AGENT_RUN_DIR/work" \
  --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
  --final-output "$AGENT_RUN_DIR/final-second.txt" \
  --trace-dir "$AGENT_RUN_DIR/trace" \
  --access read-only >/dev/null 2>/dev/null
[[ "$(tr -d '\r\n' <"$AGENT_RUN_DIR/trace/latest")" == "attempt-0002" ]]
assert_file_contains "$AGENT_RUN_DIR/trace/attempt-0001/raw-stdout.log" "Qwen final result"
assert_file_contains "$AGENT_RUN_DIR/trace/attempt-0002/raw-stdout.log" "Qwen final result"
assert_file_contains "$AGENT_RUN_DIR/final-second.txt" "Qwen final result"
agent_backend_info="$("$MULTIAGENT" agent backend-info qwen)"
[[ "$agent_backend_info" == *'"backend":"qwen"'* ]]
[[ "$agent_backend_info" == *'"native_resume":true'* ]]
[[ "$agent_backend_info" == *'"version":"qwen-code test-1.0"'* ]]
if MULTIAGENT_AGENT_TIMEOUT_SECONDS=0 "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$AGENT_RUN_DIR/work" \
  --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
  --final-output "$AGENT_RUN_DIR/invalid-timeout-final.txt" \
  --trace-dir "$AGENT_RUN_DIR/invalid-timeout-trace" \
  --access read-only >"$AGENT_RUN_DIR/invalid-timeout.out" 2>&1; then
  echo "expected zero coding-agent timeout to fail" >&2
  exit 1
fi
assert_file_contains "$AGENT_RUN_DIR/invalid-timeout.out" "MULTIAGENT_AGENT_TIMEOUT_SECONDS must be a positive integer"
[[ ! -e "$AGENT_RUN_DIR/invalid-timeout-trace/latest" ]]

set +e
QWEN_EXIT_CODE=7 "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$AGENT_RUN_DIR/work" \
  --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
  --final-output "$AGENT_RUN_DIR/nonzero-final.txt" \
  --trace-dir "$AGENT_RUN_DIR/nonzero-trace" \
  --access workspace-write >/dev/null 2>/dev/null
agent_nonzero_rc=$?
set -e
[[ "$agent_nonzero_rc" -eq 7 ]]
AGENT_NONZERO_TRACE="$AGENT_RUN_DIR/nonzero-trace/$(tr -d '\r\n' <"$AGENT_RUN_DIR/nonzero-trace/latest")"
assert_file_contains "$AGENT_NONZERO_TRACE/exit.json" '"code": 7'

set +e
MULTIAGENT_AGENT_TIMEOUT_SECONDS=1 \
  QWEN_SLEEP_SECONDS=30 \
  QWEN_DESCENDANT_PID_FILE="$AGENT_RUN_DIR/descendant.pid" \
  "$MULTIAGENT" agent run \
  --backend qwen \
  --cwd "$AGENT_RUN_DIR/work" \
  --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
  --final-output "$AGENT_RUN_DIR/timeout-final.txt" \
  --trace-dir "$AGENT_RUN_DIR/timeout-trace" \
  --access workspace-write >/dev/null 2>/dev/null
agent_timeout_rc=$?
set -e
[[ "$agent_timeout_rc" -eq 124 ]]
AGENT_TIMEOUT_TRACE="$AGENT_RUN_DIR/timeout-trace/$(tr -d '\r\n' <"$AGENT_RUN_DIR/timeout-trace/latest")"
assert_file_contains "$AGENT_TIMEOUT_TRACE/exit.json" '"timed_out": true'
assert_file_contains "$AGENT_TIMEOUT_TRACE/exit.json" '"reason": "timeout"'
if [[ -f "$AGENT_RUN_DIR/descendant.pid" ]]; then
  descendant_pid="$(tr -d '\r\n' <"$AGENT_RUN_DIR/descendant.pid")"
  for _ in $(seq 1 40); do
    if ! kill -0 "$descendant_pid" 2>/dev/null; then
      break
    fi
    sleep 0.05
  done
  if kill -0 "$descendant_pid" 2>/dev/null; then
    echo "timed-out coding-agent descendant is still alive: $descendant_pid" >&2
    exit 1
  fi
fi

if [[ "$HOST_KERNEL" == Linux ]]; then
  mkdir -p "$AGENT_RUN_DIR/landlock-output"
  set +e
  QWEN_TRY_WRITE="$AGENT_RUN_DIR/work/forbidden.txt" \
    "$MULTIAGENT" role-exec \
    --allow-write "$AGENT_RUN_DIR/landlock-output" \
    -- "$MULTIAGENT" agent run \
    --backend qwen \
    --cwd "$AGENT_RUN_DIR/work" \
    --prompt-file "$AGENT_RUN_DIR/prompt.txt" \
    --final-output "$AGENT_RUN_DIR/landlock-output/final.txt" \
    --trace-dir "$AGENT_RUN_DIR/landlock-output/trace" \
    --access read-only >/dev/null 2>/dev/null
  agent_readonly_rc=$?
  set -e
  [[ "$agent_readonly_rc" -ne 0 ]]
  [[ ! -e "$AGENT_RUN_DIR/work/forbidden.txt" ]]
  AGENT_READONLY_TRACE="$AGENT_RUN_DIR/landlock-output/trace/$(tr -d '\r\n' <"$AGENT_RUN_DIR/landlock-output/trace/latest")"
  assert_file_contains "$AGENT_READONLY_TRACE/exit.json" '"success": false'
fi

assert_file_not_contains() {
  local file="$1"
  local unexpected="$2"
  if grep -Fq -- "$unexpected" "$file"; then
    echo "expected $file not to contain: $unexpected" >&2
    echo "--- $file ---" >&2
    cat "$file" >&2
    exit 1
  fi
}

"$MULTIAGENT" policy init
assert_file_contains "$MULTIAGENT_WRITE_POLICY" "Default allowed write root"

policy_show="$("$MULTIAGENT" policy show)"
[[ "$policy_show" == *"Default write root: $ROOT"* ]]
[[ "$policy_show" == *"Approved outside write roots:"* ]]

LAUNCH_TARGET="$TMPDIR/target-repo"
LAUNCH_STATE="$TMPDIR/launch-state"
LAUNCH_POLICY="$TMPDIR/launch-policy/write-policy.paths"
mkdir -p "$LAUNCH_TARGET"
rm -f "$MOCK_TMUX_LOG"
env -u WORKER_CLI -u SUBAGENT_CLI -u VERIFIER_CLI \
  MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-cross-repo" \
  MULTIAGENT_ROOT= \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_STATE_DIR="$LAUNCH_STATE" \
  MULTIAGENT_WRITE_POLICY="$LAUNCH_POLICY" \
  "$ROOT/launch.sh" --session launch-cross-repo --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch.out"
assert_file_contains "$TMPDIR/launch.out" "Started tmux session: launch-cross-repo"
assert_file_contains "$TMPDIR/launch.out" "Resume mode: 0"
assert_file_contains "$TMPDIR/launch.out" "Verifier max iterations: 3"
assert_file_contains "$TMPDIR/launch.out" "Worker CLI: claude"
assert_file_contains "$TMPDIR/launch.out" "Subagent CLI: claude"
assert_file_contains "$TMPDIR/launch.out" "Verifier CLI: codex"
assert_file_contains "$TMPDIR/launch.out" "Default write root: $LAUNCH_TARGET"
assert_file_contains "$TMPDIR/launch.out" "Logs: $LAUNCH_STATE/logs"
assert_file_contains "$TMPDIR/launch.out" "Dashboard: MULTIAGENT_SESSION=launch-cross-repo MULTIAGENT_ROOT=$LAUNCH_TARGET $MULTIAGENT watch"
LAUNCH_BOOTSTRAP="$LAUNCH_STATE/orchestrator-bootstrap.sh"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$LAUNCH_BOOTSTRAP")"
assert_file_contains "$MOCK_TMUX_LOG" "pipe-pane launch-cross-repo:orchestrator cat >> $LAUNCH_STATE/logs/orchestrator.log"
assert_file_contains "$LAUNCH_BOOTSTRAP" "--cd $LAUNCH_STATE"
if [[ "$HOST_KERNEL" == Linux ]]; then
  assert_file_contains "$LAUNCH_BOOTSTRAP" "$MULTIAGENT role-exec"
  assert_file_contains "$LAUNCH_BOOTSTRAP" "--allow-write $LAUNCH_STATE"
  assert_file_not_contains "$LAUNCH_BOOTSTRAP" "--allow-write $LAUNCH_TARGET"
  assert_file_contains "$LAUNCH_BOOTSTRAP" "--dangerously-bypass-approvals-and-sandbox"
else
  assert_file_contains "$LAUNCH_BOOTSTRAP" "--sandbox workspace-write"
  assert_file_not_contains "$LAUNCH_BOOTSTRAP" "--dangerously-bypass-approvals-and-sandbox"
fi
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_RESUME=0"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_LOG_DIR=$LAUNCH_STATE/logs"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_VERIFIER_MAX_ITERATIONS=3"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export WORKER_CLI=claude"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export SUBAGENT_CLI=claude"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export VERIFIER_CLI=codex"
assert_file_contains "$LAUNCH_BOOTSTRAP" "Multiagent launch mode:"
assert_file_contains "$LAUNCH_BOOTSTRAP" "$(printf '%q' "$LAUNCH_STATE/runtime_state/orchestrator-prompt-bundle.md")"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_LIFECYCLE_ENFORCEMENT=1"
assert_file_contains "$LAUNCH_STATE/runtime_state/orchestrator-prompt-bundle.md" "BEGIN ORCHESTRATOR ROLE"
assert_file_contains "$LAUNCH_STATE/runtime_state/orchestrator-prompt-bundle.md" "BEGIN MANDATORY IMPLEMENTATION LIFECYCLE"
LAUNCH_WORKFLOW_ID="$(tr -d '\r\n' <"$LAUNCH_STATE/runtime_state/active-workflow-id")"
assert_file_contains "$LAUNCH_STATE/workflows/$LAUNCH_WORKFLOW_ID/lifecycle/lifecycle.env" "phase=pre-implementation"
if grep -Fq "$LAUNCH_TARGET/orchestrator_prompt.md" "$MOCK_TMUX_LOG" "$TMPDIR/launch.out" "$LAUNCH_BOOTSTRAP"; then
  echo "expected launch to use script-dir orchestrator prompt, not target-root prompt" >&2
  cat "$MOCK_TMUX_LOG" >&2
  cat "$TMPDIR/launch.out" >&2
  cat "$LAUNCH_BOOTSTRAP" >&2
  exit 1
fi

rm -f "$MOCK_TMUX_LOG"
MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-resume" \
  MULTIAGENT_ROOT= \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_VERIFIER_MAX_ITERATIONS=5 \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-resume-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-resume-policy/write-policy.paths" \
  "$ROOT/launch.sh" --session launch-resume --root "$LAUNCH_TARGET" --resume --no-attach >"$TMPDIR/launch-resume.out"
assert_file_contains "$TMPDIR/launch-resume.out" "Resume mode: 1"
assert_file_contains "$TMPDIR/launch-resume.out" "Verifier max iterations: 5"
assert_file_contains "$TMPDIR/launch-resume.out" "Worker CLI: claude"
assert_file_contains "$TMPDIR/launch-resume.out" "Verifier CLI: codex"
LAUNCH_RESUME_BOOTSTRAP="$TMPDIR/launch-resume-state/orchestrator-bootstrap.sh"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$LAUNCH_RESUME_BOOTSTRAP")"
assert_file_contains "$LAUNCH_RESUME_BOOTSTRAP" "export MULTIAGENT_RESUME=1"
assert_file_contains "$LAUNCH_RESUME_BOOTSTRAP" "export MULTIAGENT_VERIFIER_MAX_ITERATIONS=5"
assert_file_contains "$LAUNCH_RESUME_BOOTSTRAP" "resume"

if MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-invalid-verifier-cap" \
  MULTIAGENT_ROOT= \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_VERIFIER_MAX_ITERATIONS=0 \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-invalid-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-invalid-policy/write-policy.paths" \
  "$ROOT/launch.sh" --session launch-invalid-verifier-cap --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch-invalid.out" 2>&1; then
  echo "expected invalid verifier max iterations to fail" >&2
  cat "$TMPDIR/launch-invalid.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/launch-invalid.out" "MULTIAGENT_VERIFIER_MAX_ITERATIONS must be a positive integer"

EXPLICIT_PROMPT="$TMPDIR/custom-orchestrator-prompt.md"
printf 'custom prompt\n' >"$EXPLICIT_PROMPT"
rm -f "$MOCK_TMUX_LOG"
MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-explicit-prompt" \
  MULTIAGENT_PROMPT="$EXPLICIT_PROMPT" \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-explicit-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-explicit-policy/write-policy.paths" \
  "$ROOT/launch.sh" --session launch-explicit-prompt --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch-explicit.out"
assert_file_contains "$TMPDIR/launch-explicit-state/orchestrator-bootstrap.sh" "$(printf '%q' "$TMPDIR/launch-explicit-state/runtime_state/orchestrator-prompt-bundle.md")"
assert_file_contains "$TMPDIR/launch-explicit-state/runtime_state/orchestrator-prompt-bundle.md" "custom prompt"
assert_file_contains "$TMPDIR/launch-explicit-state/runtime_state/orchestrator-prompt-bundle.md" "BEGIN MANDATORY IMPLEMENTATION LIFECYCLE"

rm -f "$MOCK_TMUX_LOG"
MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-qwen" \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-qwen-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-qwen-policy/write-policy.paths" \
  ORCHESTRATOR_CLI=qwen WORKER_CLI=qwen SUBAGENT_CLI=qwen VERIFIER_CLI=qwen \
  "$ROOT/launch.sh" --session launch-qwen --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch-qwen.out"
QWEN_BOOTSTRAP="$TMPDIR/launch-qwen-state/orchestrator-bootstrap.sh"
assert_file_contains "$TMPDIR/launch-qwen.out" "Worker CLI: qwen"
assert_file_contains "$QWEN_BOOTSTRAP" "$MULTIAGENT agent run --backend qwen"
assert_file_contains "$QWEN_BOOTSTRAP" "--trace-dir $TMPDIR/launch-qwen-state/logs/agents/orchestrator"
assert_file_contains "$TMPDIR/launch-qwen-state/runtime_state/agent-backends.tsv" $'qwen\t'
assert_file_contains "$TMPDIR/launch-qwen-state/runtime_state/agent-backends.tsv" "qwen-code test-1.0"

if MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-missing-qwen" \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-missing-qwen-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-missing-qwen-policy/write-policy.paths" \
  ORCHESTRATOR_CLI=qwen WORKER_CLI=qwen SUBAGENT_CLI=qwen VERIFIER_CLI=qwen \
  QWEN_BIN="$TMPDIR/does-not-exist/qwen" \
  "$ROOT/launch.sh" --session launch-missing-qwen --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch-missing-qwen.out" 2>&1; then
  echo "expected missing Qwen Code executable to fail launch preflight" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/launch-missing-qwen.out" "run qwen coding-agent preflight"

REPAIR_STATE="$TMPDIR/repair-state"
mkdir -p "$REPAIR_STATE"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent finding-create invalid-prose-finding \
  --severity blocking \
  --type compile_failure \
  --summary "Prose-only compile finding" \
  --evidence-json '"go test failed somewhere"' \
  --required-resolution "Final diff must compile." >"$TMPDIR/finding-prose-invalid.out" 2>&1; then
  echo "expected blocking finding with prose-only evidence to fail" >&2
  cat "$TMPDIR/finding-prose-invalid.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/finding-prose-invalid.out" "evidence JSON must be an object"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent finding-create invalid-command-finding \
  --severity blocking \
  --type compile_failure \
  --summary "Missing command evidence" \
  --evidence-json '{"source_evidence":"compile failed in verifier output"}' \
  --required-resolution "Final diff must compile." >"$TMPDIR/finding-command-invalid.out" 2>&1; then
  echo "expected compile failure finding without command+returncode to fail" >&2
  cat "$TMPDIR/finding-command-invalid.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/finding-command-invalid.out" "compile_failure finding evidence requires command and returncode"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent finding-create build-go-ofrep \
  --severity blocking \
  --type compile_failure \
  --summary "Changed Go packages do not compile" \
  --affected internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go \
  --evidence-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":1,"stderr_excerpt":"undefined: req.Request"}' \
  --required-resolution "Final diff must compile with rc=0 for both changed Go packages." >"$TMPDIR/finding-create.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent todo-create todo-017 \
  --source-finding-id build-go-ofrep \
  --task "Fix Go compile failure in changed packages." \
  --context "Exact verifier evidence." \
  --done-criteria "run go test ./internal/server/ofrep" \
  --done-criteria "record returncode=0 after final diff" >"$TMPDIR/todo-create.out"
assert_file_contains "$REPAIR_STATE/todos/todo-017/todo.json" '"required_commands":'
assert_file_contains "$REPAIR_STATE/todos/todo-017/todo.json" '"go test ./internal/server/ofrep"'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent resolution-create todo-017 \
  --worker worker-02-ofrep-build \
  --status resolved \
  --changed internal/server/ofrep/evaluation.go \
  --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":1}]' \
  --why "Claimed fixed despite failing validation." >"$TMPDIR/resolution-bad.out" 2>&1; then
  echo "expected resolved todo with nonzero validation rc to fail" >&2
  cat "$TMPDIR/resolution-bad.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/resolution-bad.out" "nonzero rc=1"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent resolution-create todo-017 \
  --worker worker-02-ofrep-build \
  --status resolved \
  --changed internal/server/ofrep/evaluation.go \
  --validation-json '[{"cmd":"go test ./internal/server/evaluation","rc":0}]' \
  --why "Wrong package compiled." >"$TMPDIR/resolution-missing-required.out" 2>&1; then
  echo "expected resolved todo missing required command evidence to fail" >&2
  cat "$TMPDIR/resolution-missing-required.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/resolution-missing-required.out" "missing required command"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent resolution-create todo-017 \
  --worker worker-02-ofrep-build \
  --status resolved \
  --changed internal/server/ofrep/evaluation.go \
  --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":0}]' \
  --why "Changed package compiles after the final diff." >"$TMPDIR/resolution-create.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent todo-status todo-017 closed >"$TMPDIR/direct-close.out"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-missing-closure.out" 2>&1; then
  echo "expected direct closed todo without verifier closure to fail gate-check" >&2
  cat "$TMPDIR/gate-missing-closure.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-missing-closure.out" "closed-todo-missing-verifier-closure"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent todo-status todo-017 resolved >"$TMPDIR/reopen-resolved.out"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent todo-close todo-017 \
  --verified-by verifier-01-ofrep-build \
  --recheck-json '{"accepted":false,"finding_rechecked":"build-go-ofrep"}' >"$TMPDIR/close-rejected.out" 2>&1; then
  echo "expected verifier closure with accepted=false to fail" >&2
  cat "$TMPDIR/close-rejected.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/close-rejected.out" "accepted=true"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent todo-close todo-017 \
  --verified-by verifier-01-ofrep-build \
  --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0}],"final_diff_hash":"abc123"}' \
  --notes "Verifier rechecked original finding after worker resolution." >"$TMPDIR/todo-close.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-closed.out"
assert_file_contains "$TMPDIR/gate-closed.out" "accepted"
assert_file_contains "$REPAIR_STATE/todos/todo-017/closure.json" '"verified_by": "verifier-01-ofrep-build"'
cp "$REPAIR_STATE/findings/build-go-ofrep/finding.json" "$TMPDIR/build-go-ofrep.finding.json"
printf '{"id":"build-go-ofrep","severity":"blocking","type":"compile_failure","summary":"mutated after closure","affected_paths":[],"evidence":{"command":"go test ./internal/server/ofrep","returncode":1},"required_resolution":"mutated","created_at":"mutated"}\n' >"$REPAIR_STATE/findings/build-go-ofrep/finding.json"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-mutated-finding.out" 2>&1; then
  echo "expected gate-check to reject a closed todo after source finding mutation" >&2
  cat "$TMPDIR/gate-mutated-finding.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-mutated-finding.out" "closed-todo-source-finding-hash-changed"
cp "$TMPDIR/build-go-ofrep.finding.json" "$REPAIR_STATE/findings/build-go-ofrep/finding.json"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-restored-finding.out"
assert_file_contains "$TMPDIR/gate-restored-finding.out" "accepted"

VERIFIER_VERDICT_STATE="$TMPDIR/verifier-verdict-state"
mkdir -p "$VERIFIER_VERDICT_STATE/subagents/worker-01-fix" "$VERIFIER_VERDICT_STATE/subagents/verifier-01-fix"
printf 'BLOCKING\nworker text must not control the final gate\n' >"$VERIFIER_VERDICT_STATE/subagents/worker-01-fix/last-message.txt"
printf 'BLOCKING\nsource contract remains unsatisfied\n' >"$VERIFIER_VERDICT_STATE/subagents/verifier-01-fix/last-message.txt"
if MULTIAGENT_STATE_DIR="$VERIFIER_VERDICT_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-blocking.out" 2>&1; then
  echo "expected latest blocking verifier verdict to fail gate-check" >&2
  cat "$TMPDIR/gate-verifier-blocking.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-blocking.out" $'reject\tlatest-verifier-blocking\tverifier=verifier-01-fix'
mkdir -p "$VERIFIER_VERDICT_STATE/subagents/verifier-02-fix"
printf 'ACCEPTED\nfinal diff rechecked after repair\n' >"$VERIFIER_VERDICT_STATE/subagents/verifier-02-fix/last-message.txt"
python3 - "$VERIFIER_VERDICT_STATE/subagents/verifier-01-fix/last-message.txt" "$VERIFIER_VERDICT_STATE/subagents/verifier-02-fix/last-message.txt" <<'PY'
import os
import sys

os.utime(sys.argv[1], ns=(1_000_000_000, 1_000_000_000))
os.utime(sys.argv[2], ns=(2_000_000_000, 2_000_000_000))
PY
MULTIAGENT_STATE_DIR="$VERIFIER_VERDICT_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-accepted.out"
assert_file_contains "$TMPDIR/gate-verifier-accepted.out" "accepted"
mkdir -p "$VERIFIER_VERDICT_STATE/subagents/verifier-03-fix"
printf 'Verifier process exited before a final recommendation.\n' >"$VERIFIER_VERDICT_STATE/subagents/verifier-03-fix/last-message.txt"
python3 - "$VERIFIER_VERDICT_STATE/subagents/verifier-03-fix/last-message.txt" <<'PY'
import os
import sys

os.utime(sys.argv[1], ns=(3_000_000_000, 3_000_000_000))
PY
if MULTIAGENT_STATE_DIR="$VERIFIER_VERDICT_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-missing.out" 2>&1; then
  echo "expected newest verifier artifact without a verdict to fail gate-check" >&2
  cat "$TMPDIR/gate-verifier-missing.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-missing.out" $'reject\tlatest-verifier-missing-verdict\tverifier=verifier-03-fix'

HASH_GATE_ROOT="$TMPDIR/hash-gate-root"
HASH_GATE_STATE="$TMPDIR/hash-gate-state"
mkdir -p "$HASH_GATE_ROOT" "$HASH_GATE_STATE/subagents/verifier-01-hash"
git -C "$HASH_GATE_ROOT" init -q
git -C "$HASH_GATE_ROOT" config user.email test@example.com
git -C "$HASH_GATE_ROOT" config user.name Test
git -C "$HASH_GATE_ROOT" config commit.gpgsign false
printf 'before\n' >"$HASH_GATE_ROOT/source.txt"
git -C "$HASH_GATE_ROOT" add source.txt
git -C "$HASH_GATE_ROOT" commit -qm initial
printf 'after\n' >"$HASH_GATE_ROOT/source.txt"
printf 'ACCEPTED\nsource reviewed without hash binding\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
if MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-unbound-hash.out" 2>&1; then
  echo "expected verifier acceptance without the current final diff hash to fail gate-check" >&2
  cat "$TMPDIR/gate-verifier-unbound-hash.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-unbound-hash.out" $'reject\tlatest-verifier-final-diff-hash-mismatch'
HASH_GATE_DIFF_SHA="$("$MULTIAGENT" snapshot --root "$HASH_GATE_ROOT" --format shell | awk '{print $1}')"
printf 'ACCEPTED\nbuild-verification-passed: final-diff-sha256=%s compile_clean=true returncode=0\n' "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-bound-hash.out"
assert_file_contains "$TMPDIR/gate-verifier-bound-hash.out" "accepted"
printf 'malicious post-review source\n' >"$HASH_GATE_ROOT/untracked-source.txt"
if MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-untracked-bypass.out" 2>&1; then
  echo "expected post-review untracked source to invalidate verifier evidence" >&2
  cat "$TMPDIR/gate-verifier-untracked-bypass.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-untracked-bypass.out" $'reject\tlatest-verifier-final-diff-hash-mismatch'
HASH_GATE_UNTRACKED_SHA="$("$MULTIAGENT" snapshot --root "$HASH_GATE_ROOT" --format shell | awk '{print $1}')"
printf 'ACCEPTED\nbuild-verification-passed: final-diff-sha256=%s compile_clean=true returncode=0\n' "$HASH_GATE_UNTRACKED_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-untracked-bound.out"
assert_file_contains "$TMPDIR/gate-verifier-untracked-bound.out" "accepted"
rm "$HASH_GATE_ROOT/untracked-source.txt"
printf 'ACCEPTED\nbuild-verification-passed: final-diff-sha256=%s compile_clean=true returncode=0\n' "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'ACCEPTED\n{"verdict":"ACCEPTED","final_diff_sha256":"%s","build_verification_passed":{"final_diff_sha256":"%s","compile_clean":true,"commands":[{"cmd":"test -f source.txt","rc":0}]}}\n' \
  "$HASH_GATE_DIFF_SHA" "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'running\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-terminal-reconciled.out"
assert_file_contains "$HASH_GATE_STATE/subagents/verifier-01-hash/status" "done"
assert_file_contains "$TMPDIR/gate-verifier-terminal-reconciled.out" "accepted"
rm "$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'running\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
if MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-active.out" 2>&1; then
  echo "expected an active verifier without a terminal report to block the final gate" >&2
  cat "$TMPDIR/gate-verifier-active.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-active.out" $'reject\tactive-verifier\tverifier-01-hash\trunning'
printf 'done\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
printf 'ACCEPTED\n{"verdict":"ACCEPTED","final_diff_sha256":"%s","build_verification_passed":{"final_diff_sha256":"%s","compile_clean":true,"commands":[{"cmd":"test -f source.txt","rc":0}]}}\n' \
  "$HASH_GATE_DIFF_SHA" "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-structured-hash.out"
assert_file_contains "$TMPDIR/gate-verifier-structured-hash.out" "accepted"
printf 'policy-gate: source owner checked\nbuild-verification-passed: final-diff-sha256=%s compile_clean=true returncode=0\nfinal-recommendation: accept; source contract satisfied\n' \
  "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'running\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-final-recommendation.out"
assert_file_contains "$HASH_GATE_STATE/subagents/verifier-01-hash/status" "done"
assert_file_contains "$TMPDIR/gate-verifier-final-recommendation.out" "accepted"
printf 'ACCEPTED final_diff_sha256=%s\nbuild-verification-passed: final-diff-sha256=%s compile_clean=true returncode=0\n' \
  "$HASH_GATE_DIFF_SHA" "$HASH_GATE_DIFF_SHA" >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'running\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-inline-hash.out"
assert_file_contains "$HASH_GATE_STATE/subagents/verifier-01-hash/status" "done"
assert_file_contains "$TMPDIR/gate-verifier-inline-hash.out" "accepted"
printf 'policy-gate: source owner checked\nfinal-recommendation: block; source contract missing\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
printf 'running\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/status"
if MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-block-recommendation.out" 2>&1; then
  echo "expected normalized final block recommendation to block the gate" >&2
  exit 1
fi
assert_file_contains "$HASH_GATE_STATE/subagents/verifier-01-hash/status" "blocked"
assert_file_contains "$TMPDIR/gate-verifier-block-recommendation.out" $'reject\tlatest-verifier-blocking\tverifier=verifier-01-hash'
printf 'verdict=REJECTED\nrequired_resolution=repair semantic contract\n' >"$HASH_GATE_STATE/subagents/verifier-01-hash/last-message.txt"
if MULTIAGENT_ROOT="$HASH_GATE_ROOT" MULTIAGENT_STATE_DIR="$HASH_GATE_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
  "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-verifier-rejected-variant.out" 2>&1; then
  echo "expected normalized REJECTED verifier verdict to block the gate" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-verifier-rejected-variant.out" $'reject\tlatest-verifier-blocking\tverifier=verifier-01-hash'

AUTOCREATE_RESOLUTION_STATE="$TMPDIR/autocreate-resolution-state"
mkdir -p "$AUTOCREATE_RESOLUTION_STATE"
if MULTIAGENT_STATE_DIR="$AUTOCREATE_RESOLUTION_STATE" "$MULTIAGENT" subagent resolution-create TODO-autocreate --worker worker-autocreate --status resolved --validation-json '[{"cmd":"go test ./pkg","rc":0}]' --why "Structured resolution" >"$TMPDIR/resolution-no-autocreate.out" 2>&1; then
  echo "expected structured resolution-create without auto-create to fail for a missing todo" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/resolution-no-autocreate.out" "no todo: TODO-autocreate"
MULTIAGENT_STATE_DIR="$AUTOCREATE_RESOLUTION_STATE" MULTIAGENT_RESOLUTION_AUTOCREATE_TODO=1 "$MULTIAGENT" subagent resolution-create TODO-autocreate --worker worker-autocreate --status resolved --validation-json '[{"cmd":"go test ./pkg","rc":0}]' --why "Structured resolution" >"$TMPDIR/resolution-autocreate.out"
assert_file_contains "$TMPDIR/resolution-autocreate.out" $'resolution recorded\tTODO-autocreate\tworker-autocreate\tresolved'
assert_file_contains "$AUTOCREATE_RESOLUTION_STATE/todos/TODO-autocreate/resolution.json" '"cmd": "go test ./pkg"'
assert_file_contains "$AUTOCREATE_RESOLUTION_STATE/todos/TODO-autocreate/resolution.json" '"rc": 0'
if MULTIAGENT_STATE_DIR="$AUTOCREATE_RESOLUTION_STATE" "$MULTIAGENT" subagent gate-check >"$TMPDIR/resolution-autocreate-gate.out" 2>&1; then
  echo "expected auto-created structured resolution to remain blocked until verifier closure" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/resolution-autocreate-gate.out" $'reject\topen-blocking-todo\tfinding=auto-TODO-autocreate\ttodo=TODO-autocreate\tstatus=resolved'

MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-acquire go-ofrep \
  --owner worker-02-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" \
  --resource-risk "go test under Docker/Rosetta" >"$TMPDIR/lease-acquire.out"
assert_file_contains "$TMPDIR/lease-acquire.out" $'validation lease acquired\tgo-ofrep\tworker-02-ofrep-build\trunning'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-acquire go-ofrep-dup \
  --owner verifier-01-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" >"$TMPDIR/lease-conflict.out" 2>&1; then
  echo "expected duplicate active validation lease to fail" >&2
  cat "$TMPDIR/lease-conflict.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/lease-conflict.out" "validation lease conflict"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-status go-ofrep passed \
  --result-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":0}' >"$TMPDIR/lease-passed.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-acquire go-ofrep-followup \
  --owner verifier-01-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" >"$TMPDIR/lease-followup.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-list --state running >"$TMPDIR/lease-list.out"
assert_file_contains "$TMPDIR/lease-list.out" $'go-ofrep-followup\trunning\tverifier-01-ofrep-build'
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-show go-ofrep >"$TMPDIR/lease-show.out"
assert_file_contains "$TMPDIR/lease-show.out" '"returncode": 0'
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-run validation-run-ok \
  --owner worker-02-ofrep-build \
  --target "unit-target" \
  --resource-risk "cheap test command" \
  -- bash -lc 'printf validation-ok' >"$TMPDIR/validation-run-ok.out"
assert_file_contains "$TMPDIR/validation-run-ok.out" "validation-ok"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-show validation-run-ok >"$TMPDIR/validation-run-ok-lease.out"
assert_file_contains "$TMPDIR/validation-run-ok-lease.out" '"state": "passed"'
assert_file_contains "$TMPDIR/validation-run-ok-lease.out" '"returncode": 0'
mkdir -p "$TMPDIR/not-root"
(
  cd "$TMPDIR/not-root"
  MULTIAGENT_ROOT="$ROOT" MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-run validation-run-cwd \
    --owner worker-02-ofrep-build \
    --target "unit-target-cwd" \
    -- bash -lc 'pwd' >"$TMPDIR/validation-run-cwd.out"
)
assert_file_contains "$TMPDIR/validation-run-cwd.out" "$ROOT"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-show validation-run-cwd >"$TMPDIR/validation-run-cwd-lease.out"
assert_file_contains "$TMPDIR/validation-run-cwd-lease.out" "\"cwd\": \"$ROOT\""
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-run validation-run-fail \
  --owner worker-02-ofrep-build \
  --target "unit-target-fail" \
  -- bash -lc 'printf validation-fail >&2; exit 7' >"$TMPDIR/validation-run-fail.out" 2>"$TMPDIR/validation-run-fail.err"; then
  echo "expected validation-run to return the command failure rc" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/validation-run-fail.err" "validation-fail"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-show validation-run-fail >"$TMPDIR/validation-run-fail-lease.out"
assert_file_contains "$TMPDIR/validation-run-fail-lease.out" '"state": "failed"'
assert_file_contains "$TMPDIR/validation-run-fail-lease.out" '"returncode": 7'
set +e
MULTIAGENT_VALIDATION_TIMEOUT_SECONDS=1 MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-run validation-run-timeout \
  --owner worker-02-ofrep-build \
  --target "unit-target-timeout" \
  -- bash -lc 'sleep 2' >"$TMPDIR/validation-run-timeout.out" 2>"$TMPDIR/validation-run-timeout.err"
timeout_rc=$?
set -e
if [[ "$timeout_rc" -ne 124 ]]; then
  echo "expected validation-run timeout rc 124, got $timeout_rc" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/validation-run-timeout.err" "validation-run timed out after 1 seconds"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-lease-show validation-run-timeout >"$TMPDIR/validation-run-timeout-lease.out"
assert_file_contains "$TMPDIR/validation-run-timeout-lease.out" '"state": "timed-out"'
assert_file_contains "$TMPDIR/validation-run-timeout-lease.out" '"returncode": 124'
assert_file_contains "$TMPDIR/validation-run-timeout-lease.out" '"timed_out": true'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$MULTIAGENT" subagent validation-run validation-run-conflict \
  --owner verifier-01-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  -- bash -lc 'true' >"$TMPDIR/validation-run-conflict.out" 2>&1; then
  echo "expected validation-run to reject duplicate active validation target" >&2
  cat "$TMPDIR/validation-run-conflict.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/validation-run-conflict.out" "validation lease conflict"

assert_file_contains "$ROOT/orchestrator_prompt.md" "Do not inspect recovery state"
assert_file_contains "$ROOT/orchestrator_prompt.md" 'When `MULTIAGENT_RESUME=1`'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'Only in that mode'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'MULTIAGENT_VERIFIER_MAX_ITERATIONS'
assert_file_contains "$ROOT/docs/control-plane-boundary.md" "preventing detached or late"
assert_file_contains "$ROOT/orchestrator_prompt.md" 'SUBAGENT_CLI="$VERIFIER_CLI" multiagent subagent spawn'
assert_file_contains "$ROOT/orchestrator_prompt.md" "Core Disciplines"
assert_file_contains "$ROOT/orchestrator_prompt.md" "intent-contract.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "parallel-execution.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "validation-scheduling.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "Role Routing"
assert_file_contains "$ROOT/orchestrator_prompt.md" "contract-scout.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "scope-guard.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "validation-coordinator.md"
assert_file_contains "$ROOT/orchestrator_prompt.md" "failed relevant validation"
assert_file_contains "$ROOT/orchestrator_prompt.md" "proxy/scaffold"
assert_file_contains "$ROOT/orchestrator_prompt.md" "Prompt Modules"
assert_file_contains "$ROOT/orchestrator_prompt.md" "agent-spawning.md"
assert_file_contains "$ROOT/prompts/worker.md" "Worker Role Prompt"
assert_file_contains "$ROOT/prompts/worker.md" "Ponytail Implementation Discipline"
assert_file_contains "$ROOT/prompts/worker.md" "return shape, or package placement"
assert_file_contains "$ROOT/prompts/worker.md" "additive public surface"
assert_file_contains "$ROOT/prompts/worker.md" "one expensive validation command"
assert_file_contains "$ROOT/prompts/worker.md" "validation lease"
assert_file_contains "$ROOT/prompts/worker.md" "validation-run"
assert_file_contains "$ROOT/prompts/worker.md" "validation-lease-acquire"
assert_file_contains "$ROOT/prompts/worker.md" "legitimate product or visible-test paths"
assert_file_contains "$ROOT/prompts/worker.md" "validation-repair-needed:"
assert_file_contains "$ROOT/prompts/worker.md" "structured worker"
assert_file_contains "$ROOT/prompts/worker.md" "resolution-create"
assert_file_contains "$ROOT/prompts/worker.md" "assembled production"
assert_file_contains "$ROOT/prompts/verifier.md" "Verifier Role Prompt"
assert_file_contains "$ROOT/prompts/verifier.md" "Hidden Contract Verification"
assert_file_contains "$ROOT/prompts/verifier.md" "unresolved risk"
assert_file_contains "$ROOT/prompts/verifier.md" "component interaction test"
assert_file_contains "$ROOT/prompts/verifier.md" "overlapping validators"
assert_file_contains "$ROOT/prompts/verifier.md" "validation lease"
assert_file_contains "$ROOT/prompts/verifier.md" "validation-lease-show"
assert_file_contains "$ROOT/prompts/verifier.md" "blocked-validations:"
assert_file_contains "$ROOT/prompts/verifier.md" "Do not rely on leaked evaluator tests"
assert_file_contains "$ROOT/prompts/verifier.md" "source-derived equivalence classes"
assert_file_contains "$ROOT/prompts/verifier.md" "verify parity for each named path"
assert_file_contains "$ROOT/prompts/verifier.md" "reject first-match-only fixes"
assert_file_contains "$ROOT/prompts/verifier.md" "machine-readable verifier finding"
assert_file_contains "$ROOT/prompts/verifier.md" "finding-create"
assert_file_contains "$ROOT/prompts/verifier.md" 'MULTIAGENT_BIN:-/opt/multiagent/bin/multiagent'
assert_file_contains "$ROOT/prompts/verifier.md" "finding-create FINDING_ID"
assert_file_contains "$ROOT/prompts/verifier.md" "--severity blocking"
assert_file_contains "$ROOT/prompts/verifier.md" "--affected PATH[,PATH...]"
assert_file_contains "$ROOT/prompts/verifier.md" "--evidence-json"
assert_file_contains "$ROOT/prompts/verifier.md" "do not invent"
assert_file_contains "$ROOT/prompts/verifier.md" "assembled production"
assert_file_contains "$ROOT/prompts/worker.md" 'Every entry in a `resolved` report'
assert_file_contains "$ROOT/prompts/worker.md" 'must have `rc: 0`'
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" 'All `validation-json` entries in a resolved report'
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "Contract Scout Role Prompt"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "must-preserve"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "mismatch-risk"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "unexported helper signatures"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "task-shape classification"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "fixture assets"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "parity across every"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "first-match-only behavior"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "Acceptance Scout Role Prompt"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "hidden-contract-ledger"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "Do not rely on leaked evaluator tests"
assert_file_contains "$ROOT/orchestrator_prompt.md" "acceptance-scout.md"
assert_file_contains "$ROOT/prompts/roles/scope-guard.md" "Scope Guard Role Prompt"
assert_file_contains "$ROOT/prompts/roles/scope-guard.md" "blocking-scope-findings"
assert_file_contains "$ROOT/prompts/roles/validation-coordinator.md" "Validation Coordinator Role Prompt"
assert_file_contains "$ROOT/prompts/roles/validation-coordinator.md" "duplicate package validation"
assert_file_contains "$ROOT/prompts/roles/validation-coordinator.md" "one active validator per package/path"
assert_file_contains "$ROOT/prompts/roles/validation-coordinator.md" "validation lease table"
assert_file_contains "$ROOT/prompts/playbooks/intent-contract.md" "Intent And Contract Playbook"
assert_file_contains "$ROOT/prompts/playbooks/intent-contract.md" "proxy/scaffold limitations"
assert_file_contains "$ROOT/prompts/playbooks/intent-contract.md" "contract-ledger"
assert_file_contains "$ROOT/prompts/playbooks/parallel-execution.md" "Parallel Execution Playbook"
assert_file_contains "$ROOT/prompts/playbooks/parallel-execution.md" "Default to broad safe fan-out"
assert_file_contains "$ROOT/prompts/playbooks/parallel-execution.md" "If one subtree is blocked"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "Validation Scheduling Playbook"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "Validation Lease"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "validation-run"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "validation-lease-acquire"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "validation-lease-status"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "next-validation-owner"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "Do not spawn a verifier"
assert_file_contains "$ROOT/prompts/playbooks/validation-scheduling.md" "repair-routing:"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "Finding Todo Loop Playbook"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "verifier writes structured findings"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "resolution-create"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "todo-close"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "gate-check"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Agent Spawning Playbook"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Ponytail implementation discipline"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Ponytail over-engineering pass"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "hidden-contract probes"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" 'MAX_ITERATIONS` is an escalation threshold'
if grep -Fq -- "accepted follow-up count reaches" "$ROOT/prompts/playbooks/agent-spawning.md"; then
  echo "iteration threshold must not be an acceptance condition" >&2
  exit 1
fi
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "todo-create"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "todo-close"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "gate-check"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "ownership blocker"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" 'SUBAGENT_CLI="$WORKER_CLI" multiagent subagent spawn'
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "multiagent subagent wait worker-01-task"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Orchestration Routing Playbook"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Contract Scout Workflow"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Scope Guard Workflow"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Validation Coordinator Workflow"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "validation-scheduling.md"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Required Worker First Instruction"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Safety Rules"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "parallel-execution.md"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Validation Failure Repair Workflow"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "finding-todo-loop.md"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "todo-close"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "ownership blocker"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "Build verification failures are not eval-wrapper paperwork"
assert_file_contains "$ROOT/prompts/playbooks/dag.md" "DAG Workflow Playbook"
assert_file_contains "$ROOT/prompts/playbooks/recovery.md" "Recovery Playbook"
assert_file_contains "$ROOT/prompts/playbooks/write-policy.md" "Write Policy Playbook"
assert_file_contains "$ROOT/README.md" "Launches are clean by default"
assert_file_contains "$ROOT/README.md" "## Requirements"
assert_file_contains "$ROOT/README.md" "Python 3.8 or newer is required only for evaluation"
assert_file_contains "$ROOT/README.md" "no third-party Python package"
assert_file_contains "$ROOT/README.md" "./launch.sh --resume"
assert_file_contains "$ROOT/README.md" "Prompt Modules"
assert_file_contains "$ROOT/README.md" "validation lease table"
assert_file_contains "$ROOT/README.md" "validation-run"
assert_file_contains "$ROOT/README.md" "validation-lease-acquire"
assert_file_contains "$ROOT/README.md" "Contract Scout Workflow"
assert_file_contains "$ROOT/README.md" "acceptance-scout.md"
assert_file_contains "$ROOT/README.md" "Scope Guard Workflow"
assert_file_contains "$ROOT/README.md" "Validation Coordinator Workflow"
assert_file_contains "$ROOT/README.md" "bounded repair worker"
assert_file_contains "$ROOT/README.md" "proxy behavior"
assert_file_contains "$ROOT/README.md" "Verifier Workflow"
assert_file_contains "$ROOT/README.md" "MULTIAGENT_VERIFIER_MAX_ITERATIONS=3"
assert_file_contains "$ROOT/README.md" "compact contract ledger"
assert_file_contains "$ROOT/README.md" "hidden-contract edge cases"
assert_file_contains "$ROOT/README.md" "hidden-contract-ledger"
assert_file_contains "$ROOT/README.md" 'WORKER_CLI`: worker coding-agent backend for manual worker windows'
assert_file_contains "$ROOT/README.md" 'VERIFIER_CLI`: verifier backend, default `codex`'
assert_file_contains "$ROOT/README.md" "Evaluation Framework"
assert_file_contains "$ROOT/README.md" "Parallel DAG Discipline"
assert_file_contains "$ROOT/README.md" "Structured Repair Loop"
assert_file_contains "$ROOT/README.md" "finding-todo-loop.md"
assert_file_contains "$ROOT/README.md" "todo-close"
assert_file_contains "$ROOT/README.md" 'Python under `evaluation/`'
assert_file_contains "$ROOT/README.md" "## System Flow"
assert_file_contains "$ROOT/README.md" "flowchart TD"
assert_file_contains "$ROOT/README.md" 'benchmark execution, status reading, and provenance'
assert_file_contains "$ROOT/README.md" 'orchestration` adapter covers planning behavior'
assert_file_contains "$ROOT/README.md" "evaluation/tasks"
assert_file_contains "$ROOT/evaluation/README.md" "large-update-300"
assert_file_contains "$ROOT/evaluation/README.md" "Low-signal orchestration cases"
assert_file_contains "$ROOT/orchestrator_prompt.md" "MULTIAGENT_PROMPT_MODULE_ROOT"
assert_file_contains "$ROOT/src/runtime.rs" "MULTIAGENT_PROMPT_MODULE_ROOT"
assert_file_not_contains "$ROOT/launch.sh" "python"
assert_file_contains "$ROOT/prompts/verifier.md" "state-space partition audit"
assert_file_contains "$ROOT/prompts/verifier.md" "mixed-category, unknown/forward-compatible variant"
assert_file_contains "$ROOT/prompts/verifier.md" "state-space-partition-audit:"
assert_file_contains "$ROOT/prompts/verifier.md" "behavior-verification-passed:"
assert_file_contains "$ROOT/prompts/verifier.md" "narrowest visible test file"
assert_file_contains "$ROOT/prompts/verifier.md" "Syntax checks, compile-only commands"
assert_file_contains "$ROOT/prompts/verifier.md" "command=... returncode=0"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "partition contract"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/prompts/worker.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/prompts/verifier.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/orchestrator_prompt.md" "historical-contract-ledger:"
assert_file_contains "$ROOT/prompts/verifier.md" "source review plus"
assert_file_contains "$ROOT/prompts/verifier.md" "old/stale expectation"
assert_file_contains "$ROOT/prompts/verifier.md" "git diff --name-only"
assert_file_contains "$ROOT/prompts/worker.md" "stale hunk"
assert_file_contains "$ROOT/prompts/verifier.md" "stale-hunk"
assert_file_contains "$ROOT/prompts/worker.md" "git diff --name-only"
assert_file_contains "$ROOT/prompts/verifier.md" "replacement-probe-passed:"
assert_file_contains "$ROOT/prompts/verifier.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/prompts/verifier.md" "final-output-field="
assert_file_contains "$ROOT/prompts/verifier.md" "expected-output-count=N"
assert_file_contains "$ROOT/prompts/verifier.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/prompts/verifier.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/prompts/verifier.md" "struct field diffs"
assert_file_contains "$ROOT/prompts/verifier.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/verifier.md" "constructor-dependency-checked:"
assert_file_contains "$ROOT/prompts/verifier.md" "provider-capability-checked:"
assert_file_contains "$ROOT/prompts/verifier.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/prompts/verifier.md" "attempted validation command"
assert_file_contains "$ROOT/prompts/verifier.md" "one single machine-readable"
assert_file_contains "$ROOT/prompts/verifier.md" "owner-evidence="
assert_file_contains "$ROOT/prompts/verifier.md" "candidate-owner="
assert_file_contains "$ROOT/prompts/verifier.md" "wrong package"
assert_file_contains "$ROOT/prompts/verifier.md" "aggregate count"
assert_file_contains "$ROOT/prompts/verifier.md" "visible inline golden expectations"
assert_file_contains "$ROOT/prompts/verifier.md" "narrow root-cause"
assert_file_contains "$ROOT/prompts/verifier.md" "compiled the package's test files"
assert_file_contains "$ROOT/prompts/verifier.md" "declared static type"
assert_file_contains "$ROOT/prompts/verifier.md" "has no field or method"
assert_file_contains "$ROOT/prompts/verifier.md" "go test -run TestNonExistent"
assert_file_contains "$ROOT/prompts/verifier.md" "adapter-parity finding"
assert_file_contains "$ROOT/prompts/verifier.md" "validation-repair-needed:"
assert_file_contains "$ROOT/prompts/worker.md" "When you expand a parser/reader allowlist"
assert_file_contains "$ROOT/prompts/worker.md" "no-test compile check"
assert_file_contains "$ROOT/prompts/worker.md" "declared static type"
assert_file_contains "$ROOT/prompts/worker.md" "validation-repair-needed:"
assert_file_contains "$ROOT/prompts/worker.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/prompts/worker.md" "actual-output-count=N"
assert_file_contains "$ROOT/prompts/worker.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/prompts/worker.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/prompts/worker.md" "struct field diffs"
assert_file_contains "$ROOT/prompts/worker.md" "one single machine-readable"
assert_file_contains "$ROOT/prompts/worker.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/prompts/worker.md" "owner-evidence="
assert_file_contains "$ROOT/prompts/worker.md" "candidate-owner="
assert_file_contains "$ROOT/prompts/worker.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/worker.md" "normally limit yourself to three focused"
assert_file_contains "$ROOT/prompts/worker.md" "Do not report blocked merely because a read-count limit was consumed"
assert_file_contains "$ROOT/prompts/worker.md" 'JSON arguments include a `cmd` string'
assert_file_contains "$ROOT/prompts/worker.md" "Do not finish with only a plan"
assert_file_contains "$ROOT/prompts/worker.md" "A long-running worker with no materialized source diff"
assert_file_contains "$ROOT/prompts/worker.md" "replacement worker over the same owned paths"
assert_file_contains "$ROOT/prompts/worker.md" "request another same-scope exploratory worker"
assert_file_contains "$ROOT/prompts/worker.md" "unable-to-verify-repository-state"
assert_file_contains "$ROOT/prompts/worker.md" "constructor-dependency-checked:"
assert_file_contains "$ROOT/prompts/worker.md" "provider-capability-checked:"
assert_file_contains "$ROOT/prompts/worker.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/prompts/worker.md" "callsite="
assert_file_contains "$ROOT/prompts/worker.md" "aggregate count"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "source-count=N"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "aggregate counts"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "known failing relevant test"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "stale-visible-failure-justified:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "final-output-field="
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "aggregate counts"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "declared-type ownership risk"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "replacement-no-diff-attempt=1"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Do not spawn worker-03/worker-04 over the same owned path"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "live worker remains no-diff after a planning checkpoint"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Scout To Worker Handoff"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "active generic scout block"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "assignment-status NAME failed"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "most one same-owned-path replacement"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "live worker remains no-diff after a planning checkpoint"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "active generic scout block"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "assignment-status NAME failed"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-symbol map contract"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "constructor-dependency contract"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "build-verification-passed:"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "final-diff-sha256="
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "omits untracked new"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "contract scout validation"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "prompts/roles/build-verifier.md"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "build-verification-passed:"
assert_file_contains "$ROOT/prompts/playbooks/finding-todo-loop.md" "Do not create or reopen a todo from command evidence bound"
assert_file_contains "$MULTIAGENT" subagent '--own|--owned-path)'
assert_file_contains "$ROOT/src/runtime.rs" 'crate::snapshot::canonical_diff(&cfg.root, "HEAD")'
assert_file_contains "$MULTIAGENT" subagent '--source-finding-id|--finding)'
assert_file_contains "$MULTIAGENT" subagent '--role)'
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "declared-type ownership risk"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "visible tests"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "real production entrypoint"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "overreach boundary"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "adapter-parity contract"
assert_file_contains "$ROOT/evaluation/README.md" "The adapter only starts the"
assert_file_contains "$ROOT/evaluation/README.md" "official SWE-bench verifier"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_lifecycle.py" "workspace prepared for EvalScope submission"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_lifecycle.py" '"MULTIAGENT_PROMPT_MODULE_ROOT": str(repo_root)'
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_lifecycle.py" '"GOMODCACHE": ensure_cache_dir(RUNTIME_ROOT / "go-mod-cache")'
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "adapter only transports"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "autonomous run-to-terminal workflow"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "assignment omitted a path required by the approved plan"
assert_file_not_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "status.json"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "must not silently narrow or"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "Never forbid a required path"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "does not inspect or score patches"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "_public_solver_metadata(dict(task.metadata or {}))"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" '"fail_to_pass"'
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" '"test_patch"'
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "_collect_rejection_diagnostics"
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "git diff --check HEAD --"
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "diagnostics_tail"
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "multiagent-native no-submission"
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "git reset --hard HEAD"
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "SUBMISSION_GATE_REJECTION"
assert_file_contains "$ROOT/evaluation/swe_bench_pro.py" '"submission_policy": "pass current workspace diff'
assert_file_not_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "status.json"
assert_file_not_contains "$ROOT/evaluation/native_solver/swe_prod_bootstrap.py" "singleflight"
assert_file_not_contains "$ROOT/evaluation/native_solver/swe_prod_lifecycle.py" "validation"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "solver_internal_timeout"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "EVAL_NATIVE_SOLVER_TIMEOUT_RESERVE"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "python3 -m evaluation.native_solver.solve_swe_prod"
for obsolete_adapter_path in \
  "$ROOT/evaluation/native_solver/swe_prod_evidence.py" \
  "$ROOT/evaluation/native_solver/swe_prod_checkpoints.py" \
  "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" \
  "$ROOT/evaluation/native_solver/swe_prod_orchestration.py" \
  "$ROOT/evaluation/native_solver/swe_prod_state.py" \
  "$ROOT/evaluation/native_solver/swe_prod_transitions.py" \
  "$ROOT/evaluation/native_solver/swe_prod_types.py" \
  "$ROOT/evaluation/native_solver/swe_prod_validation.py" \
  "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" \
  "$ROOT/evaluation/support/gate.py" \
  "$ROOT/evaluation/support/snapshot.py" \
  "$ROOT/evaluation/support/verification.py"
do
  [[ ! -e "$obsolete_adapter_path" ]] || {
    echo "obsolete adapter verification path remains: $obsolete_adapter_path" >&2
    exit 1
  }
done
for solver_module in "$ROOT"/evaluation/native_solver/*.py; do
  assert_file_not_contains "$solver_module" "EVAL_ALLOW_EXPECTED_TEST_GUIDANCE"
  assert_file_not_contains "$solver_module" "official_test_contract_text"
done
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/tests/test_provenance.py"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/tests/test_native_solver_import_model.py"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/tests/test_swe_outcomes.py"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/tests/test_swe_provenance.py"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 "$ROOT/tests/test_migration_contracts.py"
python3 -m evaluation.swe_bench_pro --help >"$TMPDIR/swe-bench-pro-help.out"
assert_file_contains "$TMPDIR/swe-bench-pro-help.out" "Evaluate the production multiagent solver"
assert_file_not_contains "$TMPDIR/swe-bench-pro-help.out" "--agent-framework"
python3 -m evaluation.swe_bench_pro \
  --no-preflight \
  --write-config-only \
  --native-solver-source "$ROOT" \
  --config-json "$TMPDIR/swe-bench-pro-config.json" \
  --config-yaml "$TMPDIR/swe-bench-pro-config.yaml" \
  >"$TMPDIR/swe-bench-pro-config.out"
assert_file_contains "$TMPDIR/swe-bench-pro-config.json" '"framework": "multiagent-native"'
python3 -m evaluation.cli --list >"$TMPDIR/evaluation-list.out"
assert_file_contains "$TMPDIR/evaluation-list.out" "ponytail"
assert_file_contains "$TMPDIR/evaluation-list.out" "orchestration"
python3 -c "from evaluation.core import system_for_arm; print(system_for_arm('baseline'))" >"$TMPDIR/evaluation-baseline-arm.out"
assert_file_contains "$TMPDIR/evaluation-baseline-arm.out" "Evaluation Worker Launch Context"
assert_file_contains "$TMPDIR/evaluation-baseline-arm.out" "Stay in your assigned files only."
assert_file_contains "$TMPDIR/evaluation-baseline-arm.out" "Ponytail implementation discipline"
assert_file_contains "$TMPDIR/evaluation-baseline-arm.out" "Worker Role Prompt"
assert_file_contains "$TMPDIR/evaluation-baseline-arm.out" "Ponytail Implementation Discipline"
python3 - <<'PY' >"$TMPDIR/orchestration-arms.out"
from evaluation.adapters import load_adapter
from evaluation.core import arm_choices, default_arms, system_for_adapter_arm

adapter = load_adapter("orchestration")
print(default_arms(adapter))
print(",".join(arm_choices(adapter)))
print(system_for_adapter_arm(adapter, "baseline").splitlines()[0])
print(system_for_adapter_arm(adapter, "orchestrator").splitlines()[0])
PY
assert_file_contains "$TMPDIR/orchestration-arms.out" "baseline,orchestrator"
assert_file_contains "$TMPDIR/orchestration-arms.out" "You are Codex in planning mode."
assert_file_contains "$TMPDIR/orchestration-arms.out" "Commander Prompt: Multi-Agent Orchestrator"
python3 -m evaluation.cli --adapter ponytail --selftest >"$TMPDIR/ponytail-selftest.out"
assert_file_contains "$TMPDIR/ponytail-selftest.out" "selftest[ponytail]: all scorers valid"
python3 -m evaluation.cli --adapter ponytail --task safe-path --reference-report --run-root "$TMPDIR/eval-runs" >"$TMPDIR/ponytail-reference-report.out"
assert_file_contains "$TMPDIR/ponytail-reference-report.out" "wrote $TMPDIR/eval-runs/ponytail/"
reference_results="$(find "$TMPDIR/eval-runs/ponytail" -name results.json -print -quit)"
reference_report="$(find "$TMPDIR/eval-runs/ponytail" -name report.md -print -quit)"
[[ -n "$reference_results" && -n "$reference_report" ]]
assert_file_contains "$reference_results" '"adapter": "ponytail"'
assert_file_contains "$reference_results" '"arm": "reference-good"'
assert_file_contains "$reference_report" "Evaluation Report: ponytail"
EVAL_DIFF_REPO="$TMPDIR/evaluation-committed-diff"
mkdir -p "$EVAL_DIFF_REPO"
python3 - "$EVAL_DIFF_REPO" <<'PY'
import sys
import subprocess
from pathlib import Path
from evaluation.core import git_snapshot, git_diff_stats

workdir = Path(sys.argv[1])
(workdir / "demo.py").write_text("def demo():\n    raise NotImplementedError\n", encoding="utf-8")
git_snapshot(workdir)
subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=workdir, check=True)
(workdir / "demo.py").write_text("def demo():\n    return 1\n", encoding="utf-8")
subprocess.run(["git", "add", "demo.py"], cwd=workdir, check=True)
subprocess.run(["git", "commit", "-q", "-m", "implement demo"], cwd=workdir, check=True)
stats = git_diff_stats(workdir)
assert stats["src_loc"] == 1, stats
assert stats["src_files"] == 1, stats
PY
python3 -m evaluation.cli --adapter orchestration --selftest >"$TMPDIR/orchestration-selftest.out"
assert_file_contains "$TMPDIR/orchestration-selftest.out" "selftest[orchestration]: all scorers valid"
python3 -m evaluation.cli --adapter orchestration --task large-update-300 --reference-report --run-root "$TMPDIR/eval-runs" >"$TMPDIR/orchestration-reference-report.out"
assert_file_contains "$TMPDIR/orchestration-reference-report.out" "wrote $TMPDIR/eval-runs/orchestration/"
orchestration_results="$(find "$TMPDIR/eval-runs/orchestration" -name results.json -print -quit)"
orchestration_report="$(find "$TMPDIR/eval-runs/orchestration" -name report.md -print -quit)"
[[ -n "$orchestration_results" && -n "$orchestration_report" ]]
assert_file_contains "$orchestration_results" '"adapter": "orchestration"'
assert_file_contains "$orchestration_results" '"task": "large-update-300"'
assert_file_contains "$orchestration_results" '"nodes": 321'
assert_file_contains "$orchestration_results" '"fanout": 300'
assert_file_contains "$orchestration_results" '"max_concurrent_agents": 300'
assert_file_contains "$orchestration_results" '"avg_concurrent_agents": 160'  # mean(300 update workers, 20 validation workers) = 160
assert_file_contains "$orchestration_results" '"concurrency_ratio": 0.938'
assert_file_contains "$orchestration_results" '"repo_spawn_commands": 1'
assert_file_contains "$orchestration_report" "Evaluation Report: orchestration"
assert_file_contains "$orchestration_report" "Max Agents"

policy_check_inside="$("$MULTIAGENT" policy check "$ROOT/README.md")"
[[ "$policy_check_inside" == $'allowed\t'"$ROOT/README.md" ]]

outside_path="$TMPDIR/outside/result.txt"
policy_check_file="$TMPDIR/policy-check.out"
if "$MULTIAGENT" policy check "$outside_path" >"$policy_check_file" 2>&1; then
  echo "expected outside path to be denied before approval" >&2
  cat "$policy_check_file" >&2
  exit 1
fi
assert_file_contains "$policy_check_file" $'denied\t'"$outside_path"

if "$MULTIAGENT" policy approve "$TMPDIR/outside" >"$TMPDIR/old-approve.out" 2>&1; then
  echo "expected approve without metadata to fail" >&2
  cat "$TMPDIR/old-approve.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/old-approve.out" "approve requires --actor ACTOR"

approve_output="$("$MULTIAGENT" policy approve "$TMPDIR/outside" --actor orchestrator --assignment-id test-policy --reason "test outside output")"
[[ "$approve_output" == $'approved outside write root: '"$TMPDIR/outside" ]]
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'approval\t'
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\torchestrator\ttest-policy\t'
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\ttest outside output\t0'
policy_check_outside="$("$MULTIAGENT" policy check "$outside_path")"
[[ "$policy_check_outside" == $'allowed\t'"$outside_path" ]]

if "$MULTIAGENT" policy approve /tmp --actor orchestrator --assignment-id broad-reject --reason "too broad" >"$TMPDIR/broad-approve.out" 2>&1; then
  echo "expected broad approval to require force" >&2
  cat "$TMPDIR/broad-approve.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/broad-approve.out" "refusing broad outside approval without --force"

forced_broad_output="$("$MULTIAGENT" policy approve /tmp --actor orchestrator --assignment-id broad-force --reason "explicit user decision" --force)"
[[ "$forced_broad_output" == *"(forced)" ]]
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\tbroad-force\t'
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\texplicit user decision\t1'

ASSIGN_REPO="$TMPDIR/assignment-repo"
ASSIGN_STATE="$TMPDIR/assignment-state"
mkdir -p "$ASSIGN_REPO/src" "$ASSIGN_REPO/docs" "$ASSIGN_STATE"
(
  cd "$ASSIGN_REPO"
  git init -q
  git config user.email "test@example.com"
  git config user.name "Test User"
  git config commit.gpgsign false
  printf 'hello\n' >README.md
  printf 'code\n' >src/app.txt
  git add README.md src/app.txt
  git commit -q -m "initial"
  git switch -q -c worker/docs
)

assignment_create_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-docs --assignment-id docs-001 --branch worker/docs --owned README.md,src)"
[[ "$assignment_create_output" == $'assignment created\tworker-docs\tdocs-001\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "assignment_id=docs-001"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "branch=worker/docs"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "worker_cli=claude"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "subagent_cli=claude"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "verifier_cli=codex"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/status" "assigned"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "README.md"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "src"

if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-overlap --assignment-id docs-overlap --branch worker/docs --owned README.md >"$TMPDIR/assignment-overlap.out" 2>&1; then
  echo "expected assignment-create to reject overlapping active writable ownership" >&2
  cat "$TMPDIR/assignment-overlap.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-overlap.out" "active assignment owned-path overlap"

assignment_verifier_overlap_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create verifier-overlap --assignment-id docs-verifier --branch worker/docs --owned README.md --role verifier)"
[[ "$assignment_verifier_overlap_output" == $'assignment created\tverifier-overlap\tdocs-verifier\tworker/docs' ]]
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status verifier-overlap done >/dev/null

assignment_scout_overlap_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create scout-overlap --assignment-id docs-scout --branch worker/docs --owned README.md --role scout)"
[[ "$assignment_scout_overlap_output" == $'assignment created\tscout-overlap\tdocs-scout\tworker/docs' ]]
assignment_after_scout_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-after-scout --assignment-id docs-after-scout --branch worker/docs --owned docs)"
[[ "$assignment_after_scout_output" == $'assignment created\tworker-after-scout\tdocs-after-scout\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/scout-overlap/assignment.env" "role=scout"
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-after-scout done >/dev/null

assignment_kill_owner_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-kill-owner --assignment-id docs-kill-owner --branch worker/docs --owned docs)"
[[ "$assignment_kill_owner_output" == $'assignment created\tworker-kill-owner\tdocs-kill-owner\tworker/docs' ]]
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" MULTIAGENT_SESSION="missing-test-session" "$MULTIAGENT" subagent kill worker-kill-owner >/dev/null
assert_file_contains "$ASSIGN_STATE/assignments/worker-kill-owner/status" "failed"
assignment_after_kill_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-after-kill --assignment-id docs-after-kill --branch worker/docs --owned docs)"
[[ "$assignment_after_kill_output" == $'assignment created\tworker-after-kill\tdocs-after-kill\tworker/docs' ]]
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-after-kill done >/dev/null

assignment_show_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-show worker-docs)"
[[ "$assignment_show_output" == *"agent_name=worker-docs"* ]]
[[ "$assignment_show_output" == *"status=assigned"* ]]

assignment_status_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-docs running)"
[[ "$assignment_status_output" == $'assignment status\tworker-docs\trunning' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/status" "running"

printf 'change\n' >>"$ASSIGN_REPO/README.md"
assignment_check_ok="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-check worker-docs)"
[[ "$assignment_check_ok" == *$'branch\tworker/docs\tworker/docs'* ]]
[[ "$assignment_check_ok" == *$'ok\tREADME.md'* ]]
[[ "$assignment_check_ok" == *$'accepted\tworker-docs'* ]]

printf 'outside\n' >"$ASSIGN_REPO/docs/notes.txt"
if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-check worker-docs >"$TMPDIR/assignment-outside.out" 2>&1; then
  echo "expected assignment check to reject outside owned paths" >&2
  cat "$TMPDIR/assignment-outside.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-outside.out" $'reject\toutside-owned-path\tdocs/notes.txt'

MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-docs done >/dev/null
assignment_repeated_owned_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-repeated-owned --assignment-id docs-002 --branch worker/docs --owned README.md --owned src)"
[[ "$assignment_repeated_owned_output" == $'assignment created\tworker-repeated-owned\tdocs-002\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-repeated-owned/owned-paths" "README.md"
assert_file_contains "$ASSIGN_STATE/assignments/worker-repeated-owned/owned-paths" "src"
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-repeated-owned done >/dev/null

assignment_create_branch_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-branch --assignment-id branch-001 --branch expected/branch --owned README.md,docs)"
[[ "$assignment_create_branch_output" == $'assignment created\tworker-branch\tbranch-001\texpected/branch' ]]
if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-check worker-branch >"$TMPDIR/assignment-branch.out" 2>&1; then
  echo "expected assignment check to reject branch mismatch" >&2
  cat "$TMPDIR/assignment-branch.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-branch.out" $'reject\tbranch-mismatch\texpected=expected/branch\tactual=worker/docs'
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-status worker-branch failed >/dev/null

worktree_assignment_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-wt --assignment-id wt-001 --branch worker/wt --owned README.md)"
[[ "$worktree_assignment_output" == $'assignment created\tworker-wt\twt-001\tworker/wt' ]]
worktree_create_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent worktree-create worker-wt)"
[[ "$worktree_create_output" == *$'worktree created\tworker-wt\tworker/wt\t'"$ASSIGN_STATE/worktrees/worker-wt" ]]
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "agent_name=worker-wt"
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "branch=worker/wt"
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "path=$ASSIGN_STATE/worktrees/worker-wt"
[[ -f "$ASSIGN_STATE/worktrees/worker-wt/README.md" ]]
worktree_show_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent worktree-show worker-wt)"
[[ "$worktree_show_output" == *"branch=worker/wt"* ]]
worktree_remove_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$MULTIAGENT" subagent worktree-remove worker-wt)"
[[ "$worktree_remove_output" == *$'worktree removed\tworker-wt\t'"$ASSIGN_STATE/worktrees/worker-wt" ]]
[[ ! -e "$ASSIGN_STATE/worktrees/worker-wt.env" ]]

current_branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
checkpoint_assignment_output="$("$MULTIAGENT" subagent assignment-create subagent-structured --assignment-id structured-001 --branch "$current_branch" --owned README.md)"
[[ "$checkpoint_assignment_output" == $'assignment created\tsubagent-structured\tstructured-001\t'"$current_branch" ]]
checkpoint_update_output="$("$MULTIAGENT" subagent checkpoint-update subagent-structured --step "implemented checkpoint metadata" --idempotency "rerun checkpoint-update safely" --status running)"
[[ "$checkpoint_update_output" == $'checkpoint updated\tsubagent-structured\trunning' ]]
checkpoint_show_output="$("$MULTIAGENT" subagent checkpoint-show subagent-structured)"
[[ "$checkpoint_show_output" == *"assignment_id=structured-001"* ]]
[[ "$checkpoint_show_output" == *"completed_step=implemented checkpoint metadata"* ]]
[[ "$checkpoint_show_output" == *"idempotency=rerun checkpoint-update safely"* ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/subagent-structured/checkpoint.env" "status=running"

finding_output="$("$MULTIAGENT" subagent finding-create build-go-ofrep --severity blocking --type compile_failure --summary "Changed Go packages do not compile" --affected internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go --evidence-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":1,"stderr_excerpt":"undefined: req.Request"}' --required-resolution "Final diff must compile with rc=0 for both changed Go packages.")"
[[ "$finding_output" == $'finding created\tbuild-go-ofrep\tblocking\tcompile_failure' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"severity": "blocking"'
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"type": "compile_failure"'
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"internal/server/ofrep/evaluation.go"'

todo_output="$("$MULTIAGENT" subagent todo-create todo-017 --source-finding-id build-go-ofrep --task "Fix Go compile failure in changed packages." --context "Exact verifier evidence." --done-criteria "run go test ./internal/server/ofrep" --done-criteria "run go test ./internal/server/evaluation" --done-criteria "record returncode=0 after final diff")"
[[ "$todo_output" == $'todo created\ttodo-017\tbuild-go-ofrep\topen' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"source_finding_id": "build-go-ofrep"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "open"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"required_commands":'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"go test ./internal/server/evaluation"'

todo_assign_output="$("$MULTIAGENT" subagent todo-assign todo-017 worker-02-ofrep)"
[[ "$todo_assign_output" == $'todo assigned\ttodo-017\tworker-02-ofrep' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"assigned_to": "worker-02-ofrep"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "assigned"'

if "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-assigned.out" 2>&1; then
  echo "expected gate-check to reject an assigned todo" >&2
  cat "$TMPDIR/gate-assigned.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-assigned.out" $'reject\topen-blocking-todo\tfinding=build-go-ofrep\ttodo=todo-017\tstatus=assigned'

resolution_output="$("$MULTIAGENT" subagent resolution-create todo-017 --worker worker-02-ofrep --status resolved --changed internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}]' --why "Both changed packages compile after final diff.")"
[[ "$resolution_output" == $'resolution recorded\ttodo-017\tworker-02-ofrep\tresolved' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/resolution.json" '"status": "resolved"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "resolved"'

if "$MULTIAGENT" subagent todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"unrelated-finding","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}],"final_diff_hash":"abc123"}' >"$TMPDIR/todo-close-wrong-finding.out" 2>&1; then
  echo "expected todo-close to reject verifier closure for the wrong finding" >&2
  cat "$TMPDIR/todo-close-wrong-finding.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/todo-close-wrong-finding.out" "must name source finding build-go-ofrep"

if "$MULTIAGENT" subagent todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0}],"final_diff_hash":"abc123"}' >"$TMPDIR/todo-close-partial-recheck.out" 2>&1; then
  echo "expected todo-close to reject verifier closure missing worker validation command evidence" >&2
  cat "$TMPDIR/todo-close-partial-recheck.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/todo-close-partial-recheck.out" "missing required command"

if "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-resolved.out" 2>&1; then
  echo "expected gate-check to reject a resolved but unverified todo" >&2
  cat "$TMPDIR/gate-resolved.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-resolved.out" $'reject\topen-blocking-todo\tfinding=build-go-ofrep\ttodo=todo-017\tstatus=resolved'

todo_closed_output="$("$MULTIAGENT" subagent todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}],"final_diff_hash":"abc123"}' --notes "Verifier accepted worker resolution.")"
[[ "$todo_closed_output" == $'todo closed\ttodo-017\tverifier-01-ofrep' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/closure.json" '"accepted": true'
gate_closed_output="$("$MULTIAGENT" subagent gate-check)"
[[ "$gate_closed_output" == $'accepted\tfinal-gate' ]]

CLOSED_HASH_ROOT="$TMPDIR/closed-hash-root"
CLOSED_HASH_STATE="$TMPDIR/closed-hash-state"
mkdir -p "$CLOSED_HASH_ROOT" "$CLOSED_HASH_STATE/subagents/verifier-closed-hash"
git -C "$CLOSED_HASH_ROOT" init -q
git -C "$CLOSED_HASH_ROOT" config user.email test@example.com
git -C "$CLOSED_HASH_ROOT" config user.name Test
git -C "$CLOSED_HASH_ROOT" config commit.gpgsign false
printf 'before\n' >"$CLOSED_HASH_ROOT/source.txt"
git -C "$CLOSED_HASH_ROOT" add source.txt
git -C "$CLOSED_HASH_ROOT" commit -qm initial
printf 'after\n' >"$CLOSED_HASH_ROOT/source.txt"
CLOSED_HASH_DIFF_SHA="$(git -C "$CLOSED_HASH_ROOT" diff --binary --ignore-submodules=all | shasum -a 256 | awk '{print $1}')"
printf 'ACCEPTED\nbehavior-verification-passed: final-diff-sha256=%s behavior_clean=true public-clauses-covered=true\n' \
  "$CLOSED_HASH_DIFF_SHA" >"$CLOSED_HASH_STATE/subagents/verifier-closed-hash/last-message.txt"
printf 'done\n' >"$CLOSED_HASH_STATE/subagents/verifier-closed-hash/status"
CLOSED_HASH_ENV=(MULTIAGENT_ROOT="$CLOSED_HASH_ROOT" MULTIAGENT_STATE_DIR="$CLOSED_HASH_STATE" MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1)
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent finding-create closed-hash-finding \
  --severity blocking --type behavior --summary "Verify final diff" --affected source.txt \
  --evidence-json '{"source_evidence":"source.txt changed"}' --required-resolution "Bind closure to the final diff." >/dev/null
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent todo-create closed-hash-todo \
  --source-finding-id closed-hash-finding --task "Verify final diff." \
  --done-criteria "Bind closure evidence to the final diff." >/dev/null
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent resolution-create closed-hash-todo \
  --worker worker-closed-hash --status resolved --changed source.txt \
  --validation-json "[{\"cmd\":\"test -f source.txt\",\"rc\":0,\"final_diff_sha256\":\"$CLOSED_HASH_DIFF_SHA\"}]" \
  --why "Final diff reviewed." >/dev/null
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent todo-close closed-hash-todo \
  --verified-by verifier-closed-hash \
  --recheck-json "{\"accepted\":true,\"source_finding_id\":\"closed-hash-finding\",\"commands\":[{\"cmd\":\"test -f source.txt\",\"rc\":0}],\"final_diff_sha256\":\"$CLOSED_HASH_DIFF_SHA\"}" >/dev/null
closed_hash_gate_output="$(env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent gate-check)"
[[ "$closed_hash_gate_output" == $'accepted\tfinal-gate' ]]
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent finding-create superseded-visible-test \
  --severity blocking --type test-gap --summary "Old visible expectation conflicts with the public task" \
  --affected source.txt --evidence-json '{"source_evidence":"source.txt old expectation"}' \
  --required-resolution "Edit the old expectation." >/dev/null
if env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-undismissed-finding.out" 2>&1; then
  echo "expected gate-check to reject an undismissed blocking finding" >&2
  exit 1
fi
env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent finding-dismiss superseded-visible-test \
  --verified-by verifier-closed-hash \
  --recheck-json "{\"accepted\":true,\"source_finding_id\":\"superseded-visible-test\",\"disposition\":\"superseded\",\"evidence\":\"Public task and source.txt prove the old expectation changed.\",\"final_diff_sha256\":\"$CLOSED_HASH_DIFF_SHA\"}" >/dev/null
dismissed_finding_gate_output="$(env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent gate-check)"
[[ "$dismissed_finding_gate_output" == $'accepted\tfinal-gate' ]]
assert_file_contains "$CLOSED_HASH_STATE/findings/superseded-visible-test/dismissal.json" '"disposition": "superseded"'
python3 - "$CLOSED_HASH_STATE/todos/closed-hash-todo/closure.json" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
payload = json.loads(path.read_text())
payload["recheck"]["final_diff_sha256"] = "stale"
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
if env "${CLOSED_HASH_ENV[@]}" "$MULTIAGENT" subagent gate-check >"$TMPDIR/gate-closed-hash-stale.out" 2>&1; then
  echo "expected gate-check to reject stale closed-todo final diff evidence" >&2
  cat "$TMPDIR/gate-closed-hash-stale.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-closed-hash-stale.out" $'reject\tclosed-todo-final-diff-hash-mismatch\ttodo=closed-hash-todo'
assert_file_not_contains "$TMPDIR/gate-closed-hash-stale.out" $'accepted\tfinal-gate'

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-structured"
printf 'Final status: completed according to stale transcript text\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/current.txt"
printf 'Done and finished, but this is fallback context only\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/transcript.log"

"$MULTIAGENT" subagent spawn subagent-watch --instruction "Watch builds"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-watch"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "Claude prompt ready"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "write_policy=$MULTIAGENT_WRITE_POLICY"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "log_file=$MULTIAGENT_STATE_DIR/logs/subagent-watch.log"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "cli=claude"
assert_file_contains "$MOCK_TMUX_LOG" "new-window -d test-session subagent-watch"
assert_file_contains "$MOCK_TMUX_LOG" "pipe-pane test-session:subagent-watch cat >> $MULTIAGENT_STATE_DIR/logs/subagent-watch.log"
watch_spawn_line="$(grep -F "new-window -d test-session subagent-watch " "$MOCK_TMUX_LOG")"
[[ "$watch_spawn_line" == *"--dangerously-skip-permissions"* ]]
if [[ "$watch_spawn_line" == *"--cd"* || "$watch_spawn_line" == *"--no-alt-screen"* ]]; then
  echo "expected default subagent command to follow WORKER_CLI=claude without Codex-only flags" >&2
  echo "$watch_spawn_line" >&2
  exit 1
fi
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-watch Watch builds"

printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-file.txt"
INSTRUCTION_FILE="$TMPDIR/subagent-instruction.txt"
printf 'Watch from file\nwith exact text\n' >"$INSTRUCTION_FILE"
"$MULTIAGENT" subagent spawn subagent-file --instruction-file "$INSTRUCTION_FILE"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-file"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-file/instruction.txt" "Watch from file"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-file Read and follow the assignment in $MULTIAGENT_STATE_DIR/subagents/subagent-file/instruction.txt"

printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/owned-inline.txt"
owned_inline_output="$("$MULTIAGENT" subagent spawn owned-inline --own prompts/verifier.md -- "Repair the bounded path")"
[[ "$owned_inline_output" == $'spawned owned-inline' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-inline/assignment.env" "assignment_id=spawn-owned-inline"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-inline/assignment.env" "branch=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-inline/owned-paths" "prompts/verifier.md"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-inline/status" "running"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:owned-inline Repair the bounded path"

printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/owned-atomic.txt"
owned_atomic_output="$("$MULTIAGENT" subagent spawn owned-atomic \
  --own docs/architecture.md \
  --assignment-id atomic-001 \
  --workflow-id WF-ATOMIC \
  --decision-id DEC-ATOMIC \
  --plan-id PLAN-ATOMIC \
  --branch atomic/worker \
  --instruction "Run one atomic assignment and launch")"
[[ "$owned_atomic_output" == $'spawned owned-atomic' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/assignment.env" "assignment_id=atomic-001"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/assignment.env" "workflow_id=WF-ATOMIC"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/assignment.env" "decision_id=DEC-ATOMIC"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/assignment.env" "plan_id=PLAN-ATOMIC"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/assignment.env" "branch=atomic/worker"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/owned-atomic/status" "running"

"$MULTIAGENT" subagent assignment-create owned-mismatch --assignment-id existing-owned --branch "$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)" --owned prompts/worker.md >/dev/null
printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/owned-mismatch.txt"
if "$MULTIAGENT" subagent spawn owned-mismatch --own src/subagent.rs --instruction "Do not widen ownership" >"$TMPDIR/owned-mismatch.out" 2>&1; then
  echo "expected spawn to reject paths outside an existing assignment" >&2
  cat "$TMPDIR/owned-mismatch.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/owned-mismatch.out" "spawn requested path outside existing assignment"
if grep -Fq "new-window -d test-session owned-mismatch" "$MOCK_TMUX_LOG"; then
  echo "expected ownership validation before creating the tmux window" >&2
  exit 1
fi

printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/worker-generic-01.txt"
"$MULTIAGENT" subagent spawn worker-generic-01 --instruction "First generic worker"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/worker-generic-01/status" "running"
printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/worker-generic-02.txt"
if "$MULTIAGENT" subagent spawn worker-generic-02 --instruction "Second generic worker" >"$TMPDIR/worker-generic-conflict.out" 2>&1; then
  echo "expected generic worker spawn to reject active generic worker" >&2
  cat "$TMPDIR/worker-generic-conflict.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/worker-generic-conflict.out" "active generic worker already running"

printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/verifier-01-docs.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent spawn verifier-01-docs --instruction "Review worker-01-docs"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/meta.env" "cli=codex"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/instruction.txt" "Verifier Role Prompt"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/instruction.txt" "Review worker-01-docs"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/instruction.txt" "state-space partition audit"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:verifier-01-docs Read and follow the assignment in"
verifier_spawn_line="$(grep -F "new-window -d test-session verifier-01-docs " "$MOCK_TMUX_LOG")"
[[ "$verifier_spawn_line" == *"--cd $ROOT"* ]]
if [[ "$HOST_KERNEL" == Linux ]]; then
  [[ "$verifier_spawn_line" == *"$MULTIAGENT role-exec"* ]]
  [[ "$verifier_spawn_line" == *"--dangerously-bypass-approvals-and-sandbox"* ]]
  [[ "$verifier_spawn_line" == *"--allow-write $ROOT"* ]]
else
  [[ "$verifier_spawn_line" == *"--sandbox workspace-write --ask-for-approval never --no-alt-screen"* ]]
  [[ "$verifier_spawn_line" != *"--dangerously-bypass-approvals-and-sandbox"* ]]
fi

printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/verifier-owned-01.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent spawn verifier-owned-01 \
  --own prompts/verifier.md --instruction "Review shared source"
printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/build-verifier-owned-02.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent spawn build-verifier-owned-02 \
  --own prompts/verifier.md --instruction "Compile shared source"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/verifier-owned-01/assignment.env" "role=verifier"
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/build-verifier-owned-02/assignment.env" "role=verifier"

printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/acceptance-scout-01-contract.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent spawn acceptance-scout-01-contract --instruction "Extract acceptance risks"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/acceptance-scout-01-contract/instruction.txt" "Acceptance Scout Role Prompt"
assert_file_not_contains "$MULTIAGENT_STATE_DIR/subagents/acceptance-scout-01-contract/instruction.txt" "Contract Scout Role Prompt"

printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/contract-scout-01-contract.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent spawn contract-scout-01-contract --instruction "Extract source contracts"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/contract-scout-01-contract/instruction.txt" "Contract Scout Role Prompt"
assert_file_not_contains "$MULTIAGENT_STATE_DIR/subagents/contract-scout-01-contract/instruction.txt" "Acceptance Scout Role Prompt"

printf 'Blocker: this line is stale prompt context\nfinal status: codex exec exited rc=0\n' >"$MOCK_TMUX_CAPTURES/verifier-01-docs.txt"
cat >"$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/last-message.txt" <<'EOF'
ACCEPTED
final-diff-sha256: abc123
build-verification-passed: final-diff-sha256=abc123 compile_clean=true returncode=0
EOF
verifier_accepted_poll="$(SUBAGENT_CLI="$VERIFIER_CLI" "$MULTIAGENT" subagent poll verifier-01-docs)"
[[ "$verifier_accepted_poll" == $'verifier-01-docs\tdone' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/status" "done"

if MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex "$MULTIAGENT" subagent spawn codex-no-prompt >"$TMPDIR/codex-no-prompt.out" 2>&1; then
  echo "expected codex exec subagent spawn without instruction to fail" >&2
  cat "$TMPDIR/codex-no-prompt.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/codex-no-prompt.out" "codex exec subagent spawn requires --instruction or --instruction-file"

printf 'Codex exec prompt ready\n' >"$MOCK_TMUX_CAPTURES/codex-exec-protocol.txt"
MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex "$MULTIAGENT" subagent spawn codex-exec-protocol --instruction "Inspect /app"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/codex-exec-protocol/instruction.txt" "Codex Exec Tool Protocol"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/codex-exec-protocol/instruction.txt" '{"cmd":"cd /app && sed -n'
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/codex-exec-protocol/instruction.txt" "Inspect /app"
codex_exec_spawn_line="$(grep -F "new-window -d test-session codex-exec-protocol " "$MOCK_TMUX_LOG")"
[[ "$codex_exec_spawn_line" == *"$MULTIAGENT agent run --backend codex --cwd $ROOT"* ]]
if [[ "$HOST_KERNEL" == Linux ]]; then
  [[ "$codex_exec_spawn_line" == *"$MULTIAGENT role-exec"* ]]
  [[ "$codex_exec_spawn_line" == *"--allow-write $ROOT"* ]]
fi
[[ "$codex_exec_spawn_line" == *"--final-output $MULTIAGENT_STATE_DIR/subagents/codex-exec-protocol/last-message.txt"* ]]
[[ "$codex_exec_spawn_line" == *"--trace-dir $MULTIAGENT_STATE_DIR/logs/agents/codex-exec-protocol"* ]]
[[ "$codex_exec_spawn_line" == *"--access workspace-write"* ]]

printf 'final status: codex exec exited rc=0\n' >"$MOCK_TMUX_CAPTURES/codex-exec-protocol.txt"
codex_wait_output="$(MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex "$MULTIAGENT" subagent wait codex-exec-protocol --timeout 1 --poll-interval 0)"
[[ "$codex_wait_output" == $'codex-exec-protocol\tdone' ]]

printf 'Codex exec prompt ready\n' >"$MOCK_TMUX_CAPTURES/decision-authority-read-only.txt"
MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex "$MULTIAGENT" subagent spawn decision-authority-read-only \
  --role reviewer --instruction "Review the proposed authority"
authority_spawn_line="$(grep -F "new-window -d test-session decision-authority-read-only " "$MOCK_TMUX_LOG")"
[[ "$authority_spawn_line" == *"$MULTIAGENT agent run --backend codex --cwd $ROOT"* ]]
if [[ "$HOST_KERNEL" == Linux ]]; then
  [[ "$authority_spawn_line" == *"$MULTIAGENT role-exec"* ]]
  [[ "$authority_spawn_line" != *"--allow-write $ROOT"* ]]
fi
[[ "$authority_spawn_line" == *"--access read-only"* ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/decision-authority-read-only/meta.env" "role=reviewer"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/decision-authority-read-only/meta.env" "codex_access=read-only"

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/decision-authority-read-only.txt"
if MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex "$MULTIAGENT" subagent wait decision-authority-read-only --timeout 0 --poll-interval 0 >"$TMPDIR/authority-wait-timeout.out" 2>&1; then
  echo "expected bounded subagent wait to time out for a running reviewer" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/authority-wait-timeout.out" $'decision-authority-read-only\trunning'
assert_file_contains "$TMPDIR/authority-wait-timeout.out" "timed out after 0 seconds"

printf 'Codex exec prompt ready\n' >"$MOCK_TMUX_CAPTURES/verifier-exec-role.txt"
VERIFIER_DIFF_ROOT="$TMPDIR/verifier-diff-root"
mkdir -p "$VERIFIER_DIFF_ROOT"
git -C "$VERIFIER_DIFF_ROOT" init -q
git -C "$VERIFIER_DIFF_ROOT" config user.email test@example.com
git -C "$VERIFIER_DIFF_ROOT" config user.name Test
git -C "$VERIFIER_DIFF_ROOT" config commit.gpgsign false
printf 'before\n' >"$VERIFIER_DIFF_ROOT/source.txt"
git -C "$VERIFIER_DIFF_ROOT" add source.txt
git -C "$VERIFIER_DIFF_ROOT" commit -qm initial
printf 'after\n' >"$VERIFIER_DIFF_ROOT/source.txt"
git -C "$VERIFIER_DIFF_ROOT" add source.txt
VERIFIER_STAGED_DIFF_SHA="$(git -C "$VERIFIER_DIFF_ROOT" diff HEAD --binary --ignore-submodules=all -- | shasum -a 256 | awk '{print $1}')"
MULTIAGENT_ROOT="$VERIFIER_DIFF_ROOT" MULTIAGENT_PROMPT_MODULE_ROOT="$ROOT" \
  MULTIAGENT_CODEX_EXEC=1 SUBAGENT_CLI=codex \
  "$MULTIAGENT" subagent spawn verifier-exec-role --instruction "Review the final diff"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "Verifier Role Prompt"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "state-space partition audit"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "Review the final diff"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "Spawn-Time Final Diff Binding"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "final-diff-sha256=$VERIFIER_STAGED_DIFF_SHA"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-exec-role/instruction.txt" "behavior-verification-passed:"

printf 'Login required before Claude can start\n' >"$MOCK_TMUX_CAPTURES/subagent-auth.txt"
if "$MULTIAGENT" subagent spawn subagent-auth --instruction "Should not send" >"$TMPDIR/auth-spawn.out" 2>&1; then
  echo "expected spawn to stop when the subagent is not ready" >&2
  cat "$TMPDIR/auth-spawn.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/auth-spawn.out" "subagent window is not ready for instruction delivery: subagent-auth"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-auth/status" "delivery-blocked"
if grep -Fq "Should not send" "$MOCK_TMUX_LOG"; then
  echo "expected readiness gate to prevent send-keys" >&2
  cat "$MOCK_TMUX_LOG" >&2
  exit 1
fi

printf 'Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-claude.txt"
SUBAGENT_CLI=claude "$MULTIAGENT" subagent spawn subagent-claude --instruction "Use Claude"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-claude/meta.env" "cli=claude"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-claude/current.txt" "Claude prompt ready"
claude_spawn_line="$(grep -F "new-window -d test-session subagent-claude " "$MOCK_TMUX_LOG")"
[[ "$claude_spawn_line" == *"--dangerously-skip-permissions"* ]]
if [[ "$claude_spawn_line" == *"--cd"* || "$claude_spawn_line" == *"--no-alt-screen"* ]]; then
  echo "expected Claude command to omit Codex-only flags" >&2
  echo "$claude_spawn_line" >&2
  exit 1
fi
printf 'Final status: completed\n' >"$MOCK_TMUX_CAPTURES/subagent-claude.txt"
"$MULTIAGENT" subagent finalize subagent-claude >/dev/null

SUBAGENT_CLI=qwen "$MULTIAGENT" subagent spawn subagent-qwen --role reviewer --instruction "Review with Qwen Code"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-qwen/meta.env" "cli=qwen"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-qwen/meta.env" "cli_bin=$QWEN_BIN"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-qwen/meta.env" "access=read-only"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-qwen/meta.env" "trace_dir=$MULTIAGENT_STATE_DIR/logs/agents/subagent-qwen"
qwen_spawn_line="$(grep -F "new-window -d test-session subagent-qwen " "$MOCK_TMUX_LOG")"
[[ "$qwen_spawn_line" == *"$MULTIAGENT agent run --backend qwen --cwd $ROOT"* ]]
[[ "$qwen_spawn_line" == *"--prompt-file $MULTIAGENT_STATE_DIR/subagents/subagent-qwen/instruction.txt"* ]]
[[ "$qwen_spawn_line" == *"--access read-only"* ]]
if grep -Fq "send-key test-session:subagent-qwen" "$MOCK_TMUX_LOG"; then
  echo "headless Qwen Code must receive its prompt through stdin, not tmux send-keys" >&2
  exit 1
fi
printf 'final status: coding agent exited rc=0\n' >"$MOCK_TMUX_CAPTURES/subagent-qwen.txt"
qwen_poll="$(SUBAGENT_CLI=qwen "$MULTIAGENT" subagent poll subagent-qwen)"
[[ "$qwen_poll" == $'subagent-qwen\tdone' ]]
SUBAGENT_CLI=qwen "$MULTIAGENT" subagent kill subagent-qwen >/dev/null
assert_file_contains "$MULTIAGENT_STATE_DIR/logs/agents/subagent-qwen/supervisor-termination.json" '"reason": "canceled"'

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_output="$("$MULTIAGENT" subagent poll subagent-watch)"
[[ "$poll_output" == $'subagent-watch\trunning' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/transcript.log" "Progress update: still running"

printf 'Read and follow the assignment. Proceed now, then report progress/final status in this window.\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_prompt_output="$("$MULTIAGENT" subagent poll subagent-watch)"
[[ "$poll_prompt_output" == $'subagent-watch\trunning' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "progress/final status"

printf 'final status: codex exec exited rc=0\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_final_status_output="$("$MULTIAGENT" subagent poll subagent-watch)"
[[ "$poll_final_status_output" == $'subagent-watch\tdone' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "final status: codex exec exited rc=0"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex"
printf 'Read-only scout completed with source owner findings.\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex/last-message.txt"
printf 'final status: codex exec exited rc=0\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex/transcript.log"
poll_durable_output="$("$MULTIAGENT" subagent poll subagent-durable-codex)"
[[ "$poll_durable_output" == $'subagent-durable-codex\tdone' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex/current.txt" "recovered durable subagent output"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex/current.txt" "Read-only scout completed with source owner findings."
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-durable-codex/current.txt" "final status: codex exec exited rc=0"

printf 'Warning: no last agent message; wrote empty content to /tmp/last-message.txt\nfinal status: codex exec exited rc=0\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_empty_final_output="$("$MULTIAGENT" subagent poll subagent-watch)"
[[ "$poll_empty_final_output" == $'subagent-watch\tfailed' ]]

printf 'final status: codex exec exited rc=1\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_failed_status_output="$("$MULTIAGENT" subagent poll subagent-watch)"
[[ "$poll_failed_status_output" == $'subagent-watch\tfailed' ]]

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
"$MULTIAGENT" subagent poll subagent-watch >/dev/null

printf 'worker-01-docs\n' >>"$MOCK_TMUX_WINDOWS"
status_output="$("$MULTIAGENT" status)"
[[ "$status_output" == *$'worker\tworker-01-docs\tbusy\topen\tWorker progress: editing README\t-'* ]]
[[ "$status_output" == *$'subagent\tsubagent-watch\trunning\topen\tProgress update: still running\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-watch"* ]]
if grep -Fq $'\torchestrator\t' <<<"$status_output"; then
  echo "expected status output to exclude orchestrator" >&2
  echo "$status_output" >&2
  exit 1
fi

printf 'Final status: completed\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
finalize_output="$("$MULTIAGENT" subagent finalize subagent-watch)"
[[ "$finalize_output" == "finalized subagent-watch" ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "finalized"
if grep -Fqx -- "subagent-watch" "$MOCK_TMUX_WINDOWS"; then
  echo "expected finalize to close the subagent window" >&2
  exit 1
fi

inspect_output="$("$MULTIAGENT" subagent inspect subagent-watch --lines 5)"
[[ "$inspect_output" == *"Final status: completed"* ]]

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-restore"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/status"
printf 'Previous progress: halfway through recovery work\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/current.txt"
printf 'Older transcript context\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log"
cat >"$MULTIAGENT_STATE_DIR/subagents/subagent-restore/meta.env" <<EOF
name=subagent-restore
session=$MULTIAGENT_SESSION
root=$ROOT
write_policy=$MULTIAGENT_WRITE_POLICY
cli=claude
cli_bin=true
created_at=2026-01-01T00:00:00Z
EOF
printf 'Restored Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-restore.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-blocked"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked/status"
printf 'Blocked: need input from orchestrator\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked/current.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/logs" "$MULTIAGENT_STATE_DIR/workflows/dashboard-flow"
printf 'orchestrator event: routed worker-01-docs\n' >"$MULTIAGENT_STATE_DIR/logs/orchestrator.log"
cat >"$MULTIAGENT_STATE_DIR/workflows/dashboard-flow/nodes.tsv" <<'EOF'
node_id	agent	assignment_id	role	branch	owned_paths	status	decision_id	plan_id	added_at
impl	worker-impl	A-impl	exploitation	feature/docs	README.md	blocked	DEC-1	PLAN-1	2026-01-01T00:00:00Z
docs	worker-docs	A-docs	exploitation	feature/docs	docs/	running	DEC-1	PLAN-1	2026-01-01T00:00:01Z
EOF
watch_output="$("$MULTIAGENT" watch --once --log-lines 5)"
[[ "$watch_output" == *"Multiagent Dashboard"* ]]
[[ "$watch_output" == *"Agent Status Summary"* ]]
[[ "$watch_output" == *"Blocked Agents"* ]]
[[ "$watch_output" == *"subagent-blocked"* ]]
[[ "$watch_output" == *"DAG Summary"* ]]
[[ "$watch_output" == *"dashboard-flow"* ]]
[[ "$watch_output" == *$'dashboard-flow\timpl\tblocked\tworker-impl'* ]]
[[ "$watch_output" == *"orchestrator event: routed worker-01-docs"* ]]

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-prompt-only"
printf 'missing\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-prompt-only/status"
printf 'If blocked, stop and state what you need. Do not finish with only a plan while /app has no materialized source diff.\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-prompt-only/current.txt"
cat >"$MULTIAGENT_STATE_DIR/subagents/subagent-prompt-only/meta.env" <<EOF
name=subagent-prompt-only
session=$MULTIAGENT_SESSION
root=$ROOT
write_policy=$MULTIAGENT_WRITE_POLICY
cli=claude
cli_bin=true
created_at=2026-01-01T00:00:00Z
EOF
printf 'Restored prompt-only Claude prompt ready\n' >"$MOCK_TMUX_CAPTURES/subagent-prompt-only.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-open"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-open/status"
printf 'Still active in tmux\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-open/current.txt"
printf 'subagent-open\n' >>"$MOCK_TMUX_WINDOWS"
printf 'Open subagent prompt\n' >"$MOCK_TMUX_CAPTURES/subagent-open.txt"

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-unknown"

recover_plan="$("$MULTIAGENT" subagent recover-plan)"
[[ "$recover_plan" == *$'subagent-watch\tskip-finalized\tstatus-finalized\tfinalized\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-watch"* ]]
[[ "$recover_plan" == *$'subagent-restore\trestore\tclosed-with-recoverable-context\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-restore"* ]]
[[ "$recover_plan" == *$'subagent-blocked\tskip-blocked\trequires-orchestrator-decision\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-blocked"* ]]
[[ "$recover_plan" == *$'subagent-prompt-only\trestore\tclosed-with-recoverable-context\tmissing\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-prompt-only"* ]]
[[ "$recover_plan" == *$'subagent-open\tskip-open\ttmux-window-already-open\trunning\topen\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-open"* ]]
[[ "$recover_plan" == *$'subagent-unknown\tskip-unknown\tno-current-or-transcript\tunknown\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-unknown"* ]]
[[ "$recover_plan" == *$'subagent-structured\trestore\tcheckpoint-resumable\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-structured"* ]]
structured_blocked_output="$("$MULTIAGENT" subagent checkpoint-update subagent-structured --step "verified checkpoint recovery preference" --blocker "aggregate restore-all test should not restore this fixture")"
[[ "$structured_blocked_output" == $'checkpoint updated\tsubagent-structured\tblocked' ]]

blocked_restore_file="$TMPDIR/blocked-restore.out"
if "$MULTIAGENT" subagent restore subagent-blocked >"$blocked_restore_file" 2>&1; then
  echo "expected blocked subagent restore to require force" >&2
  cat "$blocked_restore_file" >&2
  exit 1
fi
assert_file_contains "$blocked_restore_file" "refusing to restore subagent-blocked: skip-blocked"

restore_output="$("$MULTIAGENT" subagent restore subagent-restore)"
[[ "$restore_output" == "restored subagent-restore" ]]
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-restore"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/restore_events.log" "prior_status=running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/restore_events.log" "cli=claude"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "You are a restored long-running subagent."
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "Previous progress: halfway through recovery work"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/instruction.txt" "You are a restored long-running subagent."
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-restore Read and follow the assignment in $MULTIAGENT_STATE_DIR/subagents/subagent-restore/instruction.txt"
assert_file_contains "$MOCK_TMUX_LOG" "pipe-pane test-session:subagent-restore cat >> $MULTIAGENT_STATE_DIR/logs/subagent-restore.log"
claude_restore_line="$(grep -F "new-window -d test-session subagent-restore " "$MOCK_TMUX_LOG")"
[[ "$claude_restore_line" == *"--dangerously-skip-permissions"* ]]
if [[ "$claude_restore_line" == *"--cd"* || "$claude_restore_line" == *"--no-alt-screen"* ]]; then
  echo "expected restore to use persisted Claude CLI without Codex-only flags" >&2
  echo "$claude_restore_line" >&2
  exit 1
fi

restore_all_output="$("$MULTIAGENT" subagent restore-all)"
[[ "$restore_all_output" == *$'skipped subagent-blocked\tskip-blocked'* ]]
[[ "$restore_all_output" == *$'skipped subagent-open\tskip-open'* ]]
[[ "$restore_all_output" == *$'skipped subagent-watch\tskip-finalized'* ]]
[[ "$restore_all_output" == *"restored subagent-prompt-only"* ]]
[[ "$restore_all_output" == *"restore-all complete: restored=1"* ]]

# Test organizational learning functionality

# Test multiagent decision basic functionality
DECISION_STATE_DIR="$TMPDIR/decision-state"
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision init DEC-001 --title "Test Decision" --owner "test-user"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "decision_id=DEC-001"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "title=Test Decision"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "owner=test-user"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=open"

# Test multiagent decision add-alternative
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision add-alternative DEC-001 \
  --plan-id PLAN-A --summary "First approach" --proposed-by agent-1 \
  --branch worker/plan-a --assignment-name worker-implementation \
  --expected-outcome "Fast delivery" --risk "Technical debt"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "PLAN-A"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "First approach"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "agent-1"

# Test multiagent decision add-assumption
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision add-assumption DEC-001 \
  --assumption-id ASSUME-1 --statement "API will be stable" \
  --confidence "high" --validation-method "integration tests" \
  --expected-signal "no breaking changes"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/assumptions.tsv" "ASSUME-1"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/assumptions.tsv" "API will be stable"

# Test multiagent decision commit
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision commit DEC-001 \
  --selected-plan PLAN-A --reason "Best balance of speed and quality" \
  --rollback-policy "Manual rollback" --reflection-due "2026-06-01"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=committed"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "selected_plan=PLAN-A"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "reason=Best balance of speed and quality"

# Test multiagent decision record-metric
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision record-metric DEC-001 \
  --name "delivery-time" --expected "2 weeks" --actual "3 weeks"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "delivery-time"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "2 weeks"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "3 weeks"

# Test multiagent decision reflect
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision reflect DEC-001 \
  --recommendation "adjust" --reason "Delivery was slower than expected" \
  --follow-up-assignment "optimization-task"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=reflected"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "recommendation=adjust"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "reflection_reason=Delivery was slower than expected"

# Test multiagent decision show and list
show_output="$(MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision show DEC-001)"
[[ "$show_output" == *"Decision: DEC-001"* ]]
[[ "$show_output" == *"title=Test Decision"* ]]

list_output="$(MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision list)"
[[ "$list_output" == *$'DEC-001\treflected\tTest Decision\ttest-user'* ]]

# Test multiagent decision error conditions
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision init DEC-001 --title "Duplicate" >"$TMPDIR/duplicate.out" 2>&1; then
  echo "expected duplicate decision to fail" >&2
  cat "$TMPDIR/duplicate.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate.out" "decision already exists: DEC-001"

# Test invalid decision ID
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision init "DEC/INVALID" --title "Bad ID" >"$TMPDIR/invalid-id.out" 2>&1; then
  echo "expected invalid decision ID to fail" >&2
  cat "$TMPDIR/invalid-id.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-id.out" "invalid decision ID: DEC/INVALID"

# Test invalid recommendation
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision reflect DEC-001 --recommendation "invalid" --reason "test" >"$TMPDIR/invalid-rec.out" 2>&1; then
  echo "expected invalid recommendation to fail" >&2
  cat "$TMPDIR/invalid-rec.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-rec.out" "invalid recommendation: invalid"

# Test newline rejection
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision init DEC-NEWLINE --title "$(printf 'Title\nwith\nnewlines')" >"$TMPDIR/newline.out" 2>&1; then
  echo "expected newline in title to fail" >&2
  cat "$TMPDIR/newline.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/newline.out" "--title may not contain newlines"

# Test duplicate plan ID with a new decision
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision init DEC-002 --title "Test Duplicates"
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision add-alternative DEC-002 --plan-id PLAN-B --summary "First plan" --proposed-by agent-1
set +e  # Temporarily disable exit on error
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$MULTIAGENT" decision add-alternative DEC-002 --plan-id PLAN-B --summary "Duplicate" --proposed-by agent-2 >"$TMPDIR/duplicate-plan.out" 2>&1
duplicate_result=$?
set -e  # Re-enable exit on error
if [[ "$duplicate_result" -eq 0 ]]; then
  echo "expected duplicate plan ID to fail" >&2
  cat "$TMPDIR/duplicate-plan.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate-plan.out" "plan ID already exists: PLAN-B"
# Test assignment-create with organizational metadata
ORG_ASSIGN_REPO="$TMPDIR/org-assignment-repo"
ORG_ASSIGN_STATE="$TMPDIR/org-assignment-state"
mkdir -p "$ORG_ASSIGN_REPO" "$ORG_ASSIGN_STATE"
(
  cd "$ORG_ASSIGN_REPO"
  git init -q
  git config user.email "test@example.com"
  git config user.name "Test User"
  git config commit.gpgsign false
  printf 'hello\n' >README.md
  git add README.md
  git commit -q -m "initial"
  git switch -q -c worker/org-task
)

org_assignment_create_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-org --assignment-id org-001 --branch worker/org-task --owned README.md --role qa --decision-id DEC-001 --plan-id PLAN-A)"
[[ "$org_assignment_create_output" == $'assignment created\tworker-org\torg-001\tworker/org-task' ]]
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "assignment_id=org-001"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "role=qa"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "decision_id=DEC-001"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "plan_id=PLAN-A"
# Test invalid role rejection
set +e
MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-bad --assignment-id bad-001 --branch worker/org-task --owned README.md --role invalid-role >"$TMPDIR/invalid-role.out" 2>&1
invalid_role_result=$?
set -e
if [[ "$invalid_role_result" -eq 0 ]]; then
  echo "expected invalid role to fail" >&2
  cat "$TMPDIR/invalid-role.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-role.out" "invalid role 'invalid-role'"
# Test checkpoint-update includes organizational metadata
checkpoint_org_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" subagent checkpoint-update worker-org --step "implemented org metadata" --status running)"
[[ "$checkpoint_org_output" == $'checkpoint updated\tworker-org\trunning' ]]
checkpoint_show_org_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" subagent checkpoint-show worker-org)"
[[ "$checkpoint_show_org_output" == *"role=qa"* ]]
[[ "$checkpoint_show_org_output" == *"decision_id=DEC-001"* ]]
[[ "$checkpoint_show_org_output" == *"plan_id=PLAN-A"* ]]
# Test multiagent status includes organizational metadata columns
# Create a persisted subagent with organizational metadata that won't trigger polling
mkdir -p "$ORG_ASSIGN_STATE/subagents/subagent-org-test"
printf 'running\n' >"$ORG_ASSIGN_STATE/subagents/subagent-org-test/status"
printf 'Testing organizational metadata in subagents\n' >"$ORG_ASSIGN_STATE/subagents/subagent-org-test/current.txt"

# Create assignment metadata for the subagent
ORG_SUBAGENT_ASSIGN_OUTPUT="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create subagent-org-test --assignment-id org-sub-001 --branch worker/org-task --owned README.md --role verifier --decision-id DEC-002 --plan-id PLAN-B)"

status_org_output="$(cd "$ROOT" && MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$MULTIAGENT" status)"
[[ "$status_org_output" == *$'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID'* ]]
[[ "$status_org_output" == *$'subagent\tsubagent-org-test\trunning\tclosed\tTesting organizational metadata in subagents\t'"$ORG_ASSIGN_STATE/subagents/subagent-org-test"$'\tverifier\tDEC-002\tPLAN-B'* ]]
# Test that subagents without metadata show "-" for organizational fields
mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta/status"
printf 'Subagent without org metadata\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta/current.txt"
printf 'subagent-no-meta\n' >>"$MOCK_TMUX_WINDOWS"
printf 'Subagent without org metadata progress\n' >"$MOCK_TMUX_CAPTURES/subagent-no-meta.txt"
status_no_meta_output="$("$MULTIAGENT" status)"
[[ "$status_no_meta_output" == *$'subagent\tsubagent-no-meta\trunning\topen\tSubagent without org metadata progress\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta"$'\t-\t-\t-'* ]]
# Test documentation consistency - no unsupported multiagent plan or multiagent decision resolve commands
for documentation_file in "$ROOT/README.md" "$ROOT/docs/getting-started.md"; do
  if grep -Fq "bin/multiagent plan" "$documentation_file"; then
    echo "$documentation_file should not reference unsupported bin/multiagent plan" >&2
    exit 1
  fi
  if grep -Fq "multiagent decision resolve" "$documentation_file"; then
    echo "$documentation_file should not reference unsupported multiagent decision resolve command" >&2
    exit 1
  fi
done
if grep -Fq "bin/multiagent plan" "$ROOT/orchestrator_prompt.md"; then
  echo "orchestrator_prompt.md should not reference unsupported bin/multiagent plan" >&2
  exit 1
fi
if grep -Fq "multiagent decision resolve" "$ROOT/orchestrator_prompt.md"; then
  echo "orchestrator_prompt.md should not reference unsupported multiagent decision resolve command" >&2
  exit 1
fi

# Verify that decision command examples in the operations guide use only supported commands
decision_commands_guide="$(grep "multiagent decision" "$ROOT/docs/getting-started.md" || true)"
[[ "$decision_commands_guide" == *"multiagent decision init"* ]]
[[ "$decision_commands_guide" == *"multiagent decision add-alternative"* ]]
[[ "$decision_commands_guide" == *"multiagent decision commit"* ]]
[[ "$decision_commands_guide" == *"multiagent decision list"* ]]
[[ "$decision_commands_guide" == *"multiagent decision show"* ]]

# Verify that decision command examples in the organizational-learning module use only supported commands
decision_commands_prompt="$(grep "multiagent decision" "$ROOT/prompts/roles/organizational-learning.md" || true)"
[[ "$decision_commands_prompt" == *"multiagent decision init"* ]]
[[ "$decision_commands_prompt" == *"multiagent decision add-alternative"* ]]
[[ "$decision_commands_prompt" == *"multiagent decision commit"* ]]

# Test DAG workflow control functionality

# Test basic DAG commands with temporary state
DAG_STATE_DIR="$TMPDIR/dag-state"
mkdir -p "$DAG_STATE_DIR"

# Test multiagent dag init
init_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag init WF-001 --title "Test Workflow" --owner "test-user")"
[[ "$init_output" == $'workflow created\tWF-001\tTest Workflow' ]]
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "workflow_id=WF-001"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "title=Test Workflow"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "owner=test-user"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "status=active"

# Test multiagent dag add-node
node_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-A --agent worker-a --assignment-id assign-a --role qa --branch worker/a --owned file-a.txt)"
[[ "$node_output" == $'node added\tWF-001\tNODE-A\tworker-a' ]]
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "NODE-A"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "worker-a"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "pending"

# Test multiagent dag list
list_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag list)"
[[ "$list_output" == *$'WF-001\tactive\tTest Workflow\ttest-user'* ]]

# Test multiagent dag show
show_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag show WF-001)"
[[ "$show_output" == *"Workflow: WF-001"* ]]
[[ "$show_output" == *"workflow_id=WF-001"* ]]
[[ "$show_output" == *"NODE-A"* ]]

# Test DAG sequencing: node A ready first, node B ready only after A is done
node_b_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-B --agent worker-b --assignment-id assign-b --role qa --branch worker/b --owned file-b.txt --depends-on NODE-A)"
[[ "$node_b_output" == $'node added\tWF-001\tNODE-B\tworker-b' ]]

# Test multiagent dag ready - node A should be ready, node B should not
# Also test that ready emits only node IDs, one per line, with no READY_NODES header
ready_initial_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag ready WF-001)"
[[ "$ready_initial_output" == *"NODE-A"* ]]
if [[ "$ready_initial_output" == *"NODE-B"* ]]; then
  echo "expected NODE-B to not be ready before NODE-A is done" >&2
  echo "$ready_initial_output" >&2
  exit 1
fi
# Verify no header is present
if [[ "$ready_initial_output" == *"READY_NODES"* ]]; then
  echo "expected ready output to have no READY_NODES header" >&2
  echo "$ready_initial_output" >&2
  exit 1
fi
# Verify output is just node IDs, one per line
if [[ "$ready_initial_output" != "NODE-A" ]]; then
  echo "expected ready output to be just node ID with no extra content" >&2
  echo "Got: '$ready_initial_output'" >&2
  exit 1
fi

# Mark NODE-A as done
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-A done --reason "completed task A"

# Now NODE-B should be ready
ready_after_a_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag ready WF-001)"
[[ "$ready_after_a_output" == *"NODE-B"* ]]

# Test failed upstream node causes downstream node to appear in blocked output
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-C --agent worker-c --assignment-id assign-c --role qa --branch worker/c --owned file-c.txt --depends-on NODE-B

# Mark NODE-B as failed
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-B failed --reason "task failed"

# Test multiagent dag blocked - NODE-C should be blocked
blocked_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag blocked WF-001)"
[[ "$blocked_output" == *"NODE-C"* ]]
[[ "$blocked_output" == *"dependency NODE-B failed"* ]]

# Test skipped upstream node satisfies dependencies
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-D --agent worker-d --assignment-id assign-d --role qa --branch worker/d --owned file-d.txt
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-E --agent worker-e --assignment-id assign-e --role qa --branch worker/e --owned file-e.txt --depends-on NODE-D

# Mark NODE-D as skipped
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-D skipped --reason "conditions not met"

# NODE-E should now be ready (skipped dependencies satisfy constraints)
ready_after_skip_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag ready WF-001)"
[[ "$ready_after_skip_output" == *"NODE-E"* ]]

# Test explicitly marked ready nodes
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-F --agent worker-f --assignment-id assign-f --role qa --branch worker/f --owned file-f.txt

# Mark NODE-F as explicitly ready
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-F ready --reason "manually marked ready"

# NODE-F should appear in ready output even though it was explicitly marked ready
ready_explicit_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag ready WF-001)"
[[ "$ready_explicit_output" == *"NODE-F"* ]]

# Mark NODE-F as running and verify it no longer appears in ready output
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-F running --reason "started execution"
ready_after_running_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag ready WF-001)"
if [[ "$ready_after_running_output" == *"NODE-F"* ]]; then
  echo "expected NODE-F to not appear in ready output when marked running" >&2
  echo "$ready_after_running_output" >&2
  exit 1
fi

# Test duplicate workflow rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag init WF-001 --title "Duplicate" >"$TMPDIR/duplicate-workflow.out" 2>&1; then
  echo "expected duplicate workflow to fail" >&2
  cat "$TMPDIR/duplicate-workflow.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate-workflow.out" "workflow already exists: WF-001"

# Test duplicate node rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-A --agent worker-dup --assignment-id assign-dup --role qa --branch worker/dup --owned file-dup.txt >"$TMPDIR/duplicate-node.out" 2>&1; then
  echo "expected duplicate node to fail" >&2
  cat "$TMPDIR/duplicate-node.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate-node.out" "node ID already exists: NODE-A"

# Test missing dependency rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-MISSING --agent worker-missing --assignment-id assign-missing --role qa --branch worker/missing --owned file-missing.txt --depends-on NONEXISTENT >"$TMPDIR/missing-dep.out" 2>&1; then
  echo "expected missing dependency to fail" >&2
  cat "$TMPDIR/missing-dep.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/missing-dep.out" "dependency does not exist: NONEXISTENT"

# Test invalid status rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag status WF-001 NODE-A invalid-status >"$TMPDIR/invalid-status.out" 2>&1; then
  echo "expected invalid status to fail" >&2
  cat "$TMPDIR/invalid-status.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-status.out" "invalid status: invalid-status"

# Test role validation - invalid roles should be rejected
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 NODE-INVALID-ROLE --agent worker-invalid --assignment-id assign-invalid --role decision --branch worker/invalid --owned file-invalid.txt >"$TMPDIR/invalid-role.out" 2>&1; then
  echo "expected invalid role 'decision' to fail" >&2
  cat "$TMPDIR/invalid-role.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-role.out" "invalid role: decision"

# Test role validation - valid roles should be accepted
valid_roles=("exploitation" "exploration" "reflection" "architecture" "qa" "verifier" "scout")
for i in "${!valid_roles[@]}"; do
  role="${valid_roles[$i]}"
  node_id="NODE-ROLE-$i"
  role_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-001 "$node_id" --agent "worker-$role" --assignment-id "assign-$role" --role "$role" --branch "worker/$role" --owned "file-$role.txt")"
  [[ "$role_output" == *"node added"* ]]
  [[ "$role_output" == *"$node_id"* ]]
done

# Test invalid workflow ID rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag init "WF/INVALID" --title "Bad ID" >"$TMPDIR/invalid-workflow-id.out" 2>&1; then
  echo "expected invalid workflow ID to fail" >&2
  cat "$TMPDIR/invalid-workflow-id.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-workflow-id.out" "invalid workflow ID: WF/INVALID"

# Test cycle detection
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag init WF-CYCLE --title "Cycle Test"
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-CYCLE CYCLE-A --agent worker-cycle-a --assignment-id assign-cycle-a --role qa --branch worker/cycle-a --owned file-cycle-a.txt
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-CYCLE CYCLE-B --agent worker-cycle-b --assignment-id assign-cycle-b --role qa --branch worker/cycle-b --owned file-cycle-b.txt --depends-on CYCLE-A

# This should create a cycle: CYCLE-A -> CYCLE-B -> CYCLE-A
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-CYCLE CYCLE-C --agent worker-cycle-c --assignment-id assign-cycle-c --role qa --branch worker/cycle-c --owned file-cycle-c.txt --depends-on CYCLE-B && \
   MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-CYCLE CYCLE-D --agent worker-cycle-d --assignment-id assign-cycle-d --role qa --branch worker/cycle-d --owned file-cycle-d.txt --depends-on CYCLE-A; then
  # Now try to create a cycle by making CYCLE-A depend on CYCLE-C
  temp_edges="$DAG_STATE_DIR/workflows/WF-CYCLE/edges.tsv"
  printf 'CYCLE-C\tCYCLE-A\t%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$temp_edges"
  if ! MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$MULTIAGENT" dag add-node WF-CYCLE CYCLE-TEST --agent worker-test --assignment-id assign-test --role qa --branch worker/test --owned file-test.txt --depends-on CYCLE-A >"$TMPDIR/cycle-test.out" 2>&1; then
    assert_file_contains "$TMPDIR/cycle-test.out" "dependency cycle detected"
  fi
fi

# Test assignment-create accepts DAG metadata
DAG_ASSIGN_REPO="$TMPDIR/dag-assignment-repo"
DAG_ASSIGN_STATE="$TMPDIR/dag-assignment-state"
mkdir -p "$DAG_ASSIGN_REPO" "$DAG_ASSIGN_STATE"
(
  cd "$DAG_ASSIGN_REPO"
  git init -q
  git config user.email "test@example.com"
  git config user.name "Test User"
  git config commit.gpgsign false
  printf 'hello\n' >README.md
  git add README.md
  git commit -q -m "initial"
  git switch -q -c worker/dag-task
)

dag_assignment_create_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create worker-dag --assignment-id dag-001 --branch worker/dag-task --owned README.md --role qa --workflow-id WF-001 --node-id NODE-A --depends-on NODE-B,NODE-C)"
[[ "$dag_assignment_create_output" == $'assignment created\tworker-dag\tdag-001\tworker/dag-task' ]]
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "workflow_id=WF-001"
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "node_id=NODE-A"
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "depends_on=NODE-B,NODE-C"

# Test checkpoint-update includes DAG metadata
checkpoint_dag_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$MULTIAGENT" subagent checkpoint-update worker-dag --step "implemented dag metadata support" --status running)"
[[ "$checkpoint_dag_output" == $'checkpoint updated\tworker-dag\trunning' ]]
checkpoint_show_dag_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$MULTIAGENT" subagent checkpoint-show worker-dag)"
[[ "$checkpoint_show_dag_output" == *"workflow_id=WF-001"* ]]
[[ "$checkpoint_show_dag_output" == *"node_id=NODE-A"* ]]
[[ "$checkpoint_show_dag_output" == *"depends_on=NODE-B,NODE-C"* ]]

# Test multiagent status emits WORKFLOW_ID and NODE_ID columns with metadata
# Create a persisted subagent with DAG metadata
mkdir -p "$DAG_ASSIGN_STATE/subagents/subagent-dag-test"
printf 'running\n' >"$DAG_ASSIGN_STATE/subagents/subagent-dag-test/status"
printf 'Testing DAG metadata in subagents\n' >"$DAG_ASSIGN_STATE/subagents/subagent-dag-test/current.txt"

# Create assignment metadata for the subagent with DAG metadata
DAG_SUBAGENT_ASSIGN_OUTPUT="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$MULTIAGENT" subagent assignment-create subagent-dag-test --assignment-id dag-sub-001 --branch worker/dag-task --owned README.md --role verifier --workflow-id WF-002 --node-id NODE-X)"

status_dag_output="$(cd "$ROOT" && MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$MULTIAGENT" status)"
[[ "$status_dag_output" == *$'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID\tWORKFLOW_ID\tNODE_ID'* ]]
[[ "$status_dag_output" == *$'subagent\tsubagent-dag-test\trunning\tclosed\tTesting DAG metadata in subagents\t'"$DAG_ASSIGN_STATE/subagents/subagent-dag-test"$'\tverifier\t-\t-\tWF-002\tNODE-X'* ]]

# Test documentation consistency - ensure docs do not reference unsupported DAG commands
if grep -Fq "multiagent dag update-status" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag update-status command" >&2
  exit 1
fi
if grep -Fq "multiagent dag.*--description" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag --description flag" >&2
  exit 1
fi
if grep -Fq "multiagent dag show --node" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag show --node flag" >&2
  exit 1
fi
if grep -Fq "multiagent dag show --verbose" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag show --verbose flag" >&2
  exit 1
fi
if grep -Fq "multiagent dag ready --watch" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag ready --watch flag" >&2
  exit 1
fi
if grep -Fq "multiagent dag export" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag export command" >&2
  exit 1
fi
if grep -Fq "multiagent dag status --workflow" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported multiagent dag status --workflow flag" >&2
  exit 1
fi
if grep -Fq "role decision" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported role decision" >&2
  exit 1
fi

# Test documentation consistency - ensure docs don't contain fragile parsing examples
if grep -Fq 'grep.*assignment-id' "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not contain fragile grep parsing examples with assignment-id" >&2
  exit 1
fi
if grep -Fq 'cut -d:' "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not contain fragile cut -d: parsing examples" >&2
  exit 1
fi
if grep -Fq 'grep.*\$node.*assignment-id' "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not contain fragile node assignment-id parsing examples" >&2
  exit 1
fi

echo "DAG workflow tests passed"
echo "organizational learning tests passed"
"$ROOT/tests/lifecycle.sh"
