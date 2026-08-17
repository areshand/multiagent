use std::env;
use std::path::PathBuf;

pub const ORCHESTRATOR_UID: u32 = 10001;
pub const WRITER_UID: u32 = 10002;
pub const READER_UID: u32 = 10003;
#[cfg(target_os = "linux")]
pub const SUPERVISOR_UID: u32 = 10004;
pub const ROLE_GID: u32 = 10001;

/// Return whether lifecycle gates are mandatory for the current process.
///
/// The environment flag remains useful for non-isolated local invocations, but
/// it is not an authority boundary.  A UID-isolated orchestrator must not be
/// able to disable the supervisor by overriding an environment variable in a
/// shell command.
pub fn lifecycle_enforced() -> bool {
    lifecycle_enforced_for(
        real_uid(),
        env::var("MULTIAGENT_LIFECYCLE_ENFORCEMENT").ok().as_deref(),
        env::var("MULTIAGENT_UID_SANDBOX").ok().as_deref(),
    )
}

fn lifecycle_enforced_for(
    real_uid: u32,
    requested: Option<&str>,
    uid_sandbox: Option<&str>,
) -> bool {
    real_uid == ORCHESTRATOR_UID || requested == Some("1") || uid_sandbox == Some("1")
}

#[cfg(unix)]
fn real_uid() -> u32 {
    unsafe { libc::getuid() }
}

#[cfg(not(unix))]
fn real_uid() -> u32 {
    u32::MAX
}

pub fn root() -> Result<PathBuf, String> {
    match env::var_os("MULTIAGENT_ROOT") {
        Some(value) if !value.is_empty() => Ok(PathBuf::from(value)),
        _ => env::current_dir()
            .map_err(|error| format!("cannot determine current directory: {error}")),
    }
}

pub fn state_dir() -> Result<PathBuf, String> {
    match env::var_os("MULTIAGENT_STATE_DIR") {
        Some(value) if !value.is_empty() => Ok(PathBuf::from(value)),
        _ => Ok(root()?.join(".multiagent")),
    }
}

#[cfg(test)]
mod tests {
    use super::{lifecycle_enforced_for, ORCHESTRATOR_UID};

    #[test]
    fn orchestrator_cannot_disable_lifecycle_with_environment_override() {
        assert!(lifecycle_enforced_for(
            ORCHESTRATOR_UID,
            Some("0"),
            Some("0")
        ));
        assert!(lifecycle_enforced_for(ORCHESTRATOR_UID, None, None));
        assert!(lifecycle_enforced_for(0, Some("1"), Some("0")));
        assert!(lifecycle_enforced_for(0, Some("0"), Some("1")));
        assert!(!lifecycle_enforced_for(0, Some("0"), Some("0")));
    }
}
