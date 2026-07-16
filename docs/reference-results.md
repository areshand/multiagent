# Reference Results and Evidence

This page defines the evidence required before publishing a benchmark claim.
It is intentionally a template, not a claim that a live agent run has happened.
The built-in `--reference-report` command scores authored good/bad fixtures; it
calibrates scorers but does **not** measure an agent or prove orchestration gains.

## SWE Bench Pro Publication Gate

The historical `36/50` first-50 number is a cumulative tuned aggregate, not a
single immutable run. Its recovered evidence and limitations are documented in
[Benchmark And Provenance](benchmark.md). Do not convert it into a fresh-run or
comparative claim.

A new SWE Bench Pro claim is publishable only after the capture and validate
commands in [Benchmark And Provenance](benchmark.md) both exit zero. The compact
automated provenance boundary requires:

| Evidence | Required proof |
| --- | --- |
| Source identity | Clean 40-character commits for solver, EvalScope, and SWE Bench Pro checkouts |
| Runtime identity | EvalScope, Python, Git, Docker client/server, model, and auth mode without credential material |
| Task images | Base and baked image IDs and manifest digests for every selected row |
| Agent CLI | Actual in-container `codex-cli` version captured from every baked task image before pruning |
| Configuration | Exact redacted command plus JSON/YAML config, row offset/count, platform, limits, and output paths |
| Result artifacts | Summary, effective config, preflight, image status, authoritative EvalScope report, and native event log bound by SHA-256 |
| Completion | Evaluation `rc=0`, completed summary, matching sample size, completed image status, and no missing image captures |

The validator rejects dirty checkouts, diagnostic error swallowing, duplicate
or kind-mismatched artifact paths, stale summaries, missing authoritative
reports, incomplete image/runtime records, and any manifest assertion it cannot
recompute. No complete fresh first-50 provenance manifest is currently
published. Because the task-image baker still installs unpinned
`@openai/codex`, a complete manifest makes a run auditable but bit-for-bit replay
also requires preserved derived images or a pinned Codex package specification.

Do not publish raw EvalScope work directories, configs, predictions, reviews,
HTML reports, container logs, prompts, traces, or copied image-build contexts.
They can contain host paths, auth-file locations, benchmark gold/test data, and
credential-shaped test fixtures even when they contain no live credential.
Build any public result bundle from an explicit schema allowlist containing
only run identity, aggregate/per-row outcome, failure category, patch digest,
version/commit identity, resource settings, and image identity. Generated
patches require a separate repository-license and secret review.

## No-Agent Calibration Point

The no-agent fixture implementation inspected for this protocol is
`areshand/multiagent` at commit
`f4e23920f6a519bc72790f66eaa8c7bb57804925` (2026-07-15). Reproduce the
no-agent scorer checks from a clean checkout:

```bash
git clone git@github.com:areshand/multiagent.git /tmp/multiagent-reference
cd /tmp/multiagent-reference
git checkout --detach f4e23920f6a519bc72790f66eaa8c7bb57804925
test -z "$(git status --porcelain)"

python3 -m evaluation.cli --adapter ponytail --selftest
python3 -m evaluation.cli --adapter orchestration --selftest
python3 -m evaluation.cli --adapter ponytail \
  --reference-report --run-root /tmp/multiagent-reference-evidence
python3 -m evaluation.cli --adapter orchestration \
  --reference-report --run-root /tmp/multiagent-reference-evidence
find /tmp/multiagent-reference-evidence -type f -print0 \
  | sort -z | xargs -0 shasum -a 256 \
  > /tmp/multiagent-reference-evidence.SHA256SUMS
mv /tmp/multiagent-reference-evidence.SHA256SUMS \
  /tmp/multiagent-reference-evidence/SHA256SUMS
```

Use the internal pilot for real tasks. After a team replaces the deliberately
invalid template values and freezes 5-10 authorized tasks, run the populated
manifest with:

```bash
python3 examples/internal-pilot/pilot.py validate pilot.json
python3 examples/internal-pilot/pilot.py run pilot.json \
  --output /ABSOLUTE/PATH/pilot-runs/RUN_ID
python3 examples/internal-pilot/pilot.py summarize \
  /ABSOLUTE/PATH/pilot-runs/RUN_ID
```

`pilot.json` is an operator-created manifest, not a file shipped by this
repository. The populated manifest pins the harness and target commits;
`run.json` records the observed commits and dirty state. Publish only
clean-harness runs where these values agree.

## Result Table

One row is one task/arm attempt. Do not collapse missing or pending review into
a failure or success.

| Task | Team | Target commit | Arm | Runs | Mechanical pass | Human accepted | Median minutes | Median changed LOC | Cost | Result |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---|
| `<real issue ID>` | `<owner>` | `<40-char SHA>` | baseline | `<n>` | `<n/N>` | `<n/N>` | `<value>` | `<value>` | `<value>` | pending |
| `<same issue ID>` | `<owner>` | `<same SHA>` | orchestrated | `<n>` | `<n/N>` | `<n/N>` | `<value>` | `<value>` | `<value>` | pending |

Report paired deltas only when both arms used the same prompt, base commit,
validation commands, resource limits, and model/CLI versions. Include excluded
cells and their exclusion reasons below the table.

## Evidence Index

Publish or retain an access-controlled bundle with:

| Artifact | Required content |
|---|---|
| `manifest.snapshot.json` | task text paths, arms, target commits, commands, limits |
| `run.json` | harness commit/remote/status, host metadata, start/end timestamps |
| `cells/*/evidence.json` | observed base/final commit, exits, timings, diff SHA-256 |
| `cells/*/driver.{stdout,stderr}.log` | complete solver output with secrets redacted |
| `cells/*/preflight-*.log` | proof that the task condition existed at the base commit |
| `cells/*/validation-*.log` | exact post-change verifier output |
| `cells/*/change.patch` | final base-to-worktree patch, including new files |
| `cells/*/review.json` | named reviewer verdict and failure category |
| `results.json`, `report.md` | generated row data and human-readable summary |
| `SHA256SUMS` | digest of every published artifact |

For SWE Bench Pro, the provenance `manifest.json` is the top-level index for the
run-specific files above. Preserve the complete EvalScope work directory and
per-row native solver logs alongside that manifest even when they are too large
for Git. A manifest validates identity and required outputs; it does not replace
the detailed traces needed for failure analysis.

The pilot evidence schema is specified in
`examples/internal-pilot/evidence.schema.json`. Redact credentials before
sharing, but preserve an internal unredacted checksum manifest when policy
allows so redaction cannot silently change the measured patch or verdict.

## Failure Analysis

Use one short block per failed, rejected, excluded, or timed-out cell:

```text
Task / arm:
Failure stage: selection | checkout | preflight | solver | validation | review
Observed symptom:
Primary category: task-invalid | environment | timeout | no-change |
                  incorrect | regression | unsafe | excessive-scope |
                  orchestration | verifier-gap | human-rejected
Evidence paths:
Diff SHA-256:
Root cause (fact vs inference):
Would the other arm face the same cause?: yes | no | unknown
Corrective action / owner:
Disposition: include-as-failure | exclude-with-reason | rerun
```

Infrastructure failures may be excluded only by a rule written before results
are inspected and applied symmetrically to both arms. Solver timeouts,
incorrect changes, verifier rejection, and no-change outcomes are results, not
infrastructure exclusions.
