# Known Security TODOs

## Prod-mcp observability discovery hardening

- Replace prompt-only runbook role routing with a typed workflow gate that rejects `worker` whenever the approved plan requires `ops`.
- Make subordinate waiting and resume deterministic in the runtime so provider wakeup tools cannot end a supervisor turn while role state remains `running`.
- Release or reconcile stale role records during session resume instead of asking the model to interpret failed or forbidden worker state.

These items are explicitly deferred while the production deployment is brought to functional end-to-end readiness.

1. Give the authenticated web gateway a dedicated AWS identity without model-provider, KMS-signing, S3, or prod-mcp authority. Session Jobs are now separate pods and receive a distinct Kubernetes service account, but the first deployment keeps the gateway and session service accounts on the same IRSA role while production functionality is proven.
2. Add structured audit records for every `session-control` request, including caller subject, session, semantic action, request digest, timestamp, and result. Never record submitted task plaintext or credentials in the audit event.
3. Replace the pod-lifetime authority registry with a supervisor-owned release protocol so a completed session can relinquish authority without restarting the pod.
4. Harden session path resolution against all parent-directory replacement races. The current helper validates a fixed state root, session identifier, socket type, ownership, group, and mode before dropping privileges.
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

## Trace exporter access boundary

- Add a deployment assertion that only the authority supervisor and read-only exporter receive the dedicated trace-export GID; role processes must continue to drop all supplementary groups.
- Historical mode-`0700` role evidence is deliberately left unchanged and skipped by the exporter. Define a separately authorized migration/export procedure if those historical private files must be recovered.
