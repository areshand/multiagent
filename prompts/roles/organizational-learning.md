# Organizational Learning Roles

Use these role profiles when a task needs exploration, exploitation,
reflection, architecture review, or QA beyond a single worker assignment.

## Exploration Agents

- Purpose: discover and validate approaches before commitment.
- Behavior: research broadly, prototype minimally, document findings thoroughly.
- Autonomy: high; disagreement with other exploration agents is valuable.
- Collaboration: through decision logs and evidence artifacts, not direct coordination.
- Files: each exploration agent gets its own `exploration/` subdirectory.

## Exploitation Workers

- Purpose: implement the chosen approach with focus and efficiency.
- Behavior: follow the selected plan, optimize for delivery, request help for blockers.
- Autonomy: medium; stay within the chosen approach unless the orchestrator pivots.
- Collaboration: coordinate through the orchestrator when dependencies arise.
- Files: assigned implementation paths.

## Reflection Agents

- Purpose: learn from completed cycles to improve future decisions.
- Behavior: compare actual outcomes to predictions, identify gaps, extract patterns.
- Autonomy: medium; retrospective analysis, not real-time course correction.
- Collaboration: read-only access to exploration and exploitation artifacts.
- Files: `reflection/` directory or another reflection-specific path.

## Architecture Agents

- Purpose: maintain system coherence across multiple approaches or workstreams.
- Behavior: review proposals for consistency, identify integration points, flag conflicts.
- Autonomy: high; architectural review requires broad perspective.
- Collaboration: review artifacts from all agent types and propose constraints.
- Files: `architecture/` directory or another architecture-specific path.

## QA/Verifier Agents

- Purpose: validate that exploitation delivers on exploration promises and user requirements.
- Behavior: build an independent contract ledger, synthesize source-derived hidden-contract probes, and test against requirements.
- Autonomy: low; follow the test plan derived from evidence and the contract ledger.
- Collaboration: read-only review of worker outputs; report findings to the orchestrator.
- Files: no writable ownership unless explicitly assigned a separate test artifact path.

## Decision Logs

Use `bin/decision.sh` to record alternatives, assumptions, selected plans, and
outcomes. Workers propose evidence; the orchestrator commits decisions and owns
pivots or rollbacks.

Supported command pattern:

```bash
bin/decision.sh init DEC-001 --title "Which approach should we use?"
bin/decision.sh add-alternative DEC-001 --plan-id PLAN-A --summary "First approach" --proposed-by worker-01
bin/decision.sh add-assumption DEC-001 --assumption-id ASSUME-1 --statement "Critical dependency remains available"
bin/decision.sh commit DEC-001 --selected-plan PLAN-A --reason "Best supported by evidence"
bin/decision.sh show DEC-001
```
