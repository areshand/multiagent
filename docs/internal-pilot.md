# Internal Pilot

To ask a team to run this pilot, send
[the one-page internal pilot request](internal-pilot-request.md).

## Three-Minute Pitch

This is a benchmark harness and orchestration experiment, not a request to
replace a team's coding workflow. On 5–10 already-triaged infra, evaluation, or
runtime tasks, it runs the same immutable issue/commit pair through a normal
single-agent baseline and the multiagent orchestration path. It then preserves
the patch, command logs, timings, verifier output, and human review in one
auditable bundle.

The question is narrow: does role separation, parallel ownership, and
hash-bound verification improve accepted-task reliability enough to justify
the extra runtime and cost? A pilot can answer that; synthetic scorer fixtures
cannot.

No team is currently asserted here as willing, and no internal-task result is
claimed. A human sponsor still must provide repository access, select tasks,
approve agent execution, and name independent reviewers.

## Pilot Design

- Select 5–10 real, already-understood tasks across infra/eval/runtime. Include
  small and medium changes with objective validation; exclude active incidents,
  secrets, production mutation, and tasks whose expected solution is already in
  the agent context.
- Freeze each task's issue text, 40-character base commit, reproduction command,
  post-change validation commands, and acceptance criteria before execution.
- Run both arms from separate clones of the same commit. Keep model, CLI
  version, timeout, network policy, and validation surface equal where possible.
- Do not give either arm a known solution or a future commit. Randomize task
  order outside the harness if human learning or shared caches could matter.
- Have a reviewer who did not operate the solver assess each patch before
  aggregate results are read.

Five tasks produce ten cells (two arms each); ten tasks produce twenty. Start
with one dry run on a disposable fixture, then execute real cells serially or
under the concurrency limits approved by the repository owner.

## Package

The runnable package is under `examples/internal-pilot/`:

- `pilot.py`: validates manifests, creates isolated clones, invokes drivers,
  captures evidence, and generates summaries.
- `manifest.schema.json`: machine-readable task/arm contract.
- `manifest.template.json`: five deliberately invalid placeholders; it cannot
  be run until a human replaces every placeholder and removes `template_only`.
- `drivers/codex-baseline.sh`: noninteractive single-Codex baseline.
- `drivers/multiagent-codex.sh`: the production `launch.sh` orchestration path,
  using Codex workers by default and a dedicated tmux session.
- `evidence.schema.json`: required per-cell evidence fields.
- `test_pilot.py`: no-network fixture tests for validation and evidence capture.

Python 3.8+, Git, and Bash are required. The included live drivers additionally
require Codex CLI; the orchestrated driver requires tmux. Run from a clean,
committed harness checkout so provenance identifies all executed code.

## Three-Minute Harness Demonstration

This no-network demonstration exercises manifest rejection, five isolated Git
clones, both driver contracts, patch capture, log capture, checksums, and the
human-review transition:

```bash
cd /path/to/multiagent
python3 examples/internal-pilot/test_pilot.py
python3 examples/internal-pilot/pilot.py --help
```

Expected test output ends with `Ran 4 tests` and `OK`. The drivers are mocked in
this demonstration, so it proves the harness and evidence plumbing only. It is
not an agent benchmark, a reliability result, or evidence that an internal team
has adopted the workflow.

## Prepare

```bash
cd /path/to/multiagent
cp examples/internal-pilot/manifest.template.json /secure/path/pilot.json
git rev-parse HEAD                    # set harness_commit to this full SHA
git status --porcelain                # must be empty for a publishable run
python3 examples/internal-pilot/pilot.py validate /secure/path/pilot.json
```

For every task, replace the five template slots with real values. `issue_file`
is relative to the manifest and should contain the frozen task text only.
`repository` may be a local path or an authorized clone URL. The pinned commit
must already be reachable from that repository; the runner never substitutes a
branch tip.

Driver commands are argv arrays, not shell strings. A custom driver receives:

```text
PILOT_HARNESS_ROOT             committed multiagent checkout
PILOT_WORKTREE                isolated target clone
PILOT_CELL_DIR                evidence directory outside the clone
PILOT_PROMPT_FILE             frozen task text
PILOT_TASK_ID / PILOT_ARM     cell identity
PILOT_SOLVER_TIMEOUT_SECONDS  task limit
```

The driver must block until its solver is finished and return the solver exit
code. It must not run acceptance tests or write outside the worktree/cell.

## Execute

```bash
python3 examples/internal-pilot/pilot.py run /secure/path/pilot.json \
  --output /secure/path/pilot-runs/pilot-001

# Optional staged execution, still using independent clones:
python3 examples/internal-pilot/pilot.py run /secure/path/pilot.json \
  --arm baseline --output /secure/path/pilot-runs/pilot-001-baseline
python3 examples/internal-pilot/pilot.py run /secure/path/pilot.json \
  --arm orchestrated --output /secure/path/pilot-runs/pilot-001-orchestrated
```

The runner first checks each reproduction/preflight command against its declared
exit code. It then invokes the solver, records the complete base-to-worktree
patch (including untracked files), runs validation commands, and emits
`evidence.json`. The output path must be outside the harness checkout, and the
runner rechecks harness commit/status after the last cell. A driver exit of zero
plus a nonempty patch and passing commands is only `pending-review`.

Copy each `review.template.json` to `review.json` and fill in a named reviewer,
`accepted` or `rejected`, failure category, notes, and review timestamp. Then:

```bash
python3 examples/internal-pilot/pilot.py summarize \
  /secure/path/pilot-runs/pilot-001
find /secure/path/pilot-runs/pilot-001 -type f -print0 \
  | sort -z | xargs -0 shasum -a 256 \
  > /secure/path/pilot-runs/pilot-001.SHA256SUMS
mv /secure/path/pilot-runs/pilot-001.SHA256SUMS \
  /secure/path/pilot-runs/pilot-001/SHA256SUMS
```

## Criteria

A cell is mechanically valid only when checkout and preflight succeeded, the
solver exited before its timeout, the patch is nonempty, and every post-change
command returned its declared exit. A cell succeeds only after an independent
reviewer also accepts correctness, scope, safety, and regression risk.

The pilot is promising when the orchestrated arm improves paired human-accepted
task rate or catches material defects the baseline missed, with disclosed cost
and latency. It is inconclusive when too few paired cells complete, review is
missing, task selection changes after seeing output, or infrastructure failures
are asymmetric. It fails its adoption hypothesis when reliability is no better
and overhead is materially worse, or when teams cannot operate the workflow
without benchmark authors intervening.

Classify timeouts, no-change outcomes, bad patches, verifier misses, and human
rejections as results. Exclude only predeclared infrastructure faults applied
symmetrically. Use `docs/reference-results.md` for the result table, evidence
index, and failure-analysis format.
