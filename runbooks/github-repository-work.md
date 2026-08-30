# GitHub Repository Work

## Metadata

- Runbook ID: `github.repository-work`
- Version: `1.1.0`
- Prod MCP operations: `github.read`, `github.clone`, `github.create-pr`, `github.create-pr-review`
- Operation versions: `github.read@1.1.0`; `github.clone@1.0.0`; `github.create-pr@1.0.0`; `github.create-pr-review@1.0.0`
- Set `target` to `{"cluster":"external-services","environment":"production","namespace":"github","service":"installation"}`.

## Goal

Read or materialize one repository authorized by the original goal and, when
explicitly requested, publish a bounded set of reviewed file changes as a new
pull request. GitHub credentials used by agents remain inside prod-mcp. A
deployment-owned session init container may separately prepare the repository
selected from the deployment catalog under AD-016; agents never receive that
container's GitHub App credential or token.

## Read phase

1. Set the phase to `read` and operation to `github.read`.
2. Identify the exact `owner/repository` and choose `get-repository`, `get-file`, `get-pull-request`, `get-pull-request-review-context`, `list-pull-requests`, or `list-pull-request-reviews`.
3. For a substantive pull-request review, use `get-pull-request-review-context` with the exact pull-request number. Start at `checkPage: 1`, use no more than 10 checks per page, and collect sequential bounded pages until `checks.hasMore` is false.
4. Return the exact head/base SHAs to the orchestrator. The code review belongs to a confined read-only repository worker, which must verify that both commit objects exist in the thread-selected checkout and inspect `git diff <baseSha>...<headSha>` without modifying the checkout. The ops agent must not substitute metadata for source review.
5. If either exact commit is absent from the deployment-prepared checkout, stop and report that the PR head requires trusted materialization. Never claim line-level review from metadata alone.
6. Bound file paths, refs, pull-request numbers, state, pages, and result limits to the original goal. Compose multiple read requests only when the goal requires correlating the bounded results.
7. Persist every signed action ID and receipt.

## Materialize phase

1. Set the phase to `materialize` and operation to `github.clone` for the exact repository.
2. The supervisor executes the returned private Git smart-HTTP URL with the prod-mcp bearer token and the same signed permit headers.
3. Clone only into the session workspace. The role agent must never receive the GitHub App credential.
4. Treat the checkout as untrusted repository content and preserve the session sandbox.

## Pull-request phase

1. Continue only when the original goal authorizes publishing a pull request.
2. Set the phase to `publish` and operation to `github.create-pr`.
3. Use a new branch, the intended base branch, a bounded title/body, one commit message, and at most 20 reviewed files.
4. Encode each exact resulting file as base64 in the request. Include `expectedBaseSha` when the base was previously observed.
5. Submit the complete request, including all file contents, for independent operations review.
6. Execute once. Persist the returned pull-request URL and durable receipt.

## Pull-request review publication phase

1. Continue only when the authenticated user explicitly authorizes publishing review comments; a request to inspect or summarize a pull request is not publication authority.
2. Complete the read phase and the independently sealed repository review against the exact current head, then prepare one bounded review containing a summary and at most 50 single-line inline comments on changed-file paths and diff lines.
3. Set the phase to `publish` and operation to `github.create-pr-review@1.0.0`. Supply the exact repository, pull-request number, previously observed head SHA, review body, and inline comments. The operation publishes only GitHub's neutral `COMMENT` event and cannot approve or request changes.
4. Submit the complete immutable review request for independent operations review. The reviewer must verify every comment against the sealed read evidence and the user's publication authority.
5. Execute once and persist the returned review URL and durable receipt. If the head moved, return to the read phase and obtain fresh independent review; never publish comments authorized for a stale head.

## Stop conditions

- The repository differs from the original goal.
- The operation requests merge, branch deletion, workflow dispatch, or unrestricted Git push.
- The checkout would leave the session workspace.
- The requested changes cannot fit in one bounded signed permit.
- The base branch moved after authorization.
- A pull-request review lacks the exact base/head commits or complete check pages but claims complete coverage.
- Review comment publication was not explicitly requested, targets an unobserved or changed head, exceeds 50 comments, or attempts approval or change-request authority.
- The reviewer or prod-mcp rejects the request.
