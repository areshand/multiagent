# Benchmark And Provenance

SWE Bench Pro is the advanced evaluation path for the `areshand/multiagent`
reference implementation. Start with the [local demo](demo.md); the benchmark
requires Docker images, external evaluation checkouts, Codex authentication,
model spend, substantial disk, and roughly 20 GB of memory per active task
container.

## Historical Result Snapshot

The historical first-50 report dated 2026-07-15 records the following
production-native aggregate:

| Scope | Clean official passes | Missing | Rate |
| --- | ---: | ---: | ---: |
| First 50 official-order SWE Bench Pro rows | 36 | 14 | 72% |

Passing official indices:

```text
0, 1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 14, 19, 21, 22, 23, 24, 25, 26,
29, 30, 31, 32, 33, 34, 35, 36, 38, 39, 40, 43, 45, 46, 47, 49
```

Missing official indices:

```text
8, 12, 15, 16, 17, 18, 20, 27, 28, 37, 41, 42, 44, 48
```

This is a cumulative best-known aggregate assembled during iterative solver
tuning and focused failed-row reruns. It is not a single held-out run of rows
0-49 from one immutable solver commit, and it should not be presented as one.
It demonstrates benchmark-harness and orchestration progress; it does not by
itself establish an unbiased comparative model result.

## Result Provenance

- Repository: `areshand/multiagent`
- Report snapshot commit:
  [`c4f1be715034de2a147a04e406da72289f9174aa`](https://github.com/areshand/multiagent/commit/c4f1be715034de2a147a04e406da72289f9174aa)
- Detailed 1,950-line run journal:
  [first-50 summary at that commit](https://github.com/areshand/multiagent/blob/c4f1be715034de2a147a04e406da72289f9174aa/evaluation/reports/swe-bench-pro-prod-multiagent-first50-summary.md)
- Evaluation path: production repository baked into each task image,
  `python3 -m evaluation.native_solver.solve_swe_prod` from `/opt/multiagent`,
  `launch.sh`, production orchestrator/worker/verifier roles, then official
  `run_script.sh` and `parser.py` scoring.
- Clean-pass rule: a row counted only when the native solver returned `rc=0`,
  official verifier evidence was present, and official score was `1.0`.
  Rejected, timed-out, and diagnostic scored diffs were not counted as passes.

The retained evaluator evidence resolves the following historical components:

| Component | Recovered identity | Evidence strength |
| --- | --- | --- |
| EvalScope | Version `1.8.1`; tag commit [`fce1d21391dc2d7b45c9cf0edb9b9e40d526aed3`](https://github.com/modelscope/evalscope/commit/fce1d21391dc2d7b45c9cf0edb9b9e40d526aed3) | The logs record `evalscope_version: 1.8.1`. All 815 retained Python files were byte-identical to that tag; both sorted file-hash manifests produce SHA-256 `0bcef54377e85941a75bb7eb16e48af4b096d566d6621c227918cf4e79c29379`. |
| SWE Bench Pro verifier source | Upstream commit [`ca10a60a5fcae51e6948ffe1485d4153d421e6c5`](https://github.com/scaleapi/SWE-bench_Pro-os/commit/ca10a60a5fcae51e6948ffe1485d4153d421e6c5) | All 3,060 retained `run_scripts` files were byte-identical to the corresponding files at that commit; their sorted file-hash manifest is `2e361238c59240d35976a5f78cd8ed41befd3ef23fbf9ede3c2102a21fbdefce`. |
| Historical dataset copy | 731 rows in the same instance order; SHA-256 `b2d0824b443be47a55dc1ec47136676fdc8fb45b7292f2b578413c61b72067d3` | This was a transformed public JSONL, not a clean checkout file. The upstream `ca10a60` JSONL hash is `b5b2462bfbf5aeb2cb7ba7d215778a1768b85f9d7ad7f748546c7f80a0ad1510`. |
| GitHub CI | PR checks only | PR 4 retained no Actions artifacts. The CI checks prove framework tests, not benchmark results. |

The SWE Bench Pro tree used for the focused runs was therefore a derivative of
`ca10a60`, not a clean checkout of it: 4,496 retained files matched, the JSONL
was transformed, and 89 upstream files were absent. This distinction matters
even though the retained official verifier scripts match byte for byte.

The final three recoveries in the historical aggregate were:

| Row | Repository | Solver commit | Model / auth | Native wall | Score |
| ---: | --- | --- | --- | ---: | ---: |
| 2 | NodeBB/NodeBB | [`d94100dd27e7dd77fa4f7f9a0517ae00a3094fb7`](https://github.com/areshand/multiagent/commit/d94100dd27e7dd77fa4f7f9a0517ae00a3094fb7) | `gpt-5` / `bridge` | 1233.1s | 1.0 |
| 14 | element-hq/element-web | [`11e8f4a96aa4dbc410ea7409e79746476cc1c188`](https://github.com/areshand/multiagent/commit/11e8f4a96aa4dbc410ea7409e79746476cc1c188) | `gpt-5` / `bridge` | 754.3s | 1.0 |
| 38 | gravitational/teleport | [`a577eba6f7d275004e0eca0b8f459ec5c315f494`](https://github.com/areshand/multiagent/commit/a577eba6f7d275004e0eca0b8f459ec5c315f494) | `gpt-5.4` / `chatgpt-auth-json` | 1425.5s | 1.0 |

The three detailed configs, logs, summaries, and image-status files survive in
the local ignored `evaluation/reports/` directory; they are not tracked by Git.
The following concise log facts are reproduced here so the published document
does not depend on those local paths:

| Row | Native exit log | Retained summary |
| ---: | --- | --- |
| 2 | `2026-07-14 15:18:42 ... rc=0 wall=1233.1s timed_out=False` | `score=1.0`, `official_verifier_evidence=true` |
| 14 | `2026-07-14 15:55:43 ... rc=0 wall=754.3s timed_out=False` | `score=1.0`, `official_verifier_evidence=true` |
| 38 | `2026-07-15 06:52:08 ... rc=0 wall=1425.5s timed_out=False` | `score=1.0`, `official_verifier_evidence=true` |

The surviving local image-status files contain these baked-image manifest-list
digests:

| Row | Run prefix | Baked image manifest digest |
| ---: | --- | --- |
| 2 | `swe-bench-pro-prod-pr4-d941-fsm-w0-offset2-count1` | `sha256:3dd143c85e5b093e410df9e7b44c5661cf0d07a0e89a91355f79b726c95705e2` |
| 14 | `swe-bench-pro-prod-pr4-11e-fsm-offset14-count1` | `sha256:6792905509a50d7dd0711d3e13dad7d8725546c32bf4ac52b553ace4d9b1995f` |
| 38 | `swe-bench-pro-prod-pr4-a577-verifier-wait-row38-gpt54` | `sha256:ffe220c85b2f16ee72e622ac23a36e3f4bc15a618954c9c9b2c28e4974ffaae9` |

The surviving Docker daemon currently resolves the corresponding base tags as
follows:

| Row | Base image digest | Last tag time |
| ---: | --- | --- |
| 2 | `sha256:c8017caeba773aa6d61fc05f5751f09f97715dc4d262c08d963562aa6abadf02` | `2026-07-10T23:01:48Z` |
| 14 | `sha256:0dac02327fe1fb1cb7d6a7c0745bff2d08af652ad274199e16e8a1326974ae0b` | `2026-07-06T22:38:00Z` |
| 38 | `sha256:ca78c578e77038573f9624768cd9ef5f540edd194ccacf3ba438e9cfc3d9324f` | `2026-07-11T23:27:39Z` |

Those timestamps predate the runs, but the digests were recovered after the
fact and were not hash-bound into the historical run manifests. They are
corroborating local evidence, not portable historical provenance.

The earlier 33 passes were also accumulated across the run journal, not
produced by the report commit in one batch. The journal is the authoritative
mapping of focused run prefixes, native outcomes, score movement, and tuning
notes.

Relevant row-38 evidence was bound to final diff SHA-256
`275cf530bf8388de5e0de030eef6ee0e9c91744153a171886c26e3a179564f57`.
Independent build verification compiled both changed Go packages, independent
behavior verification accepted the same hash, and the official verifier scored
the patch `1.0`.

The historical aggregate still lacks a uniform model/auth configuration, the
original shell command for every focused run, the in-container Codex CLI
version, a run-time Docker version, base-image digests bound at execution time,
and raw artifacts for every contributing pass. The runner installed unpinned
`@openai/codex` into task images, so the missing historical CLI version cannot
be inferred from the solver commit. These gaps prevent the `36/50` aggregate
from being an independently reproducible single benchmark run.

## Fresh-Run Command

The production-only benchmark implementation was established at
[`f4e23920f6a519bc72790f66eaa8c7bb57804925`](https://github.com/areshand/multiagent/commit/f4e23920f6a519bc72790f66eaa8c7bb57804925).
That commit removed scaffold, proxy, noop, and alternate solver fallbacks. Use a
newer immutable commit containing the pass-through submission boundary
described below, and record its full SHA rather than relying on a branch name.

Prerequisites:

- Docker with `linux/amd64` support and at least 20 GB available to the active
  task container;
- at least 50 GB free disk for image and cache preflight;
- clean EvalScope checkout at commit `fce1d21391dc2d7b45c9cf0edb9b9e40d526aed3`;
- clean SWE Bench Pro checkout at commit `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`;
- valid Codex auth at `$HOME/.codex/auth.json`;
- Python dependencies required by EvalScope and SWE Bench Pro.

From a clean solver checkout, install EvalScope into an isolated environment and
run the official-order first 50:

```bash
git clone https://github.com/modelscope/evalscope.git /private/tmp/evalscope-v1.8.1
git -C /private/tmp/evalscope-v1.8.1 checkout --detach \
  fce1d21391dc2d7b45c9cf0edb9b9e40d526aed3
git clone https://github.com/scaleapi/SWE-bench_Pro-os.git \
  /private/tmp/swe-bench-pro-ca10a60
git -C /private/tmp/swe-bench-pro-ca10a60 checkout --detach \
  ca10a60a5fcae51e6948ffe1485d4153d421e6c5

python3 -m venv /private/tmp/evalscope-v1.8.1-venv
/private/tmp/evalscope-v1.8.1-venv/bin/pip install \
  'evalscope[sandbox]==1.8.1'
source /private/tmp/evalscope-v1.8.1-venv/bin/activate

SOLVER="$PWD"
RUN_ID="swe-bench-pro-$(git rev-parse --short=12 HEAD)-first50"
RUN_ROOT="/private/tmp/$RUN_ID"
test -z "$(git status --porcelain)"

NATIVE_CODEX_AUTH_JSON="$HOME/.codex/auth.json" \
python3 -m evaluation.swe_bench_pro \
  --native-solver-source "$SOLVER" \
  --evalscope-path /private/tmp/evalscope-v1.8.1 \
  --swe-bench-pro-repo-path /private/tmp/swe-bench-pro-ca10a60 \
  --work-dir "$RUN_ROOT/work" \
  --output "$RUN_ROOT/summary.json" \
  --config-json "$RUN_ROOT/config.json" \
  --config-yaml "$RUN_ROOT/config.yaml" \
  --preflight-output "$RUN_ROOT/preflight.json" \
  --on-demand-image-status "$RUN_ROOT/images.json" \
  --report-prefix "$RUN_ID" \
  --agent-model-name gpt-5.4 \
  --sample-offset 0 \
  --sample-count 50 \
  --eval-batch-size 1 \
  --platform linux/amd64 \
  --memory-limit 20g \
  --max-steps 250 \
  --agent-timeout 3600 \
  --eval-timeout 3600 \
  --seed 42 \
  --persistent-cache \
  --persistent-cache-root "$RUN_ROOT/cache" \
  --persistent-cache-mode rw
```

After the run completes, capture a relocatable evidence bundle. The command
fails if any source checkout is dirty, any row lacks official verifier/native
outcome evidence, image identity is incomplete, runtime Codex/Node identity is
missing, or the effective config used `ignore_errors`:

```bash
python3 -m evaluation.swe_bench_pro_provenance capture \
  --bundle "$RUN_ROOT/provenance" \
  --solver-repo "$SOLVER" \
  --evalscope-repo /private/tmp/evalscope-v1.8.1 \
  --swe-bench-pro-repo /private/tmp/swe-bench-pro-ca10a60 \
  --summary "$RUN_ROOT/summary.json" \
  --config-json "$RUN_ROOT/config.json" \
  --config-yaml "$RUN_ROOT/config.yaml" \
  --preflight "$RUN_ROOT/preflight.json" \
  --image-status "$RUN_ROOT/images.json" \
  --evalscope-report \
    "$RUN_ROOT/work/reports/production-multiagent/swe_bench_pro.json" \
  --eval-log "$RUN_ROOT/work/logs/eval_log.log"

python3 -m evaluation.swe_bench_pro_provenance validate \
  "$RUN_ROOT/provenance"
```

The generic framework module copies each required artifact to one fixed,
kind-bound relative path and rejects duplicates, traversal, missing kinds, and
hash mismatches. The SWE adapter then recomputes the sample selection, score,
native outcomes, runtime versions, image IDs, platform, model, and solver-source
digest from those bound artifacts. It does not trust manifest booleans.

The native adapter is not a second verifier. It launches the production
workflow, sanitizes private benchmark metadata, observes terminal status, and
leaves the current task diff for EvalScope. `completed`, `blocked`, an internal
deadline, or a missing status marker do not suppress a patch: after a normal
adapter handoff, the official SWE-bench verifier scores whatever diff remains.
Only launch failures, process crashes, outer task timeouts, and failures that
prevent collecting the workspace abort the evaluation.

The command does not reproduce the tuned historical `36/50` aggregate by
construction. The current image baker still requests unpinned `@openai/codex`;
the bundle records the actual installed version and content-addressed derived
image ID, but bit-for-bit replay additionally requires preserving the derived
images or pinning the Codex package specification.

## Failure Analysis

The historical journal identifies several recurring classes. They should be
reported separately because a native rejection and an official test failure
measure different parts of the system.

| Failure class | Observed behavior | Engineering implication |
| --- | --- | --- |
| Native terminal-state or gate rejection | Useful diffs were sometimes rejected before official scoring because durable completion, verifier, or validation state was absent or stale. | Improve state transitions and repair convergence; do not count rejected diffs as solver passes. |
| Compile and interface mismatch | Patches passed a narrow command but failed when adjacent packages or concrete adapters compiled. | Require final-diff package coverage and interface/adapter parity checks. |
| Parser and collection completeness | First-match or helper-level probes missed complete multi-value output contracts. | Validate product-facing output cardinality through real visible entrypoints. |
| Wrong source ownership | New symbols were added to a plausible adjacent package instead of the package implied by public task vocabulary and tests. | Record source-owner candidates before editing and compile the conceptual owner package. |
| Official hidden-contract miss | Some `rc=0` submissions still scored `0.0` despite public checks. | Treat these as solve-quality misses; do not feed hidden answers back into active prompts. |
| Timeout or interrupted run | No clean source submission reached official scoring. | Report as infrastructure/orchestration incompletion, never as a benchmark pass. |

The strongest row-38 improvement was historical-contract coverage across every
mutated output rather than only the first obvious role field. The strongest
cross-row lesson was that exact-diff evidence and public validation must be
durable, independently rechecked, and tied to the changed packages and output
shape. These are orchestration improvements derived from visible repository
evidence; they do not justify benchmark-specific hidden-test recipes.
