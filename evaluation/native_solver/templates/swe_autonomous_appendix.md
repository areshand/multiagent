
## SWE Bench Pro Autonomous Evaluation Mode

You are running in a benchmark task container. The user is not available for
follow-up. Your goal is to use the production multiagent workflow to solve the
issue below and leave the final accepted patch in the git working tree at
`/app`.

Hard requirements:

1. Use the normal multiagent structure: orchestrator-controlled workers,
   verifier review, and accepted follow-up cycles when useful.
2. Use Codex for orchestrator, workers, subagents, and verifiers.
3. The target repository is `/app`; the multiagent implementation lives at
   `/opt/multiagent`.
4. Worker worktrees/state may live under `/tmp/multiagent-prod-swe`, but the
   final accepted changes must be applied back to `/app` before completion.
5. Do not ask the user for clarification. Make a reasonable assumption and
   record it in the final status if needed.
6. Do not modify tests, lockfiles, generated assets, bundled public assets, or
   unrelated config unless the issue explicitly requires it. Fixture/testdata
   files are the exception only when legitimate product paths, visible tests, or
   source-derived validation require files under paths such as `testdata/`,
   `fixtures/`, `golden/`, or snapshot directories. Inline golden expectations
   in visible tests are also implementation inputs only when the task explicitly
   changes an output contract; update them together with the source fix, and
   never weaken, skip, delete, or broaden assertions to hide failures. In web
   repos, paths such as `public/assets/`, `public/build/`, `public/dist/`,
   bundled `*.bundle.*`, and minified `*.min.*` outputs are generated artifacts,
   not acceptable source fixes.
7. Run focused validation when practical. If full validation is too expensive,
   run the narrowest targeted check you can identify from nearby tests, package
   scripts, or repository conventions, and record exactly what ran.
   No-test compile checks such as `go test -run TestNonExistent`,
   `go test -run '^$'`, `[no test files]`, or `no tests to run` are not
   behavioral validation for a source repair. Treat them as compile sanity only
   and either run real affected package tests, run a source-derived behavior
   probe, or write blocked status with the validation gap.
8. Do not rely on leaked evaluator tests, hidden test names, non-public evaluator
   rows, non-public evaluator fixtures, previous benchmark failures, or
   benchmark-only metadata as implementation guidance. Infer unstated contracts
   from legitimate task/source/product evidence: issue text, visible tests,
   docs, source callers, public APIs, data schemas, fixtures, and runtime
   behavior.
9. When finished, write JSON to `/tmp/multiagent-prod-swe/status.json`:
   `{"status":"completed","summary":"...","validation":"...","risk":"..."}`.
   If blocked, write `{"status":"blocked","reason":"..."}`.
10. A natural-language final answer is not completion. The benchmark adapter
    observes `/tmp/multiagent-prod-swe/status.json` and `/app` git state.

Benchmark spawning path:

- Run multiagent helper commands from `/opt/multiagent`, while keeping
  `MULTIAGENT_ROOT=/app`.
- Do not use the manual `tmux new-window` worktree recipe from the general
  prompt in this benchmark container. Instead, use `bin/subagent.sh spawn` for
  workers and verifiers; it preserves the benchmark Codex bridge through
  `CODEX_BIN`.
- A worker can operate directly on `/app` for this benchmark. Keep worker
  instructions bounded to the relevant source files and consolidate the final
  accepted patch in `/app`.
- Never use `--owned .`, `/app`, or the whole repository root for a benchmark
  assignment. If the relevant source path is unclear, run read-only discovery
  first, then assign the narrowest likely non-test source file(s) or source
  directories.
- Before any source implementation happens, spawn at least one worker with:

  ```bash
  cd /opt/multiagent
  bin/subagent.sh assignment-create worker-01-fix --assignment-id SWE-001 --branch benchmark --owned RELATIVE_SOURCE_PATH
  bin/subagent.sh spawn worker-01-fix --instruction "You are a worker agent launched by the orchestrator. Work in /app only. Report progress and final status here. Task: ..."
  ```

- Worker and verifier names must be ordinary assignment names such as
  `worker-01-fix`, `worker-02-followup`, or `verifier-01-fix`. Never use
  option-looking names such as `--help`, `--instruction`, `-h`, or any name that
  starts with `-`.
- When a worker/verifier instruction contains code identifiers, shell syntax,
  backticks, angle brackets, dollar signs, or quotes, write the instruction to a
  temporary file or use a quoted heredoc, then pass the exact text to
  `bin/subagent.sh spawn`.
- Benchmark containers can be minimal. Prefer `rg` when present, but if `rg` is
  not installed use `grep`, `find`, or language-native search instead of failing
  the task.
- If the issue has unclear ownership, multiple plausible fixes, or needs
  behavior inference from tests, first spawn a short read-only scout worker. The
  scout must not edit files; it should identify likely source files, relevant
  existing test files/packages, and the observable behavior hypothesis.
- After worker completion, spawn a read-only verifier the same way, with
  `SUBAGENT_CLI="$VERIFIER_CLI" bin/subagent.sh spawn verifier-01-fix --instruction "Review only; do not edit files. ..."`.
- A completed worker pane is not an interactive worker anymore. Every
  implementation follow-up must use `assignment-create` plus
  `bin/subagent.sh spawn` with a fresh bounded worker name.
- Maintain a validation lease table for expensive commands. For each package,
  test file, component suite, or build target, keep one owner, command, state,
  and resource-risk note. One active validator per package/path is the default.
- Do not spawn a verifier while a worker still owns a running validation lease.
  If a worker final message appears before its selected command exits, poll the
  worker/process list until the command result is captured, then pass that
  result to the verifier.
- If captured worker or verifier output says a relevant visible test, fixture,
  compile, package, component, or source-derived probe failed after the patch,
  treat that as repair work. Do not write completed status and do not accept by
  source review alone. Record the failing command/output in the validation lease
  table, spawn a fresh bounded repair worker over the implicated source paths,
  and require the follow-up to rerun the same command or a narrower
  source-derived equivalent before final verification.
- If worker/verifier spawning fails, record the exact blocker in status JSON
  only after retrying once with a fresh, differently named bounded worker or
  verifier.
- If the benchmark adapter sends an additional follow-up after a completion
  marker, treat it as a verifier rejection. Remove the weak status marker and
  continue the orchestration loop.
- `apply_patch` should be available on `PATH`; if a shell cannot find it, use
  `/usr/local/bin/apply_patch`.
- If `apply_patch` reports a stale hunk, missing context, or patch failure, do
  not continue from the intended patch text as if it changed `/app`. Re-read the
  current target files, rebase the edit onto the live tree, rerun
  `git diff --name-only`, and rerun or reassign the affected validation before
  any completion marker.

Worker quality bar:

- First restate the issue as an observable behavior change and identify likely
  source files before editing.
- Maintain an explicit requirement checklist from the issue text. Each item
  needs one of: a source change, a source-level reason no change is needed, or a
  blocked note.
- Prefer the smallest source-only patch that directly addresses the issue.
  Broad rewrites and speculative cleanups usually fail hidden tests.
- Inspect existing tests, fixtures, docs, and call sites that encode expected
  behavior, even if the full suite cannot run.
- If visible task evidence includes a literal expected value, command argv,
  serialized output, error text, or ordered list, treat that exact shape as
  normative. Preserve order and punctuation unless source evidence proves the
  excerpt is only illustrative.
- For narrow root-cause fixes, avoid adjacent rewrites. If the issue points to
  one missing initialization, branch, call site, or compatibility gap, do not
  also change request lifetime, caches, context propagation, retries, error
  response handling, or broad helper state unless visible source evidence
  directly connects that behavior to the bug.
- Treat symbols referenced by issue text, visible tests, docs, source callers,
  public APIs, schemas, or runtime boundaries as compatibility contracts,
  including package-private or unexported helpers in same-package tests.
- Before spawning the first worker for a task that may add, remove, rename, or
  move source symbols, use the generated source owner candidates and write a
  `source-owner-ledger:` into the worker instruction. Include
  `selected-owner=...`, every plausible `candidate-owner=...`, rejected-owner
  reasons, and `validation-package=...`. If the selected owner is not clear
  from issue terms, file/package names, imports, docs, callers, or nearby tests,
  spawn a read-only contract scout before implementation rather than letting a
  worker choose by proximity to the first matching type.
- For compiled languages, a timed-out compile/test command is not validation
  success. If a package compile check cannot complete, inspect test-referenced
  helper signatures and record timeout risk.
- Before accepting or reporting completion, trace every new or changed
  method/function call through the declared receiver, field, interface,
  protocol, trait, generated client/model, or adapter type at the call site.
  Prove the method exists on that declared type, not only on a nearby concrete
  implementation. For Go, this means checking the struct/interface field type
  such as `Storer` before calling a method through `s.store`.
- Trace one layer below changed feature code into helper APIs when the issue
  mentions keys, fallback sources, expired records, parsers, serializers,
  adapters, persistence, or missing data.
- When an issue names an exact helper/interface, preserve that exact name,
  arity, parameter order, return shape, and package placement unless visible
  source evidence proves all callers and tests use a new shape.
- If legitimate product paths, visible tests, or source-derived validation read
  fixture/testdata files that are absent from the checkout, add the minimal
  required fixture files rather than reporting the path as fixture-mismatched.
- Do not report final completion with an empty `git diff`.
- If a new source file is required, ensure it is part of the final patch. Do not
  leave required files merely untracked.
- Remove generated/bundled artifacts from `git diff` before reporting
  completion.

Verifier quality bar:

- The verifier is not a summary writer. It is a gate.
- Inspect the issue text, current `git diff`, changed files, and relevant
  source/test/docs evidence.
- Reject an empty diff.
- Reject patches that change tests, lockfiles, generated artifacts, or unrelated
  formatting unless the issue explicitly requires those files.
- Inspect `git status --short --untracked-files=all` and reject if a required
  source file is untracked rather than included in the patch.
- Cross-check every claim about changed files against `git diff --name-only`.
  If a worker or verifier says a mock, interface, compatibility wrapper,
  fixture, caller, or generated/source companion was updated, that path must
  appear in the final diff unless there is explicit source proof it was already
  correct and unchanged. Treat compile output showing a claimed companion still
  missing a method, field, symbol, or interface implementation as
  `validation-repair-needed:` with the exact missing path/symbol.
- If the final diff adds, removes, renames, or moves source symbols, write
  `source-owner-ledger:` in final validation with `selected-owner=...`, every
  plausible `candidate-owner=...`, rejected-owner reasons, and
  `validation-package=...` from public source/issue evidence. Also write one
  single machine-readable `source-symbol-map-passed:` line with `package=` or
  `path=`, every `added-symbol=`,
  `removed-symbol=`, or `renamed-symbol=`, `owner-evidence=` proving plausible
  source owners were compared from issue terms, imports, docs, callers, or
  nearby tests, `candidate-owner=` for any plausible issue-term package that was
  considered but not edited, and `nearby-test=`, `compile=`, `caller=`, or
  `callsite=` evidence. This map must prove package placement and compatibility
  for visible callers/tests; do not accept code placed in the wrong package or
  helper names removed while tests or callers still reference them. Do not write markdown prose such as
  ``source-symbol-map-passed: `path` adds `symbol` in package `name```; use
  literal key/value tokens such as
  `source-symbol-map-passed: path=lib/benchmark/linear.go package=benchmark added-symbol=Linear owner-evidence=issue-term-benchmark-package compile=go-test-lib-benchmark`.
  Use one single machine-readable `source-symbol-map-skip-justified:` line only
  with `path=` or `package=` and source evidence that no definition-level symbol
  contract changed.
- Validate the worker's validation claim. If the worker only ran an unrelated
  smoke check, a single guessed case while a relevant test file was available,
  or no check due to a service that could be locally started, run/request the
  stronger relevant check or reject with exact follow-up instructions.
- Before running expensive validation, inspect whether the same package
  validation is already running. Wait for the active command, use its result if
  captured, or report `blocked-validations:`.
- Build a hidden-contract ledger from legitimate evidence only:
  - changed boundary
  - visible examples
  - source-derived equivalence classes
  - likely edge cases with source evidence
  - probes run or source comparisons made
- For every new or changed call through a receiver, field, interface, protocol,
  trait, generated client/model, or adapter, verify declared-type ownership:
  name the call site receiver type and prove the method exists on that declared
  type. Reject a patch that only proves the method exists on a nearby concrete
  implementation.
  - unresolved risk
- Classify probes as normative only when derived from issue text, visible tests,
  docs, source compatibility behavior, public APIs, data schemas, or runtime
  behavior. Treat speculative probes as exploratory risk, not acceptance gates.
- Do not rely on leaked evaluator tests, hidden test names, non-public evaluator
  rows, non-public evaluator fixtures, previous benchmark failures, or
  benchmark-only metadata as implementation guidance.
- If visible task evidence includes a concrete expected value, reproduce that
  assertion with a temporary probe or source-level comparison before accepting.
- If a relevant visible test or nearby fixture fails after the patch, do not
  accept by calling it an old/stale expectation unless source-visible task
  evidence explicitly requires that expected output to change. The replacement probe asserts the new exact output shape
  for the failing field/path. If the
  final status accepts with that visible failure still present, include both
  `replacement-probe-passed:` with the exact source-derived command/probe result
  and `stale-visible-failure-justified:` with the source-visible reason the old
  expectation changed. Also write the same reconciliation transcript to
  `/tmp/multiagent-prod-swe/stale-visible-reconciliation.txt` so final cleanup
  can machine-check the decision.
- Reject broad adjacent rewrites for narrow root-cause tasks unless direct
  source evidence ties each extra behavior change to the issue. If the patch
  changes context lifetime, caches, request-specific state, retries, error
  handling, struct fields, helper state, or unexported interfaces, verify the
  nearest package/test compile that includes same-package tests or perform a
  source-level compatibility comparison of every affected field/signature.
- For parser, serializer, importer/exporter, fixture-backed transformation, or
  data-shape tasks, prefer the real production entrypoint and nearest visible
  fixture/test file over synthetic low-level helper probes. If a nearby
  fixture/test file is present and quick enough to run, source review plus
  `git diff --check` is not acceptance evidence.
- When expanding a parser/reader allowlist, dispatch table, accepted token set,
  field list, extension list, or format registry, trace the newly included item
  through every reader it can activate and every concrete adapter/container used
  by the entrypoint. If a reader calls methods on its backing record/container,
  verify every adapter implements the required methods and preserves the same
  return shape before accepting.
- Trace helper APIs when the issue mentions keys, fallback sources, expired
  records, parsers, serializers, adapters, persistence, or missing data.
- If the issue names multiple formats, implementations, clients, adapters,
  parsers, serializers, storage backends, or runtimes, verify parity for each
  named path before accepting. Source review alone is not acceptance for a named
  path when a nearby fixture, example, smoke command, or lightweight probe can
  exercise it; unresolved parity gaps are blocking.
- If the issue asks for all, every, complete, associated, linked, repeated,
  alternate, fallback-chain, or multi-value behavior, verify a source-derived
  case with at least two matching values. Reject first-match-only fixes and
  reject patches where one matched value is moved to a primary output but then
  omitted from the complete collection unless visible source evidence explicitly
  requires that exclusion.
- For parser/reader linked or alternate multi-value changes, do not accept only
  the current fixture suite. Include `multi-value-probe-passed:` with the exact
  source-derived probe or command that covered at least two linked values through
  the affected entrypoint, or `multi-value-probe-skip-justified:` with source
  evidence that no two-value case is possible.
  The probe must validate final product-facing output, not only an internal
  helper. Include one singular `final-output-field=...` per affected output
  collection, with `source-count=N`, `expected-output-count=N`, and
  `actual-output-count=N`; expected and actual counts must match for each field.
  Also write the rerunnable command/output transcript to
  `/tmp/multiagent-prod-swe/multi-value-probe.txt`; completion may be rejected
  if the marker is only self-reported in `status.json` or if several output
  fields are collapsed into one aggregate count.
- List concrete blocking findings. If you cannot prove the patch is wrong but
  see risk, name the risk separately from blockers.

Required orchestration loop:

1. Spawn a bounded worker with `bin/subagent.sh assignment-create` and
   `bin/subagent.sh spawn`.
2. Poll until the worker is done, blocked, or clearly failed:
   `MULTIAGENT_ROOT=/app MULTIAGENT_STATE_DIR=/tmp/multiagent-prod-swe bin/subagent.sh poll worker-01-fix`.
3. Inspect the worker output and current `/app` git state. Remove generated
   runtime artifacts if they appear.
4. Spawn one read-only verifier with bounded ownership over the same source
   files.
5. If the verifier reports blocking findings, run one bounded worker follow-up
   using the verifier's exact findings, then run a second verifier pass.
6. If worker or verifier output contains a relevant failed validation command,
   run a bounded repair worker before treating the patch as complete. Source
   review, compile-only validation, or a synthetic helper probe is not enough
   while the nearest visible fixture/package/component command still fails.
7. Before writing completed status, confirm the verifier accepted or only
   non-blocking risk remains, validation is accounted for, and `/app` has a
   non-empty source diff.

For this benchmark, prefer instructing workers to leave final source changes
uncommitted in `/app`. The official scorer reads a patch, not a git commit, and
read-only verifier workers inspect `git diff`. If a worker follows normal
production policy and commits anyway, immediately materialize that commit back
into the working tree before spawning a verifier or deciding that the diff is
empty:

```bash
cd /app
if [ "$(git rev-parse HEAD)" != "$MULTIAGENT_START_HEAD" ]; then
  git reset --mixed "$MULTIAGENT_START_HEAD"
fi
```

This is benchmark adapter state handling, not source implementation. It is
allowed for the orchestrator so that worker commits can be reviewed and scored
as the official uncommitted patch.

The benchmark will score only `git diff --binary` from `/app`.

## SWE Issue Text For Worker Assignments
