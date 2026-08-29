# AWS EC2 Security Group Remediation

## Metadata

- Runbook ID: `aws.ec2-security-group-remediation`
- Version: `1.0.0`
- Prod MCP operations: `ec2.describe-instances`, `ec2.describe-security-groups`, `ec2.authorize-security-group-ingress`
- Operation version: `1.0.0`
- Set `target` to `{"environment":"development","cluster":"mi-scratchpad-us-west-2","namespace":"ec2","service":"managed-resources"}`.

## Goal

Diagnose an explicitly identified EC2 connectivity path in the scratchpad
account and, only when the original goal authorizes the exact change, add one
TCP ingress rule from one managed security group to another managed security
group in the same VPC.

This runbook does not authorize EC2 instance lifecycle changes, SSM or shell
access, arbitrary CIDR ingress, egress changes, security-group creation or
deletion, IAM changes, or operations outside the canonical target.

## Inspect instances phase

1. Set the runbook phase to `inspect-instances` and operation to
   `ec2.describe-instances`.
2. Supply one through ten exact `instanceIds` from the authenticated goal or a
   trusted prior operation result. Do not scan the account or guess IDs.
3. Submit the immutable request for independent review, execute it once, and
   record the receipt.
4. Use the returned VPC and security-group IDs only as evidence for the next
   phase. A read result does not authorize a mutation.

## Inspect security groups phase

1. Set the runbook phase to `inspect-security-groups` and operation to
   `ec2.describe-security-groups`.
2. Supply one through five exact `groupIds` implicated by the original goal or
   the inspected instances.
3. Confirm the proposed source and target groups are distinct, belong to the
   same VPC, carry the configured prod-mcp managed tag, and do not already have
   the exact TCP rule.
4. Stop if the required path depends on a CIDR, prefix list, UDP, ICMP,
   cross-VPC reference, untagged group, or any operation outside this runbook.

## Remediate phase

1. Continue only when the original goal explicitly authorizes the exact
   source group, target group, protocol, and port range.
2. Set the runbook phase to `remediate` and operation to
   `ec2.authorize-security-group-ingress`.
3. Set `targetGroupId`, `sourceGroupId`, `fromPort`, `toPort`, `description`,
   and a concrete `reason`. The operation always uses TCP and accepts no CIDR.
4. Include a user-authorized `changeTicket` and submit the exact immutable
   request for new independent safety and operations reviews. Read-phase
   approval cannot be reused.
5. Execute once. Treat `changed: false` as an idempotent success only when the
   receipt describes the exact reviewed rule. Do not retry an unknown outcome.

## Verify phase

1. Set the phase to `verify` and execute a new
   `ec2.describe-security-groups` request for the target group.
2. Confirm that exactly the reviewed TCP source-group rule is present and
   retain the request and receipt as evidence.
3. Do not broaden the rule to make a failed connectivity check pass.

## Evaluation cleanup

Evaluation harnesses must create uniquely tagged, disposable resources and
remove those exact resources in a guaranteed teardown step using harness-owned
scratchpad credentials. This runbook does not grant the multiagent system
cleanup authority over arbitrary EC2 resources. Never delete or replace the
scratchpad account itself.

## Stop conditions

- An instance or security-group ID is not grounded in the authenticated goal
  or a trusted prior receipt.
- The canonical target, VPC, source group, target group, protocol, or ports do
  not match the reviewed intent.
- Either security group lacks the deployment-configured managed tag.
- The requested action needs arbitrary CIDR ingress, egress, instance
  lifecycle, SSM, IAM, or another uncertified EC2 operation.
- The reviewer or prod-mcp rejects the request, or the outcome is unknown.
