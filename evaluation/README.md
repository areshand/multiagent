# Multiagent Evaluation Framework

The evaluation framework runs adapters against multiagent instruction
profiles and writes both machine-readable scores and a Markdown
report. It is adapter-based so new datasets can be plugged in without rewriting
the runner.

## Concepts

- **Adapter**: loads evaluation tasks, prepares each task workspace, and
  scores completed work. `trace` and `bowu_bench` are unified trace entry
  points; focused adapters remain available for `ops-trace` and
  `conversation-trace`.
- **Task**: a single assignment with a prompt, seed files, and a scorer.
- **Arm**: an instruction profile to compare, such as `baseline` or
  `ponytail-full`. Adapters may load the worker rules or the full orchestrator
  prompt from `prompts/orchestrator.md`, so prompt changes are reflected in
  evaluation. For `orchestration`, `baseline` is a plain Codex planning-mode
  style prompt and `orchestrator` is the current multiagent orchestrator prompt.
- **Run directory**: preserved workspace outputs plus `results.json` and
  `report.md`.

## Commands

List adapters:

```bash
python3 -m evaluation.cli --list
```

Validate an adapter without model/API spend:

```bash
python3 -m evaluation.cli --adapter ponytail --selftest
python3 -m evaluation.cli --adapter orchestration --selftest
python3 -m evaluation.cli --adapter trace --selftest
python3 -m evaluation.cli --adapter ops-trace --selftest
python3 -m evaluation.cli --adapter bowu_bench --selftest
```

Generate a no-agent reference report:

```bash
python3 -m evaluation.cli --adapter ponytail --reference-report --run-root /tmp/multiagent-eval
python3 -m evaluation.cli --adapter orchestration --reference-report --run-root /tmp/multiagent-eval
python3 -m evaluation.cli --adapter trace --reference-report --run-root /tmp/multiagent-eval
python3 -m evaluation.cli --adapter ops-trace --reference-report --run-root /tmp/multiagent-eval
```

Run a small live evaluation:

```bash
python3 -m evaluation.cli \
  --adapter ponytail \
  --agent-cli codex \
  --task safe-path,rate-limit,sql-user,auth-token,csv-sum,cache \
  --arms baseline,ponytail-full \
  --runs 1 \
  --workers 1

python3 -m evaluation.cli \
  --adapter orchestration \
  --agent-cli codex \
  --runs 1 \
  --workers 1
```

## Unified trace benchmark

The `trace` adapter combines the 24-row ops dataset and 12-row conversational
dataset under one private manifest, one CLI entry point, and one report. It is
a suite composition, not a scorer merge:

- ops rows replay the actual user request against pseudonymized mock tool
  evidence without contacting production;
- conversation rows keep measuring completion, route, fanout, write safety,
  and latency;
- reports group rows by suite and never average the two scoring contracts into
  one benchmark-wide correctness claim. An optional LLM judge assesses answer
  quality after candidate timing has stopped.

After generating the two focused datasets, combine them locally:

```bash
python3 -m evaluation.trace_dataset \
  --ops "$HOME/projects/traces/ops-trace-cases.json" \
  --conversation "$HOME/projects/traces/conversation-trace-cases.json" \
  --output "$HOME/projects/traces/trace-cases.json"
```

The combined file remains mode `0600`, `private: true`, and
`publishable: false`. It nests the original manifests, records their hashes,
and does not add raw traces. Combining manifests does not authorize a live
model replay.

Run both suites through one matrix command:

```bash
MULTIAGENT_TRACE_SPLIT=all python3 -m evaluation.cli \
  --adapter trace \
  --agent-cli codex \
  --model gpt-5.6-sol \
  --arms baseline,multiagent,legacy,shortcut \
  --runs 1 \
  --workers 4 \
  --timeout 900
```

The runner applies `baseline,multiagent` only to ops tasks and
`legacy,shortcut` only to conversation tasks; incompatible cross-suite cells
are skipped. Set `MULTIAGENT_TRACE_DATASET` to override the combined manifest
and `MULTIAGENT_TRACE_SPLIT` to `train`, `validation`, `test`, or `all`.
When the local combined manifest is absent, the adapter uses both suites'
synthetic contract cases so CI can validate the unified entry point.

## Trace-derived operations benchmark

The `ops-trace` adapter replays the direct user turn that owned each captured
operation. Contract v4 binds an operation to the nearest preceding
`direct_or_top_level` request in the same rollout, requires a completed source
answer, and includes bounded prior conversation, a pseudonymized reference
answer, and pseudonymized captured tool results. It never includes executable
commands or secret-bearing outputs.

Candidate runs operate in an isolated fixture. They must use
`mock-ops-evidence.md`, must not contact production or edit the fixture, and
must distinguish historical mock evidence from current production state.
Deterministic scoring covers completion, result presence, route, repository
cleanliness, and absence of external operations. The optional semantic judge
assesses whether the response addresses the request. Legacy synthetic plan
cases remain available for CI contract checks.

Generate a private pseudonymized dataset from a redacted trace export:

```bash
python3 -m evaluation.ops_trace_dataset \
  --traces "$HOME/projects/traces" \
  --output "$HOME/projects/traces/ops-trace-cases.json" \
  --max-cases 24
```

The generator rejects internal role prompts, incomplete turns, and requests
whose meaning would be lost by credential or opaque-token redaction. It records
source hashes but does not copy raw commands, raw outputs, account IDs, ARNs,
emails, URLs, or local paths into cases. The result remains `private` and
`publishable: false`; keep it outside the repository.

The adapter automatically uses that default dataset path when it exists and
runs the held-out `test` split by default:

For production multiagent cells, the evaluator passes the pseudonymized direct
user request as the supervisor-authenticated original task. The generated
artifact schema, observed evidence summary, and scoring constraints remain a
separate set of evaluator-owned output requirements. This keeps the direct
request visible to every role through the normal semantic envelope.
Plan-alignment and later reviewers therefore compare work against the request
itself rather than the benchmark wrapper.

```bash
python3 -m evaluation.cli --adapter ops-trace --selftest

python3 -m evaluation.cli \
  --adapter ops-trace \
  --agent-cli codex \
  --model gpt-5.6-sol \
  --arms baseline,multiagent \
  --runs 1 \
  --workers 1
```

`baseline` is one ordinary Codex CLI invocation. `multiagent` runs the current
production Rust/tmux lifecycle in Linux. Both receive the same authenticated
conversation and mock evidence. Build the exact checkout before a live
multiagent comparison:

```bash
docker build -f docker/runtime/Dockerfile -t multiagent:ops-trace-current .
```

Override that image with `MULTIAGENT_OPS_TRACE_IMAGE`. The optional
`orchestrator` arm remains a prompt-only, single-Codex diagnostic; it is not a
measurement of the production multiagent runtime. The production arm currently
requires `--agent-cli codex` and mounts a temporary copy of local Codex
authentication into each isolated benchmark container.

To evaluate every private case instead of the held-out test split:

```bash
MULTIAGENT_OPS_TRACE_SPLIT=all python3 -m evaluation.cli \
  --adapter ops-trace \
  --agent-cli codex \
  --model gpt-5.6-sol \
  --arms baseline,multiagent \
  --runs 1 \
  --workers 1 \
  --timeout 900
```

Override the source or split explicitly when needed:

```bash
MULTIAGENT_OPS_TRACE_DATASET=/path/to/ops-trace-cases.json \
MULTIAGENT_OPS_TRACE_SPLIT=validation \
python3 -m evaluation.cli --adapter ops-trace --selftest
```

Valid split values are `train`, `validation`, `test`, and `all`. When no local
dataset exists, the adapter falls back to three synthetic contract cases so CI
can verify scorer behavior without private data. Set
`MULTIAGENT_OPS_TRACE_DATASET=synthetic` to force that fallback explicitly.

## Bowu Bench

`bowu_bench` is the named local benchmark containing 24 ops rows and 12
conversation rows. Ops rows compare `baseline,multiagent`; conversation rows
compare `legacy,shortcut`. The suites share one manifest, run directory, and
report while retaining their own deterministic scorers.

The canonical local root is `$HOME/projects/traces/bowu_bench`. Generated data
stays outside the coding repository and remains `private: true`,
`publishable: false`, and mode `0600`.

Generate the 12 conversation rows, then combine them with the 24 ops rows:

```bash
python3 -m evaluation.conversation_trace_dataset \
  --sessions "$HOME/.codex/sessions" \
  --sessions "$HOME/.codex/archived_sessions" \
  --output "$HOME/projects/traces/bowu_bench/conversation-trace-cases.json" \
  --max-cases 12

python3 -m evaluation.trace_dataset \
  --benchmark bowu_bench \
  --ops "$HOME/projects/traces/ops-trace-cases.json" \
  --conversation "$HOME/projects/traces/bowu_bench/conversation-trace-cases.json" \
  --output "$HOME/projects/traces/bowu_bench/bowu-bench-cases.json"
```

The generator accepts only multi-turn cases, removes runtime-injected context,
rejects requests mentioning credentials or external mutations, and classifies
read-only cases only when every observed tool call is on a conservative local
read allowlist. Prose-only answers and clarifications are excluded when the
preceding turn used tools, because their reference answers can depend on hidden
evidence that the replay does not provide. Standalone confirmations of a
preceding external mutation are excluded for the same reason. The resulting
dataset contains pseudonymized user/assistant prose, remains `private: true`
and `publishable: false`, and must not be committed or replayed through a model
without explicit approval. The combined manifest nests the source manifests
and their hashes; it does not copy raw traces.

Build the production images for the revisions being compared. Then exhaust all
36 rows serially so candidate latency does not include inter-cell contention:

```bash
BOWU_BENCH_SPLIT=all python3 -m evaluation.cli \
  --adapter bowu_bench \
  --agent-cli codex \
  --model gpt-5.6-sol \
  --arms baseline,multiagent,legacy,shortcut \
  --runs 1 \
  --workers 1 \
  --timeout 900 \
  --judge-model gpt-5.6-sol \
  --judge-workers 1 \
  --judge-timeout 180
```

Candidate cells finish before judging starts, so judge latency does not affect
candidate latency. With judging enabled, `contract_correct` preserves the
deterministic score, `semantic_correct` records the judge verdict at the fixed
0.75 threshold, and `correct` requires both. Safety remains deterministic.
Per-cell judgments and concise reasons are retained in the run directory.

Bowu Bench runs default to `$HOME/projects/traces/bowu_bench/runs`. Override the
manifest with `BOWU_BENCH_DATASET` and select `train`, `validation`, `test`, or
`all` with `BOWU_BENCH_SPLIT`. A live run sends pseudonymized prompts, bounded
history, mock evidence, and reference answers to the selected model provider;
that disclosure requires explicit approval.

The conversation images default to `multiagent:conversation-trace-legacy` and
`multiagent:conversation-trace-shortcut`. Override them with
`MULTIAGENT_CONVERSATION_TRACE_LEGACY_IMAGE` and
`MULTIAGENT_CONVERSATION_TRACE_SHORTCUT_IMAGE`; override the ops image with
`MULTIAGENT_OPS_TRACE_IMAGE`. The focused adapters remain available.

Use `--agent-cli claude` for Claude Code or `--agent-cli codex` for Codex. The
Codex path uses the local Codex configuration and default model unless
`--model` is supplied. Live agent runs may create commits inside their isolated
workspaces; the evaluator scores committed changes since the seeded base commit
plus any remaining uncommitted changes.

Rescore a saved run without another model call:

```bash
python3 -m evaluation.cli --adapter ponytail --rescore evaluation/runs/ponytail/<stamp>
python3 -m evaluation.cli --adapter orchestration --rescore evaluation/runs/orchestration/<stamp>
python3 -m evaluation.cli --adapter trace --rescore evaluation/runs/trace/<stamp>
python3 -m evaluation.cli --adapter ops-trace --rescore evaluation/runs/ops-trace/<stamp>
python3 -m evaluation.cli --adapter bowu_bench --rescore "$HOME/projects/traces/bowu_bench/runs/bowu_bench/<stamp>"
```

## Outputs

Each run writes:

- `results.json`: per-cell rows and aggregate scores.
- `report.md`: Markdown summary grouped by adapter, task, arm, and model.
- one saved workspace per cell, named `TASK__ARM__MODEL__RUN`.

Core metrics:

- `correct`: happy-path behavior works.
- `safe`: adversarial input or required completion axis is handled.
- `src_loc`, `src_files`: changed source size from `git diff`.
- `test_loc`, `test_files`: tests are tracked separately.
- `duration`, `turns`, `tokens`, `cost`: included when the agent CLI reports them.
- `contract_correct`, `semantic_correct`, `semantic_score`: present when the
  optional judge is enabled; `correct` then requires both contract and semantic
  correctness.

Adapter-specific metrics may also appear. The `orchestration` adapter reports
`fanout`, `first_wave_agents`, `max_concurrent_agents`,
`avg_concurrent_agents`, `concurrency_ratio`, `max_wave`, `nodes`,
`first_wave_declared`, and `repo_spawn_commands` for generated `plan.json`
files. The Markdown report shows concurrency columns when those metrics are
present. The `large-update-300` orchestration task is the broad fan-out stress
case: 300 update workers, 20 validation workers, and a final consolidation node.
It expects `max_concurrent_agents=300`.

Low-signal orchestration cases that produced the same concurrency shape for
baseline and orchestrator prompts are intentionally omitted. The remaining
orchestration task exercises broad first-wave fan-out, validation layering, and
consolidation at a size where sequential planning is visible.

## SWE Bench Pro Production Evaluation

There is one supported SWE Bench Pro implementation:

```text
evaluation.swe_bench_pro
-> EvalScope multiagent-native runner
-> production repository baked into each task image
-> python3 -m evaluation.native_solver.solve_swe_prod from /opt/multiagent
-> launch.sh and the production orchestrator/worker/verifier workflow
-> official run_script.sh and parser.py scoring
```

Run one official-order row:

```bash
NATIVE_CODEX_AUTH_JSON="$HOME/.codex/auth.json" \
python3 -m evaluation.swe_bench_pro \
  --sample-offset 0 \
  --sample-count 1 \
  --persistent-cache
```

Run four independent rows concurrently:

```bash
python3 -m evaluation.swe_bench_pro_run_parallel_shards \
  --workers 4 \
  --sample-offsets 0,1,2,3 \
  --native-codex-auth-json "$HOME/.codex/auth.json" \
  --persistent-cache
```

Capture and revalidate a completed run with
`python3 -m evaluation.swe_bench_pro_provenance`. The benchmark-specific
semantic checks consume reusable Git and artifact-integrity primitives from
`evaluation.support.provenance`; see `docs/benchmark.md` for the full command.

The evaluator accepts only the production repository root as bake input. It
does not support noop, devnull, proxy, single-agent, standalone-file, or custom
solver-command modes. The Codex auth file is copied into a live task container
at runtime, scrubbed when the solver exits, and never included in the baked
image.

`evaluation.native_solver.solve_swe_prod` is the packaged container entrypoint,
launched with `python3 -m` from `/opt/multiagent`. The adapter only starts the
workflow, waits for the Rust orchestrator process, exposes committed and
newly created untracked workspace changes, and returns control to EvalScope.
The adapter snapshots pre-existing untracked image residue before launch so it
is not misrepresented as solver output. It does not inspect status narratives,
run validation gates, decide which source changes are correct, or score the
patch. EvalScope extracts the current `/app` diff and passes it to the official
verifier.

Solver prompts and baked source must remain no-leak: they may use issue text,
visible source, local tests, docs, public APIs, and runtime evidence, but not
benchmark row identity, hidden tests, prior official failures, or learned
fixture answers. Validation belongs to the production multiagent workflow;
acceptance belongs exclusively to the official SWE-bench verifier. Adapter
timeouts and crashes remain runner failures because they prevent a reliable
workspace handoff.

## Security Model

The `ponytail` adapter scores agent output by importing and executing the
agent-produced Python file directly in the evaluator process
(`importlib.util.exec_module`). **This is a trust boundary**: only run
evaluations against agents you control in isolated environments. Do not
point this framework at untrusted or externally-sourced agents without
sandboxing the execution environment (e.g. a container or VM).

The Claude CLI arm passes `--disallowedTools Bash` to the agent during
the run, but this restriction applies only to the agent during task
execution — the scorer itself runs agent code in-process without
additional isolation.

## Adding Adapters

Add a module under `evaluation/adapters/` that exposes an `ADAPTER` object with:

- `name`
- `description`
- `tasks: dict[str, EvalTask]`
- `write_seed(workdir, task)`
- `write_reference(workdir, task, kind)` for selftests when references exist

Register the adapter in `evaluation/adapters/__init__.py`.

Put reusable task fixtures and scorers under `evaluation/tasks/` when they are
shared by an adapter.
