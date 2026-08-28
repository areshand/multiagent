#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MULTIAGENT="${MULTIAGENT_BIN:-$ROOT/target/debug/multiagent}"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

MOCK_BIN="$TEST_TMP/bin"
MOCK_WINDOWS="$TEST_TMP/windows"
MOCK_CAPTURES="$TEST_TMP/captures"
MOCK_LOG="$TEST_TMP/tmux.log"
STATE="$TEST_TMP/state"
REPO="$TEST_TMP/repo"
mkdir -p "$MOCK_BIN" "$MOCK_CAPTURES" "$STATE" "$REPO"
: >"$MOCK_WINDOWS"
: >"$MOCK_LOG"

cat >"$MOCK_BIN/tmux" <<'TMUX'
#!/usr/bin/env bash
set -euo pipefail
windows="${MOCK_TMUX_WINDOWS:?}"
captures="${MOCK_TMUX_CAPTURES:?}"
log="${MOCK_TMUX_LOG:?}"
cmd="${1:-}"
shift || true
printf '%s %s\n' "$cmd" "$*" >>"$log"
target_name() {
  local target="$1"
  printf '%s\n' "${target#*:}"
}
case "$cmd" in
  has-session)
    exit 0
    ;;
  list-windows)
    cat "$windows"
    ;;
  new-window|new-session)
    [[ "${1:-}" == "-d" ]] && shift
    session="${1:?}"
    name="${2:?}"
    printf '%s\n' "$name" >>"$windows"
    ;;
  capture-pane)
    target=""
    while (($#)); do
      if [[ "$1" == "-t" ]]; then
        target="$2"
        shift 2
      else
        shift
      fi
    done
    name="$(target_name "$target")"
    [[ -f "$captures/$name.txt" ]] && cat "$captures/$name.txt"
    ;;
  kill-window)
    [[ "${1:-}" == "-t" ]] && shift
    name="$(target_name "${1:?}")"
    awk -v name="$name" '$0 != name' "$windows" >"$windows.next"
    mv "$windows.next" "$windows"
    ;;
  display-message)
    printf 'mock-workflow\n'
    ;;
  send-keys|pipe-pane|select-window|set-option)
    ;;
  *)
    ;;
esac
TMUX
chmod +x "$MOCK_BIN/tmux"

git -C "$REPO" init -q
git -C "$REPO" config user.email test@example.com
git -C "$REPO" config user.name "Mock Workflow"
git -C "$REPO" config commit.gpgsign false
printf 'before\n' >"$REPO/source.txt"
git -C "$REPO" add source.txt
git -C "$REPO" commit -qm initial
BRANCH="$(git -C "$REPO" branch --show-current)"

TASK="$TEST_TMP/task.md"
printf 'Update source.txt from before to after and validate the result.\n' >"$TASK"

export PATH="$MOCK_BIN:$PATH"
export MOCK_TMUX_WINDOWS="$MOCK_WINDOWS"
export MOCK_TMUX_CAPTURES="$MOCK_CAPTURES"
export MOCK_TMUX_LOG="$MOCK_LOG"
export MULTIAGENT_SESSION="mock-workflow"
export MULTIAGENT_ROOT="$REPO"
export MULTIAGENT_STATE_DIR="$STATE"
export MULTIAGENT_ORIGINAL_TASK_FILE="$TASK"
export MULTIAGENT_WORKFLOW_ID="WF-MOCK-E2E"
export MULTIAGENT_RUN_ID="RUN-MOCK-E2E"
export MULTIAGENT_PROMPT_MODULE_ROOT="$ROOT"
export MULTIAGENT_WRITE_POLICY="$TEST_TMP/write-policy.paths"
export MULTIAGENT_LIFECYCLE_ENFORCEMENT=1
export MULTIAGENT_UID_SANDBOX=0
export MULTIAGENT_READY_ATTEMPTS=1
export MULTIAGENT_READY_DELAY=0
export MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=0
export CODEX_BIN=true
export CLAUDE_BIN=true
export ORCHESTRATOR_CLI=codex
export WORKER_CLI=claude
export SUBAGENT_CLI=claude
export VERIFIER_CLI=claude

ma() {
  "$MULTIAGENT" "$@"
}

ma policy init >/dev/null
ma workflow init "$MULTIAGENT_WORKFLOW_ID" >/dev/null
mkdir -p "$STATE/runtime_state"
printf '%s\n' "$MULTIAGENT_WORKFLOW_ID" >"$STATE/runtime_state/active-workflow-id"

ma decision init DEC-MOCK --title "Mock source update" --owner orchestrator >/dev/null
ma decision add-alternative DEC-MOCK --plan-id PLAN-MOCK \
  --summary "Apply the authenticated bounded update" --proposed-by orchestrator >/dev/null
ma decision commit DEC-MOCK --selected-plan PLAN-MOCK \
  --reason "Submit the bounded plan for independent authority review" >/dev/null

AUTH_REVIEWER="decision-authority-reviewer-mock"
printf 'Claude prompt ready\n' >"$MOCK_CAPTURES/$AUTH_REVIEWER.txt"
ma subagent spawn "$AUTH_REVIEWER" --role reviewer \
  --workflow-id "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DEC-MOCK --plan-id PLAN-MOCK --decision-revision 1 \
  --instruction "Review the bounded implementation plan and authority." >/dev/null
cat >"$MOCK_CAPTURES/$AUTH_REVIEWER.txt" <<'EOF'
verdict: orchestrator-may-decide
authority-findings: none
review-record: type=decision-authority verdict=pass diff=-
EOF
cp "$MOCK_CAPTURES/$AUTH_REVIEWER.txt" "$STATE/subagents/$AUTH_REVIEWER/last-message.txt"
ma subagent finalize "$AUTH_REVIEWER" >/dev/null

ma workflow record-review "$MULTIAGENT_WORKFLOW_ID" AUTH-MOCK \
  --type decision-authority --verdict pass --evidence "mock authority review passed" \
  --reviewer "$AUTH_REVIEWER" >/dev/null

CONTEXT="$STATE/approved-context.md"
cat >"$CONTEXT" <<'EOF'
# Approved implementation context
goal: update source.txt from before to after
decision: DEC-MOCK
plan: PLAN-MOCK
authority: authenticated caller plus independent authority reviewer
owned-paths: source.txt
must-do: preserve the bounded file contract
must-not-do: change unrelated paths
EOF
ma workflow prepare-implementation "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DEC-MOCK --plan-id PLAN-MOCK --decision-revision 1 \
  --implementation-context "$CONTEXT" --authority-review AUTH-MOCK >/dev/null
ma workflow transition "$MULTIAGENT_WORKFLOW_ID" implementation >/dev/null

WORKER="worker-mock-implementation"
printf 'Claude prompt ready\n' >"$MOCK_CAPTURES/$WORKER.txt"
ma subagent spawn "$WORKER" --assignment-id ASSIGN-MOCK --branch "$BRANCH" \
  --own source.txt --workflow-id "$MULTIAGENT_WORKFLOW_ID" \
  --decision-id DEC-MOCK --plan-id PLAN-MOCK --instruction-file "$CONTEXT" >/dev/null
printf 'after\n' >"$REPO/source.txt"
cat >"$MOCK_CAPTURES/$WORKER.txt" <<'EOF'
Final status: completed
Changed only source.txt from before to after.
EOF
ma subagent finalize "$WORKER" >/dev/null
ma subagent assignment-status "$WORKER" done >/dev/null
ma subagent assignment-check "$WORKER" >/dev/null

DIFF_HASH="mock-diff-v1"
ma workflow transition "$MULTIAGENT_WORKFLOW_ID" post-implementation \
  --diff-hash "$DIFF_HASH" >/dev/null

for spec in "decision-drift reviewer-decision-drift-mock REVIEW-DRIFT" \
            "technical verifier-technical-mock REVIEW-TECH"; do
  read -r review_type reviewer review_id <<<"$spec"
  printf 'Claude prompt ready\n' >"$MOCK_CAPTURES/$reviewer.txt"
  ma subagent spawn "$reviewer" --role reviewer \
    --instruction "Review the frozen mock candidate for $review_type." >/dev/null
  cat >"$MOCK_CAPTURES/$reviewer.txt" <<EOF
ACCEPTED
review-record: type=$review_type verdict=pass diff=$DIFF_HASH
EOF
  cp "$MOCK_CAPTURES/$reviewer.txt" "$STATE/subagents/$reviewer/last-message.txt"
  ma subagent finalize "$reviewer" >/dev/null
  ma workflow record-review "$MULTIAGENT_WORKFLOW_ID" "$review_id" \
    --type "$review_type" --verdict pass --diff-hash "$DIFF_HASH" \
    --evidence "mock $review_type review passed" --reviewer "$reviewer" >/dev/null
done

ma orchestrator complete >/dev/null

grep -Fq 'phase=complete' "$STATE/workflows/$MULTIAGENT_WORKFLOW_ID/lifecycle/lifecycle.env"
grep -Fq 'after' "$REPO/source.txt"
grep -Fq 'External access is an authority boundary, not a mutability classification.' \
  "$ROOT/orchestrator_prompt.md"
grep -Fq 'A scout never calls Slack, GitHub, Grafana, AWS, Kubernetes, prod-mcp' \
  "$ROOT/prompts/playbooks/orchestration-routing.md"
[[ "$(grep -c '^new-window ' "$MOCK_LOG")" -eq 4 ]]
if find "$STATE/subagents" -mindepth 1 -maxdepth 1 -type d -name '*scout*' | grep -q .; then
  echo "mock workflow spawned an unnecessary scout" >&2
  exit 1
fi

echo "mock orchestration E2E passed"
