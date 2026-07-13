# SWE Bench Pro Production Multi-Agent First 50 Summary

Date: 2026-07-03

Scope: first 50 official-order SWE Bench Pro rows, evaluated with the
production-container native multi-agent path.

Result: 33/50 rows passed with official verifier evidence.

Passing official indices:

```text
0, 1, 3, 4, 5, 6, 7, 9, 10, 11, 13, 19, 21, 22, 23, 24, 25, 26, 29, 30,
31, 32, 33, 34, 35, 36, 39, 40, 43, 45, 46, 47, 49
```

Missing official indices:

```text
2, 8, 12, 14, 15, 16, 17, 18, 20, 27, 28, 37, 38, 41, 42, 44, 48
```

The 30/50 to 31/50 increment came from row 39:

- Instance: `instance_future-architect__vuls-86b60e1478e44d28b1aff6b9ac7e95ceb05bc5fc`
- Repository: `future-architect/vuls`
- Failing official test: `TestHosts`
- Final focused run prefix:
  `swe-bench-pro-prod-multiagent-v135-vuls-hosts-official-testpatch-offset39-count1`
- Focused run score: `1.0`
- Official verifier evidence: `true`

Key correction for row 39: the public-contract probe now covers literal IP
ignore semantics, including:

```text
hosts("127.0.0.1", []string{"127.0.0.1"}) -> []
```

This fixed the previous official failure where the solver returned
`["127.0.0.1"]` for that hidden contract case.

The 31/50 to 32/50 increment came from row 5:

- Instance:
  `instance_ansible__ansible-a26c325bd8f6e2822d9d7e62f77a424c1db4fbf6-v0f01c69f1e2528b935359cfe578530722bca2c59`
- Repository: `ansible/ansible`
- Passing official tests:
  `test/units/module_utils/urls/test_Request.py` and
  `test/units/module_utils/urls/test_fetch_url.py` selected cases
- Final focused run prefix:
  `swe-bench-pro-prod-pr4-slimbake-offset5-count1-r1`
- Focused run score: `1.0`
- Official verifier evidence: `true`

Key correction for row 5: the production multi-agent solver added source-only
`use_netrc` support through `uri`, `fetch_url`, `open_url`, and `Request`, with
the default preserving existing netrc behavior. When `use_netrc=false`, netrc
credentials are ignored and explicit `Authorization` headers are preserved.

The 32/50 to 33/50 increment came from row 32:

- Instance: `instance_navidrome__navidrome-7b394fe9c3725c90d1a1518c45b943d4e155e7d9`
- Repository: `navidrome/navidrome`
- Final focused run prefix:
  `swe-bench-pro-prod-pr4-parallel4-offset32-r1`
- Focused run score: `1.0`
- Official verifier evidence: `true`

Key correction for row 32: the production native solver updated Navidrome
artist refresh logic and passed focused `go test ./model ./persistence`
validation before official scoring. The aggregate remains below the >70%
target; reaching 36/50 requires at least three more clean production-native
passes, not diagnostic scoring of rejected diffs.

Important caveat: this score is only meaningful for the production native
multi-agent path because the solver repo is baked into the task image and Codex
auth is mounted at runtime. Earlier scaffold or single-runner results were
infrastructure checks, not clean measurements of production multi-agent
capability.

## 2026-07-10 No-Leak Audit Update

The production native path was audited for benchmark/fix leakage after rerunning
missing row 16 (`swe-bench-pro-prod-pr4-noleak-offset16-count1-r2`). The live
task container metadata visible to the solver was sanitized to `{}` and no row
identity, official expected tests, selected test files, test patch, or private
requirements were injected into the solver prompt. The row reached the official
verifier with native solver exit code 0, but scored `0.0`; at that point the
score remained 31/50.

The row 16 failure exposed a general verifier weakness, not a reason to leak
official expected tests: the verifier accepted a MARC XML/Binary parser parity
patch while treating one named format path as source-reviewed residual risk.
Verifier and contract-scout prompts now require source-derived parity checks for
every named format/implementation/parser/serializer path, or a blocking finding
when a representative fixture, smoke command, probe, or source-level comparison
is missing. This keeps hidden-contract coverage based on issue text, visible
tests, docs, source callers, schemas, and runtime behavior only.

Follow-up rerun `swe-bench-pro-prod-pr4-noleak-offset16-count1-r3` also reached
the official verifier with native solver exit code 0 and solver-visible
metadata sanitized to `{}`, but still scored `0.0`. The stronger verifier did
force a source-derived follow-up for XML/Binary parity and caught a visible-test
regression before finalization. The remaining official failure was still a
complete-collection miss: linked alternate title values were not all represented
in `other_titles` when one linked value was used as a primary title-compatible
value. The verifier/contract prompts now also reject first-match-only fixes for
tasks asking for all/every/complete/associated/linked/repeated/alternate or
multi-value behavior, requiring a source-derived probe with at least two
matching values and evidence that every value appears in the expected output
shape.

Follow-up rerun `swe-bench-pro-prod-pr4-noleak-offset16-count1-r4` again used
the production-native multi-agent path with solver-visible metadata sanitized to
`{}` and reached the official verifier, but still scored `0.0`. The patch had
real source changes and no benchmark metadata leak. The remaining failure showed
another general verifier issue: it accepted relevant local test failures as
old/stale expectations without forcing an exact replacement probe for the new
source-derived output shape. Verifier, contract-scout, and autonomous SWE
prompts now treat relevant failing visible tests/fixtures as blockers unless
source-visible task evidence explicitly requires the expected output to change
and a replacement probe asserts the new exact failing field/path behavior.

No-leak hardening: the production solver no longer contains the disabled
official expected-test prompt path. The removed code could previously build
prompt text or recovered validation from `FAIL_TO_PASS`, selected test files,
or `test_patch` if re-enabled by a future edit. Production solving now keeps
official expected-test metadata out of prompt assembly and out of adapter
completion recovery; expected-test metadata remains verifier-side only.

Follow-up rerun `swe-bench-pro-prod-pr4-noleak-offset16-count1-r5` used the
same production-native no-leak path and solver-visible metadata remained `{}`.
It still scored `0.0`, but the new verifier rules changed behavior in the
intended direction: the first verifier rejected the patch after attempting a
source-derived XML/Binary parity probe, and a follow-up worker added XML-path
changes. The official result improved one previous fixture failure
(`880_alternate_script.mrc` passed) but still failed `nybc200247` and
`880_arabic_french_many_linkages.mrc`.

The general r5 lesson is that source review and synthetic helper probes are too
weak for parser/serializer/importer/exporter or fixture-backed data-shape
changes when a real nearby fixture test or production entrypoint is visible and
cheap to run. Verifier, contract-scout, and autonomous SWE prompts now require
the nearest visible fixture/test file or real production entrypoint when
practical; `git diff --check` plus source review is not acceptance evidence for
those task classes.

Attempted rerun `swe-bench-pro-prod-pr4-noleak-offset16-count1-r6` was
interrupted and is not score evidence. The first worker exited without a patch
after reporting conflicting instructions, and the orchestrator remained idle
with no status marker or source diff. Follow-up hardening changed the durable
ledger wording copied into worker prompts from verifier-only "acceptance"
language to role-neutral invariant language.

Attempted missing-row rerun `swe-bench-pro-prod-pr4-noleak-offset2-count1-r1`
was also interrupted and is not score evidence. The worker produced a non-empty
NodeBB route diff, but exited without a final message; the orchestrator then
remained idle with no status marker and no verifier window. This exposed a
general wrapper recovery gap: the production solver handled orchestrator exits
after coverage follow-up, but not the earlier state where no live agent remains,
a source diff exists, and no completion status was written. The wrapper now
runs the same adapter blocker/probe path for this orphaned-diff state, spawns a
recovery helper when blockers remain, blocks unsafe diffs, or recovers a
completion marker only when generic public checks are clean.

Follow-up missing-row rerun
`swe-bench-pro-prod-pr4-noleak-offset2-count1-r2` completed through the real
production-native multi-agent path and reached the official verifier with native
solver exit code 0. The row scored `0.0`, so at that point the first-50 score
remained 31/50.
The run is useful no-leak evidence: the task container's solver-visible
metadata was `{}`, and direct prompt/ledger inspection found no
`FAIL_TO_PASS`, `PASS_TO_PASS`, `test_patch`, selected-test, row-identity,
instance-id, score, or previous-failure strings before solving.

Additional no-leak hardening from this audit: production-facing verifier and
contract-scout prompts no longer say leaked evaluator facts may be used as
"post-hoc diagnostics" during active solving. They now explicitly prohibit
benchmark scores or hidden-test failures from being fed into verifier input,
follow-up instructions, worker requirements, or acceptance evidence. Tests also
assert that production-facing prompts do not contain expected-test accounting
tokens such as `FAIL_TO_PASS`, `PASS_TO_PASS`, or `test_patch`.

Follow-up missing-row reruns
`swe-bench-pro-prod-pr4-noleak-offset15-count1-r1` and
`swe-bench-pro-prod-pr4-noleak-offset15-count1-r2` both completed through the
real production-native multi-agent path, reached the official verifier with
native solver exit code 0, and scored `0.0`. At that point the first-50 score
remained 31/50.
Both runs preserved the no-leak metadata boundary: solver-visible task metadata
was `{}` and prompt/ledger inspection found no row identity, selected official
tests, test patch, benchmark score, or previous-failure strings.

The row 15 retries exposed a general contract-validation weakness for
parser/serializer data-shape tasks: the solver over-normalized Trivy
`CveContents` entries, removed duplicate source records, and changed CVSS fields
across broad visible parser fixtures. The official verifier failed `TestParse`.
The general prompt/guardrail change is to require exact replacement evidence
before accepting any still-failing relevant visible fixture, and to allow visible
inline golden expectation updates only when the issue explicitly changes a
serialized/CLI/parser output contract, the test update accompanies a source
fix, and the assertion is tightened to the exact source-derived new output
shape rather than weakened, skipped, deleted, or broadened. Test-only patches
remain blocked.

Additional image-bake leakage audit: direct metadata sanitization was not the
only trust boundary. The production solver repo is copied into `/opt/multiagent`
inside every task container, so host-side eval artifacts can become indirect
benchmark memory if agents inspect that tree. Generated reports were already
excluded, but the previous bake still shipped `tests/`, root docs, and
non-runtime evaluation harness files that contained synthetic private-metadata
fixtures and prior benchmark-process notes. The image baker now copies only the
runtime files needed by the production solver (`launch.sh`, `bin/`, `prompts/`,
`orchestrator_prompt.md`, and `evaluation/native_solver` runtime files) and
excludes host-side tests, reports, run artifacts, docs, and eval harnesses.
Regression checks simulate the bake context and assert that required runtime
files are present while host-side benchmark memory is absent.

The slim-bake path was then verified on row 5. Inside the live task container,
solver-visible metadata was `{}`, `/opt/multiagent/tests`,
`/opt/multiagent/evaluation/reports`, the scaffold parity harness, and root
README were absent, while the runtime solver and prompts were present. The
focused run scored `1.0`, confirming the reduced bake surface still supports a
complete production-native solve.

## 2026-07-11 Row 8 Recovery-Gate Update

Focused rerun `swe-bench-pro-prod-pr4-recovery-offset8-count1-r2` fixed a
measurement-infra gap for row 8. The previous row-8 slim-bake run produced a
source diff and worker validation evidence, but the native wrapper exited
`rc=2`, so EvalScope refused to submit the patch and the run had `score: null`.
The recovery gate now reads durable subagent last-message files for generic
visible-validation evidence, so an unrelated noisy tmux/tool-call error cannot
discard a source diff that the production agents already validated.

The rerun reached the official verifier with native solver exit code 0 and
official verifier evidence `true`, but scored `0.0`. Therefore row 8 remains in
the missing list and the first-50 score remains 32/50. The official failure is
now real solver quality evidence rather than an unscored infrastructure
failure.

The general row-8 solver lesson is overreach control plus validation freshness.
The accepted diff changed the direct service-uploader initialization path, but
also changed adjacent kube proxy context/cache/error-response behavior. The
official verifier then failed during `lib/kube/proxy` test compilation. The
general prompt update is to require verifiers and contract scouts to reject
broad adjacent rewrites for narrow root-cause tasks unless source-visible
evidence directly connects each extra behavior change to the issue. For
compiled languages, a worker's validation claim is no longer enough when the
patch touches structs, methods, helper state, or unexported interfaces; the
verifier must confirm that the relevant package command compiled test files
after the final diff, or perform a source-level compatibility comparison.

## 2026-07-11 Row 16 Measurement And Adapter-Parity Update

Focused rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r9-scorefailed` used the current
PR4 production-native no-leak path with `--score-failed-native-diff` enabled.
This makes rejected native diffs count as scored failures instead of producing
`score: null`. In this run the native solver exited `rc=0`, reached the official
verifier with official verifier evidence `true`, and scored `0.0`; row 16
therefore remains missing and the first-50 score remains 32/50.

The submitted patch was a small source-only parser field-list expansion. Local
agent validation passed a focused parser command, but the official verifier
failed two parser cases. One failure exposed an adapter-interface parity miss:
the newly retained field could route through reader code that calls back into
the record/container, but one concrete parser adapter did not provide the same
callback method. The other failure showed the complete linked-value collection
contract was still under-satisfied.

The general solver lesson is that parser allowlist, dispatch-table, accepted
token-set, field-list, extension-list, or format-registry changes are not simple
one-line inclusions. They create new execution paths through existing readers.
Workers, contract scouts, and verifiers now require adapter-parity reasoning for
those changes: trace the newly included item through reader functions, identify
every concrete adapter/container used by each entrypoint, and verify any
record/container callback methods exist with matching return shape before
accepting.

Follow-up rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r10-adapter-parity` used the
adapter-parity prompt update and the same production-native no-leak path. The
run spent longer in the multi-agent loop and the adapter public validation probe
caught failing nearby visible parser fixtures, forcing at least one follow-up.
The native solver still exited `rc=0`, reached the official verifier with
official verifier evidence `true`, and scored `0.0`; row 16 remains missing and
the first-50 score remains 32/50.

The r10 measurement exposed a wrapper recovery bug rather than a leak. After a
coverage follow-up, one recovery path could accept any non-empty source diff if
the heuristic blocker list was empty, even when an adapter-selected
repository-visible validation command existed and had not passed. That made a
known-bad public-probe failure look like a clean native completion. The wrapper
now treats unresolved public-probe failures as terminal blockers for clean
completion/recovery: normal completion, accepted-without-status recovery,
coverage-follow-up recovery, and final cleanup recovery all require the selected
public probe to pass when such a probe is available. Diagnostic runs may still
use `--score-failed-native-diff` to send rejected diffs to the official verifier,
but production-capability score runs should not count these as successful native
solver exits.

Follow-up rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r11-block-public-probe` verified
the wrapper hardening on the current PR4 branch. This time the native solver did
not hit the unresolved-public-probe path: the adapter-selected repository-visible
parser validation passed and the native solver exited `rc=0`. The official
verifier still scored `0.0`, so row 16 remains missing and the first-50 score
remains 32/50.

The r11 failure exposed the next general hidden-contract gap. Passing the current
repository fixture suite is not enough for parser/reader tasks whose issue and
diff involve complete linked, alternate, repeated, or multi-value behavior. The
official verifier can add new fixture rows to the same visible test file, while
the no-leak solver must infer that risk from source semantics rather than from
the official rows. The production validation gate now requires
`multi-value-probe-passed:` for parser/reader linked or alternate multi-value
changes: the worker/verifier must run or describe a source-derived probe with at
least two linked values through the affected entrypoint, or provide
`multi-value-probe-skip-justified:` with source evidence that no two-value case
applies. This is intentionally generic and no-leak; it does not mention
project-specific fixtures or expected answers.

Follow-up rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r12-multivalue-probe` confirmed
that the new gate changed solver behavior: the native solver ran the full
production multi-agent loop for about 824 seconds, exited `rc=0`, and its final
status included `multi-value-probe-passed:` plus adapter public validation. The
patch reached the official verifier, but still scored `0.0`, so row 16 remains
missing and the first-50 score remains 32/50.

The r12 miss showed that a marker saying a multi-value probe passed is not
strong enough if it only observes internal helper behavior or loosely states
that alternates appeared somewhere. The official verifier still found too few
values in final parser output collections. The general no-leak fix is to make
the probe prove product-facing output cardinality: `multi-value-probe-passed:`
must now include `final-output-field=...`, `source-count=N`,
`expected-output-count=N`, and `actual-output-count=N`, with expected and actual
counts equal. Prompts, scout roles, the benchmark appendix, and the wrapper gate
now all require this stronger final-output evidence without encoding the row's
fixture names or expected answers.

Follow-up rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r13-final-output-counts` used the
final-output cardinality gate. It still scored `0.0`, so row 16 remains missing
and the first-50 score remains 32/50. The important finding was wrapper-side:
the gate correctly emitted a follow-up because `multi-value-probe-passed:` was
missing, but after the orchestrator exited without a valid status the generic
"orchestrator exited with source diff" recovery path ran before the more
specific coverage-follow-up recovery path. Because the adapter public helper
probe had passed, that generic path accepted the source diff and submitted it to
the official verifier despite the unresolved final-output marker.

The wrapper now always recomputes source-derived validation blockers even after
an adapter public probe passes, and the generic no-status recovery branch is
skipped once a coverage follow-up is active. Public helper validation can clear
only blockers directly covered by the selected repository-visible tests; it
cannot clear marker-style evidence requirements such as final output
cardinality. This prevents a weak or missing verifier marker from being
converted into a clean native completion by recovery logic.

Follow-up diagnostic rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r14-recovery-blockers` verified
that hardening. The native production solver exited `rc=2` after about 2075
seconds instead of pretending that the unresolved coverage-marker state was a
clean completion. Because the run intentionally used
`--score-failed-native-diff`, EvalScope still sent the rejected source diff to
the official verifier for diagnostics. The official verifier scored that diff
`1.0`, but the regenerated run report now records `clean_native_score: null`
and `diagnostic_score: 1.0`, so this row is not counted as a clean production
multi-agent solve.

This is a no-leak measurement lesson, not a reason to feed row facts back into
the solver. Solver-facing files were scanned for project names, fixture names,
specific official failures, and row/offset identifiers, with no matches in the
baked runtime prompts/guardrails/tests. The remaining allowed learning is
generic: final-output probes must prove product-facing cardinality, public
helper probes cannot clear unrelated marker requirements, and reports must
separate clean native completions from diagnostic official scoring of rejected
diffs.

Follow-up clean rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r15-final-marker-override` used
the production-native no-leak path without diagnostic scoring. The native solver
completed cleanly (`rc=0`) after about 914 seconds and reached the official
verifier with official verifier evidence `true`, but scored `0.0`; row 16
therefore remains missing and the first-50 score remains 32/50.

The r15 examination found a trust-boundary bug in the no-leak direction. The
final status text claimed `multi-value-probe-passed:` with product-facing
counts (`source-count=3`, `expected-output-count=3`, `actual-output-count=3`),
but the official public test log showed final parser output still had too few
values in selected parser cases. The problem was not that the adapter withheld
official hidden knowledge; the problem was that orchestration trusted a
self-reported verifier sentence without machine-checkable probe evidence.

The general no-leak hardening is now stricter: for parser/reader linked,
alternate, repeated, complete, or multi-value behavior, a
`multi-value-probe-passed:` claim must be backed by a rerunnable command/output
transcript at `/tmp/multiagent-prod-swe/multi-value-probe.txt` with matching
`final-output-field=...`, `source-count=N`, `expected-output-count=N`, and
`actual-output-count=N` evidence. This still does not leak benchmark row facts
or official expected tests into the solver. It only prevents a production
multi-agent verifier from clearing hidden-contract risk by writing plausible
but unverified status text.

Follow-up rerun
`swe-bench-pro-prod-pr4-noleak-offset16-count1-r16-machine-evidence` verified
the machine-evidence gate in a clean non-diagnostic run. The native solver
exited `rc=2` after about 493 seconds, so the rejected diff was not submitted
for official scoring and row 16 remains missing; the first-50 score remains
32/50.

The r16 root cause is another general verifier precision issue. The worker did
write a machine-readable multi-value probe transcript, but the transcript
collapsed several product-facing output fields into one aggregate count. Nearby
visible tests still failed on specific output fields, so the aggregate count was
not valid acceptance evidence for the changed parser contract. The general
hardening is now per-field: `multi-value-probe-passed:` must name one singular
`final-output-field=...` per affected output collection, with matching
`source-count=N`, `expected-output-count=N`, and `actual-output-count=N` for
that field. Aggregate counts across several fields are rejected unless visible
source evidence proves that aggregate is the actual acceptance surface.

## Parallel failed-row reruns

After Docker Desktop memory was raised, the missing first-50 rows were rerun
with four concurrent one-row production-native workers. The active queue keeps
independent failed rows in flight while preserving the same official verifier
path and 20g task-container memory limit per worker.

`swe-bench-pro-prod-pr4-parallel4-offset2-r1` exited native `rc=2` with no
official score. The run appears to have tripped a helper/interface-name guard
before producing a clean patch. The available report did not preserve enough
source transcript to prove whether that was a true public contract miss or an
over-strict named-helper guard, so no solver-facing rule was changed from this
row yet.

`swe-bench-pro-prod-pr4-parallel4-offset8-r1` completed native `rc=0` and
reached the official verifier, but scored `0.0`. The patch changed a Go service
initialization path and local validation covered the edited package, while the
official verifier failed a related feature package under the same top-level
tree. The general no-leak fix is to broaden Go validation from "changed package
only" to source-visible related feature package tests: derive nearby package
subtrees from changed Go paths plus issue/diff vocabulary, then add a bounded
recursive `go test ./<related-tree>/...` when that subtree has Go tests.

`swe-bench-pro-prod-pr4-parallel4-offset12-r1` exited native `rc=2` after about
1485 seconds and was not submitted to official scoring. The solver found a
plausible patch, but its focused package test regexes matched no runnable
tests, leaving only compile/package-level evidence for a behavioral cache split
contract. The wrapper correctly treated that as unresolved risk instead of
turning a weak completion into a benchmark score.

`swe-bench-pro-prod-pr4-parallel4-offset14-r1` completed native `rc=0` and
reached the official verifier, but scored `0.0`. The official failure was a
missing module import for the newly centralized keyboard-binding utility. The
allowed general lesson is not the hidden module name; it is that verifier
acceptance was too weak for newly introduced public utilities. A source-level
verifier should require stronger evidence that a reusable public utility has a
stable import surface, nearby runnable validation if a visible test exists, or
an explicit source-based justification when no focused test harness is present.

`swe-bench-pro-prod-pr4-parallel4-offset15-r1` exited native `rc=2` after about
772 seconds and was not submitted to official scoring. The patch intentionally
changed data-shape behavior while a nearby relevant visible package test still
failed on the old shape, and the final evidence only had a no-test package
command plus source explanation. The wrapper rejection is the desired no-leak
behavior: visible relevant failures require exact replacement probes or updated
source-derived expectations, not a generic "tests are stale" assertion.

`swe-bench-pro-prod-pr4-parallel4-offset18-r1` exited native `rc=2` after about
777 seconds and was not submitted to official scoring. The solver attempted
focused Teleport validation with `go test ./lib/client ./tool/tsh`, but the
captured evidence only proved the `lib/client` side and left `tool/tsh` as
remaining risk. The wrapper correctly rejected the diff rather than treating a
partially observed multi-package validation run as clean acceptance evidence.

`swe-bench-pro-prod-pr4-parallel4-offset20-r1` did not reach the production
solver. It failed while baking the native solver into an older Alpine-based
Teleport task image because the manual Node 22 musl bootstrap hit a runtime
library compatibility problem. This is an eval-infra failure, not a solver
score. The on-demand image bake now upgrades Alpine `libstdc++`/`libgcc` before
manual Node extraction, and row 20 is being retried as
`swe-bench-pro-prod-pr4-parallel4-offset20-r2`.

`swe-bench-pro-prod-pr4-parallel4-offset20-r2` confirmed the first Alpine fix
was insufficient. The image still failed before solver launch because the Node
22 musl binary requires a newer C++ runtime symbol than this Alpine 3.17 task
image can provide, while cross-version Alpine `libstdc++` upgrades conflict
with the image's existing C toolchain packages. The general infra fix is to use
a Node 20 musl runtime for Alpine manual installs; Node 20 satisfies Codex's
minimum runtime and runs on the older Alpine image. Row 20 is being retried as
`swe-bench-pro-prod-pr4-parallel4-offset20-r3`, which has passed image bake and
started the native solver.

`swe-bench-pro-prod-pr4-parallel4-offset20-r3` verified the Alpine image-bake
fix. The task image baked successfully with Node `v20.19.0` and Codex CLI
`0.144.1`, then launched the production native solver. The solver exited
`rc=2` after about 226 seconds, so no rejected diff was submitted for official
scoring. Row 20 is no longer an eval-infra blocker; it is now a normal native
rejection.

`swe-bench-pro-prod-pr4-parallel4-offset17-r1` completed native `rc=0` after
about 1571 seconds, reached the official verifier, and scored `0.0`. This is a
clean native miss. Official output showed `TestIsOvalDefAffected` failed and
the `scanner` package no longer compiled because existing tests still referenced
package-private Alpine parser helpers removed by the patch. The general lesson
is the same public-contract principle at package-test scope: changed Go files
must preserve helper methods that visible package tests or nearby source callers
still reference, and focused validation must include the changed package's test
suite when parser/helper APIs are edited.

`swe-bench-pro-prod-pr4-parallel4-offset27-r1` exited native `rc=2` after about
443 seconds and was not submitted to official scoring. The solver attempted
`go test ./server`, but the run could not proceed because existing `go.sum`
entries for required `google.golang.org/grpc` packages were missing. The
general lesson is that validation infrastructure should separate dependency
setup/remediation from product acceptance: a dependency-resolution failure is
not proof the patch is correct, so the wrapper rejection is appropriate.

`swe-bench-pro-prod-pr4-parallel4-offset28-r1` exited native `rc=2` after about
908 seconds and was not submitted to official scoring. The solver produced a
Flipt OFREP bulk-evaluation patch and attempted
`go test ./internal/server/ofrep ./internal/server/evaluation`, but the final
report only preserved the attempted command and patch tail rather than a
completed passing validation transcript. This is another correct wrapper
rejection: attempted focused validation is not the same as observed acceptance
evidence.

`swe-bench-pro-prod-pr4-parallel4-offset32-r1` completed native `rc=0` after
about 459 seconds, reached the official verifier, and scored `1.0`. This is a
clean production-native pass. The patch updated Navidrome artist refresh logic
and passed the solver's focused `go test ./model ./persistence` validation
before official scoring.

`swe-bench-pro-prod-pr4-parallel4-offset38-r1` exited native `rc=2` after about
830 seconds and was not submitted to official scoring. The solver changed
Teleport OSS user migration behavior, but the focused visible validation
`go test ./lib/auth -run TestMigrateOSS` still failed because the patch changed
the expected migrated role set from `["ossuser"]` to `["admin", "ossuser"]`.
The wrapper rejection is correct: a visible focused test failure cannot be
overridden by asserting the visible expectation is stale.

`swe-bench-pro-prod-pr4-parallel4-offset42-r1` exited native `rc=2` after about
412 seconds and was not submitted to official scoring. The preserved tail is
Ansible collection-install source/test context rather than a clean final patch
with completed passing focused validation. The wrapper correctly treated this as
an unresolved native run instead of manufacturing an official score.

`swe-bench-pro-prod-pr4-parallel4-offset41-r1` completed native `rc=0` after
about 469 seconds, reached the official verifier, and scored `0.0`. This is a
clean native miss. The solver accepted a Proton Pass UI patch based on source
review and `git diff --check` after reporting local Jest harness issues, while
the official selected Jest test failed to run because a mocked module path could
not be resolved. The general lesson is that UI tasks still need runnable
component-level evidence or an explicit source-level import/module resolution
audit before acceptance; source review alone is too weak.

`swe-bench-pro-prod-pr4-parallel4-offset37-r1` exited native `rc=2` after about
1712 seconds and was not submitted to official scoring. The solver produced a
Teleport database/TLS patch and passed `git diff --check`, but focused
validation was incomplete and failing: `go test ./lib/srv/db ./lib/reversetunnel
./tool/tsh` only showed `lib/reversetunnel` passing before `lib/srv/db` failed
with repeated TLS setup errors (`local error: tls: bad record MAC`), and no
useful `tool/tsh` result was captured. The wrapper rejection is correct because
partial validation with an observed package failure is not acceptance evidence.

`swe-bench-pro-prod-pr4-parallel4-offset48-r1` exited native `rc=2` after about
857 seconds and was not submitted to official scoring. The solver patched
Teleport `DeleteMFADevice` last-device behavior and passed a compile-only
`go test ./lib/auth -run '^$' -count=1`, but the behavioral validations
`go test ./lib/auth -run TestMFADevice -count=1` and
`go test ./lib/auth -run TestMFADeviceManagement -count=1` failed before useful
coverage with `transport: authentication handshake failed: local error: tls:
bad record MAC`. The wrapper rejection is correct: source review plus
compile-only validation is not enough for an official submission when the
intended behavior is covered by focused tests that did not complete.

`swe-bench-pro-prod-pr4-parallel4-offset44-r1` exited native `rc=2` after about
1078 seconds and was not submitted to official scoring. The solver changed
OpenLibrary MARC author/contribution parsing and passed a focused production
parser probe plus `python -m py_compile openlibrary/catalog/marc/parse.py`, but
the visible parser fixture validation
`pytest -q openlibrary/catalog/marc/tests/test_parse.py::TestParseMARCBinary::test_binary --maxfail=1`
still failed on `bijouorannualofl1828cole_meta.mrc` because the fixture expected
the old `contributions` behavior. The wrapper rejection is correct: a source
probe cannot override a failing visible fixture test for the same parser
contract.

## 2026-07-11 Validation Failure Repair Loop Update

The newest failed-row batch showed a general orchestration gap rather than a
benchmark-specific missing fix. Rows 37, 44, and 48 all produced plausible
source diffs, but the decisive evidence was a relevant visible validation
failure or incomplete validation transcript. The wrapper correctly refused to
submit those diffs. The production multi-agent improvement is to move that
decision earlier: a worker or verifier that sees a relevant visible test,
fixture, compile, package, component, or source-derived probe fail must route a
fresh bounded repair worker before completion.

PR4 now applies this as a general rule in the orchestrator prompt, validation
scheduling playbook, orchestration routing playbook, worker prompt, verifier
prompt, and SWE autonomous benchmark instructions. The guardrail code also
treats `validation-repair-needed:` and nonzero focused validation return codes
as blockers. This does not inject hidden tests or row-specific fixes; it only
prevents source-only acceptance while repository-visible validation is still
failing.

Repair-loop reruns were launched for rows 37, 38, 44, and 48 with four parallel
production-native workers:

- `swe-bench-pro-prod-pr4-repairloop-offset37-r1` exited native `rc=2` after
  about 1347 seconds and was not submitted to official scoring. The solver
  produced a same-name Teleport database-service patch and attempted
  `go test ./lib/srv/db ./tool/tsh`, but `lib/srv/db` still failed with
  repeated `tls: bad record MAC` setup errors. The repair loop did not turn this
  into a clean native completion.
- `swe-bench-pro-prod-pr4-repairloop-offset38-r1` exited native `rc=2` after
  about 856 seconds and was not submitted to official scoring. Follow-up repair
  work still left `go test ./lib/auth -run TestMigrateOSS -count=1` failing:
  the visible test expected `[]string{"ossuser"}` while the patch returned
  `[]string{"ossuser", "admin"}`. The wrapper correctly refused the diff.
- `swe-bench-pro-prod-pr4-repairloop-offset44-r1` exited native `rc=2` after
  about 758 seconds and was not submitted to official scoring. The new loop did
  force a follow-up/reconciliation path with `replacement-probe-passed:`,
  `stale-visible-failure-justified:`, and `multi-value-probe-passed:` markers,
  but the final transcript still kept a focused visible pytest node red. The
  wrapper correctly treated this as unresolved instead of scoring the rejected
  diff.
- `swe-bench-pro-prod-pr4-repairloop-offset48-r1` exited native `rc=2` after
  about 859 seconds and was not submitted to official scoring. A follow-up
  narrowed the Teleport MFA predicate, but focused
  `go test ./lib/auth -run Test.*MFADevice -count=1` still failed before clean
  behavioral coverage with `transport: authentication handshake failed: local
  error: tls: bad record MAC`.

Net score movement from this rerun wave: no additional clean passes. The
aggregate remains `33/50`, so the >70% target is still unmet. The useful
learning is that prompt-level repair routing alone changes behavior but is not
enough for rows where the environment-level validation command stays red or the
solver decides a visible expectation is stale. The next general improvement
should make stale-visible acceptance machine-checkable by the wrapper rather
than only prompt-enforced: either the visible failing command must pass after a
repair worker, or the wrapper must verify the replacement probe artifact covers
the exact failing field/path before accepting a stale-visible exception.

## 2026-07-11 Failed-Row Parallel-4 Rerun With 20g Docker Memory

After Docker Desktop memory was raised, the unresolved first-50 rows were rerun
with a four-worker queue:

- Rows: `2, 8, 12, 14, 15, 16, 17, 18, 20, 27, 28, 37, 38, 41, 42, 44, 48`.
- Prefix: `swe-bench-pro-prod-pr4-parallel4c-offset{row}-r1`.
- Solver path: production-native multiagent baked from
  `/private/tmp/multiagent-pr4-live` with
  `--native-solver-command /tmp/evalscope-native-multiagent-solver.sh`.
- Docker memory: `--memory-limit 20g`.
- Scoring mode: clean native official verifier only. No
  `--score-failed-native-diff` diagnostic scoring was used.

One earlier attempt with prefix `parallel4b` failed at the harness level because
the sandboxed process could not bind the local model-proxy socket on
`127.0.0.1`. That attempt is not score evidence. The `parallel4c` rerun was
launched with the required host permissions and is the only four-wide rerun
counted here.

Final row outcomes:

| Row | Repo | Native rc | Official evidence | Clean native score | Wall time | Outcome |
| --- | --- | ---: | --- | ---: | ---: | --- |
| 2 | NodeBB/NodeBB | 2 | no | n/a | 533.4s | Native validation rejection. |
| 8 | gravitational/teleport | 0 | yes | 0.0 | 696.4s | Clean official miss. |
| 12 | gravitational/teleport | 2 | no | n/a | 1186.7s | Native validation rejection. |
| 14 | element-hq/element-web | 0 | yes | 0.0 | 775.2s | Clean official miss. |
| 15 | future-architect/vuls | 2 | no | n/a | 378.7s | Native validation rejection. |
| 16 | internetarchive/openlibrary | 124 | no | n/a | 3516.6s | Runtime timeout/stream failure, not a scored solver pass or official miss. |
| 17 | future-architect/vuls | 124 | no | n/a | 3513.6s | Runtime timeout/stream failure, not a scored solver pass or official miss. |
| 18 | gravitational/teleport | 2 | no | n/a | 359.9s | Native validation rejection. |
| 20 | gravitational/teleport | 0 | yes | 0.0 | 1078.6s | Clean official miss. |
| 27 | flipt-io/flipt | 2 | no | n/a | 548.1s | Native validation rejection. |
| 28 | flipt-io/flipt | 2 | no | n/a | 1822.5s | Native validation rejection. |
| 37 | gravitational/teleport | 2 | no | n/a | 1382.5s | Native validation rejection. |
| 38 | gravitational/teleport | 0 | yes | 0.0 | 801.8s | Clean official miss. |
| 41 | protonmail/webclients | 0 | yes | 0.0 | 748.9s | Clean official miss. |
| 42 | ansible/ansible | 2 | no | n/a | 1412.6s | Native validation rejection after high tool-call churn. |
| 44 | internetarchive/openlibrary | 2 | no | n/a | 1203.5s | Native validation rejection. |
| 48 | gravitational/teleport | 2 | no | n/a | 813.3s | Native validation rejection. |

Net score movement from this failed-row rerun: no additional clean passes. The
aggregate remains `33/50` production-native clean official passes, so the >70%
target is still unmet.

The useful system-level result is negative but clear. Extra Docker memory and
four-way parallelism improved throughput, but they did not close the solve-rate
gap. The dominant remaining failure modes are not memory exhaustion: most rows
either fail the native acceptance gate before official scoring, or reach the
official verifier and fail hidden/official tests. Rows 16 and 17 show a
separate reliability problem under parallel load: long Codex/API streaming runs
can still end as runtime failures. Row 42 also exposed orchestration churn,
running many tool-call turns before a native rejection.

The next general multiagent improvement should therefore target solve quality
and termination discipline, not only eval infrastructure:

- The verifier should convert relevant visible failures into bounded repair
  work earlier, but stale-visible exceptions must remain machine-checkable.
- The orchestrator should detect high-turn churn and force a concise
  hypothesis/test/fix decision rather than allowing indefinite tool-call loops.
- Official-miss rows need failure-root-cause review against the produced diff,
  then general prompt/role/tooling changes. They should not be fixed with
  benchmark-specific knowledge.

## 2026-07-11 Checkpoint Refactor And Failed-Row Retry

Two general orchestration checkpoints were added after the failed-row reruns:

- Commit `6bb967e` adds a convergence checkpoint. If a source diff exists for a
  long time without accepted verifier evidence or a terminal status, the native
  wrapper sends the orchestrator a one-shot instruction to freeze scope, run
  verifier/bounded repair, and then complete or block.
- Commit `e661d4a` adds a no-diff planning checkpoint. If the orchestrator has
  spent a long time with no `/app` diff and no status, the wrapper asks it to
  stop broad exploration, choose narrow source paths, and spawn exactly one
  bounded implementation worker or block.

Both changes are general production-native controls. They do not use row
identity, official tests, expected patches, or previous benchmark failures.
Validation before pushing included:

```text
python3 -m py_compile evaluation/native_solver/solve_swe_prod.py evaluation/native_solver/swe_prod_guardrails.py
bash -n tests/run.sh
git diff --check
perl -e 'alarm shift; exec @ARGV' 180 bash tests/run.sh
```

A four-wide retry wave was then attempted for rows `12, 20, 28, 37, 42, 44, 48`
with prefix `swe-bench-pro-prod-pr4-convergence-offset{row}-r1`, 20g task
memory, production-native solver bake, persistent caches, and clean official
scoring only. This wave is not score evidence: several rows hit the Codex usage
limit, and the remaining long-running rows were stopped after the reset window
because the run was already contaminated.

One follow-up retry with prefix
`swe-bench-pro-prod-pr4-checkpoints2-offset{row}-r1` is also not score evidence.
It was launched without the required host permission for the local model-proxy
socket and failed before solver/scoring because the proxy could not bind
`127.0.0.1`.

The clean post-reset retry used prefix
`swe-bench-pro-prod-pr4-checkpoints2b-offset{row}-r1` on rows 37 and 42:

| Row | Repo | Native rc | Official evidence | Clean native score | Wall time | Outcome |
| --- | --- | ---: | --- | ---: | ---: | --- |
| 37 | gravitational/teleport | 2 | no | n/a | 1694.4s | Produced a Teleport database/TLS diff, but focused `go test ./lib/srv/db ./tool/tsh` still failed with TLS/setup errors and only narrow compile checks passed. |
| 42 | ansible/ansible | 2 | no | n/a | 564.4s | Exited before official scoring after repeated bridge stream errors/native rejection. |

Net score movement: none. The first-50 aggregate remains `33/50`
production-native clean official passes, below the >70% target.

The latest negative result narrows the root cause. Prompt/checkpoint nudges are
helpful guardrails, but they are not strong enough by themselves:

- Row 37 still spent many turns before a rejected completion, even with a real
  diff. The orchestrator needs a wrapper-enforced progress watchdog or
  hard-state intervention that can force bounded repair/stop decisions, not just
  another text reminder.
- Row 42 exited before the no-diff checkpoint threshold, so some failures need
  earlier extraction of native validation state and faster routing to repair or
  block.
- Eval infra should detect Codex usage-limit and proxy-bind failures as harness
  contamination immediately, stop those rows, and keep them out of solver-score
  accounting.

## 2026-07-12 Progress Watchdog And Final-Cleanup Probe Update

PR4 now adds a harder production-native progress intervention on top of the
prompt checkpoints. The native wrapper tracks whether a non-empty `/app` diff
has actually changed. If the diff remains stale past
`EVAL_PROGRESS_REPAIR_AFTER` and `EVAL_PROGRESS_REPAIR_MIN_STALL`, the wrapper
runs only repository-visible validation and can launch one bounded
progress-repair worker with source-derived ownership paths and generic
blockers. This does not expose row identity, official tests, expected patches,
benchmark scores, or previous benchmark failures.

The wrapper also now has a final-cleanup recovery path for a common failed-row
pattern: nonzero native exit, real source diff, but no durable worker validation
evidence. Instead of immediately rejecting that state, it runs the same
adapter-selected public validation probe. It recovers a completed status only
when that probe passes and normal implementation/validation blockers are clean;
otherwise the rejected diff remains unscored.

Validation before pushing included:

```text
python3 -m py_compile evaluation/native_solver/solve_swe_prod.py evaluation/native_solver/swe_prod_guardrails.py evaluation/evalscope_multiagent_native_runner.py
bash -n tests/run.sh
git diff --check
perl -e 'alarm shift; exec @ARGV' 180 bash tests/run.sh
```

The first targeted row-37 retry,
`swe-bench-pro-prod-pr4-progresswatch-offset37-r1`, is not score evidence. It
failed before solver launch because the local EvalScope 1.8.1 target directory
had lost source modules such as `evalscope.api.registry` and
`evalscope.agent.external.runners`. The dependency tree was restored with a
targeted reinstall into `/private/tmp/evalscope_repair_20260702`, and imports
for `evalscope.run`, the external runner API, and the SWE Bench Pro adapter were
verified before rerunning.

Clean targeted retry `swe-bench-pro-prod-pr4-progresswatch-offset37-r2` used the
production-native solver bake, 20g task memory, persistent cache, and clean
official scoring only. It exited native `rc=2` after `1808.2s`, with no official
verifier evidence and no clean score. The run did show improved repair behavior:
the agents spawned `worker-04-repair`, identified the missing
`auth.Context.DatabaseServers` candidate-list risk, and patched
`ProxyServer.authorize` to store the selected database-server slice on the auth
context. However the final validation evidence was still too weak:
`go test -run TestNonExistent ./lib/srv/db` passed with no tests, while the
focused package validation result was not available. The native gate correctly
refused to submit that rejected diff.

Net score movement: none. The first-50 aggregate remains `33/50`
production-native clean official passes.

The useful learning is sharper than the earlier “orchestrator churn” diagnosis:
the system can now discover and repair a likely hidden-contract risk, but it
still fails to turn that repair into a clean terminal state with strong
repository-visible validation. The next general improvement should focus on
validation ownership and terminal-state discipline: repair workers must either
run the real affected package tests, produce machine-checkable source-derived
replacement evidence, or explicitly hand the diff to the wrapper's public probe
before the orchestrator exits. No-test compile checks should not be treated as
behavioral validation for source repairs.

## 2026-07-12 No-Test Gate And Failed-Row Parallel Rerun

PR4 now hardens the production-native SWE path against no-test validation
evidence. The wrapper rejects `go test -run TestNonExistent`, `go test -run
'^$'`, `[no test files]`, `no tests to run`, and similar compile-only checks as
behavioral validation for Go source repairs unless the solver gives an explicit
skip justification. The worker, verifier, and SWE appendix prompts now state the
same rule, but the important change is machine enforcement in
`solve_swe_prod.py`: persisted worker evidence, final status evidence, public
probe acceptance, and Go coverage blockers all treat no-test evidence as
insufficient.

Validation added for this change asserts that no-test command output is rejected
by `visible_validation_passed_in_text`, `validation_text_has_no_test_evidence`,
`persisted_subagent_visible_validation_evidence`, and
`validation_coverage_blockers`.

After Docker Desktop memory was raised, all unresolved first-50 failed rows were
rerun with production-native solver bake, 20g task memory, persistent caches,
clean official scoring only, and up to four rows active at a time. The main
batch prefix was `swe-bench-pro-prod-pr4-failed4d-offset{row}-r1`; row 37 used
the targeted no-test-gate prefix
`swe-bench-pro-prod-pr4-no-test-gate-offset37-r3`.

| Row | Native rc | Official evidence | Clean native score | Wall time | Outcome |
| --- | ---: | --- | ---: | ---: | --- |
| 2 | 2 | no | n/a | 838.4s | Native rejected before official scoring. |
| 8 | 2 | no | n/a | 1644.3s | Native rejected before official scoring. |
| 12 | 2 | no | n/a | 766.2s | Native rejected before official scoring. |
| 14 | 0 | yes | 0.0 | 725.7s | Clean native submission, official miss. |
| 15 | 2 | no | n/a | 713.5s | Native rejected before official scoring. |
| 16 | 2 | no | n/a | 1316.9s | Native rejected before official scoring. |
| 17 | 2 | no | n/a | 1531.9s | Native rejected before official scoring. |
| 18 | 0 | yes | 0.0 | 623.7s | Clean native submission, official miss. |
| 20 | 2 | no | n/a | 928.8s | Native rejected before official scoring. |
| 27 | 2 | no | n/a | 565.6s | Native rejected before official scoring. |
| 28 | 2 | no | n/a | 1294.6s | Native rejected before official scoring. |
| 37 | 2 | no | n/a | 1523.5s | No-test gate kept the Teleport diff unscored. |
| 38 | 0 | yes | 0.0 | 840.3s | Clean native submission, official miss. |
| 41 | 0 | yes | 0.0 | 860.3s | Clean native submission, official miss. |
| 42 | 2 | no | n/a | 1825.2s | Native rejected before official scoring. |
| 44 | 2 | no | n/a | 1271.7s | Native rejected before official scoring. |
| 48 | 2 | no | n/a | 830.9s | Native rejected before official scoring. |

Net score movement: none. The first-50 aggregate remains `33/50`
production-native clean official passes, below the >70% target.

This rerun confirms the current dominant gap is not Docker memory. With 20g
memory and four-wide scheduling, the solver still either exits with unresolved
native blockers or reaches official verification with incomplete fixes. The
general improvement target remains validation ownership and repair convergence:
the system needs to convert discovered candidate fixes into real affected-test
evidence or explicitly block before completion, rather than relying on weak
compile-only checks or stale fixture reconciliations.

## 2026-07-12 Production-Orchestrator Resume

PR4 now adds a bounded production-native resume path for the dominant rejected
diff failure mode. When the wrapper has a non-empty `/app` source diff, no live
agent process, and generic public/source blockers, it can relaunch the same
production `launch.sh --resume` orchestrator instead of either blocking
immediately or relying on the adapter helper as the default source editor.

The resume handoff is written to a new autonomous prompt file under the runtime
directory. It includes only public/source evidence: the issue excerpt, current
diff excerpt, generic adapter/verifier blockers, source-derived ownership
candidates, durable contract ledger excerpt, and public validation probe output.
It explicitly excludes row identity, hidden tests, selected official tests, test
patches, benchmark scores, and prior evaluator outcomes.

Default behavior was also tightened: the progress watchdog no longer launches a
source-editing adapter helper unless `EVAL_ADAPTER_HELPER_MODE=repair` or the
explicit source-edit opt-in is set. In ordinary production-capability runs, the
system now prefers orchestrator follow-up or full production-orchestrator
resume. This keeps the measured solver closer to the intended product
multi-agent loop.

Validation run for this change:

```text
python3 -m py_compile evaluation/native_solver/solve_swe_prod.py evaluation/native_solver/swe_prod_guardrails.py evaluation/evalscope_multiagent_native_runner.py
bash -n tests/run.sh
git diff --check
perl -e 'alarm shift; exec @ARGV' 180 bash tests/run.sh
```

Score movement for the code change itself was not assumed. The failed-row rerun
below measures whether it converted any `rc=2` rejected diffs into clean
official submissions.

## 2026-07-12 Resume Failed-Row Rerun

All unresolved first-50 rows were rerun with the production-native solver bake
from commit `dc543c1`, 20g task memory, persistent per-row caches, clean
official scoring only, and up to four concurrent rows. Prefix:
`swe-bench-pro-prod-pr4-resume-offset{row}-r1`.

| Row | Native rc | Official evidence | Clean native score | Native wall |
| --- | ---: | --- | ---: | ---: |
| 2 | 2 | no | n/a | 1172.3s |
| 8 | 2 | no | n/a | 727.8s |
| 12 | 2 | no | n/a | 1465.3s |
| 14 | 2 | no | n/a | 1217.7s |
| 15 | 2 | no | n/a | 1541.2s |
| 16 | 2 | no | n/a | 992.9s |
| 17 | 2 | no | n/a | 210.7s |
| 18 | 2 | no | n/a | 838.2s |
| 20 | 2 | no | n/a | 743.8s |
| 27 | 2 | no | n/a | 706.8s |
| 28 | 2 | no | n/a | 165.9s |
| 37 | 2 | no | n/a | 346.4s |
| 38 | 1 | no | n/a | 3600.0s |
| 41 | 0 | yes | 0.0 | 658.0s |
| 42 | 2 | no | n/a | 248.2s |
| 44 | 124 | no | n/a | 3515.8s |
| 48 | 124 | no | n/a | 3519.2s |

Net score movement: none. The first-50 aggregate remains `33/50`
production-native clean official passes, still below the >70% target.

The new production-orchestrator resume hook did not materially affect this
batch because the dominant failures were not the narrow post-exit state it
targets. Most rows exited `rc=2` from the native gate while still treated as
normal active runs, rows 44 and 48 hit the native timeout, and row 38 ended
with native `rc=1` at the timeout boundary. Row 41 reached official verification
but scored `0.0`.

This narrows the next general improvement target: fix active-run terminal
discipline, not only post-exit recovery. The orchestrator needs a stronger
in-run contract that periodically forces a real verifier/validation handoff and
terminates with a machine-readable reason before the native timeout. Otherwise
the wrapper sees an active solver until it exits or times out, so a post-exit
resume hook is too late to improve resolve rate.

## 2026-07-13 Active-Run Terminal Deadline Checkpoint

PR4 now adds a stronger active-run terminal checkpoint for the timeout/late-exit
failure mode exposed by the resume rerun. When the native solver is still live
near its deadline, the wrapper captures the current diff, runs the same generic
public/source blocker and validation-probe path, and sends the live orchestrator
a terminal countdown instruction. The instruction requires one of three
production-native outcomes: final verifier plus completed status, one bounded
repair plus verifier, or blocked status with the concrete public/source reason.

If the orchestrator still does not write machine-readable status after the
grace window, the wrapper writes a blocked status before the native timeout
instead of allowing a silent long-tail timeout. This does not accept patches on
behalf of the production solver; it preserves measurement integrity while
making active-run terminal failures explicit and faster to diagnose.

The generated terminal checkpoint is no-leak: it contains only current diff,
public/source blockers, source-derived ownership hints, contract ledger excerpt,
and adapter public validation output. It explicitly prohibits evaluator-only
metadata and does not include row identity, hidden tests, selected evaluator
tests, benchmark scores, or prior evaluator outcomes.

Validation run for this change:

```text
python3 -m py_compile evaluation/native_solver/solve_swe_prod.py evaluation/native_solver/swe_prod_guardrails.py evaluation/evalscope_multiagent_native_runner.py
bash -n tests/run.sh
git diff --check
perl -e 'alarm shift; exec @ARGV' 180 bash tests/run.sh
```

Score movement: not measured yet. The expected near-term effect is fewer
`rc=124`/timeout rows and clearer active-run blockers; a follow-up failed-row
rerun is still required to determine whether the stronger terminal checkpoint
improves clean official submissions.

Focused smoke run `swe-bench-pro-prod-pr4-terminalcheck-offset44-r1` used
aggressive terminal-deadline settings
(`EVAL_TERMINAL_DEADLINE_REMAINING=3000`,
`EVAL_TERMINAL_DEADLINE_GRACE=180`) to try to exercise the checkpoint on a row
that previously timed out. The run did not reach the checkpoint: native exited
`rc=2` after `100.0s`, with no official evidence and no score. This is not
score evidence, but it shows row 44 is not deterministically a timeout; it can
also fail early at the native gate before terminal-deadline control applies.

## 2026-07-13 Rejection Diagnostics and No-Diff Retry

PR4 now preserves richer rejection diagnostics before EvalScope deletes a task
container. For native timeouts or nonzero native exits, the runner captures the
production status file, helper/public validation probes, stale-visible and
multi-value probes, native stdout/stderr tails, `git status`, `git diff --stat`,
`git diff --check`, and the final source diff tail. These diagnostics are
attached to the rejected runner error and metrics. This does not score rejected
diffs; it makes `rc=2` failure causes auditable after the sandbox is gone.

Focused smoke run `swe-bench-pro-prod-pr4-diagnostics-offset28-r1` demonstrated
the value of the new diagnostics. Row 28 exited `rc=2` after `89.8s` with no
official evidence because production status was:

```text
Worker completed without leaving a non-empty source diff in /app.
```

The captured `git status` and diff sections were empty. The root cause for this
row was therefore not an official verifier failure; it was an orchestrator
terminal-state failure where a worker reported completion without materializing
a source patch.

PR4 also adds one bounded production-orchestrator retry for that specific
general failure mode. If the production status is blocked because there is no
materialized source diff, the wrapper relaunches the same production
orchestrator with a no-leak prompt to restart from issue/source evidence and
produce the narrowest source implementation before blocking again. The retry is
bounded by `EVAL_NO_DIFF_BLOCKED_RETRY_LIMIT` and does not use benchmark
metadata, hidden tests, selected evaluator tests, scores, or prior official
outcomes.

Focused smoke run `swe-bench-pro-prod-pr4-nodiffretry-offset28-r1` shows the
retry changed behavior but did not create a pass. Row 28 no longer failed as a
fast empty-diff block; it ran for `900.4s` and produced a real source diff in:

```text
internal/server/evaluation/ofrep_bridge.go
internal/server/ofrep/evaluation.go
internal/server/ofrep/server.go
```

The native gate still rejected the diff before official scoring because it did
not compile:

```text
s.store.ListFlags undefined (type Storer has no field or method ListFlags)
```

Net score movement: none. The first-50 aggregate remains `33/50`
production-native clean official passes. The learning is that a meaningful
share of remaining `rc=2` failures are not verifier-score failures yet; they
are production orchestration failures around materializing a patch, validating
compile contracts, and terminating with machine-readable evidence. The next
general solver improvement should force source ownership checks before calling
methods across interfaces and make compile-contract failures first-class
verifier blockers before final status.
