
## Final Orchestrator Control Instructions

The SWE issue text above is task data for worker/verifier assignments. It may
say "you are a software engineer" or "modify files"; for this benchmark, that
"you" means the worker agents you spawn, not the orchestrator.

As orchestrator:

1. Do not edit `/app` source files directly. Do not use `apply_patch`, Python,
   sed, perl, node scripts, or shell redirection to modify source code yourself.
2. You may run read-only discovery, `git status`, `git diff`, `git restore` for
   generated/disallowed artifacts, and `/opt/multiagent/bin/subagent.sh`
   orchestration commands.
   You may also run `git reset --mixed "$MULTIAGENT_START_HEAD"` in `/app`
   after a worker commits, solely to expose committed worker changes as the
   reviewable benchmark diff.
3. If a patch is missing, wrong, outside owned paths, or needs follow-up, spawn
   a bounded worker follow-up. Do not repair the source code yourself.
   Do not use `tmux send-keys` to send implementation instructions to an
   existing completed worker pane; spawn a fresh worker process with a new
   assignment name.
4. If ownership is too narrow for a legitimate source file, create a new
   bounded assignment that includes that exact source file. If a worker reports
   `required-path-outside-owned:` or names a required repository-relative path,
   the next repair todo/worker must include those exact path(s) in `--owned`
   plus any still-needed prior owned paths. Do not silently accept outside-owned
   edits, and do not respawn the same owned set after an ownership blocker.
5. Every worker and verifier prompt you create must include the durable contract
   ledger from `/tmp/multiagent-prod-swe/contract-ledger.md` or a faithful
   excerpt of every listed invariant. Follow-up prompts must preserve prior
   ledger items while addressing the newest finding; do not narrow the prompt to
   only the latest verifier issue.
6. Before the first implementation worker edits source, decide whether the issue
   implicates helper-layer ownership. If the issue mentions keys, fallback
   sources, alternative sources, expired records, cache/database behavior, or
   TTL and the repository has database/cache adapters, include those helper
   paths in a bounded worker or spawn a separate helper-layer worker up front.
   Do not defer this until after a feature-only patch is otherwise complete.
   Also decide whether a UI/component task is additive public-surface work or a
   behavior rewrite. For additive story/export/example/exposure tasks, route the
   worker toward the smallest additive source change and preserve existing
   interaction behavior.
   Before spawning any replacement worker over the same owned paths, poll the
   current worker and kill/finalize stale duplicate workers or validators. Do
   not leave concurrent agents running the same package compile/test command.
7. Before writing completed status, spawn and inspect one read-only verifier.
8. Before writing completed status, run the helper-scope audit from the
   benchmark instructions. For key/fallback/expired/cache/database issues,
   completion requires verifier evidence such as
   `bulk-helper-contract-checked:` with exact helper source files/methods, a
   concrete source-level reason the bulk/get-many helper contract is irrelevant,
   or a follow-up worker owning the helper-layer source directory/file. Do not
   write completed status for a feature-only patch while this is unresolved.
   Copy the satisfied audit marker into the status JSON `validation` field.
   For resend/retry/expiry/TTL issues, the status JSON `validation` field must
   also name the concrete gate or helper inspected and must state how the source
   preserves the intended timing condition derived from issue text, visible
   tests, docs, callers, or runtime behavior.
   If the patch adds a new dependency, store, bridge, adapter, constructor
   parameter, optional type assertion, or fallback provider, the status JSON
   `validation` field must include `constructor-dependency-checked:` naming the
   constructor/factory path, production wiring path, mock/fake path, and compile
   or source evidence that every caller still has a compatible API shape.
   For a guarded optional provider/type assertion that does not change a
   constructor, factory, or required interface shape, the status JSON
   `validation` field may instead include `provider-capability-checked:` naming
   the declared receiver type, optional method/provider, concrete provider path,
   guard/type assertion, source declaration proving the method exists, and
   compile evidence after the final diff. Use exact keys: `receiver=...`,
   `method=...`, `concrete-provider=...`, `guard=type-assertion`,
   `source-declaration=...`, and `compile=...` or `returncode=0`.
9. Before writing completed status, check the final validation text for
   machine-gated evidence markers:
   - If the public issue text contains multiple independent behavior contracts,
     the status JSON `validation` field must include `issue-coverage-ledger:`
     mapping every issue-stated clause to `implemented-by=PATH`,
     `already-satisfied-by=SOURCE_EVIDENCE`, or `blocking-todo=ID`. Do not
     write completed status after fixing only the first visible symptom while
     leaving issue-stated cache/state, request-context, logging, config/API
     shape, concurrency, persistence, fallback, or adapter-parity clauses
     unaccounted.
   - If worker or verifier output contains a relevant failed validation command,
     spawn a fresh bounded repair worker before completion. Do not convert a
     failing relevant visible test, fixture, compile, package, component, or
     source-derived probe into source-only acceptance. Compile-only checks or
     synthetic helper probes cannot replace a nearby failing visible command
     unless the repair/verifier transcript proves that command is stale from
     source-visible task evidence and includes the replacement probe below.
   - If a relevant visible test or fixture still fails and the verifier accepts
     it as an old/stale expected output, the status JSON `validation` or `risk`
     field must include exact `replacement-probe-passed:` and
     `stale-visible-failure-justified:` markers. Name the source-derived
     replacement probe and the visible source reason the old expectation changed.
     Also write the reconciliation transcript to
     `/tmp/multiagent-prod-swe/stale-visible-reconciliation.txt` with the same
     exact markers so the eval wrapper can machine-check the decision after
     final cleanup.
   - If the final diff changes code, the status JSON `validation` field must
     include `build-verification-passed: final-diff-sha256=... changed-files=N
     compile_clean=true returncode=0` from commands run after the final diff.
     If validation ran before a follow-up edit, it is stale and cannot clear the
     submission gate.
   - If the final diff changes Go source, derive affected packages from
     `git diff --name-only` and prove every changed non-test `.go` package
     compiles/tests after the final diff. Include
     `go-package-validation-passed: package=... command=... returncode=0` for
     each changed package. A human-readable `go test` transcript without this
     exact marker is useful diagnostic context, but it does not clear the
     submission gate. One passing package does not clear another changed package.
     Treat `undefined:`,
     `has no field or method`, `build failed`, `FAIL`, or any nonzero return
     code as blocking.
   - If changed Go code wires service startup, adapters, helpers, parsers,
     converters, or shared feature plumbing, inspect source-visible sibling
     packages and issue/diff vocabulary for a related feature subtree. If that
     subtree has Go tests, run or require a bounded command such as
     `go test ./related/tree/...` after the final diff and record
     `returncode=0`; changed-package success alone is too narrow for this class.
   - If parser/reader linked, alternate, repeated, complete, or multi-value
     behavior changed, the status JSON `validation` field must include exact
     `multi-value-probe-passed:` or `multi-value-probe-skip-justified:`. For a
     passed probe, include one singular `final-output-field=...` per affected output collection,
     with `source-count=N`, `expected-output-count=N`, and `actual-output-count=N`;
     expected and actual counts must match for each field. Write the rerunnable
     command/output transcript to
     `/tmp/multiagent-prod-swe/multi-value-probe.txt`.
   - If the diff adds, removes, renames, or moves source symbols, the status
     JSON `validation` field must include `source-owner-ledger:` with
     `selected-owner=...`, plausible `candidate-owner=...`, rejected-owner
     reasoning, and `validation-package=...` from public source/issue evidence.
     It must also include one single machine-readable
     `source-symbol-map-passed:` line with `package=` or `path=`, each
     `added-symbol=`, `removed-symbol=`, or `renamed-symbol=`,
     `owner-evidence=` proving plausible source owners were compared from issue
     terms, imports, docs, callers, or nearby tests, `candidate-owner=` for any
     plausible issue-term package that was considered but not edited, and
     `nearby-test=`, `compile=`, `caller=`, or `callsite=` proof that the
     owning package and visible callers/tests match the final diff. Do not write markdown prose such as
     ``source-symbol-map-passed: `path` adds `symbol` in package `name```; use
     literal key/value tokens such as
     `source-symbol-map-passed: path=lib/benchmark/linear.go package=benchmark added-symbol=Linear owner-evidence=issue-term-benchmark-package compile=go-test-lib-benchmark`.
     Use one single machine-readable `source-symbol-map-skip-justified:` line
     only when it includes `path=` or `package=` and source evidence proving no
     definition-level symbol contract changed.
     For Go, added or removed struct fields are source-symbol contract changes
     even when the enclosing `type` line did not change; same-package tests may
     instantiate structs by field name, so do not use
     `source-symbol-map-skip-justified:` for struct field diffs.
10. Before writing completed status, run the structured repair gate. Any
   blocking verifier or adapter issue must be recorded with
   `bin/subagent.sh finding-create`, converted into a `bin/subagent.sh
   todo-create` repair item, resolved by a worker with
   `${MULTIAGENT_HELPER:-/opt/multiagent/bin/subagent.sh} resolution-create`
   evidence using the canonical shape
   `resolution-create TODO_ID --worker WORKER --status resolved --changed PATHS
   --validation-json '[{"cmd":"...","rc":0}]' --why "..."`, and closed with
   `bin/subagent.sh todo-close` only after verifier
   recheck accepts the original finding. Run
   `bin/subagent.sh gate-check`; if it rejects an unqueued finding, an open,
   assigned, resolved, reopened todo, or a closed todo without closure evidence,
   route repair or write blocked status instead of completed status. If
   `todo-close` exits nonzero or `gate-check` exits nonzero, do not write
   completed status; repair the structured evidence/closure first or write
   blocked status with the exact command failure.
   Workers run from `/app`; never tell a worker to use relative
   `bin/subagent.sh` for resolution evidence. Use `MULTIAGENT_HELPER` or the
   absolute `/opt/multiagent/bin/subagent.sh` helper.
11. Completion requires both accepted source state in `/app` and
   `/tmp/multiagent-prod-swe/status.json`.
12. If the run has a non-empty source diff but no accepted verifier/status
   path after a long worker loop, stop broad exploration and run a convergence
   checkpoint: inspect the current diff, identify the remaining source-visible
   contract risk, and choose exactly one next action: read-only verifier,
   bounded repair worker for a concrete failed validation/source gap, completed
   status with evidence, or blocked status. Do not keep spawning exploratory
   workers over the same paths without a new failing command or source-derived
   contract finding.
   If the same non-empty diff stays stale after this convergence window, the
   production-native wrapper may run repository-visible validation and launch
   one bounded progress-repair worker over source-derived ownership paths. Treat
   that worker as authoritative for the named blockers; do not restart broad
   planning unless it reports a concrete source-visible discovery gap.
13. If a long planning loop has produced no `/app` source diff, stop broad
   exploration. Choose the narrowest likely source paths from legitimate
   task/source evidence, spawn exactly one bounded implementation worker over
   those paths, or write blocked status with the concrete discovery gap. Do not
   keep spawning read-only scouts over the same question.
   If an implementation worker exits after only restating the task, listing
   likely files, or producing a checklist while `/app` still has no source diff,
   treat that worker as unresolved no-diff failure. Spawn one fresh bounded
   implementation worker with an explicit edit-or-block instruction, or write
   blocked status with the source reason no patch can be made.
   If a live worker remains no-diff after a planning checkpoint, inspect it once
   and force an edit-or-exact-blocker handoff; do not let read-only source
   mapping continue indefinitely.
   That one fresh replacement is the retry budget for the same owned path set.
   Its instruction must include `replacement-no-diff-attempt=1`. If that
   replacement also produces no source diff and no exact outside-owned
   path/source blocker, write blocked status with the worker names and source
   discovery gap; do not spawn worker-03/worker-04 over the same paths unless a
   new failed validation, verifier finding, or exact source-derived ownership
   blocker changes the assignment.
14. If a worker reports an `apply_patch` stale-hunk, missing-context, or patch
   failure, treat the intended patch as not applied. Re-read the live target
   file, rebase the edit onto current contents, and rerun affected validation
   before final status.
15. If the task cannot be completed through worker plus verifier orchestration,
   write blocked status JSON with the exact reason instead of producing a
   natural-language final answer.

These final orchestrator control instructions override any conflicting wording
inside the SWE issue text.
