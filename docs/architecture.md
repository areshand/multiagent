# Architecture

Multiagent is an orchestration and evidence layer around existing coding-agent
CLIs. It is not a replacement model or a claim that every task benefits from
parallelism.

The proposed provider-neutral coding-agent boundary is described in the
[backend PRD](coding-agent-backends-prd.md) and
[refactoring design](coding-agent-backends-design.md).

```mermaid
flowchart LR
    U["Real issue + immutable base commit"] --> P["Pilot manifest"]
    P --> R["Pilot runner"]
    R --> B["Baseline: one coding-agent CLI"]
    R --> O["Orchestrated: commander in tmux"]
    O --> A["UID-isolated authority supervisor"]
    A --> C["Contract / scope scouts"]
    A --> W["One path-owned writer"]
    A --> V["Read-only verifier"]
    C --> S["Structured runtime state"]
    W --> S
    V --> S
    S --> G{"Findings closed, commands pass, verifier bound to final diff?"}
    G -->|no| O
    G -->|yes| E["Patch + logs + hash-bound evidence"]
    B --> E
    E --> H["Independent human review"]
    H --> Q["Paired result table and failure analysis"]
```

## Runtime Boundary

`launch.sh` execs the Rust `multiagent launch` command. Rust validates and
exports the target root, state directory, prompt modules, CLI choices, write
policy, and verifier iteration cap before starting the orchestrator. The
orchestrator delegates through `multiagent subagent`; assignments,
checkpoints, findings, todos, validation leases, and verifier evidence are
persisted under `MULTIAGENT_STATE_DIR`. Python under `evaluation/` provides
benchmark execution, status reading, and provenance; it does not implement a
second control plane or participate in normal launches.

On production Linux the orchestrator, writer, readers, and authority supervisor
run as different Unix users. The orchestrator decomposes work and requests typed
transitions over a Unix socket; it does not own protected state or repository
writes. The supervisor issues one-time role launches, permits only one writer,
temporarily grants that writer its predeclared existing paths, and seals reviewer
output before exposing it to the orchestrator. Scouts and verifiers are
read-only. The orchestrator can request follow-up or closure, while fixed rules
and sealed evidence decide whether the protected transition succeeds.
Hash-bound verifier evidence becomes stale when the final diff changes.

## Evaluation Boundary

The built-in adapters exercise deterministic safety/minimalism tasks and
synthetic orchestration plans. Their reference fixtures prove scorer polarity,
not production reliability. SWE Bench Pro drives the production solver inside
task containers and delegates official scoring to the benchmark parser.

The internal pilot sits outside both paths. It clones each real target commit
into isolated baseline and orchestrated cells, invokes a driver through a small
environment contract, runs the same preflight and validation commands, hashes
the resulting patch, and waits for independent human acceptance. This keeps
target selection and organizational adoption outside the framework: a human
team must still volunteer tasks, grant access, and review outcomes.

## Trust Boundaries

- Task owners supply issue text, an immutable reachable commit, reproduction
  commands, validation commands, and acceptance criteria.
- Agent CLIs may edit only the isolated target clone. Their exit code is not an
  acceptance verdict.
- The pilot runner records evidence but does not infer semantic correctness.
- Independent reviewers decide correctness, regression risk, and scope.
- Report authors disclose dirty harnesses, exclusions, reruns, missing logs,
  model/CLI versions, and costs.
