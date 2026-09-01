# GitOps integration boundary

This directory documents application-owned deployment contracts. It does not
contain the production GitOps source of truth.

The separate `InternalServices` repository owns Kubernetes resources, workload
identities, IAM, KMS, secrets, endpoints, storage, ingress, and concrete
runbook artifacts. Application code may define configuration interfaces and
image contracts here, but must not duplicate environment-specific deployment
configuration.

Phase 2 will update that external GitOps source together with the independent
audit-log image and service deployment.
