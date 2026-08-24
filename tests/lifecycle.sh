#!/usr/bin/env bash
set -euo pipefail

FRAMEWORK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
MULTIAGENT="${MULTIAGENT_BIN:-$FRAMEWORK_ROOT/target/debug/multiagent}"
TEST_TMP="$(mktemp -d)"
trap 'rm -rf "$TEST_TMP"' EXIT

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

BYPASS_STATE="$TEST_TMP/bypass-state"
if MULTIAGENT_ROOT="$TEST_REPO" MULTIAGENT_STATE_DIR="$BYPASS_STATE" \
  MULTIAGENT_UID_SANDBOX=1 MULTIAGENT_LIFECYCLE_ENFORCEMENT=0 \
  "$MULTIAGENT" subagent assignment-create worker-bypass \
    --assignment-id BYPASS-1 --role exploitation \
    --branch "$TEST_BRANCH" --owned README.md \
    >"$TEST_TMP/lifecycle-env-bypass.out" 2>&1; then
  echo "expected UID-isolated orchestrator to reject lifecycle environment bypass" >&2
  exit 1
fi
assert_contains "$TEST_TMP/lifecycle-env-bypass.out" \
  "lifecycle enforcement requires --workflow-id"

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

wf init WF-REVIEW-EVIDENCE >/dev/null
if MULTIAGENT_STATE_DIR="$TEST_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-REVIEW-EVIDENCE AUTH-MISSING \
  --type decision-authority --verdict pass --evidence "claimed pass" \
  >"$TEST_TMP/missing-reviewer-evidence.out" 2>&1; then
  echo "expected enforced review without reviewer evidence to fail" >&2
  exit 1
fi
assert_contains "$TEST_TMP/missing-reviewer-evidence.out" "requires --reviewer NAME"
REVIEWER_STATE="$TEST_STATE/subagents/authority-reviewer-test"
mkdir -p "$REVIEWER_STATE"
printf '%s\n' 'role=reviewer' 'codex_access=read-only' >"$REVIEWER_STATE/meta.env"
printf 'finalized\n' >"$REVIEWER_STATE/status"
printf '2026-08-15T00:00:00Z\n' >"$REVIEWER_STATE/finalized_at"
printf 'review-record: type=decision-authority verdict=pass diff=-\n' \
  >"$REVIEWER_STATE/last-message.txt"
MULTIAGENT_STATE_DIR="$TEST_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-REVIEW-EVIDENCE AUTH-DURABLE \
  --type decision-authority --verdict pass --evidence "durable reviewer pass" \
  --reviewer authority-reviewer-test >/dev/null

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

CONTRACT_STATE="$TEST_TMP/contract-state"
CONTRACT_TASK="$TEST_TMP/original-task.md"
printf 'Refactor Widget: do not embed LegacyConfig; use a named cfg field.\n' >"$CONTRACT_TASK"
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" MULTIAGENT_ORIGINAL_TASK_FILE="$CONTRACT_TASK" \
  "$MULTIAGENT" workflow init WF-CONTRACT >/dev/null
CONTRACT_SCOUT="$CONTRACT_STATE/subagents/contract-scout-01-widget"
mkdir -p "$CONTRACT_SCOUT"
printf '%s\n' 'role=scout' 'codex_access=read-only' 'workflow_id=WF-CONTRACT' \
  >"$CONTRACT_SCOUT/meta.env"
printf 'finalized\n' >"$CONTRACT_SCOUT/status"
printf '2026-08-17T00:00:00Z\n' >"$CONTRACT_SCOUT/finalized_at"
cat >"$CONTRACT_SCOUT/last-message.txt" <<'EOF'
contract-artifact: version=1
contract-rule: id=WIDGET-01 polarity=must statement=Widget stores configuration in named cfg evidence=public task
contract-rule: id=WIDGET-02 polarity=must-not statement=Widget must not anonymously embed LegacyConfig evidence=public task
unknowns: none
contract-ledger: migrate the internal shape
must-preserve: both rules
validation-plan: compile package tests
mismatch-risk: legacy aliases can hide an incomplete migration
implementation-routing: one bounded worker
EOF
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow contract-register \
  WF-CONTRACT --scout contract-scout-01-widget >/dev/null
CONTRACT_HASH="$(MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow value WF-CONTRACT contract_artifact_sha256)"

CONTRACT_REVIEWER="$CONTRACT_STATE/subagents/decision-authority-reviewer-contract"
mkdir -p "$CONTRACT_REVIEWER"
printf '%s\n' 'role=reviewer' 'codex_access=read-only' 'workflow_id=WF-CONTRACT' \
  >"$CONTRACT_REVIEWER/meta.env"
printf 'finalized\n' >"$CONTRACT_REVIEWER/status"
printf '2026-08-17T00:00:00Z\n' >"$CONTRACT_REVIEWER/finalized_at"
printf '%s\n' \
  'review-record: type=decision-authority verdict=pass diff=-' \
  "contract-review: artifact-sha256=$CONTRACT_HASH verdict=pass" \
  >"$CONTRACT_REVIEWER/last-message.txt"
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-CONTRACT AUTH-CONTRACT \
    --type decision-authority --verdict pass --evidence "contract preserved" \
    --reviewer decision-authority-reviewer-contract >/dev/null
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" decision init DEC-CONTRACT \
  --title "Contract plan" --owner orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" decision add-alternative DEC-CONTRACT \
  --plan-id PLAN-CONTRACT --summary "Apply the registered contract" \
  --proposed-by orchestrator >/dev/null
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" decision commit DEC-CONTRACT \
  --selected-plan PLAN-CONTRACT --reason "authority reviewer accepted the full artifact" >/dev/null
CONTRACT_CONTEXT="$TEST_TMP/contract-context.md"
printf '# Compressed context that omits the negative structural rule\n' >"$CONTRACT_CONTEXT"
if MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow prepare-implementation \
  WF-CONTRACT --decision-id DEC-CONTRACT --plan-id PLAN-CONTRACT --decision-revision 1 \
  --implementation-context "$CONTRACT_CONTEXT" --authority-review AUTH-CONTRACT \
  >"$TEST_TMP/contract-compression.out" 2>&1; then
  echo "expected compressed implementation context to be rejected" >&2
  exit 1
fi
assert_contains "$TEST_TMP/contract-compression.out" \
  "must contain the registered contract artifact"
printf 'contract-artifact-sha256=%s\n' "$CONTRACT_HASH" >"$CONTRACT_CONTEXT"
cat "$CONTRACT_SCOUT/last-message.txt" >>"$CONTRACT_CONTEXT"
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow prepare-implementation \
  WF-CONTRACT --decision-id DEC-CONTRACT --plan-id PLAN-CONTRACT --decision-revision 1 \
  --implementation-context "$CONTRACT_CONTEXT" --authority-review AUTH-CONTRACT >/dev/null
MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow transition \
  WF-CONTRACT implementation >/dev/null
printf '\nmutated\n' >>"$CONTRACT_SCOUT/last-message.txt"
if MULTIAGENT_STATE_DIR="$CONTRACT_STATE" "$MULTIAGENT" workflow gate WF-CONTRACT implementation \
  >"$TEST_TMP/contract-mutation.out" 2>&1; then
  echo "expected registered contract mutation to invalidate implementation" >&2
  exit 1
fi
assert_contains "$TEST_TMP/contract-mutation.out" "contract artifact changed after registration"

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
  if [[ "$review_type" == "technical" ]]; then
    reviewer_name="verifier-technical"
  else
    reviewer_name="reviewer-$review_type"
  fi
  reviewer_state="$LOOP_STATE/subagents/$reviewer_name"
  mkdir -p "$reviewer_state"
  printf '%s\n' 'role=reviewer' 'codex_access=read-only' >"$reviewer_state/meta.env"
  printf 'finalized\n' >"$reviewer_state/status"
  printf '2026-08-15T00:00:00Z\n' >"$reviewer_state/finalized_at"
  if [[ "$review_type" == "technical" ]]; then
    printf 'ACCEPTED\nreview-record: type=technical verdict=pass diff=DIFF-FINAL\n' \
      >"$reviewer_state/last-message.txt"
  else
    printf 'review-record: type=%s verdict=pass diff=DIFF-FINAL\n' "$review_type" \
      >"$reviewer_state/last-message.txt"
  fi
  loop record-review WF-LOOP "REVIEW-$review_type" \
    --type "$review_type" --verdict pass --diff-hash DIFF-FINAL \
    --evidence "$review_type passed" --reviewer "$reviewer_name" >/dev/null
done
SUPERSEDED_REVIEW_STATE="$TEST_TMP/superseded-review-state"
cp -R "$LOOP_STATE" "$SUPERSEDED_REVIEW_STATE"
for reviewer_name in reviewer-scope-gap reviewer-scope-recheck; do
  reviewer_state="$SUPERSEDED_REVIEW_STATE/subagents/$reviewer_name"
  mkdir -p "$reviewer_state"
  printf '%s\n' 'role=reviewer' 'codex_access=read-only' >"$reviewer_state/meta.env"
  printf 'finalized\n' >"$reviewer_state/status"
  printf '2026-08-15T00:00:00Z\n' >"$reviewer_state/finalized_at"
done
printf 'review-record: type=scope verdict=findings diff=DIFF-FINAL\n' \
  >"$SUPERSEDED_REVIEW_STATE/subagents/reviewer-scope-gap/last-message.txt"
MULTIAGENT_STATE_DIR="$SUPERSEDED_REVIEW_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-LOOP REVIEW-SCOPE-GAP \
    --type scope --verdict findings --diff-hash DIFF-FINAL \
    --evidence "scope reviewer requested additional validation" \
    --reviewer reviewer-scope-gap >/dev/null
printf 'review-record: type=scope verdict=pass diff=DIFF-FINAL\n' \
  >"$SUPERSEDED_REVIEW_STATE/subagents/reviewer-scope-recheck/last-message.txt"
MULTIAGENT_STATE_DIR="$SUPERSEDED_REVIEW_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-LOOP REVIEW-SCOPE-RECHECK \
    --type scope --verdict pass --diff-hash DIFF-FINAL \
    --evidence "independent recheck accepted the same diff after evidence collection" \
    --reviewer reviewer-scope-recheck >/dev/null
MULTIAGENT_STATE_DIR="$SUPERSEDED_REVIEW_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow completion-check WF-LOOP >/dev/null
FINDINGS_STATE="$TEST_TMP/findings-state"
cp -R "$LOOP_STATE" "$FINDINGS_STATE"
UNRECORDED_REVIEWER="$FINDINGS_STATE/subagents/reviewer-unrecorded-findings"
mkdir -p "$UNRECORDED_REVIEWER"
printf '%s\n' 'role=reviewer' 'codex_access=read-only' 'workflow_id=WF-LOOP' \
  >"$UNRECORDED_REVIEWER/meta.env"
printf 'finalized\n' >"$UNRECORDED_REVIEWER/status"
printf '2026-08-15T00:00:00Z\n' >"$UNRECORDED_REVIEWER/finalized_at"
printf 'review-record: type=technical verdict=findings diff=DIFF-FINAL\n' \
  >"$UNRECORDED_REVIEWER/last-message.txt"
if MULTIAGENT_STATE_DIR="$FINDINGS_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow completion-check WF-LOOP \
  >"$TEST_TMP/unrecorded-reviewer-findings.out" 2>&1; then
  echo "expected unrecorded reviewer findings to block completion" >&2
  exit 1
fi
assert_contains "$TEST_TMP/unrecorded-reviewer-findings.out" \
  "completion blocked by unrecorded reviewer findings: reviewer-unrecorded-findings:technical"
MULTIAGENT_STATE_DIR="$FINDINGS_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow record-review WF-LOOP REVIEW-UNRESOLVED \
    --type technical --verdict findings --diff-hash DIFF-FINAL \
    --evidence "reviewer found a source defect" \
    --reviewer reviewer-unrecorded-findings >/dev/null
if MULTIAGENT_STATE_DIR="$FINDINGS_STATE" MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" workflow completion-check WF-LOOP \
  >"$TEST_TMP/current-reviewer-findings.out" 2>&1; then
  echo "expected recorded current-diff findings to block completion" >&2
  exit 1
fi
assert_contains "$TEST_TMP/current-reviewer-findings.out" \
  "completion blocked by current-diff review findings: technical"
loop completion-check WF-LOOP >/dev/null
if loop transition WF-LOOP complete >"$TEST_TMP/direct-complete.out" 2>&1; then
  echo "expected direct lifecycle completion to be rejected" >&2
  exit 1
fi
assert_contains "$TEST_TMP/direct-complete.out" "complete is supervisor-owned"

BLOCKED_COMPLETE_STATE="$TEST_TMP/blocked-complete-state"
cp -R "$LOOP_STATE" "$BLOCKED_COMPLETE_STATE"
mkdir -p "$BLOCKED_COMPLETE_STATE/findings/BLOCK-COMPLETE"
printf '%s\n' 'severity=blocking' >"$BLOCKED_COMPLETE_STATE/findings/BLOCK-COMPLETE/finding.env"
if MULTIAGENT_ROOT="$TEST_REPO" MULTIAGENT_STATE_DIR="$BLOCKED_COMPLETE_STATE" \
  MULTIAGENT_WORKFLOW_ID=WF-LOOP MULTIAGENT_RUN_ID=RUN-BLOCKED-COMPLETE \
  MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" orchestrator complete >"$TEST_TMP/blocked-complete.out" 2>&1; then
  echo "expected supervisor completion to reject an unqueued blocking finding" >&2
  exit 1
fi
assert_contains "$TEST_TMP/blocked-complete.out" $'reject\tunqueued-blocking-finding\tfinding=BLOCK-COMPLETE'
assert_contains "$BLOCKED_COMPLETE_STATE/workflows/WF-LOOP/lifecycle/lifecycle.env" \
  "phase=post-implementation"

if ! MULTIAGENT_ROOT="$TEST_REPO" MULTIAGENT_STATE_DIR="$LOOP_STATE" \
  MULTIAGENT_WORKFLOW_ID=WF-LOOP MULTIAGENT_RUN_ID=RUN-LIFECYCLE \
  MULTIAGENT_LIFECYCLE_ENFORCEMENT=1 \
  "$MULTIAGENT" orchestrator complete >"$TEST_TMP/complete.out" 2>&1; then
  cat "$TEST_TMP/complete.out" >&2
  exit 1
fi
assert_contains "$TEST_TMP/complete.out" $'run completed\tRUN-LIFECYCLE'
assert_contains "$LOOP_STATE/workflows/WF-LOOP/lifecycle/lifecycle.env" "phase=complete"
assert_contains "$LOOP_STATE/workflows/WF-LOOP/lifecycle/events.log" "authority=supervisor"

echo "implementation lifecycle tests passed"
