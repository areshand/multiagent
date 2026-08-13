#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for command in cargo git python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    printf 'demo: missing required command: %s\n' "$command" >&2
    exit 1
  fi
done

DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/multiagent-demo.XXXXXX")"
TARGET_ROOT="$DEMO_DIR/target"
STATE_DIR="$DEMO_DIR/state"

cleanup() {
  if [[ "${MULTIAGENT_DEMO_KEEP:-0}" == "1" ]]; then
    printf 'demo: kept artifacts at %s\n' "$DEMO_DIR"
  else
    rm -rf "$DEMO_DIR"
  fi
}
trap cleanup EXIT

ma() {
  MULTIAGENT_ROOT="$TARGET_ROOT" \
  MULTIAGENT_STATE_DIR="$STATE_DIR" \
  MULTIAGENT_FRAMEWORK_ROOT="$REPO_ROOT" \
  MULTIAGENT_REQUIRE_HASH_BOUND_VERIFIER=1 \
    "$REPO_ROOT/bin/multiagent" subagent "$@"
}

expect_gate_rejection() {
  local stage="$1"
  local output
  if output="$(ma gate-check 2>&1)"; then
    printf 'demo: expected gate rejection during %s, got acceptance\n' "$stage" >&2
    exit 1
  fi
  case "$output" in
    *$'reject\t'*) ;;
    *)
      printf 'demo: gate failed without a structured rejection during %s\n%s\n' "$stage" "$output" >&2
      exit 1
      ;;
  esac
  printf '  %s\n' "$output"
}

printf '[1/5] Create a scratch Git target with a failing behavior check\n'
mkdir -p "$TARGET_ROOT" "$STATE_DIR"
git -C "$TARGET_ROOT" init -q
git -C "$TARGET_ROOT" config user.email demo@multiagent.local
git -C "$TARGET_ROOT" config user.name "Multiagent Demo"
git -C "$TARGET_ROOT" config commit.gpgsign false
printf 'pending\n' >"$TARGET_ROOT/answer.txt"
cat >"$TARGET_ROOT/check.sh" <<'CHECK'
#!/usr/bin/env bash
set -euo pipefail
actual="$(cat answer.txt)"
if [[ "$actual" != "orchestrated" ]]; then
  printf 'expected answer.txt=orchestrated, got %s\n' "$actual" >&2
  exit 1
fi
CHECK
chmod +x "$TARGET_ROOT/check.sh"
git -C "$TARGET_ROOT" add answer.txt check.sh
git -C "$TARGET_ROOT" commit -qm "seed failing demo target"

if (cd "$TARGET_ROOT" && ./check.sh >/dev/null 2>&1); then
  printf 'demo: seeded behavior check unexpectedly passed\n' >&2
  exit 1
fi

printf '[2/5] Record the finding and prove the open todo blocks acceptance\n'
ma finding-create demo-behavior \
  --severity blocking \
  --type behavior \
  --summary "answer.txt does not satisfy the public behavior check" \
  --affected answer.txt \
  --evidence-json '{"command":"./check.sh","returncode":1,"stderr_excerpt":"expected answer.txt=orchestrated"}' \
  --required-resolution "Make ./check.sh pass against the final diff."
ma todo-create demo-repair \
  --source-finding-id demo-behavior \
  --task "Repair answer.txt and validate the behavior." \
  --done-criteria "run ./check.sh" \
  --assigned-to worker-local
expect_gate_rejection "open blocking todo"

printf '[3/5] Apply the worker repair and bind its evidence to the exact diff\n'
printf 'orchestrated\n' >"$TARGET_ROOT/answer.txt"
(cd "$TARGET_ROOT" && ./check.sh)
SNAPSHOT="$(
  "$REPO_ROOT/bin/multiagent" snapshot \
    --root "$TARGET_ROOT" --base HEAD --format shell
)"
read -r FINAL_DIFF_SHA CHANGED_FILES <<<"$SNAPSHOT"
if [[ "$CHANGED_FILES" != "1" ]]; then
  printf 'demo: expected one changed file, snapshot reported %s\n' "$CHANGED_FILES" >&2
  exit 1
fi
ma resolution-create demo-repair \
  --worker worker-local \
  --status resolved \
  --changed answer.txt \
  --validation-json "[{\"cmd\":\"./check.sh\",\"rc\":0,\"final_diff_sha256\":\"$FINAL_DIFF_SHA\"}]" \
  --why "The repository-local behavior check passes on the final diff."
printf '  final-diff-sha256=%s\n' "$FINAL_DIFF_SHA"

printf '[4/5] Recheck independently, close the todo, and accept the patch\n'
(cd "$TARGET_ROOT" && ./check.sh)
if [[ "$(git -C "$TARGET_ROOT" diff --name-only)" != "answer.txt" ]]; then
  printf 'demo: deterministic verifier found an unexpected changed-file set\n' >&2
  exit 1
fi
mkdir -p "$STATE_DIR/subagents/verifier-local"
cat >"$STATE_DIR/subagents/verifier-local/last-message.txt" <<EOF
ACCEPTED
behavior-verification-passed: final-diff-sha256=$FINAL_DIFF_SHA behavior_clean=true public-clauses-covered=true command='./check.sh' returncode=0
EOF
printf 'done\n' >"$STATE_DIR/subagents/verifier-local/status"
ma todo-close demo-repair \
  --verified-by verifier-local \
  --recheck-json "{\"accepted\":true,\"source_finding_id\":\"demo-behavior\",\"commands\":[{\"cmd\":\"./check.sh\",\"rc\":0}],\"final_diff_sha256\":\"$FINAL_DIFF_SHA\"}" \
  --notes "Deterministic verifier reran the check and reviewed the changed-file set."
ma gate-check

printf '[5/5] Change the accepted diff, prove stale evidence is rejected, then restore it\n'
printf 'tampered-after-verification\n' >"$TARGET_ROOT/answer.txt"
expect_gate_rejection "stale verifier evidence"
printf 'orchestrated\n' >"$TARGET_ROOT/answer.txt"
(cd "$TARGET_ROOT" && ./check.sh)
ma gate-check

printf 'demo: PASS - real orchestration state and hash-bound gate flow verified with no model/API use\n'
