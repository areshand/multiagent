use std::collections::BTreeSet;
use std::fs;
use std::path::PathBuf;
use std::process::{Command, ExitCode};

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    let mut write_roots = BTreeSet::new();
    let mut uid = None;
    let mut gid = None;
    let mut index = 0;
    while index < args.len() {
        match args[index].as_str() {
            "--allow-write" => {
                let path = args
                    .get(index + 1)
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| "role-exec --allow-write requires PATH".to_string())?;
                let canonical = fs::canonicalize(path)
                    .map_err(|error| format!("resolve role write root {path}: {error}"))?;
                write_roots.insert(canonical);
                index += 2;
            }
            "--uid" => {
                uid = Some(parse_id(args, index, "--uid")?);
                index += 2;
            }
            "--gid" => {
                gid = Some(parse_id(args, index, "--gid")?);
                index += 2;
            }
            "--" => {
                index += 1;
                break;
            }
            other => return Err(format!("unknown role-exec argument: {other}")),
        }
    }
    let command = args
        .get(index)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "role-exec requires a command after --".to_string())?;
    let command_args = &args[index + 1..];

    if let Some(uid) = uid {
        drop_identity(
            uid,
            gid.ok_or_else(|| "role-exec --uid requires --gid".to_string())?,
        )?;
    } else {
        restrict_writes(&write_roots.into_iter().collect::<Vec<_>>())?;
    }
    exec(command, command_args)
}

fn parse_id(args: &[String], index: usize, flag: &str) -> Result<u32, String> {
    args.get(index + 1)
        .ok_or_else(|| format!("role-exec {flag} requires an integer"))?
        .parse()
        .map_err(|_| format!("role-exec {flag} requires an integer"))
}

#[cfg(unix)]
fn drop_identity(uid: u32, gid: u32) -> Result<(), String> {
    let groups = [gid as libc::gid_t];
    if unsafe { libc::setgroups(1, groups.as_ptr()) } != 0 {
        return Err(format!(
            "set role supplementary groups: {}",
            std::io::Error::last_os_error()
        ));
    }
    if unsafe { libc::setgid(gid as libc::gid_t) } != 0 {
        return Err(format!(
            "set role gid {gid}: {}",
            std::io::Error::last_os_error()
        ));
    }
    if unsafe { libc::setuid(uid as libc::uid_t) } != 0 {
        return Err(format!(
            "set role uid {uid}: {}",
            std::io::Error::last_os_error()
        ));
    }
    Ok(())
}

#[cfg(not(unix))]
fn drop_identity(_uid: u32, _gid: u32) -> Result<(), String> {
    Err("role-exec uid isolation requires Unix".into())
}

#[cfg(target_os = "linux")]
fn restrict_writes(write_roots: &[PathBuf]) -> Result<(), String> {
    linux::restrict_writes(write_roots)
}

#[cfg(not(target_os = "linux"))]
fn restrict_writes(_write_roots: &[PathBuf]) -> Result<(), String> {
    Err(
        "role-exec is only available on Linux; use the native Codex sandbox on this platform"
            .into(),
    )
}

#[cfg(unix)]
fn exec(command: &str, args: &[String]) -> Result<ExitCode, String> {
    use std::os::unix::process::CommandExt;

    let error = Command::new(command).args(args).exec();
    Err(format!("execute role command {command}: {error}"))
}

#[cfg(not(unix))]
fn exec(command: &str, args: &[String]) -> Result<ExitCode, String> {
    let status = Command::new(command)
        .args(args)
        .status()
        .map_err(|error| format!("execute role command {command}: {error}"))?;
    Ok(ExitCode::from(status.code().unwrap_or(1) as u8))
}

#[cfg(target_os = "linux")]
mod linux {
    use std::fs::File;
    use std::io;
    use std::os::fd::{AsRawFd, FromRawFd};
    use std::os::unix::fs::OpenOptionsExt;
    use std::path::PathBuf;

    const LANDLOCK_CREATE_RULESET_VERSION: u32 = 1;
    const LANDLOCK_RULE_PATH_BENEATH: i32 = 1;

    const ACCESS_WRITE_FILE: u64 = 1 << 1;
    const ACCESS_REMOVE_DIR: u64 = 1 << 4;
    const ACCESS_REMOVE_FILE: u64 = 1 << 5;
    const ACCESS_MAKE_CHAR: u64 = 1 << 6;
    const ACCESS_MAKE_DIR: u64 = 1 << 7;
    const ACCESS_MAKE_REG: u64 = 1 << 8;
    const ACCESS_MAKE_SOCK: u64 = 1 << 9;
    const ACCESS_MAKE_FIFO: u64 = 1 << 10;
    const ACCESS_MAKE_BLOCK: u64 = 1 << 11;
    const ACCESS_MAKE_SYM: u64 = 1 << 12;
    const ACCESS_REFER: u64 = 1 << 13;
    const ACCESS_TRUNCATE: u64 = 1 << 14;
    const ACCESS_IOCTL_DEV: u64 = 1 << 15;

    #[repr(C)]
    struct RulesetAttr {
        handled_access_fs: u64,
    }

    #[repr(C)]
    struct PathBeneathAttr {
        allowed_access: u64,
        parent_fd: i32,
    }

    pub fn restrict_writes(write_roots: &[PathBuf]) -> Result<(), String> {
        let abi = unsafe {
            libc::syscall(
                libc::SYS_landlock_create_ruleset,
                std::ptr::null::<RulesetAttr>(),
                0,
                LANDLOCK_CREATE_RULESET_VERSION,
            )
        };
        if abi < 1 {
            return Err(format!(
                "Landlock is unavailable; refusing to run without role write enforcement: {}",
                io::Error::last_os_error()
            ));
        }

        let handled_access = handled_access_for_abi(abi);
        let ruleset_attr = RulesetAttr {
            handled_access_fs: handled_access,
        };
        let ruleset_fd = unsafe {
            libc::syscall(
                libc::SYS_landlock_create_ruleset,
                &ruleset_attr,
                std::mem::size_of::<RulesetAttr>(),
                0,
            )
        };
        if ruleset_fd < 0 {
            return Err(format!(
                "create Landlock ruleset: {}",
                io::Error::last_os_error()
            ));
        }
        let ruleset = unsafe { File::from_raw_fd(ruleset_fd as i32) };

        for path in write_roots {
            add_path_rule(&ruleset, path, handled_access)?;
        }

        let no_new_privileges = unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) };
        if no_new_privileges != 0 {
            return Err(format!(
                "enable no-new-privileges for Landlock: {}",
                io::Error::last_os_error()
            ));
        }
        let restricted =
            unsafe { libc::syscall(libc::SYS_landlock_restrict_self, ruleset.as_raw_fd(), 0) };
        if restricted != 0 {
            return Err(format!(
                "apply Landlock role ruleset: {}",
                io::Error::last_os_error()
            ));
        }
        Ok(())
    }

    fn handled_access_for_abi(abi: libc::c_long) -> u64 {
        let mut access = ACCESS_WRITE_FILE
            | ACCESS_REMOVE_DIR
            | ACCESS_REMOVE_FILE
            | ACCESS_MAKE_CHAR
            | ACCESS_MAKE_DIR
            | ACCESS_MAKE_REG
            | ACCESS_MAKE_SOCK
            | ACCESS_MAKE_FIFO
            | ACCESS_MAKE_BLOCK
            | ACCESS_MAKE_SYM;
        if abi >= 2 {
            access |= ACCESS_REFER;
        }
        if abi >= 3 {
            access |= ACCESS_TRUNCATE;
        }
        if abi >= 5 {
            access |= ACCESS_IOCTL_DEV;
        }
        access
    }

    fn add_path_rule(ruleset: &File, path: &PathBuf, handled_access: u64) -> Result<(), String> {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_PATH | libc::O_CLOEXEC)
            .open(path)
            .map_err(|error| format!("open Landlock write root {}: {error}", path.display()))?;
        let is_dir = path.is_dir();
        let allowed_access = if is_dir {
            handled_access
        } else {
            handled_access & (ACCESS_WRITE_FILE | ACCESS_TRUNCATE | ACCESS_IOCTL_DEV)
        };
        let rule = PathBeneathAttr {
            allowed_access,
            parent_fd: file.as_raw_fd(),
        };
        let added = unsafe {
            libc::syscall(
                libc::SYS_landlock_add_rule,
                ruleset.as_raw_fd(),
                LANDLOCK_RULE_PATH_BENEATH,
                &rule,
                0,
            )
        };
        if added != 0 {
            return Err(format!(
                "allow Landlock write root {}: {}",
                path.display(),
                io::Error::last_os_error()
            ));
        }
        Ok(())
    }
}
