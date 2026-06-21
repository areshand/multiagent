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
