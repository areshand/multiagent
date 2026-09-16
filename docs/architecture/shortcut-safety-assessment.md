# Shortcut completion safety assessment

Baseline: `fcf23b6`, synchronized with fetched `origin/main` on September 16,
2026. The assessment started at `3097ce3` (plan-alignment #91, bounded
Executions #86); the subsequent #96 commit only reconciles production TODO
status and is now included without changing the local implementation. The original
`/Volumes/Extreme/projects/multiagent` checkout was inspected read-only; its
uncommitted lifecycle, prompt, and evaluation changes were not imported or
modified. This worktree already includes newer Execution and terminal-outcome
behavior absent from that checkout. The read-only reviewer's self-wait wording
was also already fixed here.

## Decision

Make the independent answer reviewer optional for repository read-only
investigations. Keep explicit reviewer requests verified, including their output
seal. Apply the existing repository shortcut checks to successful observe/auto
completion, add the existing blocking-finding/TODO gate, and reject pending
reviewed operation publications. Do not change operation authorization,
pre-execution ops review, source plan alignment, or technical review.

The supervisor owns these checks because it owns workflow authority and trusted
launch state. No provider, runbook procedure, credential, or new execution
authority is added to the orchestrator. AD-014 records the decision.

## Actual entry points

| Entry | Trigger and authority | Transition and review | Mechanical checks |
| --- | --- | --- | --- |
| `complete --direct-response` | Conversation-only result; supervisor-authenticated orchestrator in confined deployment | Pristine pre-implementation to complete/succeeded; no reviewer | Original task digest, no started source lifecycle, no launches, no active workflow TODOs or outstanding blocking findings/subagent TODOs, empty repository diff, no reviewed receipts or published requests, persisted bounded result |
| `--clarification`, `--auto-clarification` | Bounded question; automatic variant is a no-op for a non-question | Same direct route; cannot silently finish an active implementation | Same direct checks plus question validation |
| `complete --read-only` | Completed repository reader; reader/reviewer launches only | Pristine pre-implementation to complete/succeeded; reviewer now optional | Same common shortcut checks; every launch read-only and completed; at least one reader. Explicit reviewer requires matching completed launch, workflow-bound evidence, matching output hash and exact passing diff marker |
| `complete --observe`, read-only `--auto` | Current immutable Execution has no source-write/reviewed-ops effects; user or external observe origin | Pristine pre-implementation to complete/succeeded, no reviewer | Common shortcut checks plus actual read-only Execution and every workflow launch read-only/completed; no mandatory reader. Read-only scout or other confined evidence roles remain allowed |
| `complete --external-only` | Reviewed runbook operation(s), no source lifecycle started | Pre-implementation to complete; succeeded only when all counted terminal receipts succeed, otherwise failed | Original task, source-state fields, workflow TODOs, terminal reviewed receipt/disposition, existing verifier/finding/TODO gate. Independent ops review happens before execution, not at this completion command |
| `complete --request-review` | Read-only Execution proposes exact paths and/or reviewed-ops in a bounded question | Terminal review_requested; immutable proposal for human decision, not successful work | Read-only authority, pre-implementation phase, original task, bounded question/effects; unresolved work may remain because escalation does not claim completion |
| `complete --human-review` | A finalized independent reviewer cannot approve and asks one bounded question | Terminal review_requested, not succeeded; pending work is retained | Workflow-bound sealed reviewer identity/output hash and exact persisted question. No operation permit is authorized by this outcome |
| Plain `complete` / `execute-iteration` | Source implementation | Plan alignment, worker graph completion/finalization, frozen candidate, post-implementation technical review, supervisor completion | Remains the source lifecycle; no shortcut introduced |

`--auto` in an effect-bearing Execution retains the existing automatic
clarification behavior. It does not turn an implementation result into a
reviewer-free source completion.

## Boundary and evidence analysis

- **Source writes:** completed read-only permissions and unchanged diffs are
  independent checks. Neither an empty diff nor an LLM verdict proves that no
  transient/ignored/submodule change occurred. Linux UID/Landlock confinement,
  supervisor-owned grants/launches, and path-bound worker ownership remain the
  preventive boundary. The canonical diff is supplementary and requires a Git
  repository. Non-isolated local CLI fixtures are not an adversarial authority
  boundary or a deployed security test.
- **External effects:** local filesystem read-only access never grants production
  read authority. `ops read` separately validates the live prod-mcp capability
  (`read`/`materialize`, mutation=false, no approval roles), caller, task,
  runbook/schema/target and permit, and persists `direct-request.json` plus
  `receipt.redacted.json`. These records are deliberately distinct from reviewed
  `receipt.json` and published `operations/requests`. A read operation requiring
  an approval role still uses reviewed ops. No execution or signing code changes.
- **Incomplete work:** successful local shortcuts cannot absorb a sealed source
  plan, source lifecycle state, pending workflow/subagent TODO, blocking finding,
  running reader, writable launch, reviewed receipt or pending publication.
  The initial observe path previously lacked these completion checks despite
  having pre-execution mutation controls. This change closes that accounting gap.
- **Answer quality:** removing the mandatory reviewer removes an independent
  semantic check that the answer covers the request and is supported by evidence.
  Mechanical completion does not promise semantic correctness. Reader output is
  not an authorization artifact; current supervisor sealing creates independent
  evidence copies for reviewers/scouts, not readers. Even the former reviewer
  prompt could not make public reader output immutable. Optional quality review
  remains available; source and ops reviews remain mandatory where applicable.
- **Evidence seals:** the explicit read-only reviewer path formerly checked
  metadata and pass markers without recomputing its recorded output hash. It now
  verifies that hash. This is evidence integrity, not an additional permission.
- **External-only limitation:** its terminal receipt scan does not reconcile every
  pending request and non-verifier live launch. It also supports non-Git roots,
  so source exclusion relies on lifecycle/confinement rather than a repository
  snapshot. This existing gap is not used to justify relaxing ops review; fuller
  completion accounting is recorded in `docs/TODO.md` as separate work.

## Validation and limits

`cargo test -p multiagent`: 94 tests passed. `tests/lifecycle.sh`: passed,
including reviewer-free read-only/observe/auto success and rejection for running
or writable launches, sealed source state, changed original-task binding,
workflow/subagent TODOs, blocking findings, tracked/untracked changes, reviewed
receipts, pending requests, missing required reader, and substituted optional
reviewer evidence. Explicit reviewed compatibility completion still passes.
Direct-read fixtures demonstrate routing separation, not prod-mcp authorization.
`test_migration_contracts.py`: 20 tests passed. The complete `tests/run.sh`
shell contract suite and `mock_orchestration_e2e.sh` passed using the system
`/bin/bash` (with `/usr/bin:/bin` first in PATH for nested scripts). The earlier
mock setup stall was specific to the initially selected Homebrew Bash invocation;
it did not reproduce with system Bash. Formatting and diff whitespace checks
passed. A further regression confirms human escalation preserves an unresolved
TODO while sealing `review_requested`; successful completion still rejects it.
After the final code cleanup, all 94 Rust tests and the lifecycle suite passed
again. The normal reviewer-free route no longer computes the same repository
diff twice.

Linux UID/Landlock and real provider execution were not verified on this macOS
host. Docker is installed but its daemon is unavailable. No deployment, merge,
production read, or external operation was performed. Deployed reader lifecycle
and operation-boundary acceptance remains in the canonical TODO backlog.
