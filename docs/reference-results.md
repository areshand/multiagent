# Reference Results and Evidence

This page defines the evidence required before publishing a benchmark claim.
It is intentionally a template, not a claim that a live agent run has happened.
The built-in `--reference-report` command scores authored good/bad fixtures; it
calibrates scorers but does **not** measure an agent or prove orchestration gains.

## Pinned Reproduction Point

The reference implementation inspected for this protocol is
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
