use crate::config;
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::env;
use std::path::{Component, Path};

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct MutationGrant {
    kind: String,
    effects: Vec<String>,
    repository: String,
    paths: Vec<String>,
    review_id: String,
    source_session_id: String,
    source_event_id: String,
    question_sha256: String,
    granted_to_session_id: String,
    approved_by: String,
    approved_at: String,
}

pub struct SessionAuthority {
    scope: String,
    granted_paths: BTreeSet<String>,
    source_write: bool,
    reviewed_ops: bool,
}

impl SessionAuthority {
    pub fn scope(&self) -> &str {
        &self.scope
    }

    pub fn permits_workspace_write(&self, root: &Path, paths: &[std::path::PathBuf]) -> bool {
        match self.scope.as_str() {
            "human" => true,
            "approved-repair" if self.source_write => {
                !paths.is_empty()
                    && paths.iter().all(|path| {
                        path.strip_prefix(root)
                            .ok()
                            .and_then(|relative| normalized_repo_path(&relative.to_string_lossy()))
                            .is_some_and(|relative| self.granted_paths.contains(&relative))
                    })
            }
            _ => false,
        }
    }

    pub fn permits_reviewed_ops(&self) -> bool {
        self.scope == "human" || (self.scope == "approved-repair" && self.reviewed_ops)
    }
}

fn bounded(value: &str, max: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max
}

fn normalized_repo_path(value: &str) -> Option<String> {
    if !bounded(value, 512) {
        return None;
    }
    let path = Path::new(value);
    if path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return None;
    }
    Some(path.to_string_lossy().replace('\\', "/"))
}

fn parse_session_authority(
    scope: &str,
    grant_json: &str,
    session_id: &str,
    repository: &str,
) -> Result<SessionAuthority, String> {
    if matches!(scope, "human" | "observe" | "diagnosis-only") {
        if !grant_json.trim().is_empty() && grant_json.trim() != "null" {
            return Err(format!(
                "authority scope {scope} must not carry a mutation grant"
            ));
        }
        return Ok(SessionAuthority {
            scope: scope.into(),
            granted_paths: BTreeSet::new(),
            reviewed_ops: false,
            source_write: false,
        });
    }
    if scope != "approved-repair" {
        return Err("MULTIAGENT_AUTHORITY_SCOPE is invalid".into());
    }
    let grant: MutationGrant = serde_json::from_str(grant_json)
        .map_err(|error| format!("decode approved repair grant: {error}"))?;
    let paths = grant
        .paths
        .iter()
        .filter_map(|path| normalized_repo_path(path))
        .collect::<BTreeSet<_>>();
    let effects = grant.effects.iter().cloned().collect::<BTreeSet<_>>();
    let allowed_effects = ["reviewed-ops".to_string(), "source-write".to_string()]
        .into_iter()
        .collect::<BTreeSet<_>>();
    let source_write = effects.contains("source-write");
    let digest = grant.question_sha256.strip_prefix("sha256:").unwrap_or("");
    if grant.kind != "review-approved-repair"
        || effects.is_empty()
        || effects.len() != grant.effects.len()
        || !effects.is_subset(&allowed_effects)
        || grant.repository != repository
        || grant.granted_to_session_id != session_id
        || source_write != !grant.paths.is_empty()
        || grant.paths.len() > 32
        || paths.len() != grant.paths.len()
        || !bounded(&grant.review_id, 128)
        || !bounded(&grant.source_session_id, 63)
        || !bounded(&grant.source_event_id, 128)
        || digest.len() != 64
        || !digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        || !bounded(&grant.approved_by, 256)
        || !bounded(&grant.approved_at, 64)
    {
        return Err("approved repair grant is incomplete or bound to another session, repository, or path set".into());
    }
    Ok(SessionAuthority {
        scope: scope.into(),
        granted_paths: paths,
        reviewed_ops: effects.contains("reviewed-ops"),
        source_write,
    })
}

pub fn configured_session_authority() -> Result<SessionAuthority, String> {
    let scope = env::var("MULTIAGENT_AUTHORITY_SCOPE")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "human".into());
    parse_session_authority(
        &scope,
        &env::var("MULTIAGENT_MUTATION_GRANT_JSON").unwrap_or_default(),
        &env::var("MULTIAGENT_SESSION").unwrap_or_default(),
        &env::var("MULTIAGENT_REPOSITORY_NAME")
            .or_else(|_| env::var("MULTIAGENT_SESSION_REPOSITORY"))
            .unwrap_or_default(),
    )
}

/// The complete privileged surface accepted by the authority supervisor.
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
    OpsDescribe,
    OpsRead,
    OpsPublishBound,
    OpsPublish,
    OpsExecute,
}

impl AuthorityRequest {
    pub fn from_cli(command: &str, args: &[String]) -> Option<Self> {
        let (operation, forwarded) = match command {
            // Typed workflow context is read-only and verifies the direct caller's
            // kernel UID, so it must not be re-executed as the supervisor UID.
            "workflow" if args.first().map(String::as_str) == Some("context") => return None,
            "workflow" => (AuthorityOperation::Workflow, args),
            "decision" => (AuthorityOperation::Decision, args),
            "dag" => (AuthorityOperation::Dag, args),
            "ops" if args.first().map(String::as_str) == Some("describe") => {
                (AuthorityOperation::OpsDescribe, &args[1..])
            }
            "ops" if args.first().map(String::as_str) == Some("read") => {
                (AuthorityOperation::OpsRead, &args[1..])
            }
            "ops" if args.first().map(String::as_str) == Some("publish-bound") => {
                (AuthorityOperation::OpsPublishBound, &args[1..])
            }
            "ops" if args.first().map(String::as_str) == Some("publish") => {
                (AuthorityOperation::OpsPublish, &args[1..])
            }
            "ops" if args.first().map(String::as_str) == Some("execute") => {
                (AuthorityOperation::OpsExecute, &args[1..])
            }
            "orchestrator"
                if args.first().map(String::as_str) == Some("complete")
                    && (args.len() == 1
                        || (args.len() == 2 && args[1] == "--external-only")
                        || (args.len() == 4
                            && matches!(
                                args[1].as_str(),
                                "--external-only"
                                    | "--direct-response"
                                    | "--clarification"
                                    | "--auto-clarification"
                                    | "--observe"
                            )
                            && args[2] == "--result-file")
                        || valid_request_review_args(args)
                        || (args.len() == 6
                            && matches!(args[1].as_str(), "--read-only" | "--human-review")
                            && args[2] == "--result-file"
                            && args[4] == "--reviewer")) =>
            {
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
            AuthorityOperation::OpsDescribe | AuthorityOperation::OpsRead => matches!(
                uid,
                config::ORCHESTRATOR_UID
                    | config::WRITER_UID
                    | config::READER_UID
                    | config::OPS_UID
                    | config::REVIEWER_UID
            ),
            AuthorityOperation::OpsPublish => uid == config::OPS_UID,
            AuthorityOperation::OpsExecute => {
                matches!(uid, config::OPS_UID | config::REVIEWER_UID)
            }
            AuthorityOperation::OpsPublishBound => uid == config::ORCHESTRATOR_UID,
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
        }
    }

    pub fn allowed_for_authority_scope(&self, scope: &str) -> bool {
        match scope {
            "human" => true,
            "observe" | "diagnosis-only" => match self.operation {
                AuthorityOperation::ResolutionCreate => false,
                AuthorityOperation::AssignmentCreate => {
                    !has_option_value(&self.args, "--role", "exploitation")
                }
                AuthorityOperation::SupervisorRegisterLaunch => {
                    !has_option_value(&self.args, "--access", "workspace-write")
                }
                AuthorityOperation::OrchestratorComplete => matches!(
                    self.args.first().map(String::as_str),
                    Some(
                        "--observe"
                            | "--request-review"
                            | "--direct-response"
                            | "--clarification"
                            | "--auto-clarification"
                            | "--read-only"
                            | "--human-review"
                    )
                ),
                AuthorityOperation::OpsPublishBound
                | AuthorityOperation::OpsPublish
                | AuthorityOperation::OpsExecute => false,
                _ => true,
            },
            _ => false,
        }
    }

    pub fn allowed_for_session_authority(&self, authority: &SessionAuthority) -> bool {
        match authority.scope() {
            "human" | "observe" | "diagnosis-only" => {
                self.allowed_for_authority_scope(authority.scope())
            }
            "approved-repair" => match self.operation {
                AuthorityOperation::OpsPublishBound
                | AuthorityOperation::OpsPublish
                | AuthorityOperation::OpsExecute => authority.permits_reviewed_ops(),
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
                | AuthorityOperation::CheckpointUpdate
                | AuthorityOperation::CheckpointShow
                | AuthorityOperation::FindingCreate
                | AuthorityOperation::FindingShow
                | AuthorityOperation::FindingList
                | AuthorityOperation::FindingDismiss
                | AuthorityOperation::TodoCreate
                | AuthorityOperation::TodoShow
                | AuthorityOperation::TodoList
                | AuthorityOperation::TodoAssign
                | AuthorityOperation::TodoStatus
                | AuthorityOperation::ResolutionCreate
                | AuthorityOperation::TodoClose
                | AuthorityOperation::ValidationLeaseAcquire
                | AuthorityOperation::ValidationLeaseStatus
                | AuthorityOperation::ValidationLeaseShow
                | AuthorityOperation::ValidationLeaseList
                | AuthorityOperation::GateCheck
                | AuthorityOperation::OpsDescribe
                | AuthorityOperation::OpsRead => true,
            },
            _ => false,
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
            AuthorityOperation::OpsDescribe => ("ops", Some("describe")),
            AuthorityOperation::OpsRead => ("ops", Some("read")),
            AuthorityOperation::OpsPublishBound => ("ops", Some("publish-bound")),
            AuthorityOperation::OpsPublish => ("ops", Some("publish")),

            AuthorityOperation::OpsExecute => ("ops", Some("execute")),
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

fn valid_request_review_args(args: &[String]) -> bool {
    if args.len() < 5
        || args.get(1).map(String::as_str) != Some("--request-review")
        || args.get(2).map(String::as_str) != Some("--result-file")
        || args.get(3).is_none_or(String::is_empty)
    {
        return false;
    }
    let mut has_path = false;
    let mut reviewed_ops = false;
    let mut index = 4;
    while index < args.len() {
        match args[index].as_str() {
            "--path" if index + 1 < args.len() && !args[index + 1].is_empty() => {
                has_path = true;
                index += 2;
            }
            "--reviewed-ops" if !reviewed_ops => {
                reviewed_ops = true;
                index += 1;
            }
            _ => return false,
        }
    }
    has_path || reviewed_ops
}
fn has_option_value(args: &[String], option: &str, expected: &str) -> bool {
    args.windows(2)
        .any(|pair| pair[0] == option && pair[1] == expected)
}

#[cfg(test)]
mod tests {
    use super::{parse_session_authority, AuthorityRequest};
    use crate::config;

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn typed_api_excludes_runtime_and_arbitrary_execution() {
        assert!(AuthorityRequest::from_cli("workflow", &strings(&["status"])).is_some());
        assert!(
            AuthorityRequest::from_cli("workflow", &strings(&["context", "workflow-1"])).is_none()
        );
        assert!(AuthorityRequest::from_cli("subagent", &strings(&["assignment-create"])).is_some());
        assert!(AuthorityRequest::from_cli("agent", &strings(&["run"])).is_none());
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
        let ops = AuthorityRequest::from_cli(
            "ops",
            &strings(&["execute", "--request-file", "/tmp/request.json"]),
        )
        .expect("ops request");
        assert!(ops.authorized_for(config::OPS_UID));
        assert!(ops.authorized_for(config::REVIEWER_UID));
        assert!(!ops.authorized_for(config::ORCHESTRATOR_UID));
        let describe = AuthorityRequest::from_cli("ops", &strings(&["describe", "github.read"]))
            .expect("ops describe request");
        for uid in [
            config::ORCHESTRATOR_UID,
            config::WRITER_UID,
            config::READER_UID,
            config::OPS_UID,
            config::REVIEWER_UID,
        ] {
            assert!(
                describe.authorized_for(uid),
                "uid {uid} should be allowed to inspect live read capabilities"
            );
        }
        assert_eq!(
            describe.into_cli(),
            ("ops".to_string(), strings(&["describe", "github.read"]))
        );
        let direct_read = AuthorityRequest::from_cli(
            "ops",
            &strings(&["read", "--request-file", "/logs/agents/reader/request.json"]),
        )
        .expect("direct read request");
        for uid in [
            config::ORCHESTRATOR_UID,
            config::WRITER_UID,
            config::READER_UID,
            config::OPS_UID,
            config::REVIEWER_UID,
        ] {
            assert!(
                direct_read.authorized_for(uid),
                "uid {uid} should be allowed to request a direct read"
            );
        }
        assert_eq!(
            direct_read.into_cli(),
            (
                "ops".to_string(),
                strings(&["read", "--request-file", "/logs/agents/reader/request.json"]),
            )
        );
        let publish_bound = AuthorityRequest::from_cli(
            "ops",
            &strings(&["publish-bound", "--request-file", "/state/request.json"]),
        )
        .expect("ops publish-bound request");
        assert!(publish_bound.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!publish_bound.authorized_for(config::OPS_UID));

        let evidence_read = AuthorityRequest::from_cli(
            "ops",
            &strings(&[
                "execute",
                "--request-file",
                "/state/evidence.json",
                "--reviewed-request",
                "/state/reviewed.json",
                "--reviewer",
                "ops-reviewer-01",
            ]),
        )
        .expect("reviewer evidence read request");
        assert!(evidence_read.authorized_for(config::REVIEWER_UID));
        assert!(!evidence_read.authorized_for(config::READER_UID));
        assert!(evidence_read.authorized_for(config::OPS_UID));

        let external_completion =
            AuthorityRequest::from_cli("orchestrator", &strings(&["complete", "--external-only"]))
                .expect("external-only completion request");
        assert!(external_completion.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!external_completion.authorized_for(config::OPS_UID));
        assert_eq!(
            external_completion.into_cli(),
            (
                "orchestrator".to_string(),
                strings(&["complete", "--external-only"]),
            )
        );
        let external_completion_with_result = AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&[
                "complete",
                "--external-only",
                "--result-file",
                "/state/orchestrator-result.md",
            ]),
        )
        .expect("external-only completion with result request");
        assert!(external_completion_with_result.authorized_for(config::ORCHESTRATOR_UID));
        assert_eq!(
            external_completion_with_result.into_cli(),
            (
                "orchestrator".to_string(),
                strings(&[
                    "complete",
                    "--external-only",
                    "--result-file",
                    "/state/orchestrator-result.md",
                ]),
            )
        );
        let direct_completion = AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&[
                "complete",
                "--direct-response",
                "--result-file",
                "/state/direct.md",
            ]),
        )
        .expect("direct completion request");
        assert!(direct_completion.authorized_for(config::ORCHESTRATOR_UID));
        for route in ["--clarification", "--auto-clarification"] {
            let clarification_completion = AuthorityRequest::from_cli(
                "orchestrator",
                &strings(&[
                    "complete",
                    route,
                    "--result-file",
                    "/state/clarification.md",
                ]),
            )
            .expect("clarification completion request");
            assert!(clarification_completion.authorized_for(config::ORCHESTRATOR_UID));
            assert!(!clarification_completion.authorized_for(config::READER_UID));
            assert_eq!(
                clarification_completion.into_cli(),
                (
                    "orchestrator".to_string(),
                    strings(&[
                        "complete",
                        route,
                        "--result-file",
                        "/state/clarification.md",
                    ]),
                )
            );
        }
        let read_only_completion = AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&[
                "complete",
                "--read-only",
                "--result-file",
                "/state/result.md",
                "--reviewer",
                "read-only-integrity-reviewer-01",
            ]),
        )
        .expect("read-only completion request");
        assert!(read_only_completion.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!read_only_completion.authorized_for(config::READER_UID));
        let human_review_completion = AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&[
                "complete",
                "--human-review",
                "--result-file",
                "/state/question.md",
                "--reviewer",
                "ops-reviewer-01",
            ]),
        )
        .expect("human review completion request");
        assert!(human_review_completion.authorized_for(config::ORCHESTRATOR_UID));
        assert!(!human_review_completion.authorized_for(config::REVIEWER_UID));
        assert!(AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&["complete", "--unsupported"]),
        )
        .is_none());
        assert!(AuthorityRequest::from_cli(
            "orchestrator",
            &strings(&["complete", "--external-only", "--result-file"]),
        )
        .is_none());
    }

    #[test]
    fn diagnosis_only_scope_denies_writer_authority() {
        let writer_assignment = AuthorityRequest::from_cli(
            "subagent",
            &strings(&["assignment-create", "writer", "--role", "exploitation"]),
        )
        .expect("writer assignment");
        assert!(!writer_assignment.allowed_for_authority_scope("diagnosis-only"));

        let reader_assignment = AuthorityRequest::from_cli(
            "subagent",
            &strings(&["assignment-create", "reader", "--role", "exploration"]),
        )
        .expect("reader assignment");
        assert!(reader_assignment.allowed_for_authority_scope("diagnosis-only"));

        let writer_launch = AuthorityRequest::from_cli(
            "supervisor",
            &strings(&["register-launch", "writer", "--access", "workspace-write"]),
        )
        .expect("writer launch");
        assert!(!writer_launch.allowed_for_authority_scope("diagnosis-only"));

        let reader_launch = AuthorityRequest::from_cli(
            "supervisor",
            &strings(&["register-launch", "reader", "--access", "read-only"]),
        )
        .expect("reader launch");
        assert!(reader_launch.allowed_for_authority_scope("diagnosis-only"));
        assert!(!reader_launch.allowed_for_authority_scope("unknown"));
    }

    #[test]
    fn approved_repair_grant_binds_paths_and_enters_only_reviewed_ops() {
        let grant = r#"{
            "kind":"review-approved-repair",
            "effects":["source-write","reviewed-ops"],
            "repository":"multiagent",
            "paths":["deploy/service.yaml"],
            "reviewId":"review-1",
            "sourceSessionId":"session-observe",
            "sourceEventId":"event-1",
            "questionSha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "grantedToSessionId":"session-repair",
            "approvedBy":"production-e2e",
            "approvedAt":"2026-09-05T00:00:00Z"
        }"#;
        let authority =
            parse_session_authority("approved-repair", grant, "session-repair", "multiagent")
                .expect("valid repair authority");
        assert!(authority.permits_reviewed_ops());
        assert!(authority.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/deploy/service.yaml")]
        ));
        assert!(!authority.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/deploy/other.yaml")]
        ));

        let execute = AuthorityRequest::from_cli(
            "ops",
            &strings(&[
                "execute",
                "--request-file",
                "/state/request.json",
                "--reviewer",
                "ops-reviewer-01",
            ]),
        )
        .expect("typed reviewed operation");
        assert!(execute.allowed_for_session_authority(&authority));

        let observe = parse_session_authority("observe", "null", "session-observe", "multiagent")
            .expect("observe authority");
        assert!(!execute.allowed_for_session_authority(&observe));
        assert!(!observe.permits_reviewed_ops());

        let ops_only = grant
            .replace(
                r#""effects":["source-write","reviewed-ops"]"#,
                r#""effects":["reviewed-ops"]"#,
            )
            .replace(r#""paths":["deploy/service.yaml"]"#, r#""paths":[]"#);
        let ops_authority =
            parse_session_authority("approved-repair", &ops_only, "session-repair", "multiagent")
                .expect("valid reviewed-ops-only authority");
        assert!(ops_authority.permits_reviewed_ops());
        assert!(!ops_authority.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/deploy/service.yaml")]
        ));
        let invalid = grant.replace(
            r#""effects":["source-write","reviewed-ops"]"#,
            r#""effects":["source-write","admin"]"#,
        );
        assert!(parse_session_authority(
            "approved-repair",
            &invalid,
            "session-repair",
            "multiagent"
        )
        .err()
        .expect("grant with an unknown effect must fail")
        .contains("incomplete"));
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
