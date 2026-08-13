#!/usr/bin/env bash
set -euo pipefail

FRAMEWORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MULTIAGENT="$FRAMEWORK_ROOT/target/debug/multiagent"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$FRAMEWORK_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -c 'from multiagent_framework.workflow import main; assert callable(main)'

assert_contains() {
  local file="$1"
  local expected="$2"
  grep -Fq -- "$expected" "$file" || {
    echo "expected $file to contain: $expected" >&2
    exit 1
  }
}

TEST_REPO="$TEST_TMP/repo"
TEST_STATE="$TEST_TMP/state"
mkdir -p "$TEST_REPO" "$TEST_STATE"
git -C "$TEST_REPO" init -q
git -C "$TEST_REPO" config user.email test@example.com
git -C "$TEST_REPO" config user.name "Lifecycle Test"
git -C "$TEST_REPO" config commit.gpgsign false
printf 'test\n' >"$TEST_REPO/README.md"
git -C "$TEST_REPO" add README.md
git -C "$TEST_REPO" commit -q -m initial
TEST_BRANCH="$(git -C "$TEST_REPO" branch --show-current)"

IMPLEMENTATION_CONTEXT="$TEST_TMP/approved-implementation-context.md"
printf '%s\n' \
  '# Approved implementation context' \
  'decision: DEC-1' \
  'plan: PLAN-1' \
  'authority: orchestrator' \
  'must-not-do: change public behavior' >"$IMPLEMENTATION_CONTEXT"

PROMPT_BUNDLE="$TEST_TMP/orchestrator-bundle.md"
"$MULTIAGENT" prompt-bundle \
  --orchestrator "$FRAMEWORK_ROOT/orchestrator_prompt.md" \
  --lifecycle "$FRAMEWORK_ROOT/prompts/playbooks/implementation-lifecycle.md" \
  --output "$PROMPT_BUNDLE" >/dev/null
assert_contains "$PROMPT_BUNDLE" "BEGIN MANDATORY IMPLEMENTATION LIFECYCLE"
assert_contains "$PROMPT_BUNDLE" "post-implementation -> pre-implementation"

wf() {
  MULTIAGENT_STATE_DIR="$TEST_STATE" "$MULTIAGENT" workflow "$@"
}

wf init WF-LIFECYCLE >/dev/null
MULTIAGENT_STATE_DIR="$TEST_STATE" "$MULTIAGENT" decision init DEC-1 \
  --title "Lifecycle decision" --owner orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$TEST_STATE" "$MULTIAGENT" decision add-alternative DEC-1 \
  --plan-id PLAN-1 --summary "Implement approved lifecycle plan" \
  --proposed-by orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$TEST_STATE" "$MULTIAGENT" decision commit DEC-1 \
  --selected-plan PLAN-1 --reason "Authority review and evidence support this plan" >/dev/null
if wf transition WF-LIFECYCLE implementation >"$TEST_TMP/no-permit.out" 2>&1; then
  echo "expected implementation without a permit to fail" >&2
  exit 1
fi
assert_contains "$TEST_TMP/no-permit.out" "implementation gate has not passed"

wf record-review WF-LIFECYCLE AUTH-1 \
  --type decision-authority --verdict pass \
  --evidence "independent authority review passed" >/dev/null
wf add-todo WF-LIFECYCLE TODO-EVIDENCE \
  --kind evidence --summary "inspect persisted state" >/dev/null
if wf prepare-implementation WF-LIFECYCLE \
  --decision-id DEC-1 --plan-id PLAN-1 --decision-revision 1 \
  --implementation-context "$IMPLEMENTATION_CONTEXT" --authority-review AUTH-1 \
  >"$TEST_TMP/evidence-open.out" 2>&1; then
  echo "expected active evidence TODO to block implementation" >&2
  exit 1
fi
assert_contains "$TEST_TMP/evidence-open.out" "active evidence/decision TODOs"
wf resolve-todo WF-LIFECYCLE TODO-EVIDENCE \
  --resolution completed --evidence "state inspected" >/dev/null
wf prepare-implementation WF-LIFECYCLE \
  --decision-id DEC-1 --plan-id PLAN-1 --decision-revision 1 \
  --implementation-context "$IMPLEMENTATION_CONTEXT" --authority-review AUTH-1 >/dev/null
wf transition WF-LIFECYCLE implementation >/dev/null

MULTIAGENT_ROOT="$TEST_REPO" MULTIAGENT_STATE_DIR="$TEST_STATE" \
  MULTIAGENT_WORKFLOW_ID=WF-LIFECYCLE MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" subagent assignment-create worker-lifecycle \
    --assignment-id LIFE-1 --role exploitation \
    --workflow-id WF-LIFECYCLE --decision-id DEC-1 --plan-id PLAN-1 \
    --branch "$TEST_BRANCH" --owned README.md >/dev/null
assert_contains "$TEST_STATE/assignments/worker-lifecycle/assignment.env" "decision_revision=1"
assert_contains "$TEST_STATE/assignments/worker-lifecycle/assignment.env" "implementation_context_sha256="

printf '\ncontext drift\n' >>"$IMPLEMENTATION_CONTEXT"
if wf gate WF-LIFECYCLE implementation --decision-id DEC-1 --plan-id PLAN-1 \
  >"$TEST_TMP/context-drift.out" 2>&1; then
  echo "expected changed implementation context to invalidate the implementation gate" >&2
  exit 1
fi
assert_contains "$TEST_TMP/context-drift.out" "approved implementation context changed"

SKIP_STATE="$TEST_TMP/skip-state"
MULTIAGENT_STATE_DIR="$SKIP_STATE" "$MULTIAGENT" workflow init WF-SKIP >/dev/null
MULTIAGENT_STATE_DIR="$SKIP_STATE" "$MULTIAGENT" workflow add-todo WF-SKIP TODO-SKIP \
  --kind evidence --summary "requires unavailable environment" >/dev/null
if MULTIAGENT_STATE_DIR="$SKIP_STATE" "$MULTIAGENT" workflow resolve-todo WF-SKIP TODO-SKIP \
  --resolution skipped --reason-code unavailable-now --reason "environment unavailable" \
  --authority orchestrator --evidence "probe failed" >"$TEST_TMP/invalid-skip.out" 2>&1; then
  echo "expected unavailable-now skip without destination to fail" >&2
  exit 1
fi
assert_contains "$TEST_TMP/invalid-skip.out" "requires --destination or --resume-condition"

LOOP_STATE="$TEST_TMP/loop-state"
LOOP_CONTEXT="$TEST_TMP/loop-implementation-context.md"
printf 'revision 1\n' >"$LOOP_CONTEXT"
loop() {
  MULTIAGENT_STATE_DIR="$LOOP_STATE" "$MULTIAGENT" workflow "$@"
}
loop init WF-LOOP >/dev/null
MULTIAGENT_STATE_DIR="$LOOP_STATE" "$MULTIAGENT" decision init DEC-LOOP \
  --title "Loop decision" --owner orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$LOOP_STATE" "$MULTIAGENT" decision add-alternative DEC-LOOP \
  --plan-id PLAN-LOOP --summary "Implement and re-evaluate findings" \
  --proposed-by orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$LOOP_STATE" "$MULTIAGENT" decision commit DEC-LOOP \
  --selected-plan PLAN-LOOP --reason "Recorded lifecycle plan" >/dev/null
loop record-review WF-LOOP AUTH-LOOP \
  --type decision-authority --verdict pass --evidence "authority passed" >/dev/null
loop prepare-implementation WF-LOOP \
  --decision-id DEC-LOOP --plan-id PLAN-LOOP --decision-revision 1 \
  --implementation-context "$LOOP_CONTEXT" --authority-review AUTH-LOOP >/dev/null
loop transition WF-LOOP implementation >/dev/null
loop transition WF-LOOP post-implementation --diff-hash DIFF-LOOP >/dev/null
loop record-review WF-LOOP TECH-FINDING \
  --type technical --verdict findings --diff-hash DIFF-LOOP \
  --evidence "repair required" >/dev/null
loop add-todo WF-LOOP TODO-FOLLOWUP \
  --kind direct --summary "repair verifier finding" --origin TECH-FINDING >/dev/null
loop transition WF-LOOP pre-implementation >/dev/null
assert_contains "$LOOP_STATE/workflows/WF-LOOP/lifecycle/lifecycle.env" "iteration=2"

loop record-review WF-LOOP AUTH-LOOP-2 \
  --type decision-authority --verdict pass --evidence "revised authority passed" >/dev/null
printf 'revision 2\n' >"$LOOP_CONTEXT"
loop prepare-implementation WF-LOOP \
  --decision-id DEC-LOOP --plan-id PLAN-LOOP --decision-revision 2 \
  --implementation-context "$LOOP_CONTEXT" --authority-review AUTH-LOOP-2 >/dev/null
loop transition WF-LOOP implementation >/dev/null
loop transition WF-LOOP post-implementation --diff-hash DIFF-FINAL >/dev/null
loop resolve-todo WF-LOOP TODO-FOLLOWUP \
  --resolution completed --evidence "repair and verifier recheck passed" >/dev/null
for review_type in decision-drift scope technical reflection; do
  loop record-review WF-LOOP "REVIEW-$review_type" \
    --type "$review_type" --verdict pass --diff-hash DIFF-FINAL \
    --evidence "$review_type passed" >/dev/null
done
loop completion-check WF-LOOP >/dev/null
loop transition WF-LOOP complete >/dev/null
MULTIAGENT_ROOT="$TEST_REPO" MULTIAGENT_STATE_DIR="$LOOP_STATE" \
  MULTIAGENT_WORKFLOW_ID=WF-LOOP MULTIAGENT_RUN_ID=RUN-LIFECYCLE \
  MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" orchestrator complete >"$TEST_TMP/complete.out"
assert_contains "$TEST_TMP/complete.out" $'run completed\tRUN-LIFECYCLE'

echo "implementation lifecycle tests passed"
