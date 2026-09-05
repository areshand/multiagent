# GitOps integration boundary

This directory documents application-owned deployment contracts. It does not
contain the production GitOps source of truth.

The separate `InternalServices` repository owns Kubernetes resources, workload
identities, IAM, KMS, secrets, endpoints, storage, ingress, and concrete
runbook artifacts. Application code may define configuration interfaces and
image contracts here, but must not duplicate environment-specific deployment
configuration.

The application-owned Logger deployment contract is:

- deploy `docker/logger/Dockerfile` as a separate long-lived workload;
- use a dedicated ServiceAccount/workload identity and private Service;
- mount one dedicated writable volume at `/var/lib/logger`;
- mount an Ed25519 private key read-only and set
  `LOGGER_SIGNING_KEY_FILE` to its path;
- mount a client authorization file read-only and set
  `LOGGER_CLIENTS_FILE` to its path;
- set a stable `LOGGER_ID` and signing-key ID;
- allow producers to reach only the append API and permit audit readers to
  reach only the read/verify APIs through deployment network policy;
- do not mount model, KMS, `prod-mcp`, Grafana, Kubernetes, or repository
  credentials;
- back up the append-only ledger, whose records include signed checkpoints, using the
  deployment-owned retention controls;
- give producers a durable retry path or outbox and alert on delivery backlog;
- keep the existing trace sidecar and S3 data path, then submit a bounded
  `trace.artifact_exported` commitment after a successful upload;
- configure at most one active Logger replica for a ledger volume. A
  standby must not write until deployment fencing has transferred ownership.

Concrete Kubernetes resources, secret names, PVC classes, S3 destinations,
network identities, and retention policy remain in `InternalServices`.

The application-owned Slack ingress deployment contract is:

- build `docker/slack-ingress/Dockerfile` as a separate non-root workload;
- expose only `/slack/events`, `/healthz`, and `/readyz` through the
  deployment-managed public ingress;
- mount the Slack signing secret and the independent internal gateway token as
  read-only files; mount neither secret into session agents;
- configure the immutable Hangout channel ID through
  `SLACK_ALLOWED_CHANNEL_IDS`, not a channel name;
- mount one durable writable queue volume at `SLACK_INGRESS_STATE_DIR` and run
  at most one queue writer for that volume;
- allow egress only to the private control-server internal event endpoint;
- configure the control server with the same internal token file, an enabled
  `MULTIAGENT_SLACK_REVIEW_OWNER` terminal username, and the bounded
  `MULTIAGENT_SLACK_REPOSITORY` diagnosis repository;
- configure the session Job template to project immutable Secret key
  `authority-scope` into `MULTIAGENT_AUTHORITY_SCOPE`;
- do not grant the Slack ingress model, repository, GitHub, KMS, `prod-mcp`,
  Kubernetes, Grafana, client-cookie, or production credentials;
- alert when `/readyz` fails or queue depth remains non-zero; and
- verify the real Hangout message, terminal no, and terminal yes acceptance
  paths documented in `slack-ingress/README.md`.

Concrete Slack app IDs, workspace/channel IDs, callback hostnames, secret
names, PVC class, replica policy, NetworkPolicy, certificates, and public
ingress remain `InternalServices` configuration.
