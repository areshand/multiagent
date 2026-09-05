# GitHub Move Audit Request

## Metadata

- Runbook ID: `github.move-audit`
- Version: `1.0.0`
- Prod MCP operations: `github.read-audit-pr`, `github.request-move-audit`
- Set `target` to `{"cluster":"external-services","environment":"production","namespace":"github","service":"installation"}`.

## Goal

Queue one security audit for the exact head commit of a caller-selected GitHub
pull request. Repository eligibility is determined by the deployment GitHub
App installation: a repository the App cannot access is rejected. The request
does not accept credentials, workflow names, refs, URLs, callbacks, or arbitrary
runner inputs.

## Request phase

1. Confirm the original authenticated goal identifies one repository and pull-request number and explicitly asks to run a Move security audit.
2. Set the phase to `discover` and use `github.read-audit-pr` at the version returned by `multiagent ops describe github.read-audit-pr` to read that pull request through the deployment GitHub App installation. Record its current 40-character lowercase `headSha`.
3. Set the phase to `request`, operation to `github.request-move-audit` at the live described version, and parameters to exactly `repository`, `pullRequest`, `expectedHeadSha`, and `mode` (`light`, `core`, or `thorough`).
4. Obtain independent safety and operations reviews of the exact repository, PR number, head SHA, and mode. The review must account for the compute and untrusted-code exposure of the selected mode.
5. Execute once. Persist the durable prod-mcp receipt and returned audit request ID. Do not retry an unknown or failed dispatch automatically.
6. Report `queued` as an accepted audit request, not as a completed or successful audit. Audit findings arrive through the audit workflow's separate result channel.

## Stop conditions

- The repository or pull request differs from the authenticated goal.
- `github.read-audit-pr` cannot establish the exact PR head SHA.
- The requested repository is not accessible to the deployment GitHub App.
- The PR head moved after review; obtain fresh evidence and new reviews rather than reusing the old request.
- The request attempts to choose a workflow, ref, URL, credential, callback, prompt, model, or arbitrary audit-runner parameter.
- Either reviewer or prod-mcp rejects the request.
