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
assert_file_contains "$MOCK_TMUX_LOG" "--cd $LAUNCH_TARGET"
assert_file_contains "$MOCK_TMUX_LOG" "export MULTIAGENT_RESUME='0'"
assert_file_contains "$MOCK_TMUX_LOG" "export MULTIAGENT_VERIFIER_MAX_ITERATIONS='3'"
assert_file_contains "$MOCK_TMUX_LOG" "export WORKER_CLI='claude'"
assert_file_contains "$MOCK_TMUX_LOG" "export SUBAGENT_CLI='claude'"
assert_file_contains "$MOCK_TMUX_LOG" "export VERIFIER_CLI='codex'"
assert_file_contains "$MOCK_TMUX_LOG" "Multiagent launch mode: MULTIAGENT_RESUME=%s (%s)"
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$ROOT/orchestrator_prompt.md")"
if grep -Fq "$LAUNCH_TARGET/orchestrator_prompt.md" "$MOCK_TMUX_LOG" "$TMPDIR/launch.out"; then
  echo "expected launch to use script-dir orchestrator prompt, not target-root prompt" >&2
  cat "$MOCK_TMUX_LOG" >&2
  cat "$TMPDIR/launch.out" >&2
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
assert_file_contains "$MOCK_TMUX_LOG" "export MULTIAGENT_RESUME='1'"
assert_file_contains "$MOCK_TMUX_LOG" "export MULTIAGENT_VERIFIER_MAX_ITERATIONS='5'"
assert_file_contains "$MOCK_TMUX_LOG" "'resume'"

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
assert_file_contains "$MOCK_TMUX_LOG" "$(printf '%q' "$EXPLICIT_PROMPT")"

assert_file_contains "$ROOT/orchestrator_prompt.md" "Do not inspect recovery state"
assert_file_contains "$ROOT/orchestrator_prompt.md" 'When `MULTIAGENT_RESUME=1`'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'Only in that mode'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'MULTIAGENT_VERIFIER_MAX_ITERATIONS'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'verifier suggests no follow-up'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'WORKER_CLI="${WORKER_CLI:-claude}"'
assert_file_contains "$ROOT/orchestrator_prompt.md" 'SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn'
assert_file_contains "$ROOT/README.md" "Launches are clean by default"
assert_file_contains "$ROOT/README.md" "./launch.sh --resume"
assert_file_contains "$ROOT/README.md" "Verifier Workflow"
assert_file_contains "$ROOT/README.md" "MULTIAGENT_VERIFIER_MAX_ITERATIONS=3"
assert_file_contains "$ROOT/README.md" 'WORKER_CLI`: worker CLI for manual worker windows, default `claude`'
assert_file_contains "$ROOT/README.md" 'VERIFIER_CLI`: verifier CLI, default `codex`'
assert_file_contains "$ROOT/README.md" "Harness Dispatch Prototype"
assert_file_contains "$ROOT/README.md" "outside the CLI orchestrator"
assert_file_contains "$ROOT/README.md" "external supervisor assess"
assert_file_contains "$ROOT/README.md" "workers or the orchestrator window"
assert_file_contains "$ROOT/docs/harness-dispatch-design.md" "agent intent -> action manifest -> policy decision -> executor"
assert_file_contains "$ROOT/docs/harness-dispatch-design.md" "External harness"
assert_file_contains "$ROOT/docs/harness-dispatch-design.md" "CLI orchestrator"

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

cat >"$TMPDIR/harness-requires-approval.action" <<EOF
type=approve_write
path=$TMPDIR/harness-outside
actor=orchestrator
assignment_id=harness-001
reason=user approved harness export
approved=false
EOF
harness_eval="$("$ROOT/bin/harness.sh" evaluate "$TMPDIR/harness-requires-approval.action")"
[[ "$harness_eval" == *$'decision\tREQUIRE_APPROVAL'* ]]
if "$ROOT/bin/harness.sh" dispatch "$TMPDIR/harness-requires-approval.action" >"$TMPDIR/harness-requires-approval.out" 2>&1; then
  echo "expected harness dispatch to stop for unapproved outside write" >&2
  cat "$TMPDIR/harness-requires-approval.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/harness-requires-approval.out" $'decision\tREQUIRE_APPROVAL'

cat >"$TMPDIR/harness-approved.action" <<EOF
type=approve_write
path=$TMPDIR/harness-outside
actor=orchestrator
assignment_id=harness-002
reason=user approved harness export
approved=true
EOF
harness_approved="$("$ROOT/bin/harness.sh" dispatch "$TMPDIR/harness-approved.action")"
[[ "$harness_approved" == *$'decision\tALLOW'* ]]
[[ "$harness_approved" == *"approved outside write root: $TMPDIR/harness-outside"* ]]
assert_file_contains "$MULTIAGENT_WRITE_POLICY" $'\tharness-002\t'

cat >"$TMPDIR/harness-unsupported.action" <<'EOF'
type=send_instruction
name=worker-01-docs
EOF
if "$ROOT/bin/harness.sh" dispatch "$TMPDIR/harness-unsupported.action" >"$TMPDIR/harness-unsupported.out" 2>&1; then
  echo "expected harness dispatch to deny unsupported direct instruction" >&2
  cat "$TMPDIR/harness-unsupported.out" >&2
  exit 1
fi
assert_file_contains "$TMPDIR/harness-unsupported.out" $'decision\tDENY'

printf 'Orchestrator is planning and checking worker state\n' >"$MOCK_TMUX_CAPTURES/orchestrator.txt"
orchestrator_assessment="$("$ROOT/bin/harness.sh" assess orchestrator)"
[[ "$orchestrator_assessment" == *$'assessment\torchestrator\tinspect'* ]]

printf 'worker-risk\n' >>"$MOCK_TMUX_WINDOWS"
printf 'I will git push and open PR now\n' >"$MOCK_TMUX_CAPTURES/worker-risk.txt"
cat >"$TMPDIR/harness-assess-risk.action" <<'EOF'
type=assess_agent
name=worker-risk
EOF
risk_assessment="$("$ROOT/bin/harness.sh" dispatch "$TMPDIR/harness-assess-risk.action")"
[[ "$risk_assessment" == *$'decision\tALLOW'* ]]
[[ "$risk_assessment" == *$'assessment\tworker-risk\tkill'* ]]

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

cat >"$TMPDIR/harness-assignment-check.action" <<'EOF'
type=assignment_check
name=worker-docs
EOF
harness_assignment_check="$(MULTIAGENT_ROOT="$ASSIGN_REPO" MULTIAGENT_STATE_DIR="$ASSIGN_STATE" "$ROOT/bin/harness.sh" dispatch "$TMPDIR/harness-assignment-check.action")"
[[ "$harness_assignment_check" == *$'decision\tALLOW'* ]]
[[ "$harness_assignment_check" == *$'accepted\tworker-docs'* ]]

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

# Verify that decision command examples in orchestrator_prompt.md use only supported commands
decision_commands_prompt="$(grep "bin/decision.sh" "$ROOT/orchestrator_prompt.md" || true)"
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
valid_roles=("exploitation" "exploration" "reflection" "architecture" "qa" "verifier")
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
