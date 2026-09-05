use crate::{config, state};
use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::env;
use std::fs;
use std::path::{Component, Path, PathBuf};

const ACTIVE_EXECUTION_FILE: &str = "runtime_state/active-execution.json";
const ALLOWED_EFFECTS: [&str; 2] = ["reviewed-ops", "source-write"];

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ReviewMutationGrant {
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

#[derive(Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ActiveExecution {
    schema_version: u32,
    ordinal: u32,
    kind: String,
    effects: Vec<String>,
    repository: String,
    paths: Vec<String>,
    session_id: String,
}

/// One authority step inside the existing session loop.
///
/// The orchestrator remains read-only. A user-originated execution may ask the
/// supervisor to advance once into a bounded mutation execution; only worker
/// launches and reviewed operations consume those effects.
pub struct Execution {
    scope: String,
    ordinal: u32,
    granted_paths: BTreeSet<String>,
    source_write: bool,
    reviewed_ops: bool,
    fixed_grant: bool,
}

impl Execution {
    pub fn scope(&self) -> &str {
        &self.scope
    }

    pub fn ordinal(&self) -> u32 {
        self.ordinal
    }

    pub fn is_read_only(&self) -> bool {
        !self.source_write && !self.reviewed_ops
    }

    pub fn permits_workspace_write(&self, root: &Path, paths: &[PathBuf]) -> bool {
        if self.scope == "human" {
            return true;
        }
        self.source_write
            && !paths.is_empty()
            && paths.iter().all(|path| {
                path.strip_prefix(root)
                    .ok()
                    .and_then(|relative| normalized_repo_path(&relative.to_string_lossy()))
                    .is_some_and(|relative| self.granted_paths.contains(&relative))
            })
    }

    pub fn permits_reviewed_ops(&self) -> bool {
        self.scope == "human" || self.reviewed_ops
    }

    pub fn permits_mutation_request(&self) -> bool {
        self.scope == "user" && self.is_read_only() && !self.fixed_grant
    }
}

fn bounded(value: &str, max: usize) -> bool {
    !value.trim().is_empty() && value.len() <= max
}

fn normalized_repo_path(value: &str) -> Option<String> {
    if !bounded(value, 512) {
        return None;
    }
    let normalized = value.replace('\\', "/");
    let path = Path::new(&normalized);
    if path.is_absolute()
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
    {
        return None;
    }
    Some(normalized)
}

fn normalized_effects(values: &[String]) -> Option<BTreeSet<String>> {
    let effects = values.iter().cloned().collect::<BTreeSet<_>>();
    let allowed = ALLOWED_EFFECTS
        .iter()
        .map(|value| (*value).to_string())
        .collect::<BTreeSet<_>>();
    (!effects.is_empty() && effects.len() == values.len() && effects.is_subset(&allowed))
        .then_some(effects)
}

fn validated_paths(values: &[String], source_write: bool) -> Option<BTreeSet<String>> {
    let paths = values
        .iter()
        .filter_map(|path| normalized_repo_path(path))
        .collect::<BTreeSet<_>>();
    (values.len() <= 32 && paths.len() == values.len() && source_write == !values.is_empty())
        .then_some(paths)
}

fn read_only(scope: &str) -> Execution {
    Execution {
        scope: scope.into(),
        ordinal: 1,
        granted_paths: BTreeSet::new(),
        source_write: false,
        reviewed_ops: false,
        fixed_grant: false,
    }
}

fn legacy_human() -> Execution {
    Execution {
        scope: "human".into(),
        ordinal: 1,
        granted_paths: BTreeSet::new(),
        source_write: true,
        reviewed_ops: true,
        fixed_grant: true,
    }
}

fn from_review_grant(
    scope: &str,
    json: &str,
    session_id: &str,
    repository: &str,
) -> Result<Execution, String> {
    let grant: ReviewMutationGrant = serde_json::from_str(json)
        .map_err(|error| format!("decode approved mutation grant: {error}"))?;
    let effects =
        normalized_effects(&grant.effects).ok_or("approved mutation grant has invalid effects")?;
    let source_write = effects.contains("source-write");
    let paths = validated_paths(&grant.paths, source_write)
        .ok_or("approved mutation grant has invalid paths")?;
    let digest = grant.question_sha256.strip_prefix("sha256:").unwrap_or("");
    if grant.kind != "review-approved-repair"
        || grant.repository != repository
        || grant.granted_to_session_id != session_id
        || !bounded(&grant.review_id, 128)
        || !bounded(&grant.source_session_id, 63)
        || !bounded(&grant.source_event_id, 128)
        || digest.len() != 64
        || !digest.bytes().all(|byte| byte.is_ascii_hexdigit())
        || !bounded(&grant.approved_by, 256)
        || !bounded(&grant.approved_at, 64)
    {
        return Err(
            "approved mutation grant is incomplete or bound to another session or repository"
                .into(),
        );
    }
    Ok(Execution {
        scope: scope.into(),
        ordinal: 1,
        granted_paths: paths,
        source_write,
        reviewed_ops: effects.contains("reviewed-ops"),
        fixed_grant: true,
    })
}

fn from_active_execution(
    json: &str,
    session_id: &str,
    repository: &str,
) -> Result<Execution, String> {
    let active: ActiveExecution =
        serde_json::from_str(json).map_err(|error| format!("decode active execution: {error}"))?;
    let effects =
        normalized_effects(&active.effects).ok_or("active execution has invalid effects")?;
    let source_write = effects.contains("source-write");
    let paths =
        validated_paths(&active.paths, source_write).ok_or("active execution has invalid paths")?;
    if active.schema_version != 1
        || active.ordinal < 2
        || active.kind != "user-requested-mutation"
        || active.repository != repository
        || active.session_id != session_id
    {
        return Err(
            "active execution is incomplete or bound to another session or repository".into(),
        );
    }
    Ok(Execution {
        scope: "user".into(),
        ordinal: active.ordinal,
        granted_paths: paths,
        source_write,
        reviewed_ops: effects.contains("reviewed-ops"),
        fixed_grant: false,
    })
}

fn configured_from(
    scope: &str,
    initial_grant_json: &str,
    active_execution_json: Option<&str>,
    session_id: &str,
    repository: &str,
) -> Result<Execution, String> {
    let has_initial_grant =
        !initial_grant_json.trim().is_empty() && initial_grant_json.trim() != "null";
    match scope {
        "human" if !has_initial_grant => Ok(legacy_human()),
        "observe" | "diagnosis-only" if !has_initial_grant => Ok(read_only(scope)),
        "user" if has_initial_grant => {
            from_review_grant(scope, initial_grant_json, session_id, repository)
        }
        "user" => match active_execution_json {
            Some(json) => from_active_execution(json, session_id, repository),
            None => Ok(read_only(scope)),
        },
        "human" | "observe" | "diagnosis-only" => Err(format!(
            "execution scope {scope} must not carry a mutation grant"
        )),
        _ => Err("MULTIAGENT_AUTHORITY_SCOPE is invalid".into()),
    }
}

pub fn configured() -> Result<Execution, String> {
    let scope = env::var("MULTIAGENT_AUTHORITY_SCOPE")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "human".into());
    let state_path = config::state_dir()?.join(ACTIVE_EXECUTION_FILE);
    let active_json =
        if state_path.is_file() {
            Some(fs::read_to_string(&state_path).map_err(|error| {
                format!("read active execution {}: {error}", state_path.display())
            })?)
        } else {
            None
        };
    configured_from(
        &scope,
        &env::var("MULTIAGENT_MUTATION_GRANT_JSON").unwrap_or_default(),
        active_json.as_deref(),
        &env::var("MULTIAGENT_SESSION").unwrap_or_default(),
        &env::var("MULTIAGENT_REPOSITORY_NAME")
            .or_else(|_| env::var("MULTIAGENT_SESSION_REPOSITORY"))
            .unwrap_or_default(),
    )
}

pub fn request_mutation(paths: &[String], reviewed_ops: bool) -> Result<Execution, String> {
    let current = configured()?;
    if !current.permits_mutation_request() {
        return Err("only an initial read-only user execution may request mutation".into());
    }
    let normalized = paths
        .iter()
        .filter_map(|path| normalized_repo_path(path))
        .collect::<BTreeSet<_>>();
    if paths.len() > 32
        || normalized.len() != paths.len()
        || (normalized.is_empty() && !reviewed_ops)
    {
        return Err(
            "mutation request requires unique exact repository paths and/or reviewed-ops".into(),
        );
    }
    let mut effects = Vec::new();
    if !normalized.is_empty() {
        effects.push("source-write".to_string());
    }
    if reviewed_ops {
        effects.push("reviewed-ops".to_string());
    }
    let active = ActiveExecution {
        schema_version: 1,
        ordinal: current.ordinal() + 1,
        kind: "user-requested-mutation".into(),
        effects,
        repository: env::var("MULTIAGENT_REPOSITORY_NAME")
            .or_else(|_| env::var("MULTIAGENT_SESSION_REPOSITORY"))
            .map_err(|_| "mutation request requires a bound repository".to_string())?,
        paths: normalized.into_iter().collect(),
        session_id: env::var("MULTIAGENT_SESSION")
            .map_err(|_| "mutation request requires a bound session".to_string())?,
    };
    let encoded = serde_json::to_string_pretty(&active)
        .map_err(|error| format!("encode active execution: {error}"))?;
    state::atomic_write(
        &config::state_dir()?.join(ACTIVE_EXECUTION_FILE),
        &format!("{encoded}\n"),
    )?;
    configured()
}

#[cfg(test)]
mod tests {
    use super::{configured_from, normalized_repo_path};

    #[test]
    fn user_execution_starts_read_only() {
        let execution = configured_from("user", "null", None, "session-1", "repo")
            .expect("read-only user execution");
        assert!(execution.is_read_only());
        assert!(execution.permits_mutation_request());
        assert!(!execution.permits_reviewed_ops());
    }

    #[test]
    fn repository_paths_reject_parent_traversal_on_both_separator_styles() {
        assert!(normalized_repo_path("../outside").is_none());
        assert!(normalized_repo_path("..\\outside").is_none());
    }

    #[test]
    fn approved_review_starts_a_bounded_user_execution() {
        let grant = r#"{
            "kind":"review-approved-repair",
            "effects":["source-write","reviewed-ops"],
            "repository":"multiagent",
            "paths":["deploy/service.yaml"],
            "reviewId":"review-1",
            "sourceSessionId":"session-observe",
            "sourceEventId":"event-1",
            "questionSha256":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "grantedToSessionId":"session-user",
            "approvedBy":"production-e2e",
            "approvedAt":"2026-09-05T00:00:00Z"
        }"#;
        let execution = configured_from("user", grant, None, "session-user", "multiagent")
            .expect("approved user execution");
        assert!(!execution.is_read_only());
        assert!(!execution.permits_mutation_request());
        assert!(execution.permits_reviewed_ops());
        assert!(execution.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/deploy/service.yaml")]
        ));
        assert!(!execution.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/deploy/other.yaml")]
        ));
    }

    #[test]
    fn supervisor_activated_execution_is_bound_to_session_repository_and_paths() {
        let active = r#"{
            "schemaVersion":1,
            "ordinal":2,
            "kind":"user-requested-mutation",
            "effects":["source-write"],
            "repository":"multiagent",
            "paths":["src/lib.rs"],
            "sessionId":"session-user"
        }"#;
        let execution = configured_from("user", "null", Some(active), "session-user", "multiagent")
            .expect("active mutation execution");
        assert_eq!(execution.ordinal(), 2);
        assert!(!execution.is_read_only());
        assert!(!execution.permits_reviewed_ops());
        assert!(execution.permits_workspace_write(
            std::path::Path::new("/repo"),
            &[std::path::PathBuf::from("/repo/src/lib.rs")]
        ));
        assert!(configured_from("user", "null", Some(active), "session-user", "other",).is_err());
    }
}
