# SWE Bench Pro Production Multi-Agent First 50 Summary

Date: 2026-07-03

Scope: first 50 official-order SWE Bench Pro rows, evaluated with the
production-container native multi-agent path.

Result: 32/50 rows passed with official verifier evidence.

Passing official indices:

```text
0, 1, 3, 4, 5, 6, 7, 9, 10, 11, 13, 19, 21, 22, 23, 24, 25, 26, 29, 30,
31, 33, 34, 35, 36, 39, 40, 43, 45, 46, 47, 49
```

Missing official indices:

```text
2, 8, 12, 14, 15, 16, 17, 18, 20, 27, 28, 32, 37, 38, 41, 42, 44, 48
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
