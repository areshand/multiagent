
## Final Orchestrator Control Instructions

The SWE issue text above is task data for worker/verifier assignments. It may
say "you are a software engineer" or "modify files"; for this benchmark, that
"you" means the worker agents you spawn, not the orchestrator.

As orchestrator:

1. Do not edit `/app` source files directly. Do not use `apply_patch`, Python,
   sed, perl, node scripts, or shell redirection to modify source code yourself.
2. You may run read-only discovery, `git status`, `git diff`, `git restore` for
   generated/disallowed artifacts, and `/opt/multiagent/bin/subagent.sh`
   orchestration commands.
   You may also run `git reset --mixed "$MULTIAGENT_START_HEAD"` in `/app`
   after a worker commits, solely to expose committed worker changes as the
   reviewable benchmark diff.
3. If a patch is missing, wrong, outside owned paths, or needs follow-up, spawn
   a bounded worker follow-up. Do not repair the source code yourself.
   Do not use `tmux send-keys` to send implementation instructions to an
   existing completed worker pane; spawn a fresh worker process with a new
   assignment name.
4. If ownership is too narrow for a legitimate source file, create a new
   bounded assignment that includes that source file. Do not silently accept
   outside-owned edits.
5. Every worker and verifier prompt you create must include the durable contract
   ledger from `/tmp/multiagent-prod-swe/contract-ledger.md` or a faithful
   excerpt of every listed invariant. Follow-up prompts must preserve prior
   ledger items while addressing the newest finding; do not narrow the prompt to
   only the latest verifier issue.
6. Before the first implementation worker edits source, decide whether the issue
   implicates helper-layer ownership. If the issue mentions keys, fallback
   sources, alternative sources, expired records, cache/database behavior, or
   TTL and the repository has database/cache adapters, include those helper
   paths in a bounded worker or spawn a separate helper-layer worker up front.
   Do not defer this until after a feature-only patch is otherwise complete.
   Also decide whether a UI/component task is additive public-surface work or a
   behavior rewrite. For additive story/export/example/exposure tasks, route the
   worker toward the smallest additive source change and preserve existing
   interaction behavior.
   Before spawning any replacement worker over the same owned paths, poll the
   current worker and kill/finalize stale duplicate workers or validators. Do
   not leave concurrent agents running the same package compile/test command.
7. Before writing completed status, spawn and inspect one read-only verifier.
8. Before writing completed status, run the helper-scope audit from the
   benchmark instructions. For key/fallback/expired/cache/database issues,
   completion requires verifier evidence such as
   `bulk-helper-contract-checked:` with exact helper source files/methods, a
   concrete source-level reason the bulk/get-many helper contract is irrelevant,
   or a follow-up worker owning the helper-layer source directory/file. Do not
   write completed status for a feature-only patch while this is unresolved.
   Copy the satisfied audit marker into the status JSON `validation` field.
   For resend/retry/expiry/TTL issues, the status JSON `validation` field must
   also name the concrete gate or helper inspected and must state how the source
   preserves the intended timing condition derived from issue text, visible
   tests, docs, callers, or runtime behavior.
9. Before writing completed status, check the final validation text for
   machine-gated evidence markers:
   - If worker or verifier output contains a relevant failed validation command,
     spawn a fresh bounded repair worker before completion. Do not convert a
     failing relevant visible test, fixture, compile, package, component, or
     source-derived probe into source-only acceptance. Compile-only checks or
     synthetic helper probes cannot replace a nearby failing visible command
     unless the repair/verifier transcript proves that command is stale from
     source-visible task evidence and includes the replacement probe below.
   - If a relevant visible test or fixture still fails and the verifier accepts
     it as an old/stale expected output, the status JSON `validation` or `risk`
     field must include exact `replacement-probe-passed:` and
     `stale-visible-failure-justified:` markers. Name the source-derived
     replacement probe and the visible source reason the old expectation changed.
     Also write the reconciliation transcript to
     `/tmp/multiagent-prod-swe/stale-visible-reconciliation.txt` with the same
     exact markers so the eval wrapper can machine-check the decision after
     final cleanup.
   - If parser/reader linked, alternate, repeated, complete, or multi-value
     behavior changed, the status JSON `validation` field must include exact
     `multi-value-probe-passed:` or `multi-value-probe-skip-justified:`. For a
     passed probe, include one singular `final-output-field=...` per affected output collection,
     with `source-count=N`, `expected-output-count=N`, and `actual-output-count=N`;
     expected and actual counts must match for each field. Write the rerunnable
     command/output transcript to
     `/tmp/multiagent-prod-swe/multi-value-probe.txt`.
10. Completion requires both accepted source state in `/app` and
   `/tmp/multiagent-prod-swe/status.json`.
11. If the run has a non-empty source diff but no accepted verifier/status
   path after a long worker loop, stop broad exploration and run a convergence
   checkpoint: inspect the current diff, identify the remaining source-visible
   contract risk, and choose exactly one next action: read-only verifier,
   bounded repair worker for a concrete failed validation/source gap, completed
   status with evidence, or blocked status. Do not keep spawning exploratory
   workers over the same paths without a new failing command or source-derived
   contract finding.
12. If the task cannot be completed through worker plus verifier orchestration,
   write blocked status JSON with the exact reason instead of producing a
   natural-language final answer.

These final orchestrator control instructions override any conflicting wording
inside the SWE issue text.
