# Validation Scheduling Playbook

Use this playbook before launching, duplicating, or replacing expensive
compile/test validation. Its purpose is to keep validation parallel where paths
are independent while preventing same-package command storms that turn real
solver work into local resource failures.

## Validation Lease

Treat each expensive validation target as having one active lease:

- `package/path`: the package, test file, component suite, or build target.
- `command`: the exact command or command family that proves the target.
- `owner`: the worker, verifier, or coordinator responsible for the result.
- `state`: planned, running, passed, failed, timed-out, stale, or released.
- `started`: best-known start time or pane/process evidence.
- `resource-risk`: CPU, memory, cache contention, network, or emulation risk.

The orchestrator owns the lease table in its notes or checkpoint updates. A
worker or verifier may receive a lease in its first instruction, but it must
not silently take a second lease for the same package/path.

## Routing Rules

- If a package/path has a running lease, poll that owner before starting another
  equivalent command.
- Do not spawn a verifier for a worker while that worker still owns a running
  validation lease. First capture/poll the worker until the leased command
  reaches passed, failed, timed-out, stale, or released. Then pass the captured
  result to the verifier.
- If the owner is stale, capture the pane and process list, then explicitly
  kill/finalize or release the lease before replacement work starts.
- If two independent validators can run safely, record why they are disjoint:
  different package/path, different cache/resource boundary, or intentionally
  separate resource budget.
- If the orchestrator cannot tell whether validators overlap, spawn
  `prompts/roles/validation-coordinator.md` with the active agent table,
  process list, owned paths, and intended commands.
- Prefer one validation owner for each package/path. Other agents should inspect
  that result rather than rerunning the same expensive command.
- A verifier should normally receive read-only review ownership, not a
  validation lease, when the worker has already run or is still running the
  selected package command.

## Worker And Verifier Instructions

When assigning a worker or verifier that may validate, include:

- validation lease target, command, and owner
- commands it may run without asking
- commands it must not duplicate
- how to report timeout/failure without launching a replacement command
- if the verifier must inspect a worker-run command, the worker pane/log excerpt
  and whether the lease is already released

If no validation lease is granted, the agent may do read-only test discovery and
cheap source-level probes, but it must ask/report before starting a long
compile/test command for a package already owned by another live agent.

If a verifier sees an equivalent validation command still running, its correct
output is an orchestration finding: `blocked-validations:` plus the active owner
and command. It should not wait by launching a second copy.

## Output Shape

When reporting validation state to the user or a follow-up agent, include:

1. `validation-leases:` package/path, owner, command, state.
2. `released-leases:` stale or completed leases that are safe to replace.
3. `blocked-validations:` commands intentionally not duplicated and why.
4. `next-validation-owner:` the one agent expected to produce each remaining
   package/path result.
