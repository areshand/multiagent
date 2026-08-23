# Known Security TODOs

## Prod-mcp observability discovery hardening

- Add a deployment-owned allowlist for Loki label names whose values may be returned. The initial E2E permits any syntactically valid label name against an allowlisted datasource, bounded to 100 returned values.
- Classify and redact sensitive label values before returning discovery receipts. Current response-size and count bounds prevent unbounded output but are not a data-classification policy.
- Version the `grafana.read` operation and certified Markdown runbook whenever the accepted action schema changes. The initial compatibility patch adds read-only discovery actions under `1.0.0` to avoid blocking deployment.
- Persist an explicit authority-proxy attestation in operation evidence showing that prod-mcp bearer and KMS configuration came from the supervisor process, not the ops model environment.
- Improve ops-role guidance and diagnostics so an intentionally scrubbed agent environment is not misreported as missing prod-mcp deployment configuration.
- Replace prompt-only runbook role routing with a typed workflow gate that rejects `worker` whenever the approved plan requires `ops`.
- Make subordinate waiting and resume deterministic in the runtime so provider wakeup tools cannot end a supervisor turn while role state remains `running`.
- Release or reconcile stale role records during session resume instead of asking the model to interpret failed or forbidden worker state.

These items are explicitly deferred while the production deployment is brought to functional end-to-end readiness.

1. Separate the authenticated web gateway from the credential-bearing multiagent runtime pod. The web process is treated as a user-facing transport and should not share model-key mounts, AWS workload credentials, KMS authority, or the prod-mcp bearer token.
2. Add structured audit records for every `session-control` request, including caller subject, session, semantic action, request digest, timestamp, and result. Never record submitted task plaintext or credentials in the audit event.
3. Replace the pod-lifetime authority registry with a supervisor-owned release protocol so a completed session can relinquish authority without restarting the pod.
4. Harden session path resolution against all parent-directory replacement races. The current helper validates a fixed state root, session identifier, socket type, ownership, group, and mode before dropping privileges.
5. Provision named production web users through deployment-owned secret management and rotation. An empty user list must fail deployment readiness rather than leave the UI without an authorized caller.
6. Add rate limits and bounded queues for semantic session submission independently of login throttling.
## Provider-native subagent enforcement

- Deny or remove provider-native `Agent`, `Task`, team, and background-agent
  tools from the supervisor tool surface instead of relying only on prompt
  instructions. These processes do not establish the multiagent Linux role,
  Landlock policy, credential scope, or lifecycle evidence.
- Add a workflow-gate assertion that every accepted scout, worker, ops, and
  reviewer result names a registered `multiagent subagent` assignment and role.

## Per-role temporary directories

- Replace the role-home `TMPDIR` subdirectories with separate per-role mounts
  if stronger storage-level separation is required. The deployment wrappers
  currently direct each coding-agent runtime to `$HOME/tmp`; Landlock blocks
  role writes to the pod's shared `/tmp`.
- Replace prompt-coordinated one-shot ops/reviewer handoffs with a typed durable request-review-execute state machine. The current literal-JSON handoff preserves request identity but depends on model adherence and should become a first-class protocol.
- Make subagent termination reliably reap provider processes across role UIDs through the authority supervisor. Today a closed pane can leave an unkillable role-owned provider process until the pod is restarted.
- Give production operation request artifacts a deployment-managed, immutable evidence store instead of relying on private role-home scratch files before authority execution persists the final receipt.
- Tighten contract artifact transport so the scout emits the canonical header without normalization. The lifecycle parser currently accepts Markdown heading prefixes (`#`) around the exact header to tolerate provider formatting while retaining the original sealed artifact and all structured rule validation.

- [ ] Remove contract-header compatibility normalization after scout transport emits the canonical `contract-artifact: version=1` header byte-for-byte; until then, Markdown heading prefixes are accepted only for that header while rule text and SHA binding remain exact.

- [ ] Remove the prior `grafana-log-read.md` digest from prod-mcp after the corrected trace-owned request-path runbook is merged and the deployment bootstrap checkout is confirmed at that revision. Deployment must eventually enforce image/source runbook parity instead of a two-digest compatibility window.
## Trace exporter access boundary

- Add a deployment assertion that only the authority supervisor and read-only exporter receive the dedicated trace-export GID; role processes must continue to drop all supplementary groups.
- The EKS runtime drops the sidecar's requested `DAC_READ_SEARCH` capability from its permitted and effective sets even though the pod spec requests it. Remove that ineffective declaration after the dedicated-GID deployment is established.
- Historical mode-`0700` role evidence is deliberately left unchanged and skipped by the exporter. Define a separately authorized migration/export procedure if those historical private files must be recovered.
