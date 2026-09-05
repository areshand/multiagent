# Multiagent Slack ingress

This service receives Slack Events API callbacks for deployment-allowlisted
on-call channels and hands them to the multiagent control server through a
durable local queue. It does not run agents or hold human/production authority.

## Slack app configuration

1. Create or select a Slack app and enable Event Subscriptions.
2. Point the Request URL at `https://DEPLOYMENT/slack/events`.
3. Subscribe to `message.channels` for a public channel or `message.groups` for
   a private channel, with the corresponding history scope.
4. Invite the app to the target channel when Slack requires membership.
5. Resolve the Hangout channel's immutable Slack channel ID and inject it
   through `SLACK_ALLOWED_CHANNEL_IDS`. Do not configure by mutable channel
   name.

The endpoint also supports Slack's signed URL-verification challenge. Incoming
webhooks are not used because they send messages to Slack rather than receive
channel events.

## Runtime configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `SLACK_SIGNING_SECRET_FILE` | yes | Read-only file containing the Slack app signing secret |
| `SLACK_ALLOWED_CHANNEL_IDS` | yes | Comma-separated immutable channel IDs, initially Hangout only |
| `SLACK_APP_BOT_USER_ID` | no | Bot user/app ID to ignore to prevent response loops |
| `MULTIAGENT_CONTROL_SERVER_URL` | yes | Private control-server base URL |
| `MULTIAGENT_SLACK_INGRESS_TOKEN_FILE` | yes | Read-only file containing the internal delivery bearer token |
| `SLACK_INGRESS_STATE_DIR` | yes in deployment | Writable durable queue directory; defaults to `/var/lib/multiagent-slack` |
| `HOST` / `PORT` | no | Listen address; defaults to `0.0.0.0:8080` |

The control server must receive the same internal token file and configure:

- `MULTIAGENT_SLACK_INGRESS_TOKEN_FILE`
- `MULTIAGENT_SLACK_REVIEW_OWNER=production-e2e` (or another enabled terminal user)
- `MULTIAGENT_SLACK_REPOSITORY`: repository name already present in the deployment catalog
- `MULTIAGENT_SLACK_DIAGNOSIS_CONTEXT` (optional): bounded, non-secret,
  deployment-owned metadata for approved read-only evidence targets; this is
  passed outside the untrusted Slack message and grants no repair authority

The session Job template must expose immutable session Secret keys
`authority-scope` and `mutation-grant.json` as
`MULTIAGENT_AUTHORITY_SCOPE` and `MULTIAGENT_MUTATION_GRANT_JSON`, and bind the
grant to the selected repository and fresh Session ID.

## Local tests

```bash
cd slack-ingress
npm test
```

The tests cover signature verification, timestamp replay rejection, channel
allowlisting, event normalization, durable deduplication, retained retry, and
authenticated delivery. They do not claim that the real Hangout Slack app,
public callback ingress, or deployed session runtime is configured.

## Deployed acceptance

The MVP is complete only after a real message in Hangout proves all of the
following together:

1. Slack receives HTTP 200 from the signed callback.
2. The ingress queue drains the event exactly once into the control server.
3. A `production-e2e`-owned thread starts with `observe` authority.
4. The real session diagnoses using deployed read-only evidence paths.
5. A repair proposal appears automatically in the terminal review window.
6. `no` closes the thread without a new session or production action.
7. On a separate test event, `yes` creates a fresh path-bound
   `user` Session carrying the original review question and digest. Its initial
   Execution has only the proposed exact paths and/or `reviewed-ops` effect.
8. Any production mutation still passes the normal independent reviewer,
   runbook, permit, allowlist, receipt, Logger, and trace gates.

Use `docker/slack-ingress/Dockerfile` from the repository root to build the
non-root service image. Kubernetes resources, secrets, hostname, certificate,
PVC, NetworkPolicy, and concrete channel/workspace IDs belong in
`InternalServices`.
