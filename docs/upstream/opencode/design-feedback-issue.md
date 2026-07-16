# Issue draft for `anomalyco/opencode`: external agent adapter

## Proposed title

Design feedback: capability-based external CLI agent adapter and conformance tests

## Enhancement

Add an opt-in, process-based external-agent adapter to the current
`anomalyco/opencode` ecosystem, subject to maintainer confirmation of the proper
extension point. This request is for `anomalyco/opencode`, not the archived
`opencode-ai/opencode` repository.

The proposed adapter would invoke a noninteractive external CLI process through
a small versioned contract. It would exchange a structured start request and
JSONL lifecycle/result events, while the host owns workspace policy, environment
allowlisting, timeout, cancellation, process cleanup, and artifact collection.
Concrete Codex/Claude command mappings would remain outside the core contract.

Existing subagent issues cover related delegation and native-agent behavior,
but the upstream audit found no exact duplicate for an external CLI process
contract plus evidence gate. This enhancement should reuse established
subagent/session concepts where they fit. It differs by focusing on process
interoperability, host-enforced permissions, lifecycle cancellation, and
normalized evidence rather than introducing another native subagent design.

Minimal scope:

- Capability manifest plus structured start request.
- A small event set: ready, progress, tool observation, validation, finding,
  artifact, result, and error.
- Stable terminal reason codes and fail-closed handling of missing/malformed
  results.
- Offline conformance fixtures for lifecycle, cancellation, policy, and
  redaction behavior.
- Separate records for requested capabilities and effective runtime policy.

Non-goals:

- No bundled vendor CLI, model, or credentials.
- No attempt to normalize every native tool call.
- No claim that the process protocol itself provides sandboxing.
- No default provider or UI changes.
- No benchmark score reporting.

Questions to settle before code:

1. Should this target a provider/plugin API, ACP-compatible boundary, SDK
   package, standalone bridge, or another extension point?
2. Is there an existing event envelope and artifact abstraction to reuse?
3. Which runtime API should own process-tree cancellation and effective policy
   evidence?
4. Should discovery use configuration, executable manifests, or package
   registration?
5. Would a protocol/conformance-only first PR be useful without a vendor adapter?

Acceptance criteria for a first PR:

- Feature is inert unless explicitly configured.
- A fake adapter completes a request with ordered structured events and an
  artifact digest.
- Timeout, cancellation, malformed JSON, nonzero exit, and zero exit without a
  result have distinct tested outcomes.
- Process-tree cleanup, canonical path checks, output limits, and log redaction
  are covered offline.
- Requested capabilities are never presented as proof of effective permissions.
- Protocol versioning and unknown-event behavior are documented.

The preferred rollout is a protocol/conformance-only first PR. A concrete CLI
adapter would follow only after the extension boundary is accepted. A separate
integration package is an acceptable outcome if maintainers do not want the
process contract in core.
