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

The final three recoveries in the historical aggregate were:

| Row | Repository | Baked solver commit | Run prefix | Native wall | Score |
| ---: | --- | --- | --- | ---: | ---: |
| 2 | NodeBB/NodeBB | [`d94100dd27e7dd77fa4f7f9a0517ae00a3094fb7`](https://github.com/areshand/multiagent/commit/d94100dd27e7dd77fa4f7f9a0517ae00a3094fb7) | `swe-bench-pro-prod-pr4-d941-fsm-w0-offset2-count1` | 1233.1s | 1.0 |
| 14 | element-hq/element-web | [`11e8f4a96aa4dbc410ea7409e79746476cc1c188`](https://github.com/areshand/multiagent/commit/11e8f4a96aa4dbc410ea7409e79746476cc1c188) | `swe-bench-pro-prod-pr4-11e-fsm-offset14-count1` | 754.3s | 1.0 |
| 38 | gravitational/teleport | [`a577eba6f7d275004e0eca0b8f459ec5c315f494`](https://github.com/areshand/multiagent/commit/a577eba6f7d275004e0eca0b8f459ec5c315f494) | `swe-bench-pro-prod-pr4-a577-verifier-wait-row38-gpt54` | 1425.5s | 1.0 |

The earlier 33 passes were also accumulated across the run journal, not
produced by the report commit in one batch. The journal is the authoritative
mapping of focused run prefixes, native outcomes, score movement, and tuning
notes.

Relevant row-38 evidence was bound to final diff SHA-256
`275cf530bf8388de5e0de030eef6ee0e9c91744153a171886c26e3a179564f57`.
Independent build verification compiled both changed Go packages, independent
behavior verification accepted the same hash, and the official verifier scored
the patch `1.0`.

The repository does not retain the raw EvalScope work directories, container
logs, and official JSON for every historical focused rerun. The committed run
journal is available, but those absent raw artifacts cannot be independently
reconstructed from Git. A new run should retain the output paths listed below
before publishing a comparative result.

## Pinned Solver Fresh-Run Command

The current production-only benchmark implementation was pinned at
[`f4e23920f6a519bc72790f66eaa8c7bb57804925`](https://github.com/areshand/multiagent/commit/f4e23920f6a519bc72790f66eaa8c7bb57804925).
That commit removed scaffold, proxy, noop, and alternate solver fallbacks. The
following command evaluates official-order rows 0-49 from that exact solver
source. It is a clean new run; it does not recreate the cumulative historical
`36/50` by construction. The two external evaluator checkouts and task-image
digests were not retained with the historical aggregate, so this is not a fully
immutable reproduction until the operator pins and records them before launch.

Prerequisites:

- Docker with `linux/amd64` support and at least 20 GB available to the active
  task container;
- at least 50 GB free disk for image and cache preflight;
- EvalScope checkout at `/private/tmp/evalscope_tmp`;
- SWE Bench Pro OS checkout at `/private/tmp/SWE-bench_Pro-os-complete`;
- valid Codex auth at `$HOME/.codex/auth.json`;
- Python dependencies required by EvalScope and SWE Bench Pro.

Before launching, record and freeze both external checkout commits with
`git -C PATH rev-parse HEAD`, require clean checkouts, and retain every resolved
task image digest. A report without those values is a solver-pinned experiment,
not a fully reproducible benchmark publication.

Run from a clean clone:

```bash
git checkout --detach f4e23920f6a519bc72790f66eaa8c7bb57804925
test "$(git rev-parse HEAD)" = "f4e23920f6a519bc72790f66eaa8c7bb57804925"

NATIVE_CODEX_AUTH_JSON="$HOME/.codex/auth.json" \
python3 -m evaluation.swe_bench_pro \
  --native-solver-source "$PWD" \
  --evalscope-path /private/tmp/evalscope_tmp \
  --swe-bench-pro-repo-path /private/tmp/SWE-bench_Pro-os-complete \
  --work-dir /private/tmp/evalscope-swe-bench-pro-f4e2392-first50 \
  --output evaluation/reports/swe-bench-pro-f4e2392-first50.json \
  --config-json evaluation/reports/swe-bench-pro-f4e2392-first50-config.json \
  --config-yaml evaluation/reports/swe-bench-pro-f4e2392-first50-config.yaml \
  --preflight-output evaluation/reports/swe-bench-pro-f4e2392-first50-preflight.json \
  --on-demand-image-status evaluation/reports/swe-bench-pro-f4e2392-first50-images.json \
  --report-prefix swe-bench-pro-f4e2392-first50 \
  --agent-model-name gpt-5 \
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
  --persistent-cache-root /private/tmp/swe-bench-pro-persistent-cache-f4e2392 \
  --persistent-cache-mode rw
```

The command intentionally omits `--ignore-errors` and does not enable any
diagnostic path that scores a native-gate-rejected or timed-out diff. Those
diagnostic CLI options may exist for infrastructure investigation, but results
produced through them must not be counted as production-solver passes.

Retain at minimum:

- `evaluation/reports/swe-bench-pro-f4e2392-first50.json`;
- the config, preflight, and image-status JSON files named above;
- `/private/tmp/evalscope-swe-bench-pro-f4e2392-first50/reports/`;
- per-row native solver logs and final diff hashes;
- `git rev-parse HEAD`, Docker version, EvalScope commit, SWE Bench Pro commit,
  model name, and run timestamps.

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
