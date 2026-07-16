# Upstream contribution drafts

These documents preserve the design proposals and the status of focused
upstream contributions. Each proposal is self-contained so a maintainer can
review it without adopting the local multiagent framework.

| Area | Proposal | Pre-code issue draft | Upstream status |
| --- | --- | --- | --- |
| SWE-agent benchmark runner, traces, verifier gate, regression harness | [proposal](swe-agent/contribution-proposal.md) | [issue](swe-agent/design-feedback-issue.md) | [SWE-agent #1464](https://github.com/SWE-agent/SWE-agent/issues/1464), closed because the project is maintenance-only |
| OpenHands benchmark runner, traces, verifier gate, regression harness | [proposal](openhands/contribution-proposal.md) | [issue](openhands/design-feedback-issue.md) | Not posted |
| `anomalyco/opencode` external CLI agent adapter | [proposal](opencode/contribution-proposal.md) | [issue](opencode/design-feedback-issue.md) | [OpenCode #37388](https://github.com/anomalyco/opencode/issues/37388) |
| Security/runtime tool-execution audit correlation | [proposal](security-runtime/contribution-proposal.md) | [issue](security-runtime/design-feedback-issue.md) | [OpenHands SDK draft PR #4131](https://github.com/OpenHands/software-agent-sdk/pull/4131) |

The SWE-agent and OpenCode issues were posted on 2026-07-16 after a current
duplicate search found no exact external-worker/evidence-gate or external-CLI
process-contract proposal. SWE-agent's maintainer response makes it unsuitable
for new feature work; no code contribution should follow there. OpenCode's
automation accepted the corrected feature-request template and the issue remains
open. The OpenHands SDK contribution is deliberately
narrower than the complete gate proposal: it adds the missing stable event ID
to existing tool-span metadata and tests correlation with persisted action and
observation events. It remains a draft and is not represented as accepted or
merged.
Repository names, extension points, schemas, and test locations must be checked
against the target upstream at contribution time.

The proposals reflect an upstream audit performed before writing: OpenHands
issues #14590 and #13781 are related but do not cover this exact runner/evidence
gate, and existing `anomalyco/opencode` subagent issues do not define the
proposed external-process adapter. Recheck issue state immediately before
posting.
