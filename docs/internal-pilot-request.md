# Internal Pilot Request

A one-page ask a maintainer can send to a single internal team. Full pilot
design, package layout, and run instructions are in
[the internal pilot guide](internal-pilot.md).

## The Ask

One team runs this orchestration harness on 5–10 real, already-triaged bugs or
internal tasks, in a paired design: each task executes once through a normal
single-agent baseline and once through the multiagent orchestration path, from
the same frozen issue text and pinned commit.

This is a bounded experiment, not a workflow change. The question is narrow:
does role separation, parallel ownership, and hash-bound verification improve
human-accepted task reliability enough to justify the extra runtime and cost?

## What The Team Provides

- A named sponsor who grants repository access, approves agent execution, and
  approves output locations and concurrency limits.
- 5–10 selected tasks with frozen issue text, a pinned 40-character base
  commit, a reproduction command, and post-change validation commands —
  excluding active incidents, secrets, production mutation, and tasks whose
  solution is already in the agent context.
- One or more named reviewers, independent of whoever operates the solver, who
  accept or reject each patch before aggregate results are read.

## What The Harness Produces

For every task/arm cell, `examples/internal-pilot/pilot.py` records an
auditable evidence bundle: the complete base-to-worktree patch, command logs,
timings, solver output, checksums, and the human review record. Orchestrated
cells additionally preserve the multiagent runtime state and orchestrator pane
log. The guide defines the final review and summary procedure.

## What This Is Not

- Not a request for broad workflow adoption; the pilot ends when review does.
- No team commitment is evidenced here yet.
- No pilot results are evidenced here yet; current repository evidence covers
  only no-network fixture tests and mocked drivers.

## Completion Evidence Required

The Internal Validation items in [TODO.md](../TODO.md) stay unchecked until
these artifacts exist:

1. A record that the pitch was actually delivered internally (date, audience).
2. A named sponsor and team, with the agreed task list and reviewer names.
3. A completed pilot run directory: validated manifest, per-cell
   `evidence.json`, filled `review.json` files from independent reviewers, the
   generated summary, and checksums.
