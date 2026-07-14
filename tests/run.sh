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
export MULTIAGENT_READY_ATTEMPTS=1
export MULTIAGENT_READY_DELAY=0
export CODEX_BIN="true"
export CLAUDE_BIN="true"
export ORCHESTRATOR_CLI="codex"
export WORKER_CLI="claude"
export SUBAGENT_CLI="claude"
export VERIFIER_CLI="codex"

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

"$ROOT/bin/write-policy.sh" init
assert_file_contains "$MULTIAGENT_WRITE_POLICY" "Default allowed write root"

policy_show="$("$ROOT/bin/write-policy.sh" show)"
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
LAUNCH_BOOTSTRAP="$LAUNCH_STATE/orchestrator-bootstrap.sh"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$LAUNCH_BOOTSTRAP")"
assert_file_contains "$LAUNCH_BOOTSTRAP" "--cd $LAUNCH_TARGET"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_RESUME=0"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export MULTIAGENT_VERIFIER_MAX_ITERATIONS=3"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export WORKER_CLI=claude"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export SUBAGENT_CLI=claude"
assert_file_contains "$LAUNCH_BOOTSTRAP" "export VERIFIER_CLI=codex"
assert_file_contains "$LAUNCH_BOOTSTRAP" "Multiagent\\ launch\\ mode:"
assert_file_contains "$LAUNCH_BOOTSTRAP" "$(printf '%q' "$ROOT/orchestrator_prompt.md")"
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
assert_file_contains "$TMPDIR/launch-explicit-state/orchestrator-bootstrap.sh" "$(printf '%q' "$EXPLICIT_PROMPT")"

REPAIR_STATE="$TMPDIR/repair-state"
mkdir -p "$REPAIR_STATE"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" finding-create build-go-ofrep \
  --severity blocking \
  --type compile_failure \
  --summary "Changed Go packages do not compile" \
  --affected internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go \
  --evidence-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":1,"stderr_excerpt":"undefined: req.Request"}' \
  --required-resolution "Final diff must compile with rc=0 for both changed Go packages." >"$TMPDIR/finding-create.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" todo-create todo-017 \
  --source-finding-id build-go-ofrep \
  --task "Fix Go compile failure in changed packages." \
  --context "Exact verifier evidence." \
  --done-criteria "run go test ./internal/server/ofrep" \
  --done-criteria "record returncode=0 after final diff" >"$TMPDIR/todo-create.out"
assert_file_contains "$REPAIR_STATE/todos/todo-017/todo.json" '"required_commands":'
assert_file_contains "$REPAIR_STATE/todos/todo-017/todo.json" '"go test ./internal/server/ofrep"'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" resolution-create todo-017 \
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
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" resolution-create todo-017 \
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
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" resolution-create todo-017 \
  --worker worker-02-ofrep-build \
  --status resolved \
  --changed internal/server/ofrep/evaluation.go \
  --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":0}]' \
  --why "Changed package compiles after the final diff." >"$TMPDIR/resolution-create.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" todo-status todo-017 closed >"$TMPDIR/direct-close.out"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" gate-check >"$TMPDIR/gate-missing-closure.out" 2>&1; then
  echo "expected direct closed todo without verifier closure to fail gate-check" >&2
  cat "$TMPDIR/gate-missing-closure.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-missing-closure.out" "closed-todo-missing-verifier-closure"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" todo-status todo-017 resolved >"$TMPDIR/reopen-resolved.out"
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" todo-close todo-017 \
  --verified-by verifier-01-ofrep-build \
  --recheck-json '{"accepted":false,"finding_rechecked":"build-go-ofrep"}' >"$TMPDIR/close-rejected.out" 2>&1; then
  echo "expected verifier closure with accepted=false to fail" >&2
  cat "$TMPDIR/close-rejected.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/close-rejected.out" "accepted=true"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" todo-close todo-017 \
  --verified-by verifier-01-ofrep-build \
  --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0}],"final_diff_hash":"abc123"}' \
  --notes "Verifier rechecked original finding after worker resolution." >"$TMPDIR/todo-close.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" gate-check >"$TMPDIR/gate-closed.out"
assert_file_contains "$TMPDIR/gate-closed.out" "accepted"
assert_file_contains "$REPAIR_STATE/todos/todo-017/closure.json" '"verified_by": "verifier-01-ofrep-build"'

MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-acquire go-ofrep \
  --owner worker-02-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" \
  --resource-risk "go test under Docker/Rosetta" >"$TMPDIR/lease-acquire.out"
assert_file_contains "$TMPDIR/lease-acquire.out" $'validation lease acquired\tgo-ofrep\tworker-02-ofrep-build\trunning'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-acquire go-ofrep-dup \
  --owner verifier-01-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" >"$TMPDIR/lease-conflict.out" 2>&1; then
  echo "expected duplicate active validation lease to fail" >&2
  cat "$TMPDIR/lease-conflict.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/lease-conflict.out" "validation lease conflict"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-status go-ofrep passed \
  --result-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":0}' >"$TMPDIR/lease-passed.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-acquire go-ofrep-followup \
  --owner verifier-01-ofrep-build \
  --target "./internal/server/ofrep ./internal/server/evaluation" \
  --command "go test ./internal/server/ofrep ./internal/server/evaluation" >"$TMPDIR/lease-followup.out"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-list --state running >"$TMPDIR/lease-list.out"
assert_file_contains "$TMPDIR/lease-list.out" $'go-ofrep-followup\trunning\tverifier-01-ofrep-build'
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-show go-ofrep >"$TMPDIR/lease-show.out"
assert_file_contains "$TMPDIR/lease-show.out" '"returncode": 0'
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-run validation-run-ok \
  --owner worker-02-ofrep-build \
  --target "unit-target" \
  --resource-risk "cheap test command" \
  -- bash -lc 'printf validation-ok' >"$TMPDIR/validation-run-ok.out"
assert_file_contains "$TMPDIR/validation-run-ok.out" "validation-ok"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-show validation-run-ok >"$TMPDIR/validation-run-ok-lease.out"
assert_file_contains "$TMPDIR/validation-run-ok-lease.out" '"state": "passed"'
assert_file_contains "$TMPDIR/validation-run-ok-lease.out" '"returncode": 0'
mkdir -p "$TMPDIR/not-root"
(
  cd "$TMPDIR/not-root"
  MULTIAGENT_ROOT="$ROOT" MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-run validation-run-cwd \
    --owner worker-02-ofrep-build \
    --target "unit-target-cwd" \
    -- bash -lc 'pwd' >"$TMPDIR/validation-run-cwd.out"
)
assert_file_contains "$TMPDIR/validation-run-cwd.out" "$ROOT"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-show validation-run-cwd >"$TMPDIR/validation-run-cwd-lease.out"
assert_file_contains "$TMPDIR/validation-run-cwd-lease.out" "\"cwd\": \"$ROOT\""
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-run validation-run-fail \
  --owner worker-02-ofrep-build \
  --target "unit-target-fail" \
  -- bash -lc 'printf validation-fail >&2; exit 7' >"$TMPDIR/validation-run-fail.out" 2>"$TMPDIR/validation-run-fail.err"; then
  echo "expected validation-run to return the command failure rc" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/validation-run-fail.err" "validation-fail"
MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-lease-show validation-run-fail >"$TMPDIR/validation-run-fail-lease.out"
assert_file_contains "$TMPDIR/validation-run-fail-lease.out" '"state": "failed"'
assert_file_contains "$TMPDIR/validation-run-fail-lease.out" '"returncode": 7'
if MULTIAGENT_STATE_DIR="$REPAIR_STATE" "$ROOT/bin/subagent.sh" validation-run validation-run-conflict \
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
assert_file_contains "$ROOT/orchestrator_prompt.md" 'SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn'
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
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" 'verifier suggests no follow-up'
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "todo-create"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "todo-close"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "gate-check"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" "ownership blocker"
assert_file_contains "$ROOT/prompts/playbooks/agent-spawning.md" 'WORKER_CLI="${WORKER_CLI:-claude}"'
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
assert_file_contains "$ROOT/README.md" 'WORKER_CLI`: worker CLI for manual worker windows, default `claude`'
assert_file_contains "$ROOT/README.md" 'VERIFIER_CLI`: verifier CLI, default `codex`'
assert_file_contains "$ROOT/README.md" "Evaluation Framework"
assert_file_contains "$ROOT/README.md" "Parallel DAG Discipline"
assert_file_contains "$ROOT/README.md" "Structured Repair Loop"
assert_file_contains "$ROOT/README.md" "finding-todo-loop.md"
assert_file_contains "$ROOT/README.md" "todo-close"
assert_file_contains "$ROOT/README.md" 'orchestration` adapter covers planning behavior'
assert_file_contains "$ROOT/README.md" "evaluation/tasks"
assert_file_contains "$ROOT/evaluation/README.md" "large-update-300"
assert_file_contains "$ROOT/evaluation/README.md" "Low-signal orchestration cases"
assert_file_contains "$ROOT/evaluation/README.md" "EVAL_VALIDATION_PROBE_TIMEOUT"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" "Return generic source-derived blockers without benchmark answer leakage"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" "hidden-test-shaped commands"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "One active validator per package/path"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "validation lease table"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "validation-run"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "validation-lease-acquire"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "Do not spawn a verifier while a worker still owns"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "Fixture/testdata"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "unresolved parity gaps are blocking"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "Reject first-match-only fixes"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "replacement probe asserts the new exact output shape"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "replacement-probe-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "stale-visible-failure-justified:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "final-output-field="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "expected-output-count=N"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "owner-evidence="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "candidate-owner="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "source-owner-ledger:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "constructor-dependency-checked:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "provider-capability-checked:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "ownership blocker"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "finding-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "todo-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "resolution-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "todo-close"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "gate-check"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "one single machine-readable"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "removed-symbol="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "stale-visible-reconciliation.txt"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "aggregate count"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "machine-gated evidence markers"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "replacement-probe-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "stale-visible-failure-justified:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "multi-value-probe-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "final-output-field="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "multi-value-probe.txt"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "source-owner-ledger:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "constructor-dependency-checked:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "provider-capability-checked:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "required-path-outside-owned:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "ownership blocker"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "finding-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "todo-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "resolution-create"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "todo-close"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "gate-check"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "owner-evidence="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "candidate-owner="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "one single machine-readable"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "renamed-symbol="
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "stale-visible-reconciliation.txt"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "per affected output collection"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "run a convergence"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "long planning loop"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "stale hunk"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "stale-hunk"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "Inline golden expectations"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "nearest visible"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "narrow root-cause"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "same-package tests"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "fresh bounded repair worker"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "Convergence checkpoint"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_CONVERGENCE_FOLLOWUP_AFTER"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "No-diff planning checkpoint"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_NO_DIFF_CHECKPOINT_AFTER"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_PROGRESS_REPAIR_ENABLED"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "progress watchdog spawned bounded repair worker"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "validation_text_has_no_test_evidence"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "treated this command as insufficient because it did not execute real selected tests"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "source-owner-candidates.md"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "go-mod-cache-adapter"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "\"GOMODCACHE\": ensure_cache_dir(RUNTIME_ROOT / \"go-mod-cache\")"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_VALIDATION_PROBE_TIMEOUT\", 900"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "source-owner-candidates"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_final_override.md" "production-native wrapper may run repository-visible validation"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "No-test compile checks"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "git diff --name-only"
assert_file_contains "$ROOT/evaluation/README.md" "production-native progress watchdog"
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
assert_file_contains "$ROOT/prompts/verifier.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/verifier.md" "constructor-dependency-checked:"
assert_file_contains "$ROOT/prompts/verifier.md" "provider-capability-checked:"
assert_file_contains "$ROOT/prompts/verifier.md" "go-package-validation-passed:"
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
assert_file_contains "$ROOT/prompts/worker.md" "one single machine-readable"
assert_file_contains "$ROOT/prompts/worker.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/prompts/worker.md" "owner-evidence="
assert_file_contains "$ROOT/prompts/worker.md" "candidate-owner="
assert_file_contains "$ROOT/prompts/worker.md" "source-owner-ledger:"
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
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-symbol map contract"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-symbol-map-passed:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "constructor-dependency contract"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "build-verification-passed:"
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "final-diff-sha256="
assert_file_contains "$ROOT/prompts/roles/build-verifier.md" "go-package-validation-passed:"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "source-owner-ledger:"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "prompts/roles/build-verifier.md"
assert_file_contains "$ROOT/prompts/playbooks/orchestration-routing.md" "build-verification-passed:"
assert_file_contains "$ROOT/prompts/roles/acceptance-scout.md" "declared-type ownership risk"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "declared receiver"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "declared type at that call site"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "visible tests"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "real production entrypoint"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "overreach boundary"
assert_file_contains "$ROOT/prompts/roles/contract-scout.md" "adapter-parity contract"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_ADAPTER_HELPER_MODE"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "adapter helper advisory mode: not spawning source-editing helper"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "completion marker refused because coverage blockers remain after follow-ups"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "final cleanup recovery requires adapter public validation before accepting visible-validation text"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "final cleanup recovery found a source diff but no durable worker validation evidence"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "completion marker recovered at final cleanup after adapter public probe passed without durable worker evidence"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "status.json already records completed final-diff build verification"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "stale-visible-reconciliation-passed:"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "STALE_VISIBLE_RECONCILIATION_PATH"
assert_file_contains "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md" "Do not rely on leaked evaluator tests"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" "must not inject benchmark-row-specific probes"
assert_file_contains "$ROOT/evaluation/README.md" "adapter helper defaults to advisory mode"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "_public_solver_metadata(dict(task.metadata or {}))"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" '"fail_to_pass"'
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" '"test_patch"'
assert_file_not_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "_enrich_metadata_with_official_contract(dict(task.metadata"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "_collect_rejection_diagnostics"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "/tmp/multiagent-prod-swe/status.json"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "helper-validation-probe.txt"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "git diff --stat HEAD --"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "diagnostics_tail"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "solver_internal_timeout"
assert_file_contains "$ROOT/evaluation/evalscope_multiagent_native_runner.py" "EVAL_NATIVE_SOLVER_TIMEOUT_RESERVE"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "Never gate production solving on official expected-test metadata"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "public solver inputs"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "solver metadata is public-only"
assert_file_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "orchestrator exited with unverified source diff"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" "changed_python_test_commands"
assert_file_contains "$ROOT/evaluation/native_solver/swe_prod_guardrails.py" "changed_go_feature_test_commands"
assert_file_not_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "EVAL_ALLOW_EXPECTED_TEST_GUIDANCE"
assert_file_not_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "official_test_contract_text"
assert_file_not_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "full official contract"
assert_file_not_contains "$ROOT/evaluation/native_solver/solve_swe_prod.py" "Official requirements/interface excerpt"
for prompt_path in \
  "$ROOT/prompts/worker.md" \
  "$ROOT/prompts/verifier.md" \
  "$ROOT/prompts/roles/acceptance-scout.md" \
  "$ROOT/prompts/roles/contract-scout.md" \
  "$ROOT/evaluation/native_solver/templates/swe_autonomous_appendix.md"
do
  assert_file_not_contains "$prompt_path" "FAIL_TO_PASS"
  assert_file_not_contains "$prompt_path" "PASS_TO_PASS"
  assert_file_not_contains "$prompt_path" "test_patch"
  assert_file_not_contains "$prompt_path" "hidden-test failures as post-hoc diagnostics"
done
python3 - "$ROOT" <<'PY'
import os
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from evaluation.native_solver import solve_swe_prod
from evaluation import swe_bench_pro_scaffold_parity
from evaluation.swe_bench_pro_on_demand import OnDemandImageManager
from evaluation import swe_bench_pro_run_parallel_shards

evalscope = SimpleNamespace()
sys.modules.setdefault("evalscope", evalscope)
sys.modules.setdefault("evalscope.agent", SimpleNamespace())
sys.modules.setdefault("evalscope.agent.external", SimpleNamespace())
sys.modules["evalscope.agent.external.runners"] = SimpleNamespace(
    AgentRunResult=object,
    AgentRunner=object,
    BridgeEndpoint=object,
    ExternalAgentTask=object,
    RunnerTimeoutError=RuntimeError,
)
sys.modules.setdefault("evalscope.api", SimpleNamespace())
sys.modules["evalscope.api.agent"] = SimpleNamespace(AgentEnvironment=object)
sys.modules["evalscope.api.registry"] = SimpleNamespace(register_runner=lambda _name: (lambda cls: cls))
sys.modules.setdefault("evalscope.utils", SimpleNamespace())
sys.modules["evalscope.utils.logger"] = SimpleNamespace(
    get_logger=lambda: SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
)
from evaluation import evalscope_multiagent_native_runner
from evaluation import swe_bench_pro_scaffold_parity

assert evalscope_multiagent_native_runner.solver_internal_timeout(3600) == 3000
os.environ["EVAL_NATIVE_SOLVER_TIMEOUT_RESERVE"] = "900"
try:
    assert evalscope_multiagent_native_runner.solver_internal_timeout(3600) == 2700
finally:
    os.environ.pop("EVAL_NATIVE_SOLVER_TIMEOUT_RESERVE", None)

captured_tmux_messages = []
original_run = solve_swe_prod.run
try:
    def fake_tmux_run(args, **_kwargs):
        if args[:3] == ["tmux", "send-keys", "-t"]:
            captured_tmux_messages.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    solve_swe_prod.run = fake_tmux_run
    solve_swe_prod.send_orchestrator_convergence_review(
        "test-session",
        elapsed_seconds=901,
        diff="diff --git a/src/service.py b/src/service.py\n+def fixed():\n+    return True\n",
        source_hints=["src/service.py"],
    )
finally:
    solve_swe_prod.run = original_run
literal_messages = [args[-1] for args in captured_tmux_messages if len(args) >= 6 and args[4] == "-l"]
assert literal_messages, captured_tmux_messages
convergence_message = literal_messages[0]
assert "Convergence checkpoint" in convergence_message, convergence_message
assert "spawn/read one verifier" in convergence_message, convergence_message
assert "source-derived probe failed" in convergence_message, convergence_message
assert "finding-create adapter-convergence-001" in convergence_message, convergence_message
assert "todo-create todo-adapter-convergence-001" in convergence_message, convergence_message
assert "gate-check" in convergence_message, convergence_message
assert "src/service.py" in convergence_message, convergence_message
for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "selected_test_files_to_run"):
    assert forbidden not in convergence_message, convergence_message

captured_tmux_messages = []
try:
    solve_swe_prod.run = fake_tmux_run
    solve_swe_prod.send_orchestrator_no_diff_checkpoint(
        "test-session",
        elapsed_seconds=601,
        issue="The CLI should preserve explicit output ordering when parsing repeated flags.",
    )
finally:
    solve_swe_prod.run = original_run
literal_messages = [args[-1] for args in captured_tmux_messages if len(args) >= 6 and args[4] == "-l"]
assert literal_messages, captured_tmux_messages
no_diff_message = literal_messages[0]
assert "No-diff planning checkpoint" in no_diff_message, no_diff_message
assert "spawn exactly one bounded implementation worker" in no_diff_message, no_diff_message
assert "concrete discovery gap" in no_diff_message, no_diff_message
for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "selected_test_files_to_run"):
    assert forbidden not in no_diff_message, no_diff_message

captured_tmux_messages = []
try:
    solve_swe_prod.run = fake_tmux_run
    solve_swe_prod.send_orchestrator_terminal_deadline(
        "test-session",
        remaining_seconds=599,
        diff="diff --git a/src/service.py b/src/service.py\n+def fixed():\n+    return True\n",
        blockers=["terminal deadline adapter-selected public validation failed; inspect helper-validation-probe.txt"],
        probe_report="pytest -q tests/test_service.py failed",
        source_hints=["src/service.py"],
    )
finally:
    solve_swe_prod.run = original_run
literal_messages = [args[-1] for args in captured_tmux_messages if len(args) >= 6 and args[4] == "-l"]
assert literal_messages, captured_tmux_messages
terminal_message = literal_messages[0]
assert "Terminal deadline checkpoint" in terminal_message, terminal_message
assert "write completed status" in terminal_message, terminal_message
assert "write blocked status" in terminal_message, terminal_message
assert "No-test compile checks are not behavioral validation" in terminal_message, terminal_message
assert "finding-create adapter-terminal-deadline-001" in terminal_message, terminal_message
assert "todo-create todo-adapter-terminal-deadline-001" in terminal_message, terminal_message
assert "gate-check" in terminal_message, terminal_message
assert "src/service.py" in terminal_message, terminal_message
for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "selected_test_files_to_run", "official failure", "selected official"):
    assert forbidden not in terminal_message, terminal_message

with tempfile.TemporaryDirectory() as td:
    runtime_root = Path(td) / "runtime"
    runtime_root.mkdir()
    original_runtime_root = solve_swe_prod.RUNTIME_ROOT
    original_ledger_path = solve_swe_prod.CONTRACT_LEDGER_PATH
    try:
        solve_swe_prod.RUNTIME_ROOT = runtime_root
        solve_swe_prod.CONTRACT_LEDGER_PATH = runtime_root / "contract-ledger.md"
        solve_swe_prod.CONTRACT_LEDGER_PATH.write_text("public issue/source invariant only\n", encoding="utf-8")
        base_prompt = runtime_root / "base-prompt.md"
        base_prompt.write_text("Base orchestrator prompt\n", encoding="utf-8")
        resume_prompt = solve_swe_prod.write_orchestrator_resume_prompt(
            base_prompt,
            attempt=1,
            reason="orchestrator exited with unverified source diff",
            issue="The public API should preserve caller ordering.",
            diff="diff --git a/src/service.py b/src/service.py\n+def fixed():\n+    return True\n",
            blockers=["adapter-selected public validation failed; inspect helper-validation-probe.txt"],
            probe_report="pytest -q tests/test_service.py failed",
            source_hints=["src/service.py"],
        )
        resume_text = resume_prompt.read_text(encoding="utf-8")
        assert "Production Native Resume Handoff" in resume_text, resume_text
        assert "not a new benchmark hint" in resume_text, resume_text
        assert "finding-create adapter-resume-01" in resume_text, resume_text
        assert "todo-create todo-adapter-resume-01" in resume_text, resume_text
        assert "gate-check" in resume_text, resume_text
        assert "src/service.py" in resume_text, resume_text
        assert "pytest -q tests/test_service.py failed" in resume_text, resume_text
        for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "selected_test_files_to_run", "official failure"):
            assert forbidden not in resume_text, resume_text
    finally:
        solve_swe_prod.RUNTIME_ROOT = original_runtime_root
        solve_swe_prod.CONTRACT_LEDGER_PATH = original_ledger_path

with tempfile.TemporaryDirectory() as td:
    runtime_root = Path(td) / "runtime"
    workdir = Path(td) / "repo"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "main.go").write_text("package main\nfunc EvaluateBulk() {}\n", encoding="utf-8")
    original_runtime_root = solve_swe_prod.RUNTIME_ROOT
    original_which = solve_swe_prod.shutil.which
    try:
        solve_swe_prod.RUNTIME_ROOT = runtime_root
        runtime_root.mkdir()
        solve_swe_prod.shutil.which = lambda cmd: None if cmd == "rg" else original_which(cmd)
        solve_swe_prod.write_rg_fallback()
        rg = runtime_root / "rg"
        assert rg.exists(), rg
        search = subprocess.run([str(rg), "-n", "EvaluateBulk", str(workdir)], text=True, capture_output=True, check=False)
        assert search.returncode == 0, search.stderr
        assert "src/main.go:2:func EvaluateBulk()" in search.stdout, search.stdout
        listed = subprocess.run([str(rg), "--files", str(workdir)], text=True, capture_output=True, check=False)
        assert listed.returncode == 0, listed.stderr
        assert "src/main.go" in listed.stdout, listed.stdout
    finally:
        solve_swe_prod.RUNTIME_ROOT = original_runtime_root
        solve_swe_prod.shutil.which = original_which

with tempfile.TemporaryDirectory() as td:
    runtime_root = Path(td) / "runtime"
    workdir = Path(td) / "repo"
    fake_go = Path(td) / "go-real"
    count_file = Path(td) / "go-count"
    workdir.mkdir()
    subprocess.run(["git", "init"], cwd=workdir, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    (workdir / "tracked.go").write_text("package main\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.go"], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=workdir, check=True, stdout=subprocess.DEVNULL)
    fake_go.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> " + str(count_file) + "\n"
        "sleep 0.2\n"
        "printf 'fake go %s\\n' \"$*\"\n",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    original_runtime_root = solve_swe_prod.RUNTIME_ROOT
    try:
        solve_swe_prod.RUNTIME_ROOT = runtime_root
        runtime_root.mkdir()
        solve_swe_prod.write_go_singleflight_wrapper(str(fake_go))
        go = runtime_root / "go"
        first_proc = subprocess.Popen([str(go), "test", "./pkg"], cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second_proc = subprocess.Popen([str(go), "test", "./pkg"], cwd=workdir, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        first_stdout, first_stderr = first_proc.communicate(timeout=10)
        second_stdout, second_stderr = second_proc.communicate(timeout=10)
        first = SimpleNamespace(returncode=first_proc.returncode, stdout=first_stdout, stderr=first_stderr)
        second = SimpleNamespace(returncode=second_proc.returncode, stdout=second_stdout, stderr=second_stderr)
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert "fake go test ./pkg" in first.stdout, first.stdout
        assert "fake go test ./pkg" in second.stdout, second.stdout
        assert count_file.read_text(encoding="utf-8").splitlines() == ["test ./pkg"]
        assert "waiting for duplicate validation" in (first.stderr + second.stderr), (first.stderr, second.stderr)
        assert "replaying completed validation" in (first.stderr + second.stderr), (first.stderr, second.stderr)
        (workdir / "tracked.go").write_text("package main\n// changed\n", encoding="utf-8")
        third = subprocess.run([str(go), "test", "./pkg"], cwd=workdir, text=True, capture_output=True, check=False)
        assert third.returncode == 0, third.stderr
        assert count_file.read_text(encoding="utf-8").splitlines() == ["test ./pkg", "test ./pkg"]
        system_go = fake_go.with_name("go")
        assert system_go.exists(), system_go
        fourth = subprocess.run([str(system_go), "test", "./system"], cwd=workdir, text=True, capture_output=True, check=False)
        assert fourth.returncode == 0, fourth.stderr
        assert count_file.read_text(encoding="utf-8").splitlines() == ["test ./pkg", "test ./pkg", "test ./system"]
    finally:
        solve_swe_prod.RUNTIME_ROOT = original_runtime_root

captured_worker_commands = []
try:
    def fake_worker_run(args, **_kwargs):
        captured_worker_commands.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    solve_swe_prod.run = fake_worker_run
    worker_name = solve_swe_prod.spawn_adapter_helper_worker(
        root,
        root,
        {},
        "The API should preserve explicit output ordering when parsing repeated flags.",
        "diff --git a/src/service.py b/src/service.py\n+def fixed():\n+    return True\n",
        ["progress watchdog adapter-selected public validation failed; inspect /tmp/multiagent-prod-swe/helper-validation-probe.txt"],
        ["src/service.py"],
        1,
        "adapter public validation probe failed",
        launch_reason="the production-native progress watchdog",
    )
finally:
    solve_swe_prod.run = original_run
assert worker_name == "worker-adapter-helper-01", worker_name
assignment_commands = [args for args in captured_worker_commands if "assignment-create" in args]
assert assignment_commands, captured_worker_commands
assert "--role" in assignment_commands[-1], assignment_commands[-1]
role_index = assignment_commands[-1].index("--role")
assert assignment_commands[-1][role_index + 1] == "exploitation", assignment_commands[-1]
spawn_commands = [args for args in captured_worker_commands if "spawn" in args]
assert spawn_commands, captured_worker_commands
spawn_instruction = spawn_commands[-1][-1]
assert "production-native progress watchdog" in spawn_instruction, spawn_instruction
assert "src/service.py" in spawn_instruction, spawn_instruction
for forbidden in ("FAIL_TO_PASS", "PASS_TO_PASS", "test_patch", "selected_test_files_to_run"):
    assert forbidden not in spawn_instruction, spawn_instruction

with tempfile.TemporaryDirectory() as td:
    repo = Path(td) / "repo"
    repo.mkdir()
    (repo / "internal/server/evaluation").mkdir(parents=True)
    (repo / "internal/server/ofrep").mkdir(parents=True)
    (repo / "internal/server/evaluation/ofrep_bridge.go").write_text("package evaluation\n", encoding="utf-8")
    (repo / "internal/server/ofrep/evaluation.go").write_text("package ofrep\n", encoding="utf-8")
    blockers = [
        "required-path-outside-owned: internal/server/evaluation/ofrep_bridge.go because it is the production bridge implementation",
        "prior owned path internal/server/ofrep/evaluation.go contains the call site",
    ]
    hints = solve_swe_prod.helper_scope_hints(repo, "OFREP bulk evaluation should list namespace flags.", "", blockers)
    assert "internal/server/evaluation/ofrep_bridge.go" in hints, hints
    assert "internal/server/ofrep/evaluation.go" in hints, hints

captured_worker_commands = []
try:
    solve_swe_prod.run = fake_worker_run
    worker_name = solve_swe_prod.spawn_adapter_helper_worker(
        root,
        root,
        {},
        "OFREP bulk evaluation should list namespace flags.",
        "",
        ["required-path-outside-owned: evaluation/native_solver/solve_swe_prod.py because it owns the wrapper handoff"],
        [],
        2,
        "",
        launch_reason="ownership blocker regression",
    )
finally:
    solve_swe_prod.run = original_run
assert worker_name == "worker-adapter-helper-02", worker_name
assignment_commands = [args for args in captured_worker_commands if "assignment-create" in args]
assert assignment_commands, captured_worker_commands
owned_index = assignment_commands[-1].index("--owned")
assert assignment_commands[-1][owned_index + 1] == "evaluation/native_solver/solve_swe_prod.py", assignment_commands[-1]

solver_source = (root / "evaluation/native_solver/solve_swe_prod.py").read_text(encoding="utf-8")
assert 'adapter_helper_repair_allowed("progress watchdog stale diff")' in solver_source, (
    "progress watchdog must not spawn source-editing adapter helpers by default"
)
assert "launch_production_session" in solver_source and "resume=True" in solver_source and "--resume" in solver_source, (
    "unverified diffs should be recoverable by relaunching the production orchestrator"
)
assert "EVAL_TERMINAL_DEADLINE_REMAINING" in solver_source and "EVAL_TERMINAL_DEADLINE_GRACE" in solver_source, (
    "active native runs need a terminal deadline checkpoint before timeout"
)
assert "EVAL_TERMINAL_FORCE_RESUME" in solver_source and "force_live_handoff=True" in solver_source, (
    "active no-status terminal deadlines should hand off once to the production orchestrator before outer timeout"
)
assert "verifier_exact_followup_available" in solver_source and "Verifier exact-follow-up handoff" in solver_source, (
    "verifier findings with exact public follow-up instructions should get one production repair handoff"
)
assert "EVAL_SOURCE_SYMBOL_RESUME_LIMIT" in solver_source and "source_symbol_map_resume_instructions" in solver_source, (
    "source-symbol blockers should get one bounded production-orchestrator recovery handoff with exact status marker instructions"
)
assert "stale_patch_application_blockers" in solver_source and "could not find hunk context" in solver_source, (
    "stale patch application failures should be machine-gated before acceptance"
)
assert "blocked_status_needs_diff_reconciliation" in solver_source and "blocked-status diff reconciliation resume launched" in solver_source, (
    "blocked stale-claim/stale-patch statuses with live source diffs should get one production resume before terminal rejection"
)
assert "EVAL_NO_DIFF_BLOCKED_RETRY_LIMIT" in solver_source and "blocked with no materialized source diff" in solver_source, (
    "blocked no-diff worker outcomes should get one production-orchestrator retry"
)
assert "post-cleanup final gate rejected stale validation evidence" in solver_source and "benchmark cleanup changed the final submitted diff after verifier acceptance" in solver_source, (
    "cleanup must not change the submitted diff after verifier hash-bound acceptance without forcing reverification"
)
multi_value_section = re.search(
    r"parser_multi_value_diff = any\(\s*marker in diff_lower\s*for marker in \((?P<markers>.*?)\)\s*\)",
    solver_source,
    flags=re.S,
)
assert multi_value_section, "multi-value guardrail marker list missing"
quoted_markers = re.findall(r'"([^"]+)"', multi_value_section.group("markers"))
field_shaped_markers = [
    marker for marker in quoted_markers
    if re.fullmatch(r"[a-z]+(?:_[a-z]+)+", marker)
]
assert not field_shaped_markers, field_shaped_markers

with tempfile.TemporaryDirectory() as td:
    work_dir = Path(td) / "work"
    report_dir = work_dir / "reports" / "codex-scaffold-parity"
    log_dir = work_dir / "logs"
    report_dir.mkdir(parents=True)
    log_dir.mkdir(parents=True)
    report_path = report_dir / "swe_bench_pro.json"
    report_path.write_text('{"score": 1.0, "num": 1}\n', encoding="utf-8")
    (log_dir / "eval_log.log").write_text(
        "2026-07-11 12:15:01 - evalscope - INFO: multiagent-native exited: sample=0 rc=2 wall=2074.8s timed_out=False\n"
        "2026-07-11 12:15:01 - evalscope - WARNING: multiagent-native exited with code 2; scoring current git diff by explicit config\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        work_dir=work_dir,
        limit=1,
        on_demand_image_preload=True,
        sample_count=None,
        sample_offset=0,
        output=Path(td) / "summary.json",
        config_json=Path(td) / "config.json",
        config_yaml=Path(td) / "config.yaml",
        preflight_output=Path(td) / "preflight.json",
        swe_bench_pro_repo_path=Path("/tmp/swe"),
        dockerhub_username="jefzda",
        platform="linux/amd64",
        command_timeout=60.0,
        agent_timeout=3600.0,
        eval_timeout=3600,
        no_auto_install=False,
        agent_model_name="gpt-5",
        agent_working_dir="/app",
        on_demand_prune_after_sample=False,
        on_demand_image_status=Path(td) / "image-status.json",
        persistent_cache=False,
        persistent_cache_root=Path("/tmp/cache"),
        persistent_cache_mode="rw",
        bake_native_solver=True,
        native_solver_source=root,
        native_codex_auth_json="",
        native_codex_auth_container_home="/root/.codex-multiagent-prod",
        score_failed_native_diff=True,
        score_timed_out_native_diff=False,
    )
    config = {
        "agent_config": {"mode": "external", "framework": "multiagent-native"},
        "dataset_args": {
            "swe_bench_pro": {
                "extra_params": {"command_timeout": 60, "eval_timeout": 3600}
            }
        },
    }
    payload = swe_bench_pro_scaffold_parity.summarize_result(
        args=args,
        config=config,
        run_result={"status": "completed"},
        evalscope_report_path=report_path,
        preflight={"official_scaffold_ready": True, "official_image_set_ready": False},
        started_at=swe_bench_pro_scaffold_parity.dt.datetime.now(swe_bench_pro_scaffold_parity.dt.UTC),
        completed_at=swe_bench_pro_scaffold_parity.dt.datetime.now(swe_bench_pro_scaffold_parity.dt.UTC),
        status="completed",
    )
    assert payload["score"] == 1.0, json.dumps(payload, indent=2)
    assert payload["clean_native_score"] is None, json.dumps(payload, indent=2)
    assert payload["diagnostic_score"] == 1.0, json.dumps(payload, indent=2)
    assert payload["native_runner"]["latest"]["returncode"] == 2, payload["native_runner"]
    assert payload["native_runner"]["diagnostic_scored_diff"], payload["native_runner"]

public_metadata = evalscope_multiagent_native_runner._public_solver_metadata(
    {
        "sample_id": 7,
        "id": "row-7",
        "task_id": "task-7",
        "repo": "example/repo",
        "language": "python",
        "problem_statement": "hidden prompt copy",
        "FAIL_TO_PASS": ["TestHidden"],
        "test_patch": "diff --git a/tests/hidden_test.py b/tests/hidden_test.py",
        "swe_bench_pro": {
            "instance_id": "instance-7",
            "fail_to_pass": ["TestNestedHidden"],
            "selected_test_files_to_run": ["tests/hidden_test.py"],
            "requirements": "private evaluator contract",
        },
    }
)
assert public_metadata == {"language": "python"}, public_metadata
solver_metadata = solve_swe_prod.public_solver_metadata(
    {
        "sample_id": 7,
        "id": "row-7",
        "task_id": "task-7",
        "repo": "example/repo",
        "language": "python",
        "problem_statement": "hidden prompt copy",
        "requirements": "private requirements copy",
        "interface": "private interface copy",
        "FAIL_TO_PASS": ["TestHidden"],
        "test_patch": "diff --git a/tests/hidden_test.py b/tests/hidden_test.py",
        "swe_bench_pro": {
            "instance_id": "instance-7",
            "fail_to_pass": ["TestNestedHidden"],
            "selected_test_files_to_run": ["tests/hidden_test.py"],
            "requirements": "private evaluator contract",
        },
    }
)
assert solver_metadata == {"language": "python"}, solver_metadata
raw_private_contract = solve_swe_prod.official_test_contract(
    {
        "sample_id": 7,
        "instance_id": "instance-7",
        "language": "python",
        "FAIL_TO_PASS": ["TestHidden"],
        "selected_test_files_to_run": ["tests/hidden_test.py"],
        "swe_bench_pro": {
            "instance_id": "nested-instance-7",
            "fail_to_pass": ["TestNestedHidden"],
            "selected_test_files_to_run": ["tests/nested_hidden_test.py"],
        },
    }
)
assert raw_private_contract == {
    "instance_id": None,
    "fail_to_pass": [],
    "pass_to_pass": [],
    "selected_test_files_to_run": [],
    "expected_test_count": 0,
}, raw_private_contract
symbols_from_raw_metadata = solve_swe_prod.required_public_symbols(
    "Function Name: VisibleThing",
    {
        "requirements": "Function Name: LeakedThing",
        "swe_bench_pro": {"requirements": "Function Name: NestedLeakedThing"},
    },
)
assert symbols_from_raw_metadata == ["VisibleThing"], symbols_from_raw_metadata
ledger = solve_swe_prod.contract_ledger_text(
    "visible issue text",
    {
        "sample_id": 7,
        "id": "row-7",
        "task_id": "task-7",
        "repo": "example/repo",
        "language": "python",
        "problem_statement": "hidden prompt copy",
        "requirements": "private requirements copy",
        "interface": "private interface copy",
        "FAIL_TO_PASS": ["TestHidden"],
        "test_patch": "diff --git a/tests/hidden_test.py b/tests/hidden_test.py",
        "swe_bench_pro": {
            "instance_id": "instance-7",
            "fail_to_pass": ["TestNestedHidden"],
            "selected_test_files_to_run": ["tests/hidden_test.py"],
            "requirements": "private evaluator contract",
        },
    },
)
assert "public solver inputs" in ledger, ledger
assert "full official contract" not in ledger, ledger
assert "Official requirements/interface excerpt" not in ledger, ledger
for forbidden in (
    "sample_id",
    "row-7",
    "task-7",
    "example/repo",
    "instance-7",
    "hidden prompt copy",
    "private requirements copy",
    "private interface copy",
    "TestHidden",
    "TestNestedHidden",
    "hidden_test.py",
    "private evaluator contract",
):
    assert forbidden not in ledger, forbidden

for excluded in (
    "tests/run.sh",
    "evaluation/README.md",
    "evaluation/reports/prior-run.json",
    "evaluation/runs/prior-run/results.json",
    "evaluation/swe_bench_pro_scaffold_parity.py",
    "README.md",
    "docs/write-policy.paths",
    "permission-investigation.md",
):
    assert OnDemandImageManager._skip_repo_bake_path(Path(excluded)), excluded
for included in (
    "launch.sh",
    "orchestrator_prompt.md",
    "bin/subagent.sh",
    "prompts/verifier.md",
    "evaluation",
    "evaluation/native_solver",
    "evaluation/native_solver/solve_swe_prod.py",
    "evaluation/native_solver/swe_prod_guardrails.py",
    "evaluation/native_solver/templates/swe_autonomous_appendix.md",
):
    assert not OnDemandImageManager._skip_repo_bake_path(Path(included)), included

with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "requirements.txt").write_text("PyYAML==5.4.1\n")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 1}\n')
    (repo / "internal" / "server" / "evaluation").mkdir(parents=True)
    (repo / "internal" / "server" / "evaluation" / "evaluation_store_mock.go").write_text(
        "package evaluation\n\nfunc OldMock() {}\n"
    )
    (repo / "source.py").write_text("old = True\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    start = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    (repo / "requirements.txt").write_text("PyYAML>=6.0,<7\n")
    (repo / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (repo / "source.py").write_text("old = False\n")
    restored = solve_swe_prod.cleanup_initial_environment_diff(repo, start)

    assert set(restored) == {"requirements.txt", "package-lock.json"}, restored
    changed = subprocess.check_output(["git", "diff", "--name-only"], cwd=repo, text=True).splitlines()
    assert changed == ["source.py"], changed

    (repo / ".gomodcache" / "example.com" / "dep").mkdir(parents=True)
    (repo / ".gomodcache" / "example.com" / "dep" / "dep.go").write_text("package dep\n")
    (repo / "internal" / "server" / "evaluation" / "evaluation_store_mock.go").write_text(
        "package evaluation\n\nfunc NewMock() {}\n"
    )
    (repo / "new_source.py").write_text("value = 1\n")
    intent = solve_swe_prod.mark_untracked_source_intent_to_add(repo)
    assert "new_source.py" in intent, intent
    assert ".gomodcache/example.com/dep/dep.go" not in intent, intent
    removed = solve_swe_prod.cleanup_patch(repo, start)
    assert not (repo / ".gomodcache").exists(), "tool cache directory should be removed"
    assert removed == [], removed
    source_mock = (repo / "internal" / "server" / "evaluation" / "evaluation_store_mock.go").read_text()
    assert "NewMock" in source_mock, "source mock files are compiled Go sources and must not be restored by cleanup"

assert not solve_swe_prod.benchmark_specific_recovery_enabled(
    "Configuration loading should return a structured result with warnings for deprecated options.",
    ["Go source changed, but status.json does not record a Go package validation command"],
    "diff --git a/internal/config/database.go b/internal/config/database.go\n",
)
assert not solve_swe_prod.benchmark_specific_recovery_enabled(
    "The service should support separate database credential keys.",
    ["missing database.protocol error"],
    "diff --git a/internal/config/database.go b/internal/config/database.go\n",
), "row-specific adapter repair should stay disabled in no-leak production eval"
metadata = {
    "swe_bench_pro": {
        "instance_id": "synthetic_instance",
        "fail_to_pass": ["TestConfigLoad", "TestSchemaValidation"],
        "pass_to_pass": [],
        "selected_test_files_to_run": ["internal/config/config_test.go"],
    }
}
row56_status = {
    "status": "completed",
    "validation": (
        "official-expected-tests: FAIL_TO_PASS source-inspected TestSchemaValidation passed locally; "
        "TestConfigLoad source-inspected and visible failure is old-return-shape mismatch while official contract requires Result. "
        "official-test-source-inspected: internal/config/config_test.go"
    ),
}
assert not solve_swe_prod.official_expected_test_blockers(metadata, row56_status), "expected-test guidance should be off by default"
blockers = solve_swe_prod.official_expected_test_blockers(metadata, row56_status)
assert blockers == [], "official expected-test metadata must not gate no-leak production mode"
absent_patch_status = {
    "status": "completed",
    "validation": (
        "official-expected-tests: FAIL_TO_PASS source-inspected because the official test patch is not present locally; "
        "official-test-source-inspected: internal/config/config_test.go public function Load and Result symbols preserved"
    ),
}
assert not solve_swe_prod.official_expected_test_blockers(metadata, absent_patch_status), solve_swe_prod.official_expected_test_blockers(metadata, absent_patch_status)
with tempfile.TemporaryDirectory() as td:
    runtime = Path(td) / "runtime"
    runtime.mkdir()
    original_runtime = solve_swe_prod.RUNTIME_ROOT
    original_workdir = solve_swe_prod.DEFAULT_WORKDIR
    original_multiagent_root = solve_swe_prod.DEFAULT_MULTIAGENT_ROOT
    solve_swe_prod.RUNTIME_ROOT = runtime
    solve_swe_prod.DEFAULT_WORKDIR = Path(td) / "app"
    solve_swe_prod.DEFAULT_WORKDIR.mkdir()
    solve_swe_prod.DEFAULT_MULTIAGENT_ROOT = root
    try:
        subprocess.run(
            [
                str(root / "bin/subagent.sh"),
                "finding-create",
                "F-OPEN",
                "--severity",
                "blocking",
                "--type",
                "compile_failure",
                "--summary",
                "compile failed",
                "--evidence-json",
                '{"cmd":"go test ./pkg","rc":1}',
                "--required-resolution",
                "go test ./pkg returns 0",
                "--affected",
                "pkg",
            ],
            env={**os.environ, "MULTIAGENT_STATE_DIR": str(runtime), "MULTIAGENT_ROOT": str(solve_swe_prod.DEFAULT_WORKDIR)},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                str(root / "bin/subagent.sh"),
                "todo-create",
                "T-OPEN",
                "--source-finding-id",
                "F-OPEN",
                "--task",
                "fix compile",
                "--done-criteria",
                "go test ./pkg returns 0",
                "--required-command",
                "go test ./pkg",
            ],
            env={**os.environ, "MULTIAGENT_STATE_DIR": str(runtime), "MULTIAGENT_ROOT": str(solve_swe_prod.DEFAULT_WORKDIR)},
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                str(root / "bin/subagent.sh"),
                "resolution-create",
                "T-OPEN",
                "--worker",
                "worker-01",
                "--status",
                "resolved",
                "--validation-json",
                '[{"cmd":"go test ./pkg","rc":0}]',
                "--why",
                "compiled",
            ],
            env={**os.environ, "MULTIAGENT_STATE_DIR": str(runtime), "MULTIAGENT_ROOT": str(solve_swe_prod.DEFAULT_WORKDIR)},
            check=True,
            capture_output=True,
            text=True,
        )
        gate_blockers = solve_swe_prod.structured_repair_gate_blockers()
        assert gate_blockers and "status=resolved" in gate_blockers[0], gate_blockers
    finally:
        solve_swe_prod.RUNTIME_ROOT = original_runtime
        solve_swe_prod.DEFAULT_WORKDIR = original_workdir
        solve_swe_prod.DEFAULT_MULTIAGENT_ROOT = original_multiagent_root
with tempfile.TemporaryDirectory() as td:
    runtime = Path(td)
    original_runtime = solve_swe_prod.RUNTIME_ROOT
    original_status = solve_swe_prod.STATUS_PATH
    original_probe_path = solve_swe_prod.HELPER_PROBE_PATH
    old_probe_commands = solve_swe_prod.coverage_probe_commands
    try:
        solve_swe_prod.RUNTIME_ROOT = runtime
        solve_swe_prod.STATUS_PATH = runtime / "status.json"
        solve_swe_prod.HELPER_PROBE_PATH = runtime / "helper-validation-probe.txt"
        diff = "diff --git a/pkg/service.go b/pkg/service.go\n+func Service() {}\n"
        diff_hash = solve_swe_prod.final_diff_sha256(diff)
        solve_swe_prod.STATUS_PATH.write_text(
            json.dumps(
                {
                    "status": "completed",
                    "validation": (
                        "build-verification-passed: "
                        f"final-diff-sha256={diff_hash} compile_clean=true returncode=0"
                    ),
                }
            ),
            encoding="utf-8",
        )
        solve_swe_prod.coverage_probe_commands = lambda *_args: [["bash", "-lc", "exit 42"]]
        report, passed = solve_swe_prod.run_validation_coverage_probe(
            Path(td),
            "Service should work.",
            diff,
            ["stale pre-status blocker"],
        )
        assert passed, report
        assert "status.json already records completed final-diff build verification" in report, report
    finally:
        solve_swe_prod.RUNTIME_ROOT = original_runtime
        solve_swe_prod.STATUS_PATH = original_status
        solve_swe_prod.HELPER_PROBE_PATH = original_probe_path
        solve_swe_prod.coverage_probe_commands = old_probe_commands
generic_commands = solve_swe_prod.coverage_probe_commands(
    Path("/tmp"),
    "A text parser should decode escaped strings.",
    "diff --git a/lib/parsers/text_parser.py b/lib/parsers/text_parser.py\n+def _parse_text(data):\n+    pass\n",
)
assert generic_commands == [], generic_commands
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "records/decoder/tests").mkdir(parents=True)
    (repo / "records/decoder/tests/test_decode.py").write_text("def test_decode(): pass\n", encoding="utf-8")
    python_commands = solve_swe_prod.coverage_probe_commands(
        repo,
        "Record parser should preserve alternate linked fields.",
        "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n+def read_title(rec):\n+    pass\n",
    )
    assert ["python", "-m", "pytest", "records/decoder/tests/test_decode.py", "-q", "--tb=short"] in python_commands, python_commands
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "components/scanner/pkg").mkdir(parents=True)
    (repo / "components/scanner/parser/v2").mkdir(parents=True)
    (repo / "components/scanner/parser/v2/parser_test.go").write_text("package v2\n", encoding="utf-8")
    go_commands = solve_swe_prod.coverage_probe_commands(
        repo,
        "Converter output should keep duplicate vulnerability records in parser fixtures.",
        "diff --git a/components/scanner/pkg/converter.go b/components/scanner/pkg/converter.go\n+func Convert() {}\n",
    )
    assert ["go", "test", "./components/scanner/pkg"] in go_commands, go_commands
    assert ["go", "test", "./components/scanner/..."] in go_commands, go_commands
with tempfile.TemporaryDirectory() as td:
    repo = Path(td)
    (repo / "lib/service").mkdir(parents=True)
    (repo / "lib/kube/proxy").mkdir(parents=True)
    (repo / "lib/kube/proxy/forwarder_test.go").write_text("package proxy\n", encoding="utf-8")
    go_related_commands = solve_swe_prod.coverage_probe_commands(
        repo,
        "Kubernetes service startup should initialize credentials used by proxy forwarding.",
        "diff --git a/lib/service/kubernetes.go b/lib/service/kubernetes.go\n+func initKubernetesService() {}\n",
    )
    assert ["go", "test", "./lib/service"] in go_related_commands, go_related_commands
    assert ["go", "test", "./lib/kube/..."] in go_related_commands, go_related_commands

false_helper_blockers = solve_swe_prod.implementation_scope_blockers(
    "`Panel` `Submit` flow fails when independent `app` files use API scripts and a keyboard key command result in the working directory.",
    "diff --git a/src/controller.js b/src/controller.js\n+db.getObjectField('x', 'y')\n",
    {"status": "completed", "validation": "visible source check passed"},
)
assert not any("helper/interface" in blocker for blocker in false_helper_blockers), false_helper_blockers
assert not any("helper-layer validation" in blocker for blocker in false_helper_blockers), false_helper_blockers

real_helper_blockers = solve_swe_prod.implementation_scope_blockers(
    "The helper `load_config_value` must preserve config fallback behavior.",
    "diff --git a/src/config.js b/src/config.js\n+async function loadConfigValue() { return await db.get('config:key'); }\n",
    {"status": "completed", "validation": "visible source check passed"},
)
assert any("load_config_value" in blocker for blocker in real_helper_blockers), real_helper_blockers
assert any("helper-layer validation" in blocker for blocker in real_helper_blockers), real_helper_blockers
prompt_only_helper_evidence = solve_swe_prod.helper_preservation_evidence(
    "Bulk evaluation should preserve `context.flags` behavior.",
    "Task: preserve `context.flags` behavior before completing the fix.",
)
assert not prompt_only_helper_evidence, prompt_only_helper_evidence
accepted_helper_evidence = solve_swe_prod.helper_preservation_evidence(
    "Bulk evaluation should preserve `context.flags` behavior.",
    "ACCEPTED\n- No blocking findings.\n- Explicit `context.flags` behavior is preserved after source inspection.",
)
assert "context.flags" in accepted_helper_evidence, accepted_helper_evidence
context_flags_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should preserve `context.flags` behavior.",
    "diff --git a/internal/server/ofrep/evaluation.go b/internal/server/ofrep/evaluation.go\n"
    "+if flagKeys, ok := evalContext[\"flags\"]; ok {\n"
    "+    return strings.Split(flagKeys, \",\"), nil\n"
    "+}\n",
    {
        "status": "completed",
        "validation": (
            "go test ./internal/server/ofrep ./internal/server/evaluation passed. "
            "helper-validation-passed: adapter public helper probe. "
            "helper-contract-preserved: context.flags"
        ),
    },
)
assert not any("context.flags" in blocker for blocker in context_flags_blockers), context_flags_blockers
recovered_context_flags_status = solve_swe_prod.status_with_recovered_public_evidence(
    {},
    "helper-validation-passed: adapter public helper probe",
    "Bulk evaluation should preserve `context.flags` behavior.",
    "ACCEPTED\n- No blocking findings.\n- Explicit `context.flags` behavior is preserved after source inspection.",
)
assert "helper-contract-preserved: context.flags" in recovered_context_flags_status["validation"], recovered_context_flags_status
assert solve_swe_prod.blocked_status_recoverable_by_public_probe(
    {
        "status": "blocked",
        "blockers": [
            "Go source changed, but status.json does not record a Go package validation command such as `go test ./affected/package`"
        ],
    }
)
assert solve_swe_prod.blocked_status_recoverable_by_public_probe(
    {
        "status": "blocked",
        "reason": (
            "Required worker agents completed without inspecting or modifying /app, "
            "leaving an empty git diff."
        ),
    }
)
assert not solve_swe_prod.blocked_status_recoverable_by_public_probe(
    {"status": "blocked", "blockers": ["[official-hard] public API contract missing"]}
)
go_two_pkg_diff = (
    "diff --git a/lib/a/foo.go b/lib/a/foo.go\n+func Foo() {}\n"
    "diff --git a/lib/b/bar.go b/lib/b/bar.go\n+func Bar() {}\n"
)
go_two_pkg_hash = solve_swe_prod.final_diff_sha256(go_two_pkg_diff)
go_missing_build_blockers = solve_swe_prod.validation_coverage_blockers(
    "Go packages should compile after changing request handling.",
    go_two_pkg_diff,
    "",
    {
        "status": "completed",
        "validation": (
            "go-package-validation-passed: package=./lib/a command='go test ./lib/a' returncode=0. "
            "go-package-validation-passed: package=./lib/b command='go test ./lib/b' returncode=0."
        ),
    },
)
assert any("hash-bound build verification" in blocker for blocker in go_missing_build_blockers), go_missing_build_blockers
go_wrong_hash_blockers = solve_swe_prod.validation_coverage_blockers(
    "Go packages should compile after changing request handling.",
    go_two_pkg_diff,
    "",
    {
        "status": "completed",
        "validation": (
            "build-verification-passed: final-diff-sha256=deadbeef changed-files=2 compile_clean=true returncode=0. "
            "go-package-validation-passed: package=./lib/a command='go test ./lib/a' returncode=0. "
            "go-package-validation-passed: package=./lib/b command='go test ./lib/b' returncode=0."
        ),
    },
)
assert any("hash-bound build verification" in blocker for blocker in go_wrong_hash_blockers), go_wrong_hash_blockers
go_partial_pkg_blockers = solve_swe_prod.validation_coverage_blockers(
    "Go packages should compile after changing request handling.",
    go_two_pkg_diff,
    "",
    {
        "status": "completed",
        "validation": (
            f"build-verification-passed: final-diff-sha256={go_two_pkg_hash} changed-files=2 compile_clean=true returncode=0. "
            "go-package-validation-passed: package=./lib/a command='go test ./lib/a' returncode=0"
        ),
    },
)
assert any("./lib/b" in blocker for blocker in go_partial_pkg_blockers), go_partial_pkg_blockers
go_all_pkg_blockers = solve_swe_prod.validation_coverage_blockers(
    "Go packages should compile after changing request handling.",
    go_two_pkg_diff,
    "",
    {
        "status": "completed",
        "validation": (
            f"build-verification-passed: final-diff-sha256={go_two_pkg_hash} changed-files=2 compile_clean=true returncode=0. "
            "go-package-validation-passed: package=./lib/a command='go test ./lib/a' returncode=0. "
            "go-package-validation-passed: package=./lib/b command='go test ./lib/b' returncode=0."
        ),
    },
)
assert not any("affected package compile/test success" in blocker for blocker in go_all_pkg_blockers), go_all_pkg_blockers
go_compile_failure_blockers = solve_swe_prod.validation_coverage_blockers(
    "Go package should compile after storage request changes.",
    "diff --git a/internal/store/list.go b/internal/store/list.go\n+func List() { _ = req.Request }\n",
    "",
    {
        "status": "completed",
        "validation": (
            "Command: go test ./internal/store\nReturn code: 1\n"
            "Output tail: req.Request undefined (type *storage.ListRequest has no field or method Request)\nFAIL"
        ),
    },
)
assert any("compile/build failure evidence" in blocker for blocker in go_compile_failure_blockers), go_compile_failure_blockers

with tempfile.TemporaryDirectory() as td:
    postmortem_root = Path(td)
    (postmortem_root / "logs").mkdir(parents=True)
    (postmortem_root / "logs" / "eval_log.log").write_text(
        "official verifier: undefined: req.Request\nFAIL pkg [build failed]\n",
        encoding="utf-8",
    )
    compile_postmortem = swe_bench_pro_scaffold_parity.failure_postmortem(
        work_dir=postmortem_root,
        run_result={"status": "completed"},
        evalscope_report={"score": 0.0},
        score=0.0,
        native_summary={"clean_native_completion": True},
    )
    assert compile_postmortem and compile_postmortem["category"] == "official_compile_failure", compile_postmortem

    (postmortem_root / "logs" / "eval_log.log").write_text(
        "multiagent-native exited with code 2; refusing to score rejected git diff: "
        "final patch changes code, but submission lacks hash-bound build verification\n",
        encoding="utf-8",
    )
    gate_postmortem = swe_bench_pro_scaffold_parity.failure_postmortem(
        work_dir=postmortem_root,
        run_result={"status": "completed"},
        evalscope_report=None,
        score=None,
        native_summary={"clean_native_completion": False},
    )
    assert gate_postmortem and gate_postmortem["category"] == "native_submission_gate_rejection", gate_postmortem
    timeout_postmortem = swe_bench_pro_scaffold_parity.failure_postmortem(
        work_dir=postmortem_root,
        run_result={"status": "completed"},
        evalscope_report=None,
        score=None,
        native_summary={"clean_native_completion": False, "latest": {"returncode": 124}},
    )
    assert timeout_postmortem and timeout_postmortem["category"] == "native_timeout_without_submission", timeout_postmortem

stale_without_probe_blockers = solve_swe_prod.implementation_scope_blockers(
    "Normalize duplicate serialized vulnerability content into one source record.",
    "diff --git a/converter.go b/converter.go\n+func Convert() {}\n",
    {"status": "completed", "validation": "1 failed because visible fixture still expects duplicate old shape"},
)
assert any("replacement-probe-passed:" in blocker for blocker in stale_without_probe_blockers), stale_without_probe_blockers
stale_with_probe_blockers = solve_swe_prod.implementation_scope_blockers(
    "Normalize duplicate serialized vulnerability content into one source record.",
    "diff --git a/converter.go b/converter.go\n+func Convert() {}\n",
    {
        "status": "completed",
        "validation": (
            "visible parser/v2 fixture failed because it asserts the old duplicate object shape. "
            "replacement-probe-passed: temporary converter probe returned one source record with merged severity. "
            "stale-visible-failure-justified: issue/source contract requires one cveContents entry per source key."
        ),
    },
)
assert not any("failing evidence" in blocker for blocker in stale_with_probe_blockers), stale_with_probe_blockers
stale_claim_without_failed_word_blockers = solve_swe_prod.implementation_scope_blockers(
    "Parser output should preserve alternate linked fields.",
    "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n+def decode_record() {}\n",
    {"status": "completed", "risk": "visible fixture expectations are stale relative to the issue requirement"},
)
assert any("visible test/fixture expectation is stale" in blocker for blocker in stale_claim_without_failed_word_blockers), stale_claim_without_failed_word_blockers
stale_claim_with_probe_markers = solve_swe_prod.implementation_scope_blockers(
    "Parser output should preserve alternate linked fields.",
    "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n+def decode_record() {}\n",
    {
        "status": "completed",
        "risk": (
            "visible fixture expectations are stale relative to the issue requirement. "
            "replacement-probe-passed: temporary parser probe covered the exact alternate field path. "
            "stale-visible-failure-justified: issue-visible source requires alternate fields to remain linked."
        ),
    },
)
assert not any("visible test/fixture expectation is stale" in blocker for blocker in stale_claim_with_probe_markers), stale_claim_with_probe_markers
compile_error_blockers = solve_swe_prod.implementation_scope_blockers(
    "Normalize duplicate serialized vulnerability content into one source record.",
    "diff --git a/converter.go b/converter.go\n+func Convert() {}\n",
    {
        "status": "completed",
        "validation": (
            "compile error: undefined: Convert. replacement-probe-passed: not relevant. "
            "stale-visible-failure-justified: not relevant."
        ),
    },
)
assert any("compile-error evidence" in blocker for blocker in compile_error_blockers), compile_error_blockers
declared_type_compile_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when the request omits an explicit flag list.",
    "diff --git a/internal/server/evaluation/ofrep_bridge.go b/internal/server/evaluation/ofrep_bridge.go\n+func (s *Server) OFREPListFlags(ctx context.Context, namespace string) ([]string, error) { return s.store.ListFlags(ctx, nil) }\n",
    {
        "status": "completed",
        "validation": (
            "go test ./internal/server/evaluation failed: "
            "s.store.ListFlags undefined (type Storer has no field or method ListFlags)"
        ),
    },
)
assert any("compile-error evidence" in blocker for blocker in declared_type_compile_blockers), declared_type_compile_blockers
validation_repair_needed_blockers = solve_swe_prod.implementation_scope_blockers(
    "Parser output should preserve author contribution shape.",
    "diff --git a/openlibrary/catalog/marc/parse.py b/openlibrary/catalog/marc/parse.py\n+def read_authors(record):\n+    return []\n",
    {
        "status": "completed",
        "validation": (
            "validation-repair-needed: pytest -q openlibrary/catalog/marc/tests/test_parse.py failed. "
            "Implicated source path: openlibrary/catalog/marc/parse.py"
        ),
    },
)
assert any("requires a repair worker" in blocker for blocker in validation_repair_needed_blockers), validation_repair_needed_blockers
nonzero_validation_blockers = solve_swe_prod.implementation_scope_blockers(
    "Parser output should preserve author contribution shape.",
    "diff --git a/openlibrary/catalog/marc/parse.py b/openlibrary/catalog/marc/parse.py\n+def read_authors(record):\n+    return []\n",
    {
        "status": "completed",
        "validation": (
            "Command: pytest -q openlibrary/catalog/marc/tests/test_parse.py::TestParseMARCBinary::test_binary\n"
            "Return code: 1\n"
            "Output tail: assertion mismatch"
        ),
    },
)
assert any("nonzero focused validation return code" in blocker for blocker in nonzero_validation_blockers), nonzero_validation_blockers
source_symbol_map_blockers = solve_swe_prod.implementation_scope_blockers(
    "Add a linear benchmark generator for benchmark tests.",
    "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
    "+type LinearBenchmark struct { Step int }\n"
    "+func NewLinearBenchmarkGenerator() {}\n",
    {
        "status": "completed",
        "validation": "go test ./lib/client passed",
    },
)
assert any("source-symbol-map-passed:" in blocker for blocker in source_symbol_map_blockers), source_symbol_map_blockers
assert solve_swe_prod.source_symbol_map_blocker_present(source_symbol_map_blockers), source_symbol_map_blockers
assert "source-symbol-map-passed:" in solve_swe_prod.source_symbol_map_resume_instructions(source_symbol_map_blockers)
source_symbol_map_evidence_blockers = solve_swe_prod.implementation_scope_blockers(
    "Add a linear benchmark generator for benchmark tests.",
    "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
    "+type LinearBenchmark struct { Step int }\n"
    "+func NewLinearBenchmarkGenerator() {}\n",
    {
        "status": "completed",
        "validation": (
            "go test ./lib/client passed. "
            "source-symbol-map-passed: path=lib/client/bench.go package=client "
            "added-symbol=LinearBenchmark added-symbol=NewLinearBenchmarkGenerator "
            "nearby-test=go test ./lib/client compile=go test ./lib/client caller=lib/client"
        ),
    },
)
assert any("source-symbol-map-passed:" in blocker for blocker in source_symbol_map_evidence_blockers), source_symbol_map_evidence_blockers
source_symbol_map_owner_evidence_blockers = solve_swe_prod.implementation_scope_blockers(
    "Add a linear benchmark generator for benchmark tests.",
    "diff --git a/lib/benchmark/linear.go b/lib/benchmark/linear.go\n"
    "+type Linear struct { Step int }\n"
    "+func NewLinearGenerator() {}\n",
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=lib/benchmark candidate-owner=lib/benchmark "
            "rejected-owner=lib/client-not-benchmark-owner validation-package=./lib/benchmark. "
            "go test ./lib/benchmark passed. "
            "source-symbol-map-passed: path=lib/benchmark/linear.go package=benchmark "
            "added-symbol=Linear added-symbol=NewLinearGenerator "
            "owner-evidence=issue-term-benchmark-package "
            "nearby-test=go test ./lib/benchmark compile=go test ./lib/benchmark caller=lib/benchmark"
        ),
    },
)
assert not any("source-symbol-map-passed:" in blocker for blocker in source_symbol_map_owner_evidence_blockers), source_symbol_map_owner_evidence_blockers
assert not any("source-owner-ledger:" in blocker for blocker in source_symbol_map_owner_evidence_blockers), source_symbol_map_owner_evidence_blockers
assert not solve_swe_prod.source_symbol_map_blocker_present(source_symbol_map_owner_evidence_blockers), source_symbol_map_owner_evidence_blockers
dependency_contract_diff = (
    "diff --git a/internal/server/ofrep/evaluation.go b/internal/server/ofrep/evaluation.go\n"
    "+type flagLister interface { ListFlags(ctx context.Context, namespace string) ([]string, error) }\n"
    "+lister, ok := s.bridge.(flagLister)\n"
    "+keys, err := lister.ListFlags(ctx, namespaceKey)\n"
    "diff --git a/internal/server/evaluation/server.go b/internal/server/evaluation/server.go\n"
    "+type Storer interface { ListFlags(ctx context.Context, req *storage.ListRequest[storage.NamespaceRequest]) (storage.ResultSet[*flipt.Flag], error) }\n"
)
dependency_contract_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    dependency_contract_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation rejected-owner=evaluation-bridge-helper validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=flagLister owner-evidence=bulk-endpoint-owner candidate-owner=internal/server/evaluation "
            "callsite=EvaluateBulk compile=go-test-ofrep"
        ),
    },
)
assert any("constructor-dependency-checked:" in blocker for blocker in dependency_contract_blockers), dependency_contract_blockers
optional_provider_diff = (
    "diff --git a/internal/server/ofrep/evaluation.go b/internal/server/ofrep/evaluation.go\n"
    "+bridge, ok := s.bridge.(interface { OFREPFlagKeys(context.Context, string) ([]string, error) })\n"
    "+if !ok { return nil, newFlagsMissingError() }\n"
    "+return bridge.OFREPFlagKeys(ctx, namespaceKey)\n"
    "diff --git a/internal/server/evaluation/ofrep_bridge.go b/internal/server/evaluation/ofrep_bridge.go\n"
    "+store, ok := s.store.(interface { ListFlags(context.Context, *storage.ListRequest[storage.NamespaceRequest]) (storage.ResultSet[*flipt.Flag], error) })\n"
    "+if !ok { return nil, errors.New(\"ofrep bridge store does not support listing flags\") }\n"
    "+return store.ListFlags(ctx, req)\n"
)
optional_provider_missing_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    optional_provider_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=bulkEvaluationKeys owner-evidence=bulk-endpoint-owner compile=go-test-ofrep"
        ),
    },
)
assert any("provider-capability-checked:" in blocker for blocker in optional_provider_missing_blockers), optional_provider_missing_blockers
optional_provider_evidence_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    optional_provider_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=bulkEvaluationKeys owner-evidence=bulk-endpoint-owner compile=go-test-ofrep. "
            "provider-capability-checked: declared-receiver=internal/server/ofrep.Server.bridge "
            "method=OFREPFlagKeys concrete-provider=internal/server/evaluation.Server "
            "guard=type-assertion source-declaration=internal/server/evaluation/ofrep_bridge.go compile=go-test-ofrep returncode=0"
        ),
    },
)
assert not any("provider-capability-checked:" in blocker or "constructor-dependency-checked:" in blocker for blocker in optional_provider_evidence_blockers), optional_provider_evidence_blockers
weak_dependency_contract_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    dependency_contract_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation rejected-owner=evaluation-bridge-helper validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=flagLister owner-evidence=bulk-endpoint-owner candidate-owner=internal/server/evaluation "
            "callsite=EvaluateBulk compile=go-test-ofrep. "
            "constructor-dependency-checked: constructor=internal/server/ofrep/server.go wiring=internal/cmd/grpc.go "
            "api-compatible=all-visible-callers compile=go-test-ofrep"
        ),
    },
)
assert any("constructor-dependency-checked:" in blocker for blocker in weak_dependency_contract_blockers), weak_dependency_contract_blockers
ambiguous_dependency_contract_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    dependency_contract_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation rejected-owner=evaluation-bridge-helper validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=flagLister owner-evidence=bulk-endpoint-owner candidate-owner=internal/server/evaluation "
            "callsite=EvaluateBulk compile=go-test-ofrep. "
            "constructor-dependency-checked: constructor=internal/server/ofrep/server.go "
            "wiring=internal/cmd/grpc.go mock-fake=ambiguous-unchanged-provider "
            "api-compatible=all-visible-callers compile=go-test-ofrep"
        ),
    },
)
assert any("constructor-dependency-checked:" in blocker for blocker in ambiguous_dependency_contract_blockers), ambiguous_dependency_contract_blockers
full_dependency_contract_blockers = solve_swe_prod.implementation_scope_blockers(
    "Bulk evaluation should list all flags when an explicit flag list is omitted.",
    dependency_contract_diff,
    {
        "status": "completed",
        "validation": (
            "source-owner-ledger: selected-owner=internal/server/ofrep candidate-owner=internal/server/ofrep "
            "candidate-owner=internal/server/evaluation rejected-owner=evaluation-bridge-helper validation-package=./internal/server/ofrep. "
            "source-symbol-map-passed: path=internal/server/ofrep/evaluation.go package=ofrep "
            "added-symbol=flagLister owner-evidence=bulk-endpoint-owner candidate-owner=internal/server/evaluation "
            "callsite=EvaluateBulk compile=go-test-ofrep. "
            "constructor-dependency-checked: constructor=internal/server/ofrep/server.go "
            "wiring=internal/cmd/grpc.go mock=internal/common/store_mock.go "
            "callsite=internal/server/ofrep/evaluation_test.go api-compatible=all-visible-callers compile=go-test-ofrep returncode=0"
        ),
    },
)
assert not any("constructor-dependency-checked:" in blocker for blocker in full_dependency_contract_blockers), full_dependency_contract_blockers
source_symbol_map_without_owner_ledger_blockers = solve_swe_prod.implementation_scope_blockers(
    "Add a linear benchmark generator for benchmark tests.",
    "diff --git a/lib/benchmark/linear.go b/lib/benchmark/linear.go\n"
    "+type Linear struct { Step int }\n"
    "+func NewLinearGenerator() {}\n",
    {
        "status": "completed",
        "validation": (
            "source-symbol-map-passed: path=lib/benchmark/linear.go package=benchmark "
            "added-symbol=Linear added-symbol=NewLinearGenerator "
            "owner-evidence=issue-term-benchmark-package "
            "nearby-test=go test ./lib/benchmark compile=go test ./lib/benchmark caller=lib/benchmark"
        ),
    },
)
assert any("source-owner-ledger:" in blocker for blocker in source_symbol_map_without_owner_ledger_blockers), source_symbol_map_without_owner_ledger_blockers
with tempfile.TemporaryDirectory() as adapter_symbol_tmp:
    adapter_repo = Path(adapter_symbol_tmp)
    (adapter_repo / "internal" / "server" / "ofrep").mkdir(parents=True)
    (adapter_repo / "errors").mkdir(parents=True)
    (adapter_repo / "examples" / "audit" / "webhook").mkdir(parents=True)
    (adapter_repo / "internal" / "server" / "ofrep" / "server.go").write_text(
        "package ofrep\n\ntype flagLister interface {}\nfunc (s *Server) bulkFlagKeys() {}\n",
        encoding="utf-8",
    )
    (adapter_repo / "errors" / "errors.go").write_text("package errors\n", encoding="utf-8")
    (adapter_repo / "examples" / "audit" / "webhook" / "main.go").write_text("package main\n", encoding="utf-8")
    adapter_symbol_diff = (
        "diff --git a/internal/server/ofrep/server.go b/internal/server/ofrep/server.go\n"
        "+type flagLister interface {}\n"
        "+func (s *Server) bulkFlagKeys() {}\n"
    )
    adapter_symbol_evidence = solve_swe_prod.source_symbol_adapter_evidence(adapter_repo, adapter_symbol_diff)
    assert "source-owner-ledger:" in adapter_symbol_evidence, adapter_symbol_evidence
    assert "source-symbol-map-passed:" in adapter_symbol_evidence, adapter_symbol_evidence
    assert "added-symbol=flagLister" in adapter_symbol_evidence, adapter_symbol_evidence
    adapter_symbol_blockers = solve_swe_prod.implementation_scope_blockers(
        "OFREP bulk evaluation should list flags when context flags are missing; examples mention errors.",
        adapter_symbol_diff,
        {
            "status": "completed",
            "validation": "helper-validation-passed: adapter public helper probe. " + adapter_symbol_evidence,
        },
        {"_solver_workdir": str(adapter_repo)},
    )
    assert not any("source-symbol-map-passed:" in blocker for blocker in adapter_symbol_blockers), adapter_symbol_blockers
    assert not any("source-owner-ledger:" in blocker for blocker in adapter_symbol_blockers), adapter_symbol_blockers
    assert not any("errors" in blocker or "examples" in blocker for blocker in adapter_symbol_blockers), adapter_symbol_blockers
with tempfile.TemporaryDirectory() as source_owner_tmp:
    source_owner_repo = Path(source_owner_tmp)
    (source_owner_repo / "lib" / "client").mkdir(parents=True)
    (source_owner_repo / "lib" / "benchmark").mkdir(parents=True)
    (source_owner_repo / "lib" / "client" / "bench.go").write_text("package client\n", encoding="utf-8")
    (source_owner_repo / "lib" / "benchmark" / "benchmark.go").write_text("package benchmark\n", encoding="utf-8")
    wrong_owner_blockers = solve_swe_prod.implementation_scope_blockers(
        "Add a linear benchmark generator for benchmark tests.",
        "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
        "+type LinearBenchmarkConfigGenerator struct { Step int }\n",
        {
            "status": "completed",
            "validation": (
                "source-owner-ledger: selected-owner=lib/client candidate-owner=lib/client "
                "rejected-owner=tool-cli-not-source-owner validation-package=./lib/client. "
                "source-symbol-map-passed: path=lib/client/bench.go package=client "
                "added-symbol=LinearBenchmarkConfigGenerator owner-evidence=issue-terms-benchmark-generator "
                "compile=go-test-lib-client"
            ),
        },
        {"_solver_workdir": str(source_owner_repo)},
    )
    assert any("lib/benchmark" in blocker for blocker in wrong_owner_blockers), wrong_owner_blockers
    auto_wrong_owner_evidence = solve_swe_prod.source_symbol_adapter_evidence(
        source_owner_repo,
        "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
        "+type LinearBenchmarkConfigGenerator struct { Step int }\n",
    )
    auto_wrong_owner_blockers = solve_swe_prod.implementation_scope_blockers(
        "Add a linear benchmark generator for benchmark tests.",
        "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
        "+type LinearBenchmarkConfigGenerator struct { Step int }\n",
        {
            "status": "completed",
            "validation": "helper-validation-passed: adapter public helper probe. " + auto_wrong_owner_evidence,
        },
        {"_solver_workdir": str(source_owner_repo)},
    )
    assert any("lib/benchmark" in blocker for blocker in auto_wrong_owner_blockers), auto_wrong_owner_blockers
    compared_owner_blockers = solve_swe_prod.implementation_scope_blockers(
        "Add a linear benchmark generator for benchmark tests.",
        "diff --git a/lib/client/bench.go b/lib/client/bench.go\n"
        "+type LinearBenchmarkConfigGenerator struct { Step int }\n",
        {
            "status": "completed",
            "validation": (
                "source-owner-ledger: selected-owner=lib/client candidate-owner=lib/client "
                "candidate-owner=lib/benchmark rejected-owner=lib/benchmark-existing-api-not-edit-target "
                "validation-package=./lib/client. "
                "source-symbol-map-passed: path=lib/client/bench.go package=client "
                "added-symbol=LinearBenchmarkConfigGenerator owner-evidence=compared-lib/benchmark-existing-api "
                "candidate-owner=lib/benchmark compile=go-test-lib-client"
            ),
        },
        {"_solver_workdir": str(source_owner_repo)},
    )
    assert not any("lib/benchmark" in blocker for blocker in compared_owner_blockers), compared_owner_blockers
with tempfile.TemporaryDirectory() as preedit_owner_tmp:
    preedit_repo = Path(preedit_owner_tmp)
    (preedit_repo / "lib" / "client").mkdir(parents=True)
    (preedit_repo / "lib" / "client" / "bench.go").write_text(
        "package client\n\ntype Benchmark struct{}\n",
        encoding="utf-8",
    )
    explicit_owner_issue = (
        "Add linear benchmark generator for progressive request rate configurations.\n"
        "New file: `lib/benchmark/linear.go`\n"
        "Path: `lib/benchmark/linear.go`\n"
        "Name: `Linear`\n"
        "Name: `validateConfig`\n"
        "The command status output is not the owner."
    )
    explicit_terms = solve_swe_prod.source_owner_issue_terms(explicit_owner_issue)
    assert "linear" in explicit_terms, explicit_terms
    assert "generator" in explicit_terms, explicit_terms
    assert "config" in explicit_terms, explicit_terms
    assert "command" not in explicit_terms, explicit_terms
    assert "status" not in explicit_terms, explicit_terms
    explicit_paths = solve_swe_prod.source_owner_issue_paths(explicit_owner_issue)
    assert explicit_paths == ["lib/benchmark/linear.go"], explicit_paths
    explicit_discovery = solve_swe_prod.source_owner_discovery(preedit_repo, explicit_owner_issue)
    assert "Explicit source paths from issue: lib/benchmark/linear.go" in explicit_discovery, explicit_discovery
    assert "candidate-owner=lib/benchmark/linear.go score=100 reason=issue-explicit-source-path" in explicit_discovery, explicit_discovery
    assert "candidate-owner=lib/benchmark score=95 reason=issue-explicit-source-path-parent=lib/benchmark/linear.go" in explicit_discovery, explicit_discovery
    preedit_discovery = solve_swe_prod.source_owner_discovery(
        preedit_repo,
        "Add a linear benchmark generator for benchmark tests.",
    )
    assert "source-owner-ledger:" in preedit_discovery, preedit_discovery
    assert "candidate-owner=lib/client/bench.go" in preedit_discovery, preedit_discovery
    assert "candidate-owner=lib/benchmark" in preedit_discovery, preedit_discovery
    assert "prospective-owner-from-issue-term=benchmark" in preedit_discovery, preedit_discovery
removed_symbol_map_blockers = solve_swe_prod.implementation_scope_blockers(
    "Preserve Alpine package parser compatibility while adding source package support.",
    "diff --git a/scanner/alpine.go b/scanner/alpine.go\n"
    "-func (o *alpine) parseApkInstalledList(stdout string) {}\n"
    "+func (o *alpine) parseApkInstalledDatabase(stdout string) {}\n",
    {
        "status": "completed",
        "validation": "go test ./scanner/... passed",
    },
)
assert any("source-symbol-map-passed:" in blocker for blocker in removed_symbol_map_blockers), removed_symbol_map_blockers

output_contract_test_update_blockers = solve_swe_prod.implementation_scope_blockers(
    "What did you expect to happen? The parser current output should become exactly one record per source. Current output has duplicate records.",
    "diff --git a/converter.go b/converter.go\n+func Convert() {}\n"
    "diff --git a/converter_test.go b/converter_test.go\n- old duplicate output\n+ new one-record output\n",
    {"status": "completed", "validation": "source fix plus inline golden expectation updated to exact output shape"},
)
assert not any("patch changes test files" in blocker for blocker in output_contract_test_update_blockers), output_contract_test_update_blockers
test_only_blockers = solve_swe_prod.implementation_scope_blockers(
    "What did you expect to happen? The parser current output should become exactly one record per source. Current output has duplicate records.",
    "diff --git a/converter_test.go b/converter_test.go\n- old duplicate output\n+ new one-record output\n",
    {"status": "completed", "validation": "test expectation changed"},
)
assert any("patch only changes tests" in blocker for blocker in test_only_blockers), test_only_blockers

multi_value_blockers = solve_swe_prod.validation_coverage_blockers(
    "Record parser should preserve complete alternate linked fields.",
    "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
    "+def collect_linked_values(record, link):\n"
    "+    linked_values = []\n"
    "+    linked_values.append(link)\n",
    "",
    {
        "status": "completed",
        "validation": "pytest -q records/decoder/tests/test_decode.py passed",
    },
)
assert any("multi-value-probe-passed:" in blocker for blocker in multi_value_blockers), multi_value_blockers
webfinger_route_blockers = solve_swe_prod.validation_coverage_blockers(
    "Add WebFinger support for local user profiles and include aliases and links in the JSON response.",
    "diff --git a/src/routes/well-known.js b/src/routes/well-known.js\n"
    "+function parseResource(resource) { return { username: resource.split(':').pop() }; }\n"
    "+res.type('application/jrd+json').json({\n"
    "+  subject: `acct:${user.username}@${host}`,\n"
    "+  aliases: [profileUrl],\n"
    "+  links: [{ rel: 'http://webfinger.net/rel/profile-page', href: profileUrl }],\n"
    "+});\n",
    "",
    {
        "status": "completed",
        "validation": "node route-smoke.js passed",
    },
)
assert not any("multi-value-probe-passed:" in blocker for blocker in webfinger_route_blockers), webfinger_route_blockers
multi_value_probe_blockers = solve_swe_prod.validation_coverage_blockers(
    "Record parser should preserve complete alternate linked fields.",
    "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
    "+def collect_linked_values(record, link):\n"
    "+    linked_values = []\n"
    "+    linked_values.append(link)\n",
    "",
    {
        "status": "completed",
        "validation": (
            "pytest -q records/decoder/tests/test_decode.py passed. "
            "multi-value-probe-passed: temporary decoder probe built one primary record "
            "with two linked alternate fields and observed both alternates in parsed output."
        ),
    },
)
assert any("final product-facing output" in blocker for blocker in multi_value_probe_blockers), multi_value_probe_blockers
original_multi_value_probe_path = solve_swe_prod.MULTI_VALUE_PROBE_PATH
try:
    with tempfile.TemporaryDirectory() as td:
        solve_swe_prod.MULTI_VALUE_PROBE_PATH = Path(td) / "multi-value-probe.txt"
        counted_status = {
            "status": "completed",
            "validation": (
                "pytest -q records/decoder/tests/test_decode.py passed. "
                "multi-value-probe-passed: temporary decoder probe exercised final parser output; "
                "final-output-field=parsed.related_values source-count=2 "
                "expected-output-count=2 actual-output-count=2."
            ),
        }
        multi_value_missing_artifact_blockers = solve_swe_prod.validation_coverage_blockers(
            "Record parser should preserve complete alternate linked fields.",
            "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
            "+def collect_linked_values(record, link):\n"
            "+    linked_values = []\n"
            "+    linked_values.append(link)\n",
            "",
            counted_status,
        )
        assert any("multi-value-probe.txt" in blocker for blocker in multi_value_missing_artifact_blockers), multi_value_missing_artifact_blockers
        solve_swe_prod.MULTI_VALUE_PROBE_PATH.write_text(
            "Command: python /tmp/probe.py\n"
            "Return code: 0\n"
            "multi-value-probe-passed: final-output-field=parsed.related_values "
            "source-count=2 expected-output-count=2 actual-output-count=2.\n",
            encoding="utf-8",
        )
        multi_value_counted_probe_blockers = solve_swe_prod.validation_coverage_blockers(
            "Record parser should preserve complete alternate linked fields.",
            "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
            "+def collect_linked_values(record, link):\n"
            "+    linked_values = []\n"
            "+    linked_values.append(link)\n",
            "",
            counted_status,
        )
        assert not any("multi-value-probe-passed:" in blocker for blocker in multi_value_counted_probe_blockers), multi_value_counted_probe_blockers
        composite_status = {
            "status": "completed",
            "validation": (
                "multi-value-probe-passed: final-output-field=parsed.primary+parsed.related_values "
                "source-count=2 expected-output-count=2 actual-output-count=2."
            ),
        }
        solve_swe_prod.MULTI_VALUE_PROBE_PATH.write_text(
            "Command: python probe.py\n"
            "Return code: 0\n"
            "multi-value-probe-passed: final-output-field=parsed.primary+parsed.related_values "
            "source-count=2 expected-output-count=2 actual-output-count=2.\n",
            encoding="utf-8",
        )
        multi_value_composite_field_blockers = solve_swe_prod.validation_coverage_blockers(
            "Record parser should preserve complete alternate linked fields.",
            "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
            "+def collect_linked_values(record, link):\n"
            "+    linked_values = []\n"
            "+    linked_values.append(link)\n",
            "",
            composite_status,
        )
        assert any("singular `final-output-field=...`" in blocker for blocker in multi_value_composite_field_blockers), multi_value_composite_field_blockers
finally:
    solve_swe_prod.MULTI_VALUE_PROBE_PATH = original_multi_value_probe_path

multi_value_mismatched_count_blockers = solve_swe_prod.validation_coverage_blockers(
    "Record parser should preserve complete alternate linked fields.",
    "diff --git a/records/decoder/decode.py b/records/decoder/decode.py\n"
    "+def collect_linked_values(record, link):\n"
    "+    linked_values = []\n"
    "+    linked_values.append(link)\n",
    "",
    {
        "status": "completed",
        "validation": (
            "multi-value-probe-passed: final-output-field=parsed.related_values "
            "source-count=2 expected-output-count=2 actual-output-count=1."
        ),
    },
)
assert any("final product-facing output" in blocker for blocker in multi_value_mismatched_count_blockers), multi_value_mismatched_count_blockers
assert any(
    "final product-facing output" in blocker
    for blocker in solve_swe_prod.blockers_after_passing_public_probe(multi_value_mismatched_count_blockers)
), "public helper probes must not clear final-output cardinality blockers"
solver_source_after_recovery_fix = (root / "evaluation/native_solver/solve_swe_prod.py").read_text(encoding="utf-8")
assert "and not coverage_followup_at" in solver_source_after_recovery_fix, "coverage follow-up recovery must not use generic no-status recovery first"
assert "coverage_blockers = [] if coverage_probe_satisfied" not in solver_source_after_recovery_fix

ui_blockers = solve_swe_prod.validation_coverage_blockers(
    "Keyboard shortcuts in the message composer should be customizable.",
    "diff --git a/src/Keyboard.ts b/src/Keyboard.ts\n+export function isKeyboardShortcut() {}\n"
    "diff --git a/src/components/views/rooms/BasicMessageComposer.tsx b/src/components/views/rooms/BasicMessageComposer.tsx\n+function onKeyDown() {}\n",
    "",
    {
        "status": "completed",
        "risk": "No browser interaction tests were run; residual risk is limited to runtime shortcut event behavior.",
        "validation": "yarn lint:types passed",
    },
)
assert any("UI/keyboard interaction source changed" in blocker for blocker in ui_blockers), ui_blockers
ui_skip_blockers = solve_swe_prod.validation_coverage_blockers(
    "Keyboard shortcuts in the message composer should be customizable.",
    "diff --git a/src/Keyboard.ts b/src/Keyboard.ts\n+export function isKeyboardShortcut() {}\n",
    "",
    {
        "status": "completed",
        "validation": (
            "ui-validation-skip-justified: no component test harness exists; "
            "source-level event matcher table inspected. "
            "build-verification-passed: "
            "final-diff-sha256=7fbc8818b5b782df7e698f4d12d7b406e1cca2ec1a3c2fc779b9d7977dfa3b8d "
            "changed-files=1 compile_clean=true returncode=0"
        ),
    },
)
assert not ui_skip_blockers, ui_skip_blockers

assert solve_swe_prod.visible_validation_passed_in_text(
    "pytest -q pkg/tests\n================= 5 passed, 54 deselected, 1 warning in 0.03s ==================\n"
)
assert solve_swe_prod.visible_validation_passed_in_text(
    "Validation passed:\n`pytest -q records/decoder/tests/test_decode.py -k 'linked-fields' --tb=short`\n"
    "Result: 5 passed, 54 deselected, 1 warning.\nfinal status: codex exec exited rc=0\n"
)
assert not solve_swe_prod.visible_validation_passed_in_text(
    "================= 1 failed, 4 passed, 54 deselected in 0.06s ==================\n"
)
assert not solve_swe_prod.visible_validation_passed_in_text("pytest reported no tests ran")
assert not solve_swe_prod.visible_validation_passed_in_text(
    "Validation passed:\n`go test -run TestNonExistent ./lib/srv/db`\n"
    "ok github.com/example/project/lib/srv/db 0.111s [no tests to run]\n"
)
assert solve_swe_prod.validation_text_has_no_test_evidence("go test -run '^$' ./pkg")
mixed_go_probe_output = (
    "ok github.com/example/project/internal/server/evaluation (cached)\n"
    "? github.com/example/project/internal/server/metrics [no test files]\n"
    "ok github.com/example/project/internal/server/ofrep 0.148s\n"
)
assert solve_swe_prod.go_test_output_has_real_package_evidence(mixed_go_probe_output)
assert not solve_swe_prod.validation_probe_has_no_test_evidence("go test ./internal/server/...", mixed_go_probe_output)
assert solve_swe_prod.validation_probe_has_no_test_evidence(
    "go test -run '^$' ./internal/server/ofrep",
    "ok github.com/example/project/internal/server/ofrep 0.111s [no tests to run]\n",
)
assert solve_swe_prod.validation_probe_has_no_test_evidence(
    "go test ./internal/server/metrics",
    "? github.com/example/project/internal/server/metrics [no test files]\n",
)

claim_diff = (
    "diff --git a/internal/server/evaluation/server.go b/internal/server/evaluation/server.go\n"
    "+type Storer interface { ListFlags() }\n"
)
claim_text = (
    "Evidence:\n"
    "- `internal/storage/storage.go` declares the existing storage signature.\n"
    "Changes:\n"
    "- Added the same method to `internal/server/evaluation/evaluation_store_mock.go` so tests compile.\n"
)
claim_blockers = solve_swe_prod.claimed_changed_path_blockers(claim_diff, claim_text)
assert claim_blockers and "evaluation_store_mock.go" in claim_blockers[0], claim_blockers
assert "internal/storage/storage.go" not in claim_blockers[0], claim_blockers
claim_text_with_diff = claim_text + "Changed source files:\n- `internal/server/evaluation/server.go`\n"
claim_diff_with_mock = claim_diff + (
    "diff --git a/internal/server/evaluation/evaluation_store_mock.go b/internal/server/evaluation/evaluation_store_mock.go\n"
    "+func (m *evaluationStoreMock) ListFlags() {}\n"
)
assert not solve_swe_prod.claimed_changed_path_blockers(claim_diff_with_mock, claim_text_with_diff)
assert solve_swe_prod.verifier_exact_followup_available(
    "BLOCKING FINDINGS with exact follow-up instructions: update middleware validation and rerun go test ./pkg"
)
assert not solve_swe_prod.verifier_exact_followup_available(
    "Findings: reviewed source files and no blocker remains"
)
stale_patch_blockers = solve_swe_prod.stale_patch_application_blockers(
    "apply_patch: could not find hunk context in internal/server/ofrep/evaluation.go"
)
assert stale_patch_blockers and "re-read the current target files" in stale_patch_blockers[0], stale_patch_blockers
assert not solve_swe_prod.stale_patch_application_blockers("apply_patch completed successfully")
assert solve_swe_prod.blocked_status_needs_diff_reconciliation(
    {
        "status": "blocked",
        "reason": "coverage blockers remain",
        "blockers": [
            "agent claimed changed source paths are absent from final git diff; make the missing edits or remove the stale claim before acceptance: src/user/index.js"
        ],
    }
)
assert solve_swe_prod.blocked_status_needs_diff_reconciliation(
    {
        "status": "blocked",
        "reason": "worker attempted a stale patch that did not apply cleanly",
        "blockers": ["apply_patch: could not find hunk context in src/Keyboard.ts"],
    }
)
assert solve_swe_prod.blocked_status_needs_diff_reconciliation(
    {
        "status": "blocked",
        "reason": (
            "Required worker agents completed without inspecting or modifying /app, "
            "leaving an empty git diff."
        ),
    }
)
assert not solve_swe_prod.blocked_status_needs_diff_reconciliation(
    {
        "status": "blocked",
        "reason": "focused validation failed",
        "blockers": ["go test ./pkg failed with a visible assertion"],
    }
)

with tempfile.TemporaryDirectory() as td:
    runtime_root = Path(td)
    agent_dir = runtime_root / "state" / "subagents" / "worker-04-fix"
    agent_dir.mkdir(parents=True)
    (agent_dir / "last-message.txt").write_text(
        "Updated source.\n\nValidation passed:\n`go test ./lib/service ./lib/kube/proxy`\n\nPatch is left uncommitted.\n",
        encoding="utf-8",
    )
    go_diff = "diff --git a/lib/service/kubernetes.go b/lib/service/kubernetes.go\n+func changed() {}\n"
    noisy_text = "tool router error: failed to parse function arguments\n"
    assert not solve_swe_prod.visible_validation_passed_in_text(noisy_text), noisy_text
    validation_evidence = solve_swe_prod.persisted_subagent_visible_validation_evidence(go_diff, runtime_root)
    assert "go test ./lib/service ./lib/kube/proxy" in validation_evidence, validation_evidence
    (agent_dir / "last-message.txt").write_text(
        "**Validation**\n"
        "- Ran `go test ./internal/server/ofrep ./internal/server/evaluation`\n\n"
        "Exact test output:\n"
        "```text\n"
        "ok      go.flipt.io/flipt/internal/server/ofrep (cached)\n"
        "ok      go.flipt.io/flipt/internal/server/evaluation    0.151s\n"
        "```\n",
        encoding="utf-8",
    )
    structured_validation_evidence = solve_swe_prod.persisted_subagent_visible_validation_evidence(go_diff, runtime_root)
    assert "go test ./internal/server/ofrep ./internal/server/evaluation" in structured_validation_evidence, structured_validation_evidence
    (agent_dir / "last-message.txt").write_text(
        "Updated source.\n\nValidation passed:\n`go test -run TestNonExistent ./lib/service`\n"
        "ok github.com/example/project/lib/service 0.111s [no tests to run]\n",
        encoding="utf-8",
    )
    no_test_validation_evidence = solve_swe_prod.persisted_subagent_visible_validation_evidence(go_diff, runtime_root)
    assert not no_test_validation_evidence, no_test_validation_evidence
    recovered_status = solve_swe_prod.status_with_recovered_validation(
        {
            "status": "blocked",
            "reason": "validation coverage gate remained unresolved after helper probe follow-up",
        },
        validation_evidence,
    )
    recovered_blockers = solve_swe_prod.validation_coverage_blockers(
        "Kubernetes exec session recording should initialize async upload state.",
        go_diff,
        noisy_text,
        recovered_status,
    )
    assert not any("Go source changed" in blocker for blocker in recovered_blockers), recovered_blockers
    no_test_status_blockers = solve_swe_prod.validation_coverage_blockers(
        "Kubernetes exec session recording should initialize async upload state.",
        go_diff,
        noisy_text,
        {
            "status": "completed",
            "validation": "go test -run TestNonExistent ./lib/service returned ok [no tests to run]",
        },
    )
    assert any("no-test compile check" in blocker for blocker in no_test_status_blockers), no_test_status_blockers
    assert solve_swe_prod.non_recoverable_final_validation_blockers(no_test_status_blockers), no_test_status_blockers

with tempfile.TemporaryDirectory() as td:
    runtime_root = Path(td)
    old_multi_value_probe_path = solve_swe_prod.MULTI_VALUE_PROBE_PATH
    try:
        solve_swe_prod.MULTI_VALUE_PROBE_PATH = runtime_root / "multi-value-probe.txt"
        reconciliation_path = runtime_root / "stale-visible-reconciliation.txt"
        reconciliation_path.write_text(
            "replacement-probe-passed: pytest tests/test_reader.py::test_final_shape passed\n"
            "stale-visible-failure-justified: source-visible schema now emits all linked aliases.\n",
            encoding="utf-8",
        )
        stale_evidence = solve_swe_prod.persisted_stale_visible_reconciliation_evidence(runtime_root)
        assert "stale-visible-reconciliation-passed:" in stale_evidence, stale_evidence

        reconciliation_path.write_text(
            "replacement-probe-passed: not relevant\n"
            "stale-visible-failure-justified: source-visible schema changed.\n",
            encoding="utf-8",
        )
        assert solve_swe_prod.persisted_stale_visible_reconciliation_evidence(runtime_root) == ""

        reconciliation_path.write_text(
            "replacement-probe-passed: pytest tests/test_reader.py::test_final_shape passed\n"
            "stale-visible-failure-justified: source-visible schema now emits all linked aliases.\n"
            "multi-value-probe-passed: final-output-field=aliases source-count=2 expected-output-count=2 actual-output-count=2\n",
            encoding="utf-8",
        )
        assert solve_swe_prod.persisted_stale_visible_reconciliation_evidence(runtime_root) == ""
        solve_swe_prod.MULTI_VALUE_PROBE_PATH.write_text(
            "multi-value-probe-passed: final-output-field=aliases source-count=2 expected-output-count=2 actual-output-count=2\n",
            encoding="utf-8",
        )
        stale_evidence = solve_swe_prod.persisted_stale_visible_reconciliation_evidence(runtime_root)
        assert "multi-value-probe-passed:" in stale_evidence, stale_evidence
    finally:
        solve_swe_prod.MULTI_VALUE_PROBE_PATH = old_multi_value_probe_path

assert solve_swe_prod.is_disallowed_patch_path("patch.txt")
assert solve_swe_prod.is_disallowed_patch_path("candidate.patch")

with tempfile.TemporaryDirectory() as td:
    old_probe_commands = solve_swe_prod.coverage_probe_commands
    old_timeout = os.environ.get("EVAL_VALIDATION_PROBE_TIMEOUT")
    try:
        solve_swe_prod.RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        solve_swe_prod.coverage_probe_commands = lambda *_args: [["bash", "-lc", "sleep 2"]]
        os.environ["EVAL_VALIDATION_PROBE_TIMEOUT"] = "1"
        timeout_report, timeout_passed = solve_swe_prod.run_validation_coverage_probe(
            Path(td),
            "Timeout probe regression",
            "diff --git a/main.go b/main.go\n",
            ["force timeout"],
        )
        assert not timeout_passed, timeout_report
        assert "adapter validation probe timed out after" in timeout_report, timeout_report
        assert solve_swe_prod.HELPER_PROBE_PATH.read_text(encoding="utf-8") == timeout_report
    finally:
        solve_swe_prod.coverage_probe_commands = old_probe_commands
        if old_timeout is None:
            os.environ.pop("EVAL_VALIDATION_PROBE_TIMEOUT", None)
        else:
            os.environ["EVAL_VALIDATION_PROBE_TIMEOUT"] = old_timeout

with tempfile.TemporaryDirectory() as td:
    aggregate_json = Path(td) / "aggregate.json"
    aggregate_json.write_text("{}", encoding="utf-8")
    dry_run = subprocess.check_output(
        [
            sys.executable,
            "-m",
            "evaluation.swe_bench_pro_run_next_shard",
            "--aggregate-json",
            str(aggregate_json),
            "--no-refresh-before",
            "--no-refresh-after",
            "--skip-scaffold-audit",
            "--sample-offset",
            "58",
            "--sample-count",
            "1",
            "--memory-limit",
            "16g",
            "--cpu-limit",
            "2",
            "--dry-run",
        ],
        cwd=root,
        text=True,
    )
    assert "--memory-limit 16g" in dry_run, dry_run
    assert "--cpu-limit 2" in dry_run, dry_run

parallel_cmd = swe_bench_pro_run_parallel_shards.build_worker_command(
    SimpleNamespace(
        proxy_port_base=9300,
        report_prefix_template="prefix-w{worker}-offset{offset}-count{count}",
        shard_size=1,
        agent_framework="multiagent-native",
        agent_model_name="gpt-5.5",
        max_steps=250,
        agent_timeout=3600,
        on_demand_min_free_gb=20,
        swe_bench_pro_repo_path=Path("/tmp/swe"),
        memory_limit="16g",
        cpu_limit="2",
        evalscope_path=None,
        native_solver_command="/tmp/evalscope-native-multiagent-solver.sh",
        native_solver_setup_command="",
        bake_native_solver=True,
        native_solver_source=root,
        native_codex_auth_json="",
        native_codex_auth_container_home="/root/.codex-multiagent-prod",
        persistent_cache=False,
        persistent_cache_root=Path("/tmp/cache"),
        persistent_cache_mode="rw",
        workers=1,
        responses_keepalive=False,
        no_start_proxy=False,
        ignore_errors=False,
        proxy_timeout=1800,
        proxy_ready_timeout=30,
    ),
    offset=58,
    count=1,
    worker_index=0,
)
assert "--memory-limit" in parallel_cmd and "16g" in parallel_cmd, parallel_cmd
assert "--cpu-limit" in parallel_cmd and "2" in parallel_cmd, parallel_cmd

parallel_offsets_dry_run = subprocess.check_output(
    [
        sys.executable,
        "-m",
        "evaluation.swe_bench_pro_run_parallel_shards",
        "--no-refresh-before",
        "--no-refresh-after",
        "--dry-run",
        "--workers",
        "4",
        "--shard-size",
        "1",
        "--sample-offsets",
        "2,8,12,14",
        "--report-prefix-template",
        "failed-w{worker}-offset{offset}-count{count}",
    ],
    cwd=root,
    text=True,
)
for expected_offset in ("2", "8", "12", "14"):
    assert f"--sample-offset {expected_offset} " in parallel_offsets_dry_run, parallel_offsets_dry_run
assert "--sample-offset 3 " not in parallel_offsets_dry_run, parallel_offsets_dry_run
PY
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

if "$ROOT/bin/write-policy.sh" approve "$TMPDIR/outside" >"$TMPDIR/old-approve.out" 2>&1; then
  echo "expected approve without metadata to fail" >&2
  cat "$TMPDIR/old-approve.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/old-approve.out" "approve requires --actor ACTOR"

approve_output="$("$ROOT/bin/write-policy.sh" approve "$TMPDIR/outside" --actor orchestrator --assignment-id test-policy --reason "test outside output")"
[[ "$approve_output" == $'approved outside write root: '"$TMPDIR/outside" ]]
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'approval\t'
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\torchestrator\ttest-policy\t'
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\ttest outside output\t0'
policy_check_outside="$("$ROOT/bin/write-policy.sh" check "$outside_path")"
[[ "$policy_check_outside" == $'allowed\t'"$outside_path" ]]

if "$ROOT/bin/write-policy.sh" approve /tmp --actor orchestrator --assignment-id broad-reject --reason "too broad" >"$TMPDIR/broad-approve.out" 2>&1; then
  echo "expected broad approval to require force" >&2
  cat "$TMPDIR/broad-approve.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/broad-approve.out" "refusing broad outside approval without --force"

forced_broad_output="$("$ROOT/bin/write-policy.sh" approve /tmp --actor orchestrator --assignment-id broad-force --reason "explicit user decision" --force)"
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

assignment_create_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-docs --assignment-id docs-001 --branch worker/docs --owned README.md,src)"
[[ "$assignment_create_output" == $'assignment created\tworker-docs\tdocs-001\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "assignment_id=docs-001"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "branch=worker/docs"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "worker_cli=claude"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "subagent_cli=claude"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "verifier_cli=codex"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/status" "assigned"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "README.md"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "src"

if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-overlap --assignment-id docs-overlap --branch worker/docs --owned README.md >"$TMPDIR/assignment-overlap.out" 2>&1; then
  echo "expected assignment-create to reject overlapping active writable ownership" >&2
  cat "$TMPDIR/assignment-overlap.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-overlap.out" "active assignment owned-path overlap"

assignment_verifier_overlap_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create verifier-overlap --assignment-id docs-verifier --branch worker/docs --owned README.md --role verifier)"
[[ "$assignment_verifier_overlap_output" == $'assignment created\tverifier-overlap\tdocs-verifier\tworker/docs' ]]
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status verifier-overlap done >/dev/null

assignment_scout_overlap_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create scout-overlap --assignment-id docs-scout --branch worker/docs --owned README.md --role scout)"
[[ "$assignment_scout_overlap_output" == $'assignment created\tscout-overlap\tdocs-scout\tworker/docs' ]]
assignment_after_scout_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-after-scout --assignment-id docs-after-scout --branch worker/docs --owned docs)"
[[ "$assignment_after_scout_output" == $'assignment created\tworker-after-scout\tdocs-after-scout\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/scout-overlap/assignment.env" "role=scout"
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status worker-after-scout done >/dev/null

assignment_show_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-show worker-docs)"
[[ "$assignment_show_output" == *"agent_name=worker-docs"* ]]
[[ "$assignment_show_output" == *"status=assigned"* ]]

assignment_status_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status worker-docs running)"
[[ "$assignment_status_output" == $'assignment status\tworker-docs\trunning' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/status" "running"

printf 'change\n' >>"$ASSIGN_REPO/README.md"
assignment_check_ok="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-check worker-docs)"
[[ "$assignment_check_ok" == *$'branch\tworker/docs\tworker/docs'* ]]
[[ "$assignment_check_ok" == *$'ok\tREADME.md'* ]]
[[ "$assignment_check_ok" == *$'accepted\tworker-docs'* ]]

printf 'outside\n' >"$ASSIGN_REPO/docs/notes.txt"
if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-check worker-docs >"$TMPDIR/assignment-outside.out" 2>&1; then
  echo "expected assignment check to reject outside owned paths" >&2
  cat "$TMPDIR/assignment-outside.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-outside.out" $'reject\toutside-owned-path\tdocs/notes.txt'

MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status worker-docs done >/dev/null
assignment_repeated_owned_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-repeated-owned --assignment-id docs-002 --branch worker/docs --owned README.md --owned src)"
[[ "$assignment_repeated_owned_output" == $'assignment created\tworker-repeated-owned\tdocs-002\tworker/docs' ]]
assert_file_contains "$ASSIGN_STATE/assignments/worker-repeated-owned/owned-paths" "README.md"
assert_file_contains "$ASSIGN_STATE/assignments/worker-repeated-owned/owned-paths" "src"
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status worker-repeated-owned done >/dev/null

assignment_create_branch_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-branch --assignment-id branch-001 --branch expected/branch --owned README.md,docs)"
[[ "$assignment_create_branch_output" == $'assignment created\tworker-branch\tbranch-001\texpected/branch' ]]
if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-check worker-branch >"$TMPDIR/assignment-branch.out" 2>&1; then
  echo "expected assignment check to reject branch mismatch" >&2
  cat "$TMPDIR/assignment-branch.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-branch.out" $'reject\tbranch-mismatch\texpected=expected/branch\tactual=worker/docs'
MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-status worker-branch failed >/dev/null

worktree_assignment_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-wt --assignment-id wt-001 --branch worker/wt --owned README.md)"
[[ "$worktree_assignment_output" == $'assignment created\tworker-wt\twt-001\tworker/wt' ]]
worktree_create_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" worktree-create worker-wt)"
[[ "$worktree_create_output" == *$'worktree created\tworker-wt\tworker/wt\t'"$ASSIGN_STATE/worktrees/worker-wt" ]]
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "agent_name=worker-wt"
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "branch=worker/wt"
assert_file_contains "$ASSIGN_STATE/worktrees/worker-wt.env" "path=$ASSIGN_STATE/worktrees/worker-wt"
[[ -f "$ASSIGN_STATE/worktrees/worker-wt/README.md" ]]
worktree_show_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" worktree-show worker-wt)"
[[ "$worktree_show_output" == *"branch=worker/wt"* ]]
worktree_remove_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" worktree-remove worker-wt)"
[[ "$worktree_remove_output" == *$'worktree removed\tworker-wt\t'"$ASSIGN_STATE/worktrees/worker-wt" ]]
[[ ! -e "$ASSIGN_STATE/worktrees/worker-wt.env" ]]

current_branch="$(git -C "$ROOT" rev-parse --abbrev-ref HEAD)"
checkpoint_assignment_output="$("$ROOT/bin/subagent.sh" assignment-create subagent-structured --assignment-id structured-001 --branch "$current_branch" --owned README.md)"
[[ "$checkpoint_assignment_output" == $'assignment created\tsubagent-structured\tstructured-001\t'"$current_branch" ]]
checkpoint_update_output="$("$ROOT/bin/subagent.sh" checkpoint-update subagent-structured --step "implemented checkpoint metadata" --idempotency "rerun checkpoint-update safely" --status running)"
[[ "$checkpoint_update_output" == $'checkpoint updated\tsubagent-structured\trunning' ]]
checkpoint_show_output="$("$ROOT/bin/subagent.sh" checkpoint-show subagent-structured)"
[[ "$checkpoint_show_output" == *"assignment_id=structured-001"* ]]
[[ "$checkpoint_show_output" == *"completed_step=implemented checkpoint metadata"* ]]
[[ "$checkpoint_show_output" == *"idempotency=rerun checkpoint-update safely"* ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/assignments/subagent-structured/checkpoint.env" "status=running"

finding_output="$("$ROOT/bin/subagent.sh" finding-create build-go-ofrep --severity blocking --type compile_failure --summary "Changed Go packages do not compile" --affected internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go --evidence-json '{"command":"go test ./internal/server/ofrep ./internal/server/evaluation","returncode":1,"stderr_excerpt":"undefined: req.Request"}' --required-resolution "Final diff must compile with rc=0 for both changed Go packages.")"
[[ "$finding_output" == $'finding created\tbuild-go-ofrep\tblocking\tcompile_failure' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"severity": "blocking"'
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"type": "compile_failure"'
assert_file_contains "$MULTIAGENT_STATE_DIR/findings/build-go-ofrep/finding.json" '"internal/server/ofrep/evaluation.go"'

todo_output="$("$ROOT/bin/subagent.sh" todo-create todo-017 --source-finding-id build-go-ofrep --task "Fix Go compile failure in changed packages." --context "Exact verifier evidence." --done-criteria "run go test ./internal/server/ofrep" --done-criteria "run go test ./internal/server/evaluation" --done-criteria "record returncode=0 after final diff")"
[[ "$todo_output" == $'todo created\ttodo-017\tbuild-go-ofrep\topen' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"source_finding_id": "build-go-ofrep"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "open"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"required_commands":'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"go test ./internal/server/evaluation"'

todo_assign_output="$("$ROOT/bin/subagent.sh" todo-assign todo-017 worker-02-ofrep)"
[[ "$todo_assign_output" == $'todo assigned\ttodo-017\tworker-02-ofrep' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"assigned_to": "worker-02-ofrep"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "assigned"'

if "$ROOT/bin/subagent.sh" gate-check >"$TMPDIR/gate-assigned.out" 2>&1; then
  echo "expected gate-check to reject an assigned todo" >&2
  cat "$TMPDIR/gate-assigned.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-assigned.out" $'reject\topen-blocking-todo\tfinding=build-go-ofrep\ttodo=todo-017\tstatus=assigned'

resolution_output="$("$ROOT/bin/subagent.sh" resolution-create todo-017 --worker worker-02-ofrep --status resolved --changed internal/server/ofrep/evaluation.go,internal/server/evaluation/ofrep_bridge.go --validation-json '[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}]' --why "Both changed packages compile after final diff.")"
[[ "$resolution_output" == $'resolution recorded\ttodo-017\tworker-02-ofrep\tresolved' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/resolution.json" '"status": "resolved"'
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/todo.json" '"status": "resolved"'

if "$ROOT/bin/subagent.sh" todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"unrelated-finding","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}],"final_diff_hash":"abc123"}' >"$TMPDIR/todo-close-wrong-finding.out" 2>&1; then
  echo "expected todo-close to reject verifier closure for the wrong finding" >&2
  cat "$TMPDIR/todo-close-wrong-finding.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/todo-close-wrong-finding.out" "must name source finding build-go-ofrep"

if "$ROOT/bin/subagent.sh" todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0}],"final_diff_hash":"abc123"}' >"$TMPDIR/todo-close-partial-recheck.out" 2>&1; then
  echo "expected todo-close to reject verifier closure missing worker validation command evidence" >&2
  cat "$TMPDIR/todo-close-partial-recheck.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/todo-close-partial-recheck.out" "missing required command"

if "$ROOT/bin/subagent.sh" gate-check >"$TMPDIR/gate-resolved.out" 2>&1; then
  echo "expected gate-check to reject a resolved but unverified todo" >&2
  cat "$TMPDIR/gate-resolved.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/gate-resolved.out" $'reject\topen-blocking-todo\tfinding=build-go-ofrep\ttodo=todo-017\tstatus=resolved'

todo_closed_output="$("$ROOT/bin/subagent.sh" todo-close todo-017 --verified-by verifier-01-ofrep --recheck-json '{"accepted":true,"finding_rechecked":"build-go-ofrep","commands":[{"cmd":"go test ./internal/server/ofrep","rc":0},{"cmd":"go test ./internal/server/evaluation","rc":0}],"final_diff_hash":"abc123"}' --notes "Verifier accepted worker resolution.")"
[[ "$todo_closed_output" == $'todo closed\ttodo-017\tverifier-01-ofrep' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/todos/todo-017/closure.json" '"accepted": true'
gate_closed_output="$("$ROOT/bin/subagent.sh" gate-check)"
[[ "$gate_closed_output" == $'accepted\tfinal-gate' ]]

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-structured"
printf 'Final status: completed according to stale transcript text\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/current.txt"
printf 'Done and finished, but this is fallback context only\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/transcript.log"

"$ROOT/bin/subagent.sh" spawn subagent-watch --instruction "Watch builds"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-watch"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "Claude prompt ready"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "write_policy=$MULTIAGENT_WRITE_POLICY"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "cli=claude"
assert_file_contains "$MOCK_TMUX_LOG" "new-window -d test-session subagent-watch"
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
"$ROOT/bin/subagent.sh" spawn subagent-file --instruction-file "$INSTRUCTION_FILE"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-file"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-file/instruction.txt" "Watch from file"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-file Read and follow the assignment in $MULTIAGENT_STATE_DIR/subagents/subagent-file/instruction.txt"

printf 'Codex prompt ready\n' >"$MOCK_TMUX_CAPTURES/verifier-01-docs.txt"
SUBAGENT_CLI="$VERIFIER_CLI" "$ROOT/bin/subagent.sh" spawn verifier-01-docs --instruction "Review worker-01-docs"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/verifier-01-docs/meta.env" "cli=codex"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:verifier-01-docs Review worker-01-docs"
verifier_spawn_line="$(grep -F "new-window -d test-session verifier-01-docs " "$MOCK_TMUX_LOG")"
[[ "$verifier_spawn_line" == *"--cd $ROOT"* ]]
[[ "$verifier_spawn_line" == *"--dangerously-bypass-approvals-and-sandbox --no-alt-screen"* ]]

printf 'Login required before Claude can start\n' >"$MOCK_TMUX_CAPTURES/subagent-auth.txt"
if "$ROOT/bin/subagent.sh" spawn subagent-auth --instruction "Should not send" >"$TMPDIR/auth-spawn.out" 2>&1; then
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
SUBAGENT_CLI=claude "$ROOT/bin/subagent.sh" spawn subagent-claude --instruction "Use Claude"
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
"$ROOT/bin/subagent.sh" finalize subagent-claude >/dev/null

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_output="$("$ROOT/bin/subagent.sh" poll subagent-watch)"
[[ "$poll_output" == $'subagent-watch\trunning' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/transcript.log" "Progress update: still running"

printf 'Read and follow the assignment. Proceed now, then report progress/final status in this window.\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_prompt_output="$("$ROOT/bin/subagent.sh" poll subagent-watch)"
[[ "$poll_prompt_output" == $'subagent-watch\trunning' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "progress/final status"

printf 'final status: codex exec exited rc=0\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
poll_final_status_output="$("$ROOT/bin/subagent.sh" poll subagent-watch)"
[[ "$poll_final_status_output" == $'subagent-watch\tdone' ]]
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "final status: codex exec exited rc=0"

printf 'Progress update: still running\n' >"$MOCK_TMUX_CAPTURES/subagent-watch.txt"
"$ROOT/bin/subagent.sh" poll subagent-watch >/dev/null

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
[[ "$recover_plan" == *$'subagent-structured\trestore\tcheckpoint-resumable\trunning\tclosed\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-structured"* ]]
structured_blocked_output="$("$ROOT/bin/subagent.sh" checkpoint-update subagent-structured --step "verified checkpoint recovery preference" --blocker "aggregate restore-all test should not restore this fixture")"
[[ "$structured_blocked_output" == $'checkpoint updated\tsubagent-structured\tblocked' ]]

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
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/restore_events.log" "cli=claude"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "You are a restored long-running subagent."
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/transcript.log" "Previous progress: halfway through recovery work"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-restore/instruction.txt" "You are a restored long-running subagent."
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-restore Read and follow the assignment in $MULTIAGENT_STATE_DIR/subagents/subagent-restore/instruction.txt"
claude_restore_line="$(grep -F "new-window -d test-session subagent-restore " "$MOCK_TMUX_LOG")"
[[ "$claude_restore_line" == *"--dangerously-skip-permissions"* ]]
if [[ "$claude_restore_line" == *"--cd"* || "$claude_restore_line" == *"--no-alt-screen"* ]]; then
  echo "expected restore to use persisted Claude CLI without Codex-only flags" >&2
  echo "$claude_restore_line" >&2
  exit 1
fi

restore_all_output="$("$ROOT/bin/subagent.sh" restore-all)"
[[ "$restore_all_output" == *$'skipped subagent-blocked\tskip-blocked'* ]]
[[ "$restore_all_output" == *$'skipped subagent-open\tskip-open'* ]]
[[ "$restore_all_output" == *$'skipped subagent-watch\tskip-finalized'* ]]
[[ "$restore_all_output" == *"restore-all complete: restored=0"* ]]

# Test organizational learning functionality

# Test decision.sh basic functionality
DECISION_STATE_DIR="$TMPDIR/decision-state"
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" init DEC-001 --title "Test Decision" --owner "test-user"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "decision_id=DEC-001"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "title=Test Decision"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "owner=test-user"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=open"

# Test decision.sh add-alternative
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" add-alternative DEC-001 \
  --plan-id PLAN-A --summary "First approach" --proposed-by agent-1 \
  --branch worker/plan-a --assignment-name worker-implementation \
  --expected-outcome "Fast delivery" --risk "Technical debt"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "PLAN-A"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "First approach"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/alternatives.tsv" "agent-1"

# Test decision.sh add-assumption
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" add-assumption DEC-001 \
  --assumption-id ASSUME-1 --statement "API will be stable" \
  --confidence "high" --validation-method "integration tests" \
  --expected-signal "no breaking changes"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/assumptions.tsv" "ASSUME-1"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/assumptions.tsv" "API will be stable"

# Test decision.sh commit
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" commit DEC-001 \
  --selected-plan PLAN-A --reason "Best balance of speed and quality" \
  --rollback-policy "Manual rollback" --reflection-due "2026-06-01"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=committed"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "selected_plan=PLAN-A"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "reason=Best balance of speed and quality"

# Test decision.sh record-metric
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" record-metric DEC-001 \
  --name "delivery-time" --expected "2 weeks" --actual "3 weeks"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "delivery-time"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "2 weeks"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/metrics.tsv" "3 weeks"

# Test decision.sh reflect
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" reflect DEC-001 \
  --recommendation "adjust" --reason "Delivery was slower than expected" \
  --follow-up-assignment "optimization-task"

assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/decision.env" "status=reflected"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "recommendation=adjust"
assert_file_contains "$DECISION_STATE_DIR/decisions/DEC-001/outcome.env" "reflection_reason=Delivery was slower than expected"

# Test decision.sh show and list
show_output="$(MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" show DEC-001)"
[[ "$show_output" == *"Decision: DEC-001"* ]]
[[ "$show_output" == *"title=Test Decision"* ]]

list_output="$(MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" list)"
[[ "$list_output" == *$'DEC-001\treflected\tTest Decision\ttest-user'* ]]

# Test decision.sh error conditions
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" init DEC-001 --title "Duplicate" >"$TMPDIR/duplicate.out" 2>&1; then
  echo "expected duplicate decision to fail" >&2
  cat "$TMPDIR/duplicate.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate.out" "decision already exists: DEC-001"

# Test invalid decision ID
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" init "DEC/INVALID" --title "Bad ID" >"$TMPDIR/invalid-id.out" 2>&1; then
  echo "expected invalid decision ID to fail" >&2
  cat "$TMPDIR/invalid-id.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-id.out" "invalid decision ID: DEC/INVALID"

# Test invalid recommendation
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" reflect DEC-001 --recommendation "invalid" --reason "test" >"$TMPDIR/invalid-rec.out" 2>&1; then
  echo "expected invalid recommendation to fail" >&2
  cat "$TMPDIR/invalid-rec.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-rec.out" "invalid recommendation: invalid"

# Test newline rejection
if MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" init DEC-NEWLINE --title "$(printf 'Title\nwith\nnewlines')" >"$TMPDIR/newline.out" 2>&1; then
  echo "expected newline in title to fail" >&2
  cat "$TMPDIR/newline.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/newline.out" "--title may not contain newlines"

# Test duplicate plan ID with a new decision
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" init DEC-002 --title "Test Duplicates"
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" add-alternative DEC-002 --plan-id PLAN-B --summary "First plan" --proposed-by agent-1
set +e  # Temporarily disable exit on error
MULTIAGENT_STATE_DIR="$DECISION_STATE_DIR" "$ROOT/bin/decision.sh" add-alternative DEC-002 --plan-id PLAN-B --summary "Duplicate" --proposed-by agent-2 >"$TMPDIR/duplicate-plan.out" 2>&1
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

org_assignment_create_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-org --assignment-id org-001 --branch worker/org-task --owned README.md --role qa --decision-id DEC-001 --plan-id PLAN-A)"
[[ "$org_assignment_create_output" == $'assignment created\tworker-org\torg-001\tworker/org-task' ]]
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "assignment_id=org-001"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "role=qa"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "decision_id=DEC-001"
assert_file_contains "$ORG_ASSIGN_STATE/assignments/worker-org/assignment.env" "plan_id=PLAN-A"
# Test invalid role rejection
set +e
MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-bad --assignment-id bad-001 --branch worker/org-task --owned README.md --role invalid-role >"$TMPDIR/invalid-role.out" 2>&1
invalid_role_result=$?
set -e
if [[ "$invalid_role_result" -eq 0 ]]; then
  echo "expected invalid role to fail" >&2
  cat "$TMPDIR/invalid-role.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-role.out" "invalid role 'invalid-role'"
# Test checkpoint-update includes organizational metadata
checkpoint_org_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" checkpoint-update worker-org --step "implemented org metadata" --status running)"
[[ "$checkpoint_org_output" == $'checkpoint updated\tworker-org\trunning' ]]
checkpoint_show_org_output="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" checkpoint-show worker-org)"
[[ "$checkpoint_show_org_output" == *"role=qa"* ]]
[[ "$checkpoint_show_org_output" == *"decision_id=DEC-001"* ]]
[[ "$checkpoint_show_org_output" == *"plan_id=PLAN-A"* ]]
# Test status.sh includes organizational metadata columns
# Create a persisted subagent with organizational metadata that won't trigger polling
mkdir -p "$ORG_ASSIGN_STATE/subagents/subagent-org-test"
printf 'running\n' >"$ORG_ASSIGN_STATE/subagents/subagent-org-test/status"
printf 'Testing organizational metadata in subagents\n' >"$ORG_ASSIGN_STATE/subagents/subagent-org-test/current.txt"

# Create assignment metadata for the subagent
ORG_SUBAGENT_ASSIGN_OUTPUT="$(MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create subagent-org-test --assignment-id org-sub-001 --branch worker/org-task --owned README.md --role verifier --decision-id DEC-002 --plan-id PLAN-B)"

status_org_output="$(cd "$ROOT" && MULTIAGENT_ROOT="$ORG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ORG_ASSIGN_STATE" bin/status.sh)"
[[ "$status_org_output" == *$'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID'* ]]
[[ "$status_org_output" == *$'subagent\tsubagent-org-test\trunning\tclosed\tTesting organizational metadata in subagents\t'"$ORG_ASSIGN_STATE/subagents/subagent-org-test"$'\tverifier\tDEC-002\tPLAN-B'* ]]
# Test that subagents without metadata show "-" for organizational fields
mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta"
printf 'running\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta/status"
printf 'Subagent without org metadata\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta/current.txt"
printf 'subagent-no-meta\n' >>"$MOCK_TMUX_WINDOWS"
printf 'Subagent without org metadata progress\n' >"$MOCK_TMUX_CAPTURES/subagent-no-meta.txt"
status_no_meta_output="$("$ROOT/bin/status.sh")"
[[ "$status_no_meta_output" == *$'subagent\tsubagent-no-meta\trunning\topen\tSubagent without org metadata progress\t'"$MULTIAGENT_STATE_DIR/subagents/subagent-no-meta"$'\t-\t-\t-'* ]]
# Test documentation consistency - no unsupported plan.sh or decision.sh resolve commands
if grep -Fq "bin/plan.sh" "$ROOT/README.md"; then
  echo "README.md should not reference unsupported bin/plan.sh" >&2
  exit 1
fi
if grep -Fq "decision.sh resolve" "$ROOT/README.md"; then
  echo "README.md should not reference unsupported decision.sh resolve command" >&2
  exit 1
fi
if grep -Fq "bin/plan.sh" "$ROOT/orchestrator_prompt.md"; then
  echo "orchestrator_prompt.md should not reference unsupported bin/plan.sh" >&2
  exit 1
fi
if grep -Fq "decision.sh resolve" "$ROOT/orchestrator_prompt.md"; then
  echo "orchestrator_prompt.md should not reference unsupported decision.sh resolve command" >&2
  exit 1
fi

# Verify that decision command examples in README.md use only supported commands
decision_commands_readme="$(grep "bin/decision.sh" "$ROOT/README.md" || true)"
[[ "$decision_commands_readme" == *"bin/decision.sh init"* ]]
[[ "$decision_commands_readme" == *"bin/decision.sh add-alternative"* ]]
[[ "$decision_commands_readme" == *"bin/decision.sh commit"* ]]
[[ "$decision_commands_readme" == *"bin/decision.sh list"* ]]
[[ "$decision_commands_readme" == *"bin/decision.sh show"* ]]

# Verify that decision command examples in the organizational-learning module use only supported commands
decision_commands_prompt="$(grep "bin/decision.sh" "$ROOT/prompts/roles/organizational-learning.md" || true)"
[[ "$decision_commands_prompt" == *"bin/decision.sh init"* ]]
[[ "$decision_commands_prompt" == *"bin/decision.sh add-alternative"* ]]
[[ "$decision_commands_prompt" == *"bin/decision.sh commit"* ]]

# Test DAG workflow control functionality

# Test basic DAG commands with temporary state
DAG_STATE_DIR="$TMPDIR/dag-state"
mkdir -p "$DAG_STATE_DIR"

# Test bin/dag.sh init
init_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" init WF-001 --title "Test Workflow" --owner "test-user")"
[[ "$init_output" == $'workflow created\tWF-001\tTest Workflow' ]]
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "workflow_id=WF-001"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "title=Test Workflow"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "owner=test-user"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/workflow.env" "status=active"

# Test bin/dag.sh add-node
node_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-A --agent worker-a --assignment-id assign-a --role qa --branch worker/a --owned file-a.txt)"
[[ "$node_output" == $'node added\tWF-001\tNODE-A\tworker-a' ]]
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "NODE-A"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "worker-a"
assert_file_contains "$DAG_STATE_DIR/workflows/WF-001/nodes.tsv" "pending"

# Test bin/dag.sh list
list_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" list)"
[[ "$list_output" == *$'WF-001\tactive\tTest Workflow\ttest-user'* ]]

# Test bin/dag.sh show
show_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" show WF-001)"
[[ "$show_output" == *"Workflow: WF-001"* ]]
[[ "$show_output" == *"workflow_id=WF-001"* ]]
[[ "$show_output" == *"NODE-A"* ]]

# Test DAG sequencing: node A ready first, node B ready only after A is done
node_b_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-B --agent worker-b --assignment-id assign-b --role qa --branch worker/b --owned file-b.txt --depends-on NODE-A)"
[[ "$node_b_output" == $'node added\tWF-001\tNODE-B\tworker-b' ]]

# Test bin/dag.sh ready - node A should be ready, node B should not
# Also test that ready emits only node IDs, one per line, with no READY_NODES header
ready_initial_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" ready WF-001)"
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
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-A done --reason "completed task A"

# Now NODE-B should be ready
ready_after_a_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" ready WF-001)"
[[ "$ready_after_a_output" == *"NODE-B"* ]]

# Test failed upstream node causes downstream node to appear in blocked output
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-C --agent worker-c --assignment-id assign-c --role qa --branch worker/c --owned file-c.txt --depends-on NODE-B

# Mark NODE-B as failed
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-B failed --reason "task failed"

# Test bin/dag.sh blocked - NODE-C should be blocked
blocked_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" blocked WF-001)"
[[ "$blocked_output" == *"NODE-C"* ]]
[[ "$blocked_output" == *"dependency NODE-B failed"* ]]

# Test skipped upstream node satisfies dependencies
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-D --agent worker-d --assignment-id assign-d --role qa --branch worker/d --owned file-d.txt
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-E --agent worker-e --assignment-id assign-e --role qa --branch worker/e --owned file-e.txt --depends-on NODE-D

# Mark NODE-D as skipped
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-D skipped --reason "conditions not met"

# NODE-E should now be ready (skipped dependencies satisfy constraints)
ready_after_skip_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" ready WF-001)"
[[ "$ready_after_skip_output" == *"NODE-E"* ]]

# Test explicitly marked ready nodes
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-F --agent worker-f --assignment-id assign-f --role qa --branch worker/f --owned file-f.txt

# Mark NODE-F as explicitly ready
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-F ready --reason "manually marked ready"

# NODE-F should appear in ready output even though it was explicitly marked ready
ready_explicit_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" ready WF-001)"
[[ "$ready_explicit_output" == *"NODE-F"* ]]

# Mark NODE-F as running and verify it no longer appears in ready output
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-F running --reason "started execution"
ready_after_running_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" ready WF-001)"
if [[ "$ready_after_running_output" == *"NODE-F"* ]]; then
  echo "expected NODE-F to not appear in ready output when marked running" >&2
  echo "$ready_after_running_output" >&2
  exit 1
fi

# Test duplicate workflow rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" init WF-001 --title "Duplicate" >"$TMPDIR/duplicate-workflow.out" 2>&1; then
  echo "expected duplicate workflow to fail" >&2
  cat "$TMPDIR/duplicate-workflow.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate-workflow.out" "workflow already exists: WF-001"

# Test duplicate node rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-A --agent worker-dup --assignment-id assign-dup --role qa --branch worker/dup --owned file-dup.txt >"$TMPDIR/duplicate-node.out" 2>&1; then
  echo "expected duplicate node to fail" >&2
  cat "$TMPDIR/duplicate-node.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/duplicate-node.out" "node ID already exists: NODE-A"

# Test missing dependency rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-MISSING --agent worker-missing --assignment-id assign-missing --role qa --branch worker/missing --owned file-missing.txt --depends-on NONEXISTENT >"$TMPDIR/missing-dep.out" 2>&1; then
  echo "expected missing dependency to fail" >&2
  cat "$TMPDIR/missing-dep.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/missing-dep.out" "dependency does not exist: NONEXISTENT"

# Test invalid status rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" status WF-001 NODE-A invalid-status >"$TMPDIR/invalid-status.out" 2>&1; then
  echo "expected invalid status to fail" >&2
  cat "$TMPDIR/invalid-status.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-status.out" "invalid status: invalid-status"

# Test role validation - invalid roles should be rejected
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 NODE-INVALID-ROLE --agent worker-invalid --assignment-id assign-invalid --role decision --branch worker/invalid --owned file-invalid.txt >"$TMPDIR/invalid-role.out" 2>&1; then
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
  role_output="$(MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-001 "$node_id" --agent "worker-$role" --assignment-id "assign-$role" --role "$role" --branch "worker/$role" --owned "file-$role.txt")"
  [[ "$role_output" == *"node added"* ]]
  [[ "$role_output" == *"$node_id"* ]]
done

# Test invalid workflow ID rejection
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" init "WF/INVALID" --title "Bad ID" >"$TMPDIR/invalid-workflow-id.out" 2>&1; then
  echo "expected invalid workflow ID to fail" >&2
  cat "$TMPDIR/invalid-workflow-id.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/invalid-workflow-id.out" "invalid workflow ID: WF/INVALID"

# Test cycle detection
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" init WF-CYCLE --title "Cycle Test"
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-CYCLE CYCLE-A --agent worker-cycle-a --assignment-id assign-cycle-a --role qa --branch worker/cycle-a --owned file-cycle-a.txt
MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-CYCLE CYCLE-B --agent worker-cycle-b --assignment-id assign-cycle-b --role qa --branch worker/cycle-b --owned file-cycle-b.txt --depends-on CYCLE-A

# This should create a cycle: CYCLE-A -> CYCLE-B -> CYCLE-A
if MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-CYCLE CYCLE-C --agent worker-cycle-c --assignment-id assign-cycle-c --role qa --branch worker/cycle-c --owned file-cycle-c.txt --depends-on CYCLE-B && \
   MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-CYCLE CYCLE-D --agent worker-cycle-d --assignment-id assign-cycle-d --role qa --branch worker/cycle-d --owned file-cycle-d.txt --depends-on CYCLE-A; then
  # Now try to create a cycle by making CYCLE-A depend on CYCLE-C
  temp_edges="$DAG_STATE_DIR/workflows/WF-CYCLE/edges.tsv"
  printf 'CYCLE-C\tCYCLE-A\t%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" >>"$temp_edges"
  if ! MULTIAGENT_STATE_DIR="$DAG_STATE_DIR" "$ROOT/bin/dag.sh" add-node WF-CYCLE CYCLE-TEST --agent worker-test --assignment-id assign-test --role qa --branch worker/test --owned file-test.txt --depends-on CYCLE-A >"$TMPDIR/cycle-test.out" 2>&1; then
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

dag_assignment_create_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-dag --assignment-id dag-001 --branch worker/dag-task --owned README.md --role qa --workflow-id WF-001 --node-id NODE-A --depends-on NODE-B,NODE-C)"
[[ "$dag_assignment_create_output" == $'assignment created\tworker-dag\tdag-001\tworker/dag-task' ]]
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "workflow_id=WF-001"
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "node_id=NODE-A"
assert_file_contains "$DAG_ASSIGN_STATE/assignments/worker-dag/assignment.env" "depends_on=NODE-B,NODE-C"

# Test checkpoint-update includes DAG metadata
checkpoint_dag_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" checkpoint-update worker-dag --step "implemented dag metadata support" --status running)"
[[ "$checkpoint_dag_output" == $'checkpoint updated\tworker-dag\trunning' ]]
checkpoint_show_dag_output="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" checkpoint-show worker-dag)"
[[ "$checkpoint_show_dag_output" == *"workflow_id=WF-001"* ]]
[[ "$checkpoint_show_dag_output" == *"node_id=NODE-A"* ]]
[[ "$checkpoint_show_dag_output" == *"depends_on=NODE-B,NODE-C"* ]]

# Test status.sh emits WORKFLOW_ID and NODE_ID columns with metadata
# Create a persisted subagent with DAG metadata
mkdir -p "$DAG_ASSIGN_STATE/subagents/subagent-dag-test"
printf 'running\n' >"$DAG_ASSIGN_STATE/subagents/subagent-dag-test/status"
printf 'Testing DAG metadata in subagents\n' >"$DAG_ASSIGN_STATE/subagents/subagent-dag-test/current.txt"

# Create assignment metadata for the subagent with DAG metadata
DAG_SUBAGENT_ASSIGN_OUTPUT="$(MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create subagent-dag-test --assignment-id dag-sub-001 --branch worker/dag-task --owned README.md --role verifier --workflow-id WF-002 --node-id NODE-X)"

status_dag_output="$(cd "$ROOT" && MULTIAGENT_ROOT="$DAG_ASSIGN_REPO" MULTIAGENT_STATE_DIR="$DAG_ASSIGN_STATE" bin/status.sh)"
[[ "$status_dag_output" == *$'TYPE\tNAME\tSTATUS\tWINDOW\tLAST_PROGRESS\tSTATE_DIR\tROLE\tDECISION_ID\tPLAN_ID\tWORKFLOW_ID\tNODE_ID'* ]]
[[ "$status_dag_output" == *$'subagent\tsubagent-dag-test\trunning\tclosed\tTesting DAG metadata in subagents\t'"$DAG_ASSIGN_STATE/subagents/subagent-dag-test"$'\tverifier\t-\t-\tWF-002\tNODE-X'* ]]

# Test documentation consistency - ensure docs do not reference unsupported DAG commands
if grep -Fq "dag.sh update-status" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh update-status command" >&2
  exit 1
fi
if grep -Fq "dag.sh.*--description" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh --description flag" >&2
  exit 1
fi
if grep -Fq "dag.sh show --node" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh show --node flag" >&2
  exit 1
fi
if grep -Fq "dag.sh show --verbose" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh show --verbose flag" >&2
  exit 1
fi
if grep -Fq "dag.sh ready --watch" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh ready --watch flag" >&2
  exit 1
fi
if grep -Fq "dag.sh export" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh export command" >&2
  exit 1
fi
if grep -Fq "dag.sh status --workflow" "$ROOT/README.md" "$ROOT/orchestrator_prompt.md" 2>/dev/null; then
  echo "docs should not reference unsupported dag.sh status --workflow flag" >&2
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
