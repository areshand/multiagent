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
    name=""
    command=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
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
    printf 'new-window %s %s\n' "$name" "$command" >>"$log_file"
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
export WORKER_CLI="codex"
export SUBAGENT_CLI="codex"

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

LAUNCH_TARGET="$TMPDIR/target-repo"
LAUNCH_STATE="$TMPDIR/launch-state"
LAUNCH_POLICY="$TMPDIR/launch-policy/write-policy.paths"
mkdir -p "$LAUNCH_TARGET"
rm -f "$MOCK_TMUX_LOG"
MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-cross-repo" \
  MULTIAGENT_ROOT= \
  MULTIAGENT_PROMPT= \
  MULTIAGENT_STATE_DIR="$LAUNCH_STATE" \
  MULTIAGENT_WRITE_POLICY="$LAUNCH_POLICY" \
  "$ROOT/launch.sh" --session launch-cross-repo --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch.out"
assert_file_contains "$TMPDIR/launch.out" "Started tmux session: launch-cross-repo"
assert_file_contains "$TMPDIR/launch.out" "Default write root: $LAUNCH_TARGET"
assert_file_contains "$MOCK_TMUX_LOG" "--cd $LAUNCH_TARGET"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$ROOT/orchestrator_prompt.md")"
if grep -Fq "$LAUNCH_TARGET/orchestrator_prompt.md" "$MOCK_TMUX_LOG" "$TMPDIR/launch.out"; then
  echo "expected launch to use script-dir orchestrator prompt, not target-root prompt" >&2
  cat "$MOCK_TMUX_LOG" >&2
  cat "$TMPDIR/launch.out" >&2
  exit 1
fi

EXPLICIT_PROMPT="$TMPDIR/custom-orchestrator-prompt.md"
printf 'custom prompt\n' >"$EXPLICIT_PROMPT"
rm -f "$MOCK_TMUX_LOG"
MOCK_TMUX_HAS_SESSION=0 \
  MULTIAGENT_SESSION="launch-explicit-prompt" \
  MULTIAGENT_PROMPT="$EXPLICIT_PROMPT" \
  MULTIAGENT_STATE_DIR="$TMPDIR/launch-explicit-state" \
  MULTIAGENT_WRITE_POLICY="$TMPDIR/launch-explicit-policy/write-policy.paths" \
  "$ROOT/launch.sh" --session launch-explicit-prompt --root "$LAUNCH_TARGET" --no-attach >"$TMPDIR/launch-explicit.out"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$EXPLICIT_PROMPT")"

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
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "worker_cli=codex"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/assignment.env" "subagent_cli=codex"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/status" "assigned"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "README.md"
assert_file_contains "$ASSIGN_STATE/assignments/worker-docs/owned-paths" "src"

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

assignment_create_branch_output="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-create worker-branch --assignment-id branch-001 --branch expected/branch --owned README.md,docs)"
[[ "$assignment_create_branch_output" == $'assignment created\tworker-branch\tbranch-001\texpected/branch' ]]
if MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/subagent.sh" assignment-check worker-branch >"$TMPDIR/assignment-branch.out" 2>&1; then
  echo "expected assignment check to reject branch mismatch" >&2
  cat "$TMPDIR/assignment-branch.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/assignment-branch.out" $'reject\tbranch-mismatch\texpected=expected/branch\tactual=worker/docs'

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

mkdir -p "$MULTIAGENT_STATE_DIR/subagents/subagent-structured"
printf 'Final status: completed according to stale transcript text\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/current.txt"
printf 'Done and finished, but this is fallback context only\n' >"$MULTIAGENT_STATE_DIR/subagents/subagent-structured/transcript.log"

"$ROOT/bin/subagent.sh" spawn subagent-watch --instruction "Watch builds"
assert_file_contains "$MOCK_TMUX_WINDOWS" "subagent-watch"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/status" "running"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/current.txt" "Codex prompt ready"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "write_policy=$MULTIAGENT_WRITE_POLICY"
assert_file_contains "$MULTIAGENT_STATE_DIR/subagents/subagent-watch/meta.env" "cli=codex"
assert_file_contains "$MOCK_TMUX_LOG" "new-window subagent-watch"
assert_file_contains "$MOCK_TMUX_LOG" "--cd $ROOT"
assert_file_contains "$MOCK_TMUX_LOG" "--dangerously-bypass-approvals-and-sandbox --no-alt-screen"
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-watch Watch builds"

printf 'Login required before Codex can start\n' >"$MOCK_TMUX_CAPTURES/subagent-auth.txt"
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
claude_spawn_line="$(grep -F "new-window subagent-claude " "$MOCK_TMUX_LOG")"
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
assert_file_contains "$MOCK_TMUX_LOG" "send-key test-session:subagent-restore You are a restored long-running subagent."
claude_restore_line="$(grep -F "new-window subagent-restore " "$MOCK_TMUX_LOG")"
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

echo "tests passed"
