# Upstream contribution drafts

These documents are local drafts only. Nothing has been posted externally.
Each proposal is self-contained so a maintainer can review it without adopting
the local multiagent framework.

| Area | Proposal | Pre-code issue draft |
| --- | --- | --- |
| SWE-agent benchmark runner, traces, verifier gate, regression harness | [proposal](swe-agent/contribution-proposal.md) | [issue](swe-agent/design-feedback-issue.md) |
| OpenHands benchmark runner, traces, verifier gate, regression harness | [proposal](openhands/contribution-proposal.md) | [issue](openhands/design-feedback-issue.md) |
| `anomalyco/opencode` external CLI agent adapter | [proposal](opencode/contribution-proposal.md) | [issue](opencode/design-feedback-issue.md) |
| Security/runtime evidence gates | [proposal](security-runtime/contribution-proposal.md) | [issue](security-runtime/design-feedback-issue.md) |

The issue drafts intentionally request design feedback before implementation.
Repository names, extension points, schemas, and test locations must be checked
against the target upstream at contribution time.

The drafts reflect an upstream audit performed before writing: OpenHands issues
#14590 and #13781 are related but do not cover this exact runner/evidence gate,
and existing `anomalyco/opencode` subagent issues do not define the proposed
external-process adapter. Recheck issue state immediately before posting.
