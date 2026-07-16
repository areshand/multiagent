# Parallel Execution Playbook

Use this playbook when work can be split across independent agents or when
uncertainty justifies parallel exploration before implementation.

## Fan-Out Rule

Default to broad safe fan-out. Build a dependency graph from true blocking
artifacts, not vague ordering preferences. When multiple useful workers are
ready and their owned paths do not overlap, spawn them in the same wave and
consolidate their outputs later.

If one subtree is blocked, keep spawning every other ready subtree. If work runs
sequentially, state the exact dependency that prevents safe parallelism.

## Exploration Before Commitment

Exploration is parallel work. When a task has material uncertainty, plausible
competing designs, unclear blast radius, or high cost of choosing wrong, spawn
competing exploration agents before committing to implementation.

Balance exploration and exploitation deliberately:

- Use exploration to discover alternatives, constraints, risks, and simpler
  approaches.
- Use exploitation to implement the selected approach once evidence is good
  enough.
- Keep exploration branches independent; synthesize them through the
  orchestrator or a consolidation role.
- Record major alternatives and outcomes with `bin/decision.sh` when useful.
- Stop exploring when extra evidence is unlikely to change the selected plan.

Load `prompts/roles/organizational-learning.md` when assigning explicit
exploration, exploitation, reflection, architecture, or QA roles.
