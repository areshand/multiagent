# Multiagent Evaluation Framework

The evaluation framework runs adapters against multiagent instruction
profiles and writes both machine-readable scores and a Markdown
report. It is adapter-based so new datasets can be plugged in without rewriting
the runner.

## Concepts

- **Adapter**: loads evaluation tasks, prepares each task workspace, and
  scores completed work. Current adapters are `ponytail` and `orchestration`.
- **Task**: a single assignment with a prompt, seed files, and a scorer.
- **Arm**: an instruction profile to compare, such as `baseline` or
  `ponytail-full`. Adapters may load the worker rules or the full orchestrator
  prompt from `orchestrator_prompt.md`, so prompt changes are reflected in
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
```

Generate a no-agent reference report:

```bash
python3 -m evaluation.cli --adapter ponytail --reference-report --run-root /tmp/multiagent-eval
python3 -m evaluation.cli --adapter orchestration --reference-report --run-root /tmp/multiagent-eval
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

Use `--agent-cli claude` for Claude Code or `--agent-cli codex` for Codex. The
Codex path uses the local Codex configuration and default model unless
`--model` is supplied. Live agent runs may create commits inside their isolated
workspaces; the evaluator scores committed changes since the seeded base commit
plus any remaining uncommitted changes.

Rescore a saved run without another model call:

```bash
python3 -m evaluation.cli --adapter ponytail --rescore evaluation/runs/ponytail/<stamp>
python3 -m evaluation.cli --adapter orchestration --rescore evaluation/runs/orchestration/<stamp>
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
-> evaluation/native_solver/solve_swe_prod.py
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

The evaluator accepts only the production repository root as bake input. It
does not support noop, devnull, proxy, single-agent, standalone-file, or custom
solver-command modes. The Codex auth file is copied into a live task container
at runtime, scrubbed when the solver exits, and never included in the baked
image.

`evaluation/native_solver/solve_swe_prod.py` is the container entrypoint. Its
modules own SWE-specific metadata sanitization, bootstrap, lifecycle, and
public-probe policy. Exact Git snapshots, final-diff hash verification, atomic
status, and generic coding guardrails live under `multiagent_framework/` and are
shared by normal production launches.

Solver prompts and baked source must remain no-leak: they may use issue text,
visible source, local tests, docs, public APIs, and runtime evidence, but not
benchmark row identity, hidden tests, prior official failures, or learned
fixture answers. Adapter probes are additional pre-submission evidence; the
official verifier remains authoritative.

`EVAL_VALIDATION_PROBE_TIMEOUT` caps each adapter-selected public probe at 300
seconds by default. The adapter helper defaults to advisory mode and does not
edit source. The production-native progress watchdog can launch one bounded
repair worker after a non-empty diff remains stale; it uses only
repository-visible evidence and is part of the production convergence loop.

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
