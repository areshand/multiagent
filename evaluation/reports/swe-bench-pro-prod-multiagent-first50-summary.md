# SWE Bench Pro Production Multi-Agent First 50 Summary

Date: 2026-07-03

Scope: first 50 official-order SWE Bench Pro rows, evaluated with the
production-container native multi-agent path.

Result: 31/50 rows passed with official verifier evidence.

Passing official indices:

```text
0, 1, 3, 4, 6, 7, 9, 10, 11, 13, 19, 21, 22, 23, 24, 25, 26, 29, 30,
31, 33, 34, 35, 36, 39, 40, 43, 45, 46, 47, 49
```

Missing official indices:

```text
2, 5, 8, 12, 14, 15, 16, 17, 18, 20, 27, 28, 32, 37, 38, 41, 42, 44, 48
```

The final increment from 30/50 to 31/50 came from row 39:

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
verifier with native solver exit code 0, but scored `0.0`; the score remains
31/50.

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
