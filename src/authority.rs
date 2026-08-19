use crate::config;
use serde::{Deserialize, Serialize};

/// The complete mutation surface accepted by the authority supervisor.
///
/// CLI parsing happens before a request crosses the Unix socket. The server
/// authorizes this enum instead of independently interpreting command strings,
/// so routing and role policy cannot drift apart.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct AuthorityRequest {
    operation: AuthorityOperation,
    args: Vec<String>,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum AuthorityOperation {
    Workflow,
    Decision,
    Dag,
    OrchestratorComplete,
    SupervisorRegisterLaunch,
    SupervisorRenewLaunch,
    SupervisorShutdown,
    AssignmentCreate,
    AssignmentShow,
    AssignmentStatus,
    AssignmentCheck,
    CheckpointUpdate,
    CheckpointShow,
    FindingCreate,
    FindingShow,
    FindingList,
    FindingDismiss,
    TodoCreate,
    TodoShow,
    TodoList,
    TodoAssign,
    TodoStatus,
    ResolutionCreate,
    TodoClose,
    ValidationLeaseAcquire,
    ValidationLeaseStatus,
    ValidationLeaseShow,
    ValidationLeaseList,
    GateCheck,
    ProdOpsRoleAssign,
    ProdOpsRoleRevoke,
    ProdOpsPermitIssue,
    ProdOpsSubmit,
    /// A `decision commit --owner-type user ...` request.
    ///
    /// Role UIDs (`ORCHESTRATOR_UID`, `WRITER_UID`, `READER_UID`,
    /// `SUPERVISOR_UID`) are dedicated, fixed service accounts that no human
    /// ever logs in as directly, so any peer UID that isn't one of those four
    /// IS a real human/operator session by construction. This makes it
    /// impossible for any agent role process to satisfy `authorized_for` for
    /// this variant no matter what `--owner-type` value it claims, because its
    /// peer UID is always one of the four fixed constants.
    DecisionCommitUserOwned,
}

impl AuthorityRequest {
    pub fn from_cli(command: &str, args: &[String]) -> Option<Self> {
        let (operation, forwarded) = match command {
            "workflow" => (AuthorityOperation::Workflow, args),
            "decision" => {
                let owner_type_user = args.first().map(String::as_str) == Some("commit")
                    && args
                        .windows(2)
                        .any(|pair| pair[0] == "--owner-type" && pair[1] == "user");
                if owner_type_user {
                    (AuthorityOperation::DecisionCommitUserOwned, args)
                } else {
                    (AuthorityOperation::Decision, args)
                }
            }
            "dag" => (AuthorityOperation::Dag, args),
            "orchestrator" if args == ["complete"] => {
                (AuthorityOperation::OrchestratorComplete, &args[1..])
            }
            "supervisor" => match args.first().map(String::as_str) {
                Some("register-launch") => {
                    (AuthorityOperation::SupervisorRegisterLaunch, &args[1..])
                }
                Some("renew-launch") => (AuthorityOperation::SupervisorRenewLaunch, &args[1..]),
                Some("stop") => (AuthorityOperation::SupervisorShutdown, &args[1..]),
                _ => return None,
            },
            "subagent" => {
                let operation = match args.first().map(String::as_str) {
                    Some("assignment-create") => AuthorityOperation::AssignmentCreate,
                    Some("assignment-show") => AuthorityOperation::AssignmentShow,
                    Some("assignment-status") => AuthorityOperation::AssignmentStatus,
                    Some("assignment-check") => AuthorityOperation::AssignmentCheck,
                    Some("checkpoint-update") => AuthorityOperation::CheckpointUpdate,
                    Some("checkpoint-show") => AuthorityOperation::CheckpointShow,
                    Some("finding-create") => AuthorityOperation::FindingCreate,
                    Some("finding-show") => AuthorityOperation::FindingShow,
                    Some("finding-list") => AuthorityOperation::FindingList,
                    Some("finding-dismiss") => AuthorityOperation::FindingDismiss,
                    Some("todo-create") => AuthorityOperation::TodoCreate,
                    Some("todo-show") => AuthorityOperation::TodoShow,
                    Some("todo-list") => AuthorityOperation::TodoList,
                    Some("todo-assign") => AuthorityOperation::TodoAssign,
                    Some("todo-status") => AuthorityOperation::TodoStatus,
                    Some("resolution-create") => AuthorityOperation::ResolutionCreate,
                    Some("todo-close") => AuthorityOperation::TodoClose,
                    Some("validation-lease-acquire") => AuthorityOperation::ValidationLeaseAcquire,
                    Some("validation-lease-status") => AuthorityOperation::ValidationLeaseStatus,
                    Some("validation-lease-show") => AuthorityOperation::ValidationLeaseShow,
                    Some("validation-lease-list") => AuthorityOperation::ValidationLeaseList,
                    Some("gate-check") => AuthorityOperation::GateCheck,
                    _ => return None,
                };
                (operation, &args[1..])
            }
            "prod-ops" => {
                let operation = match args.first().map(String::as_str) {
                    Some("role-assign") => AuthorityOperation::ProdOpsRoleAssign,
                    Some("role-revoke") => AuthorityOperation::ProdOpsRoleRevoke,
                    Some("permit-issue") => AuthorityOperation::ProdOpsPermitIssue,
                    Some("submit") => AuthorityOperation::ProdOpsSubmit,
                    _ => return None,
                };
                (operation, &args[1..])
            }
            _ => return None,
        };
        Some(Self {
            operation,
            args: forwarded.to_vec(),
        })
    }

    pub fn shutdown() -> Self {
        Self {
            operation: AuthorityOperation::SupervisorShutdown,
            args: Vec::new(),
        }
    }

    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn is_shutdown(&self) -> bool {
        self.operation == AuthorityOperation::SupervisorShutdown
    }

    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn authorized_for(&self, uid: u32) -> bool {
        if uid == 0 {
            return true;
        }
        match self.operation {
            AuthorityOperation::Workflow
            | AuthorityOperation::Decision
            | AuthorityOperation::Dag
            | AuthorityOperation::OrchestratorComplete
            | AuthorityOperation::SupervisorRegisterLaunch
            | AuthorityOperation::SupervisorRenewLaunch
            | AuthorityOperation::SupervisorShutdown
            | AuthorityOperation::AssignmentCreate
            | AuthorityOperation::AssignmentShow
            | AuthorityOperation::AssignmentStatus
            | AuthorityOperation::AssignmentCheck
            | AuthorityOperation::TodoCreate
            | AuthorityOperation::TodoAssign
            | AuthorityOperation::TodoStatus
            | AuthorityOperation::GateCheck => uid == config::ORCHESTRATOR_UID,
            AuthorityOperation::FindingCreate => uid == config::READER_UID,
            AuthorityOperation::FindingDismiss | AuthorityOperation::TodoClose => {
                matches!(uid, config::ORCHESTRATOR_UID | config::READER_UID)
            }
            AuthorityOperation::ResolutionCreate => uid == config::WRITER_UID,
            AuthorityOperation::CheckpointUpdate | AuthorityOperation::CheckpointShow => matches!(
                uid,
                config::ORCHESTRATOR_UID | config::WRITER_UID | config::READER_UID
            ),
            AuthorityOperation::FindingShow
            | AuthorityOperation::FindingList
            | AuthorityOperation::TodoShow
            | AuthorityOperation::TodoList
            | AuthorityOperation::ValidationLeaseShow
            | AuthorityOperation::ValidationLeaseList => matches!(
                uid,
                config::ORCHESTRATOR_UID | config::WRITER_UID | config::READER_UID
            ),
            AuthorityOperation::ValidationLeaseAcquire
            | AuthorityOperation::ValidationLeaseStatus => {
                matches!(uid, config::WRITER_UID | config::READER_UID)
            }
            AuthorityOperation::ProdOpsRoleAssign
            | AuthorityOperation::ProdOpsRoleRevoke
            | AuthorityOperation::ProdOpsPermitIssue
            | AuthorityOperation::ProdOpsSubmit => matches!(uid, 0 | config::SUPERVISOR_UID),
            AuthorityOperation::DecisionCommitUserOwned => !matches!(
                uid,
                config::ORCHESTRATOR_UID
                    | config::WRITER_UID
                    | config::READER_UID
                    | config::SUPERVISOR_UID
            ),
        }
    }

    pub fn into_cli(self) -> (String, Vec<String>) {
        let (command, subcommand) = match self.operation {
            AuthorityOperation::Workflow => ("workflow", None),
            AuthorityOperation::Decision => ("decision", None),
            AuthorityOperation::Dag => ("dag", None),
            AuthorityOperation::OrchestratorComplete => ("orchestrator", Some("complete")),
            AuthorityOperation::SupervisorRegisterLaunch => ("supervisor", Some("register-launch")),
            AuthorityOperation::SupervisorRenewLaunch => ("supervisor", Some("renew-launch")),
            AuthorityOperation::SupervisorShutdown => ("supervisor", Some("shutdown")),
            AuthorityOperation::AssignmentCreate => ("subagent", Some("assignment-create")),
            AuthorityOperation::AssignmentShow => ("subagent", Some("assignment-show")),
            AuthorityOperation::AssignmentStatus => ("subagent", Some("assignment-status")),
            AuthorityOperation::AssignmentCheck => ("subagent", Some("assignment-check")),
            AuthorityOperation::CheckpointUpdate => ("subagent", Some("checkpoint-update")),
            AuthorityOperation::CheckpointShow => ("subagent", Some("checkpoint-show")),
            AuthorityOperation::FindingCreate => ("subagent", Some("finding-create")),
            AuthorityOperation::FindingShow => ("subagent", Some("finding-show")),
            AuthorityOperation::FindingList => ("subagent", Some("finding-list")),
            AuthorityOperation::FindingDismiss => ("subagent", Some("finding-dismiss")),
            AuthorityOperation::TodoCreate => ("subagent", Some("todo-create")),
            AuthorityOperation::TodoShow => ("subagent", Some("todo-show")),
            AuthorityOperation::TodoList => ("subagent", Some("todo-list")),
            AuthorityOperation::TodoAssign => ("subagent", Some("todo-assign")),
            AuthorityOperation::TodoStatus => ("subagent", Some("todo-status")),
            AuthorityOperation::ResolutionCreate => ("subagent", Some("resolution-create")),
            AuthorityOperation::TodoClose => ("subagent", Some("todo-close")),
            AuthorityOperation::ValidationLeaseAcquire => {
                ("subagent", Some("validation-lease-acquire"))
            }
            AuthorityOperation::ValidationLeaseStatus => {
                ("subagent", Some("validation-lease-status"))
            }
            AuthorityOperation::ValidationLeaseShow => ("subagent", Some("validation-lease-show")),
            AuthorityOperation::ValidationLeaseList => ("subagent", Some("validation-lease-list")),
            AuthorityOperation::GateCheck => ("subagent", Some("gate-check")),
            AuthorityOperation::ProdOpsRoleAssign => ("prod-ops", Some("role-assign")),
            AuthorityOperation::ProdOpsRoleRevoke => ("prod-ops", Some("role-revoke")),
            AuthorityOperation::ProdOpsPermitIssue => ("prod-ops", Some("permit-issue")),
            AuthorityOperation::ProdOpsSubmit => ("prod-ops", Some("submit")),
            AuthorityOperation::DecisionCommitUserOwned => ("decision", None),
        };
        let mut args = self.args;
        if let Some(subcommand) = subcommand {
            args.insert(0, subcommand.to_string());
        }
        (command.to_string(), args)
    }

    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    pub fn display(&self) -> String {
        let (command, args) = self.clone().into_cli();
        match args.first() {
            Some(subcommand) => format!("{command} {subcommand}"),
            None => command,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::AuthorityRequest;
    use crate::config;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn typed_api_excludes_runtime_and_arbitrary_execution() {
        assert!(AuthorityRequest::from_cli("workflow", &strings(&["status"])).is_some());
        assert!(AuthorityRequest::from_cli("subagent", &strings(&["assignment-create"])).is_some());
        assert!(AuthorityRequest::from_cli("agent", &strings(&["run"])).is_none());
        assert!(AuthorityRequest::from_cli("prod-ops", &strings(&["permit-issue"])).is_some());
        assert!(AuthorityRequest::from_cli("prod-ops", &strings(&["submit"])).is_some());
        assert!(AuthorityRequest::from_cli("prod-ops", &strings(&["validate"])).is_none());
        assert!(AuthorityRequest::from_cli("role-exec", &[]).is_none());
        assert!(AuthorityRequest::from_cli("subagent", &strings(&["spawn"])).is_none());
        assert!(AuthorityRequest::from_cli("subagent", &strings(&["worktree-create"])).is_none());
        assert!(AuthorityRequest::from_cli("subagent", &strings(&["validation-run"])).is_none());
    }

    #[test]
    fn authority_mutations_are_role_typed() {
        let workflow = AuthorityRequest::from_cli("workflow", &strings(&["transition"]))
            .expect("workflow request");
        let finding = AuthorityRequest::from_cli("subagent", &strings(&["finding-create"]))
            .expect("finding request");
        let close = AuthorityRequest::from_cli("subagent", &strings(&["todo-close"]))
            .expect("close request");
        assert!(workflow.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!finding.authorized_for(config::ORCHESTRATOR_UID));
        assert!(finding.authorized_for(config::READER_UID));
        assert!(close.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!workflow.authorized_for(config::WRITER_UID));
    }

    #[test]
    fn prod_ops_mutations_are_supervisor_only() {
        for request in [
            AuthorityRequest::from_cli("prod-ops", &strings(&["permit-issue"]))
                .expect("permit-issue request"),
            AuthorityRequest::from_cli("prod-ops", &strings(&["submit"])).expect("submit request"),
            AuthorityRequest::from_cli("prod-ops", &strings(&["role-assign"]))
                .expect("role-assign request"),
            AuthorityRequest::from_cli("prod-ops", &strings(&["role-revoke"]))
                .expect("role-revoke request"),
        ] {
            assert!(!request.authorized_for(config::ORCHESTRATOR_UID));
            assert!(!request.authorized_for(config::WRITER_UID));
            assert!(!request.authorized_for(config::READER_UID));
            assert!(request.authorized_for(config::SUPERVISOR_UID));
            assert!(request.authorized_for(0));
        }
    }

    #[test]
    fn decision_commit_ownership_is_argument_sensitive() {
        let user_owned = AuthorityRequest::from_cli(
            "decision",
            &strings(&[
                "commit",
                "DEC-1",
                "--selected-plan",
                "plan-1",
                "--reason",
                "because",
                "--owner-type",
                "user",
                "--bound-action-sha256",
                &format!("sha256:{}", "a".repeat(64)),
            ]),
        )
        .expect("user-owned commit request");
        assert!(!user_owned.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!user_owned.authorized_for(config::WRITER_UID));
        assert!(!user_owned.authorized_for(config::READER_UID));
        assert!(!user_owned.authorized_for(config::SUPERVISOR_UID));
        assert!(user_owned.authorized_for(12345));

        let orchestrator_owned = AuthorityRequest::from_cli(
            "decision",
            &strings(&[
                "commit",
                "DEC-1",
                "--selected-plan",
                "plan-1",
                "--reason",
                "because",
            ]),
        )
        .expect("orchestrator-owned commit request");
        assert!(orchestrator_owned.authorized_for(config::ORCHESTRATOR_UID));
    }

    #[test]
    fn request_round_trips_to_the_legacy_cli_contract() {
        let original = strings(&[
            "todo-create",
            "todo-1",
            "--task",
            "repair the implementation",
        ]);
        let request = AuthorityRequest::from_cli("subagent", &original).expect("typed request");
        let (command, args) = request.into_cli();
        assert_eq!(command, "subagent");
        assert_eq!(args, original);
    }
}
