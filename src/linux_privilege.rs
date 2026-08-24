use std::io;
use std::process::{Command, ExitCode};

#[cfg(unix)]
use std::os::unix::process::CommandExt;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct IdentitySpec {
    uid: u32,
    gid: u32,
    supplementary_gids: Vec<u32>,
}

impl IdentitySpec {
    pub fn new(uid: u32, gid: u32) -> Self {
        Self {
            uid,
            gid,
            supplementary_gids: Vec::new(),
        }
    }

    pub fn with_supplementary_gids(mut self, supplementary_gids: &[u32]) -> Self {
        self.supplementary_gids
            .extend_from_slice(supplementary_gids);
        self
    }

    fn prepare(&self) -> PreparedIdentity {
        // Keep the primary role group in the supplementary set to preserve the
        // existing shared-artifact and tmux socket access model.
        let mut groups = vec![self.gid as libc::gid_t];
        groups.extend(
            self.supplementary_gids
                .iter()
                .map(|value| *value as libc::gid_t),
        );
        groups.sort_unstable();
        groups.dedup();
        PreparedIdentity {
            uid: self.uid as libc::uid_t,
            gid: self.gid as libc::gid_t,
            groups,
        }
    }
}

#[derive(Clone, Debug)]
struct PreparedIdentity {
    uid: libc::uid_t,
    gid: libc::gid_t,
    groups: Vec<libc::gid_t>,
}

/// Retain setuid-root authority only when application policy accepts the real
/// caller UID. All other setuid invocations permanently drop to the caller.
#[cfg(unix)]
pub fn guard_setuid_invocation(
    authorized: impl FnOnce(u32) -> bool,
) -> Result<(), String> {
    let real_uid = unsafe { libc::getuid() };
    let effective_uid = unsafe { libc::geteuid() };
    if effective_uid != 0 || real_uid == 0 {
        return Ok(());
    }
    if authorized(real_uid) {
        return Ok(());
    }
    if unsafe { libc::setuid(real_uid) } != 0 || unsafe { libc::geteuid() } != real_uid {
        return Err(format!(
            "drop setuid privilege: {}",
            io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
pub fn guard_setuid_invocation(
    _authorized: impl FnOnce(u32) -> bool,
) -> Result<(), String> {
    Ok(())
}

#[cfg(unix)]
pub fn apply_identity(spec: &IdentitySpec) -> Result<(), String> {
    let prepared = spec.prepare();
    apply_prepared_identity(&prepared).map_err(|error| {
        format!(
            "drop to uid {} gid {}: {error}",
            spec.uid, spec.gid
        )
    })
}

#[cfg(not(unix))]
pub fn apply_identity(_spec: &IdentitySpec) -> Result<(), String> {
    Err("identity transitions require Unix".into())
}

#[cfg(unix)]
pub fn configure_command_identity(command: &mut Command, spec: IdentitySpec) {
    let prepared = spec.prepare();
    unsafe {
        command.pre_exec(move || apply_prepared_identity(&prepared));
    }
}

#[cfg(not(unix))]
pub fn configure_command_identity(_command: &mut Command, _spec: IdentitySpec) {}

#[cfg(unix)]
pub fn exec_as_identity(
    spec: &IdentitySpec,
    command: &str,
    args: &[String],
) -> Result<ExitCode, String> {
    apply_identity(spec)?;
    let error = Command::new(command).args(args).exec();
    Err(format!("execute {command}: {error}"))
}

#[cfg(not(unix))]
pub fn exec_as_identity(
    _spec: &IdentitySpec,
    _command: &str,
    _args: &[String],
) -> Result<ExitCode, String> {
    Err("identity-bound execution requires Unix".into())
}

#[cfg(unix)]
fn apply_prepared_identity(identity: &PreparedIdentity) -> io::Result<()> {
    if unsafe { libc::setgroups(identity.groups.len(), identity.groups.as_ptr()) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::setgid(identity.gid) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::setuid(identity.uid) } != 0 {
        return Err(io::Error::last_os_error());
    }
    if unsafe { libc::getuid() } != identity.uid
        || unsafe { libc::geteuid() } != identity.uid
        || unsafe { libc::getgid() } != identity.gid
        || unsafe { libc::getegid() } != identity.gid
    {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "identity transition postcondition failed",
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::IdentitySpec;

    #[test]
    fn identity_groups_include_primary_and_deduplicate_supplementary_groups() {
        let identity = IdentitySpec::new(10004, 10001)
            .with_supplementary_gids(&[10006, 10004, 10001, 10006]);
        assert_eq!(identity.prepare().groups, [10001, 10004, 10006]);
    }
}
