use chrono::{Duration, SecondsFormat, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
#[cfg(target_os = "linux")]
use std::os::unix::fs::MetadataExt;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::thread;
use std::time::{Duration as StdDuration, Instant, SystemTime, UNIX_EPOCH};

const MAX_OPERATION_REQUEST_BYTES: u64 = 65_536;
const MAX_RUNBOOK_BYTES: u64 = 1_048_576;
const MATERIALIZATION_TIMEOUT: StdDuration = StdDuration::from_secs(120);
const MAX_MATERIALIZATION_FILES: u64 = 200_000;
const MAX_MATERIALIZATION_BYTES: u64 = 1024 * 1024 * 1024;
const OPS_USAGE: &str = "usage:\n  multiagent ops describe OPERATION_ID\n  multiagent ops read --request-file PATH\n  multiagent ops template\n  multiagent ops bind-runbook --request-file PATH --runbook-document PATH\n  multiagent ops publish --draft-file PATH --runbook-document PATH\n  multiagent ops review-bind --request-file PATH\n  multiagent ops execute --request-file PATH --reviewer NAME [--reviewed-request PATH]";

pub(crate) struct PublishedRequest {
    artifact_path: PathBuf,
    sha256: String,
    bytes: usize,
}

impl PublishedRequest {
    pub(crate) fn descriptor_json(&self) -> Result<String, String> {
        serde_json::to_string(&json!({
            "artifactPath": self.artifact_path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "mediaType": "application/json",
            "truncated": false
        }))
        .map_err(|error| format!("encode ops publication descriptor: {error}"))
    }
}

struct TrustedApproval {
    subject: String,
    role: &'static str,
    evidence_sha256: String,
    approved_at: String,
}

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    match args.first().map(String::as_str) {
        Some("describe") => describe(&args[1..]),
        Some("read") => execute_direct_read(&args[1..]),
        Some("template") => template(&args[1..]),
        Some("bind-runbook") => bind_runbook(&args[1..]),
        Some("publish-bound") => publish_bound(&args[1..]),
        Some("publish") => publish(&args[1..]),
        Some("execute") => execute(&args[1..]),
        Some("review-bind") => review_bind(&args[1..]),
        Some("help" | "--help" | "-h") => {
            print_ops_help();
            Ok(ExitCode::SUCCESS)
        }
        _ => Err(OPS_USAGE.into()),
    }
}

fn describe(args: &[String]) -> Result<ExitCode, String> {
    if args.len() != 1 || args[0].is_empty() {
        return Err("usage: multiagent ops describe OPERATION_ID".into());
    }
    let response = call_prod_mcp_tool("operations_capabilities", json!({}))?;
    let operation = operation_capability(&response, &args[0])?;
    let mut compact = serde_json::Map::new();
    for key in [
        "id",
        "version",
        "description",
        "access",
        "mutation",
        "allowedRunbooks",
        "parameterSchema",
        "parameterExamples",
        "requireChangeTicket",
        "requiredApprovalRoles",
    ] {
        if let Some(value) = operation.get(key) {
            compact.insert(key.into(), value.clone());
        }
    }
    println!(
        "{}",
        serde_json::to_string(&Value::Object(compact))
            .map_err(|error| format!("encode prod-mcp operation capability: {error}"))?
    );
    Ok(ExitCode::SUCCESS)
}

fn operation_capability<'a>(response: &'a Value, operation_id: &str) -> Result<&'a Value, String> {
    let result = response
        .get("result")
        .and_then(Value::as_object)
        .ok_or("prod-mcp capabilities response has no result object")?;
    if result.get("isError").and_then(Value::as_bool) == Some(true) {
        return Err(format!(
            "prod-mcp capabilities failed: {}",
            Value::Object(result.clone())
        ));
    }
    let operations = result
        .get("structuredContent")
        .and_then(|value| value.get("operations"))
        .and_then(Value::as_array)
        .ok_or("prod-mcp capabilities response has no operations array")?;
    operations
        .iter()
        .find(|operation| operation.get("id").and_then(Value::as_str) == Some(operation_id))
        .ok_or_else(|| format!("prod-mcp does not advertise operation {operation_id}"))
}

fn template(args: &[String]) -> Result<ExitCode, String> {
    if !args.is_empty() {
        return Err("usage: multiagent ops template".into());
    }
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "taskId": "replace-with-stable-task-id",
            "goal": "replace with the bounded operation goal",
            "operation": {
                "id": "replace.with.operation-id",
                "version": "replace-with-version-from-ops-describe"
            },
            "parameters": {},
            "runbook": {
                "id": "replace.with-runbook-id",
                "phase": "replace-with-runbook-phase",
                "version": "1.0.0"
            }
        }))
        .map_err(|error| format!("encode ops request template: {error}"))?
    );
    Ok(ExitCode::SUCCESS)
}

fn print_ops_help() {
    println!("{OPS_USAGE}");
    println!(
        "\nCall `multiagent ops describe OPERATION_ID` before constructing parameters; it returns prod-mcp's live description, JSON schema, examples, and authorization requirements.\n\nDraft schema:\n  taskId: non-empty stable string\n  goal: bounded goal copied from the authenticated task\n  operation: object with id and semantic version\n  parameters: exact provider operation parameters from `ops describe`\n  runbook: object with id, phase, and semantic version\n\nGenerate a valid starting envelope with `multiagent ops template`, then bind it with a normalized framework-relative runbook path such as `runbooks/name.md`. After binding, run `chmod 0640 DRAFT_FILE` so the ops-owned request is supervisor-readable and not group-writable. The reviewed-ops-cycle publishes the immutable request. For `ops read`, create a request containing exactly operation, parameters, runbook, and runbookDocument; the supervisor replaces task/goal and derives target plus runbookContentSha256 from that framework-relative runbook. For reviewed mutating operations, do not supply target, approvals, runbookDocument, or runbookContentSha256 yourself."
    );
}

fn bind_runbook(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let state = fs::canonicalize(required_env("MULTIAGENT_STATE_DIR")?)
        .map_err(|error| format!("resolve multiagent state: {error}"))?;
    let request_file = fs::canonicalize(required(&options, "--request-file")?)
        .map_err(|error| format!("resolve ops request file: {error}"))?;
    if !request_file.starts_with(&state) {
        return Err("ops request file must be inside MULTIAGENT_STATE_DIR".into());
    }
    let (bytes, _) = read_bounded_file(&request_file, MAX_OPERATION_REQUEST_BYTES, false)?;
    let relative = required(&options, "--runbook-document")?;
    let (template, digest) = bind_request_template(&bytes, relative, false)?;
    let encoded = serde_json::to_vec_pretty(&template)
        .map_err(|error| format!("encode bound ops request: {error}"))?;
    fs::write(&request_file, encoded)
        .map_err(|error| format!("write bound ops request: {error}"))?;
    println!("request-template-sha256={}", digest_json(&template)?);
    println!("runbook-content-sha256={digest}");
    Ok(ExitCode::SUCCESS)
}

fn publish(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let state = fs::canonicalize(required_env("MULTIAGENT_STATE_DIR")?)
        .map_err(|error| format!("resolve multiagent state: {error}"))?;
    let draft_file = PathBuf::from(required(&options, "--draft-file")?);
    let runbook_document = required(&options, "--runbook-document")?;
    let draft = read_ops_draft(&draft_file)?;
    let (template, _) = bind_request_template(&draft, runbook_document, true)?;
    let encoded = serde_json::to_vec_pretty(&template)
        .map_err(|error| format!("encode published ops request: {error}"))?;
    let descriptor = publish_request_bytes(&state, &encoded)?;
    println!("{}", descriptor.descriptor_json()?);
    Ok(ExitCode::SUCCESS)
}

#[cfg_attr(not(target_os = "linux"), allow(unused_variables))]
fn publish_bound(args: &[String]) -> Result<ExitCode, String> {
    #[cfg(target_os = "linux")]
    if unsafe { libc::geteuid() } != crate::config::SUPERVISOR_UID {
        return Err("ops publish-bound is reserved for the authority supervisor".into());
    }
    #[cfg(not(target_os = "linux"))]
    return Err("ops publish-bound requires Linux".into());

    #[cfg(target_os = "linux")]
    {
        let options = options(args)?;
        let state = PathBuf::from(required_env("MULTIAGENT_STATE_DIR")?);
        let request_file = PathBuf::from(required(&options, "--request-file")?);
        let descriptor = publish_bound_request(&state, &request_file)?;
        println!("{}", descriptor.descriptor_json()?);
        Ok(ExitCode::SUCCESS)
    }
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub(crate) fn publish_bound_request(
    state: &Path,
    request_file: &Path,
) -> Result<PublishedRequest, String> {
    let state =
        fs::canonicalize(state).map_err(|error| format!("resolve multiagent state: {error}"))?;
    let (_, bytes) = read_reviewable_request(&state, request_file)?;
    let template: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode bound ops request: {error}"))?;
    validate_request_template(&template)?;
    verified_runbook_content(&template)?;
    publish_request_bytes(&state, &bytes)
}

fn bind_request_template(
    bytes: &[u8],
    runbook_document: &str,
    require_canonical_target: bool,
) -> Result<(Value, String), String> {
    let mut template: Value = serde_json::from_slice(bytes)
        .map_err(|error| format!("decode ops request template: {error}"))?;
    validate_request_envelope(&template)?;
    let runbook_bytes = exact_runbook_bytes(runbook_document)?;
    let canonical_target = canonical_runbook_target(&runbook_bytes)?;
    if require_canonical_target && canonical_target.is_none() {
        return Err("published runbook must declare one canonical target".into());
    }
    let digest = runbook_content_digest(&runbook_bytes);
    let object = template
        .as_object_mut()
        .ok_or("ops request template must be an object")?;
    if let Some(target) = canonical_target {
        object.insert("target".into(), target);
    }
    object.insert(
        "runbookDocument".into(),
        Value::String(runbook_document.into()),
    );
    object.insert("runbookContentSha256".into(), Value::String(digest.clone()));
    validate_request_template(&template)?;
    Ok((template, digest))
}

fn publish_request_bytes(state: &Path, bytes: &[u8]) -> Result<PublishedRequest, String> {
    if bytes.is_empty() || bytes.len() as u64 > MAX_OPERATION_REQUEST_BYTES {
        return Err("ops request must contain between 1 and 65536 bytes".into());
    }
    let hex = format!("{:x}", Sha256::digest(bytes));
    let directory = state.join("operations").join("requests").join(&hex);
    fs::create_dir_all(&directory)
        .map_err(|error| format!("create operation request store: {error}"))?;
    secure_publication_path(&directory, true)?;
    let artifact_path = directory.join("request.json");
    if artifact_path.exists() {
        let (existing, metadata) =
            read_bounded_file(&artifact_path, MAX_OPERATION_REQUEST_BYTES, true)?;
        validate_published_metadata(&metadata)?;
        if existing != bytes {
            return Err("content-addressed operation request collision".into());
        }
    } else {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_nanos();
        let temporary = directory.join(format!(".request.{}.{}.tmp", std::process::id(), unique));
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(0o440);
        let mut file = options
            .open(&temporary)
            .map_err(|error| format!("create operation request artifact: {error}"))?;
        secure_publication_file(&file)?;
        file.write_all(bytes)
            .and_then(|_| file.sync_all())
            .map_err(|error| format!("persist operation request artifact: {error}"))?;
        match fs::hard_link(&temporary, &artifact_path) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                let (existing, metadata) =
                    read_bounded_file(&artifact_path, MAX_OPERATION_REQUEST_BYTES, true)?;
                validate_published_metadata(&metadata)?;
                if existing != bytes {
                    let _ = fs::remove_file(&temporary);
                    return Err("content-addressed operation request collision".into());
                }
            }
            Err(error) => {
                let _ = fs::remove_file(&temporary);
                return Err(format!("publish operation request artifact: {error}"));
            }
        }
        let _ = fs::remove_file(&temporary);
    }
    Ok(PublishedRequest {
        artifact_path,
        sha256: format!("sha256:{hex}"),
        bytes: bytes.len(),
    })
}

fn read_ops_draft(path: &Path) -> Result<Vec<u8>, String> {
    let agents =
        fs::canonicalize(PathBuf::from(required_env("MULTIAGENT_LOG_DIR")?).join("agents"))
            .map_err(|error| format!("resolve ops agents log directory: {error}"))?;
    let canonical =
        fs::canonicalize(path).map_err(|error| format!("resolve ops draft file: {error}"))?;
    if !canonical.starts_with(&agents) {
        return Err("ops draft file must be inside MULTIAGENT_LOG_DIR/agents".into());
    }
    let (bytes, _metadata) = read_bounded_file(&canonical, MAX_OPERATION_REQUEST_BYTES, true)?;
    if bytes.is_empty() {
        return Err("ops request must contain between 1 and 65536 bytes".into());
    }
    #[cfg(target_os = "linux")]
    if _metadata.uid() != crate::config::OPS_UID || _metadata.mode() & 0o022 != 0 {
        return Err(
            "ops draft must be owned by the ops UID and not group- or world-writable".into(),
        );
    }
    Ok(bytes)
}

fn read_reviewable_request(state: &Path, path: &Path) -> Result<(PathBuf, Vec<u8>), String> {
    let canonical =
        fs::canonicalize(path).map_err(|error| format!("resolve ops request file: {error}"))?;
    if !canonical.starts_with(state) {
        return Err("ops request file must be inside MULTIAGENT_STATE_DIR".into());
    }
    let (bytes, _metadata) = read_bounded_file(&canonical, MAX_OPERATION_REQUEST_BYTES, true)?;
    if bytes.is_empty() {
        return Err("ops request must contain between 1 and 65536 bytes".into());
    }
    #[cfg(target_os = "linux")]
    if !matches!(
        _metadata.uid(),
        crate::config::OPS_UID | crate::config::SUPERVISOR_UID
    ) || _metadata.mode() & 0o022 != 0
    {
        return Err("ops request must be safely owned by the ops or supervisor UID".into());
    }
    Ok((canonical, bytes))
}

fn read_bounded_file(
    path: &Path,
    limit: u64,
    no_follow: bool,
) -> Result<(Vec<u8>, fs::Metadata), String> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    if no_follow {
        options.custom_flags(libc::O_NOFOLLOW);
    }
    let mut file = options
        .open(path)
        .map_err(|error| format!("open {}: {error}", path.display()))?;
    let metadata = file
        .metadata()
        .map_err(|error| format!("inspect {}: {error}", path.display()))?;
    if !metadata.is_file() || metadata.len() > limit {
        return Err(format!(
            "{} must be a regular file no larger than {limit} bytes",
            path.display()
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::take(&mut file, limit + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| format!("read {}: {error}", path.display()))?;
    if bytes.len() as u64 > limit {
        return Err(format!(
            "{} exceeds the configured byte limit",
            path.display()
        ));
    }
    Ok((bytes, metadata))
}

#[cfg(target_os = "linux")]
fn secure_publication_path(path: &Path, directory: bool) -> Result<(), String> {
    use std::ffi::CString;
    use std::os::unix::ffi::OsStrExt;

    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect publication path {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() || (directory && !metadata.is_dir()) {
        return Err(format!("unsafe publication path: {}", path.display()));
    }
    if metadata.uid() != crate::config::SUPERVISOR_UID {
        if unsafe { libc::geteuid() } != 0 {
            return Err("operation request store must be supervisor-owned".into());
        }
        let raw = CString::new(path.as_os_str().as_bytes())
            .map_err(|_| format!("publication path contains NUL: {}", path.display()))?;
        if unsafe {
            libc::lchown(
                raw.as_ptr(),
                crate::config::SUPERVISOR_UID,
                crate::config::ROLE_GID,
            )
        } != 0
        {
            return Err(format!(
                "set publication ownership {}: {}",
                path.display(),
                std::io::Error::last_os_error()
            ));
        }
    }
    fs::set_permissions(
        path,
        fs::Permissions::from_mode(if directory { 0o750 } else { 0o440 }),
    )
    .map_err(|error| format!("secure publication path {}: {error}", path.display()))
}

#[cfg(not(target_os = "linux"))]
fn secure_publication_path(path: &Path, directory: bool) -> Result<(), String> {
    #[cfg(unix)]
    fs::set_permissions(
        path,
        fs::Permissions::from_mode(if directory { 0o750 } else { 0o440 }),
    )
    .map_err(|error| format!("secure publication path {}: {error}", path.display()))?;
    Ok(())
}

fn secure_publication_file(file: &fs::File) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        use std::os::fd::AsRawFd;
        let metadata = file
            .metadata()
            .map_err(|error| format!("inspect operation request artifact: {error}"))?;
        if metadata.uid() != crate::config::SUPERVISOR_UID {
            if unsafe { libc::geteuid() } != 0 {
                return Err("operation request artifact must be supervisor-owned".into());
            }
            if unsafe {
                libc::fchown(
                    file.as_raw_fd(),
                    crate::config::SUPERVISOR_UID,
                    crate::config::ROLE_GID,
                )
            } != 0
            {
                return Err(format!(
                    "set operation request artifact ownership: {}",
                    std::io::Error::last_os_error()
                ));
            }
        }
    }
    #[cfg(unix)]
    file.set_permissions(fs::Permissions::from_mode(0o440))
        .map_err(|error| format!("secure operation request artifact: {error}"))?;
    Ok(())
}

fn validate_published_metadata(_metadata: &fs::Metadata) -> Result<(), String> {
    #[cfg(target_os = "linux")]
    if _metadata.uid() != crate::config::SUPERVISOR_UID || _metadata.mode() & 0o227 != 0 {
        return Err("published operation request has unsafe ownership or mode".into());
    }
    Ok(())
}

fn review_bind(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let bytes = fs::read(required(&options, "--request-file")?)
        .map_err(|error| format!("read ops request: {error}"))?;
    let template: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode ops request template: {error}"))?;
    validate_request_template(&template)?;
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;
    let runbook_content_sha256 = verified_runbook_content(&template)?;
    let binding = review_binding_value(&template, &runbook_content_sha256)?;
    let binding_path = review_binding_artifact_path()?;
    write_review_binding_artifact(
        &binding_path,
        &serde_json::to_vec_pretty(&binding)
            .map_err(|error| format!("encode ops review binding: {error}"))?,
    )?;
    println!("request-template-sha256={}", digest_json(&template)?);
    println!(
        "goal-sha256={}",
        digest_json(
            object
                .get("goal")
                .ok_or("ops request template requires goal")?
        )?
    );
    println!(
        "runbook-sha256={}",
        digest_json(
            object
                .get("runbook")
                .ok_or("ops request template requires runbook")?
        )?
    );
    println!("runbook-content-sha256={runbook_content_sha256}");
    println!("review-binding-artifact={}", binding_path.display());
    Ok(ExitCode::SUCCESS)
}

pub(crate) fn review_binding_for_request(request_file: &Path) -> Result<String, String> {
    let bytes = fs::read(request_file).map_err(|error| format!("read ops request: {error}"))?;
    let template: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode ops request template: {error}"))?;
    validate_request_template(&template)?;
    let runbook_content_sha256 = verified_runbook_content(&template)?;
    review_binding_marker(&template, &runbook_content_sha256)
}

fn load_reviewed_request(
    request_file: &Path,
    reviewer: &str,
) -> Result<(PathBuf, Value, TrustedApproval), String> {
    validate_id("reviewer name", reviewer)?;
    let state = fs::canonicalize(required_env("MULTIAGENT_STATE_DIR")?)
        .map_err(|error| format!("resolve multiagent state: {error}"))?;
    let (_, bytes) = read_reviewable_request(&state, request_file)?;
    let template: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode ops request template: {error}"))?;
    let runbook_content_sha256 = verified_runbook_content(&template)?;
    let reviewer_approval = verify_reviewer(&state, reviewer, &template, &runbook_content_sha256)?;
    Ok((state, template, reviewer_approval))
}

pub(crate) fn preflight_reviewed_request(
    request_file: &Path,
    reviewer: &str,
) -> Result<(), String> {
    load_reviewed_request(request_file, reviewer).map(|_| ())
}

/// Executes a reviewer-requested observation without granting the reviewer
/// transport credentials or mutation authority. The authority socket admits
/// this command only from REVIEWER_UID; these checks bind the request further
/// to the live reviewer identity and the immutable operation under review.
fn execute_reviewer_read(_args: &[String]) -> Result<ExitCode, String> {
    #[cfg(not(target_os = "linux"))]
    return Err("reviewer ops execute requires Linux reviewer isolation".into());

    #[cfg(target_os = "linux")]
    {
        let args = _args;
        let options = options(args)?;
        let reviewer = required(&options, "--reviewer")?;
        validate_id("reviewer name", reviewer)?;
        let state = fs::canonicalize(required_env("MULTIAGENT_STATE_DIR")?)
            .map_err(|error| format!("resolve multiagent state: {error}"))?;
        validate_live_reviewer(&state, reviewer)?;

        let reviewed_path = PathBuf::from(required(&options, "--reviewed-request")?);
        let (_, reviewed_bytes) = read_reviewable_request(&state, &reviewed_path)?;
        let reviewed: Value = serde_json::from_slice(&reviewed_bytes)
            .map_err(|error| format!("decode reviewed ops request: {error}"))?;
        validate_request_template(&reviewed)?;
        verified_runbook_content(&reviewed)?;

        let evidence_path = PathBuf::from(required(&options, "--request-file")?);
        let evidence_bytes = read_reviewer_request(reviewer, &evidence_path)?;
        let evidence_template: Value = serde_json::from_slice(&evidence_bytes)
            .map_err(|error| format!("decode reviewer evidence request: {error}"))?;
        validate_request_template(&evidence_template)?;
        verified_runbook_content(&evidence_template)?;
        validate_evidence_scope(&reviewed, &evidence_template)?;

        let operation_id = evidence_template
            .pointer("/operation/id")
            .and_then(Value::as_str)
            .ok_or("reviewer evidence request has no operation ID")?;
        let capabilities = call_prod_mcp_tool("operations_capabilities", json!({}))?;
        let capability = operation_capability(&capabilities, operation_id)?;
        validate_read_capability(capability, &evidence_template)?;

        let now = Utc::now();
        let caller_subject = env::var("MULTIAGENT_CALLER_SUBJECT")
            .ok()
            .filter(|value| !value.is_empty())
            .unwrap_or_else(|| "multiagent-control".into());
        validate_id("caller subject", &caller_subject)?;
        let caller = TrustedApproval {
            subject: caller_subject,
            role: "safety-reviewer",
            evidence_sha256: digest_json(
                reviewed
                    .get("goal")
                    .ok_or("reviewed ops request requires goal")?,
            )?,
            approved_at: env::var("MULTIAGENT_CALLER_APPROVED_AT")
                .ok()
                .filter(|value| !value.is_empty())
                .unwrap_or_else(|| now.to_rfc3339_opts(SecondsFormat::Millis, true)),
        };
        let request = build_request(
            &evidence_template,
            &[&caller],
            "runbook-observer",
            reviewer,
            &execution_context_from_environment()?,
            now,
        )?;
        let action_id = request["actionId"]
            .as_str()
            .ok_or("generated reviewer evidence request has no action ID")?
            .to_string();
        let payload = canonical(&json!({
            "apiVersion":"prod.moveindustries.io/v1",
            "kind":"ActionPermit",
            "request": request
        }))?;
        let permit = sign_permit(&payload)?;
        let result = call_prod_mcp(&permit)?;
        let evidence_dir = state
            .join("reviewer-live-evidence")
            .join(reviewer)
            .join(&action_id);
        fs::create_dir_all(&evidence_dir)
            .map_err(|error| format!("create reviewer evidence receipt directory: {error}"))?;
        secure_publication_path(&evidence_dir, true)?;
        let request_artifact = evidence_dir.join("request.json");
        let receipt_artifact = evidence_dir.join("receipt.json");
        fs::write(
            &request_artifact,
            serde_json::to_vec_pretty(&evidence_template).map_err(|error| error.to_string())?,
        )
        .map_err(|error| format!("persist reviewer evidence request: {error}"))?;
        fs::write(
            &receipt_artifact,
            serde_json::to_vec_pretty(&result).map_err(|error| error.to_string())?,
        )
        .map_err(|error| format!("persist reviewer evidence receipt: {error}"))?;
        secure_publication_path(&request_artifact, false)?;
        secure_publication_path(&receipt_artifact, false)?;
        let structured = result
            .pointer("/result/structuredContent")
            .cloned()
            .unwrap_or(Value::Null);
        println!(
            "{}",
            serde_json::to_string(&json!({
                "apiVersion": "multiagent.moveindustries.io/v1",
                "kind": "ReviewerEvidenceResult",
                "reviewer": reviewer,
                "actionId": action_id,
                "operationId": structured.get("operationId").cloned().unwrap_or(Value::Null),
                "state": structured.get("state").cloned().unwrap_or(Value::Null),
                "outcome": structured.get("outcome").cloned().unwrap_or(Value::Null),
                "code": structured.get("code").cloned().unwrap_or(Value::Null),
                "message": structured.get("message").cloned().unwrap_or(Value::Null),
                "evidence": structured.get("summary").cloned().unwrap_or(Value::Null),
                "receiptPath": receipt_artifact,
            }))
            .map_err(|error| format!("encode reviewer evidence result: {error}"))?
        );
        Ok(ExitCode::SUCCESS)
    }
}

#[cfg(target_os = "linux")]
fn validate_live_reviewer(state: &Path, reviewer: &str) -> Result<(), String> {
    let metadata =
        crate::state::read_env(&state.join("subagents").join(reviewer).join("meta.env"))?;
    if metadata.get("role").map(String::as_str) != Some("reviewer")
        || metadata.get("access").map(String::as_str) != Some("read-only")
    {
        return Err("reviewer evidence reads require a live read-only reviewer identity".into());
    }
    let status = fs::read_to_string(state.join("subagents").join(reviewer).join("status"))
        .unwrap_or_default();
    if !matches!(status.trim(), "running" | "waiting" | "restoring") {
        return Err("reviewer evidence reads require the reviewer to be active".into());
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn read_reviewer_request(reviewer: &str, path: &Path) -> Result<Vec<u8>, String> {
    let logs = fs::canonicalize(required_env("MULTIAGENT_LOG_DIR")?)
        .map_err(|error| format!("resolve multiagent log directory: {error}"))?;
    let reviewer_root = fs::canonicalize(logs.join("agents").join(reviewer))
        .map_err(|error| format!("resolve reviewer trace directory: {error}"))?;
    let canonical = fs::canonicalize(path)
        .map_err(|error| format!("resolve reviewer evidence request: {error}"))?;
    if !canonical.starts_with(&reviewer_root) {
        return Err("reviewer evidence request must be inside its own trace directory".into());
    }
    let (bytes, metadata) = read_bounded_file(&canonical, MAX_OPERATION_REQUEST_BYTES, true)?;
    if metadata.uid() != crate::config::REVIEWER_UID || metadata.mode() & 0o022 != 0 {
        return Err(
            "reviewer evidence request must be reviewer-owned and not group- or world-writable"
                .into(),
        );
    }
    Ok(bytes)
}

fn validate_evidence_scope(reviewed: &Value, evidence: &Value) -> Result<(), String> {
    for pointer in [
        "/taskId",
        "/goal",
        "/target",
        "/runbook",
        "/runbookDocument",
        "/runbookContentSha256",
    ] {
        if reviewed.pointer(pointer) != evidence.pointer(pointer) {
            return Err(format!(
                "reviewer evidence request widened reviewed scope at {pointer}"
            ));
        }
    }
    Ok(())
}

fn validate_read_capability(capability: &Value, template: &Value) -> Result<(), String> {
    if capability.get("access").and_then(Value::as_str) != Some("read")
        || capability.get("mutation").and_then(Value::as_bool) != Some(false)
    {
        return Err(
            "reviewer evidence operation must be advertised as read-only and non-mutating".into(),
        );
    }
    if capability
        .get("requiredApprovalRoles")
        .and_then(Value::as_array)
        .is_none_or(|roles| !roles.is_empty())
    {
        return Err("reviewer evidence operation must not require mutation approvals".into());
    }
    if capability.get("version") != template.pointer("/operation/version") {
        return Err(
            "reviewer evidence operation version does not match prod-mcp capability".into(),
        );
    }
    let runbook = format!(
        "{}@{}",
        template
            .pointer("/runbook/id")
            .and_then(Value::as_str)
            .unwrap_or(""),
        template
            .pointer("/runbook/version")
            .and_then(Value::as_str)
            .unwrap_or("")
    );
    let allowed = capability
        .get("allowedRunbooks")
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some(&runbook)));
    if !allowed {
        return Err("reviewer evidence operation is not allowed by the reviewed runbook".into());
    }
    Ok(())
}

fn validate_diagnosis_capability(capability: &Value) -> Result<(), String> {
    let access = capability.get("access").and_then(Value::as_str);
    if !matches!(access, Some("read" | "materialize"))
        || capability.get("mutation").and_then(Value::as_bool) != Some(false)
    {
        return Err("diagnosis-only sessions may execute only non-mutating read or materialize capabilities".into());
    }
    if capability
        .get("requiredApprovalRoles")
        .and_then(Value::as_array)
        .is_none_or(|roles| !roles.is_empty())
    {
        return Err("diagnosis-only capability must not require mutation approval roles".into());
    }
    Ok(())
}

fn enforce_authority_scope(template: &Value) -> Result<(), String> {
    match env::var("MULTIAGENT_AUTHORITY_SCOPE")
        .as_deref()
        .unwrap_or("human")
    {
        "human" => Ok(()),
        "observe" | "diagnosis-only" => {
            let operation_id = template
                .pointer("/operation/id")
                .and_then(Value::as_str)
                .ok_or("ops request has no operation ID")?;
            let capabilities = call_prod_mcp_tool("operations_capabilities", json!({}))?;
            validate_diagnosis_capability(operation_capability(&capabilities, operation_id)?)
        }
        "user" => {
            if crate::execution::configured()?.permits_reviewed_ops() {
                Ok(())
            } else {
                Err("the active Execution does not authorize reviewed operations".into())
            }
        }
        _ => Err("MULTIAGENT_AUTHORITY_SCOPE is invalid".into()),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DirectAccess {
    Read,
    Materialize,
}

fn execute_direct_read(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    if options.len() != 1 {
        return Err("usage: multiagent ops read --request-file PATH".into());
    }
    let caller_uid = env::var("MULTIAGENT_AUTHORITY_CALLER_UID")
        .map_err(|_| "ops read must be mediated by the authority supervisor")?
        .parse::<u32>()
        .map_err(|_| "authority caller UID is invalid")?;
    let caller_role = direct_requester_role(caller_uid)?;
    let request_file = PathBuf::from(required(&options, "--request-file")?);
    let bytes = read_direct_request(&request_file, caller_uid)?;
    let execution_context = execution_context_from_environment()?;
    let template = bind_direct_request(&bytes, &execution_context)?;

    let operation_id = template
        .pointer("/operation/id")
        .and_then(Value::as_str)
        .ok_or("direct read request has no operation ID")?
        .to_string();
    let capabilities = call_prod_mcp_tool("operations_capabilities", json!({}))?;
    let capability = operation_capability(&capabilities, &operation_id)?;
    let access = validate_direct_capability(capability, &template)?;
    let parameters = template
        .get("parameters")
        .ok_or("direct read request requires parameters")?;
    reject_arbitrary_urls(parameters)?;
    match (access, operation_id.as_str()) {
        (DirectAccess::Materialize, "github.clone") => {
            let object = parameters
                .as_object()
                .ok_or("github.clone parameters must be an object")?;
            if object.len() != 1 {
                return Err("github.clone parameters may contain only repository".into());
            }
            validate_repository(
                object
                    .get("repository")
                    .and_then(Value::as_str)
                    .ok_or("github.clone parameters require repository")?,
            )?;
        }
        (DirectAccess::Materialize, _) => {
            return Err("direct materialization has no credential-safe runtime handler".into());
        }
        (DirectAccess::Read, "github.clone") => {
            return Err("github.clone must be advertised as a materialize operation".into());
        }
        (DirectAccess::Read, _) => {}
    }

    let bound_template = template;
    let delegated_role = match access {
        DirectAccess::Read => "runbook-observer",
        // The deployed github.clone@1.0.0 contract admits the operator role for
        // materialization. This role labels the signed permit; no ops agent is
        // created and the authenticated requester remains locally audited.
        DirectAccess::Materialize => "runbook-operator",
    };
    let now = Utc::now();
    let request = build_request(
        &bound_template,
        &[],
        delegated_role,
        "multiagent-supervisor",
        &execution_context,
        now,
    )?;
    let action_id = request["actionId"]
        .as_str()
        .ok_or("generated direct read request has no action ID")?
        .to_string();
    let payload = canonical(&json!({
        "apiVersion":"prod.moveindustries.io/v1",
        "kind":"ActionPermit",
        "request": request
    }))?;
    let permit = sign_permit(&payload)?;
    let result = call_prod_mcp(&permit)?;
    let state = fs::canonicalize(required_env("MULTIAGENT_STATE_DIR")?)
        .map_err(|error| format!("resolve multiagent state: {error}"))?;
    persist_direct_receipt(
        &state,
        &action_id,
        caller_uid,
        caller_role,
        &bound_template,
        &result,
    )?;
    let structured = successful_operation_receipt(&result)?;

    if operation_id == "github.clone" {
        let repository = bound_template
            .pointer("/parameters/repository")
            .and_then(Value::as_str)
            .ok_or("github.clone parameters require repository")?;
        validate_repository(repository)?;
        let summary = clone_summary(structured, repository, &required_env("PROD_MCP_URL")?)?;
        let token = required_env("PROD_MCP_BEARER_TOKEN")?;
        let (path, commit) =
            materialize_repository(&state, repository, &summary.clone_url, &token, &permit)?;
        println!(
            "{}",
            serde_json::to_string(&json!({
                "apiVersion": "multiagent.moveindustries.io/v1",
                "kind": "RepositoryMaterializationResult",
                "actionId": action_id,
                "operationId": structured.get("operationId").cloned().unwrap_or(Value::Null),
                "repository": repository,
                "path": path,
                "commit": commit,
                "requesterRole": caller_role,
                "receiptRecorded": true,
            }))
            .map_err(|error| format!("encode repository materialization result: {error}"))?
        );
        return Ok(ExitCode::SUCCESS);
    }

    let evidence = structured
        .get("summary")
        .and_then(Value::as_str)
        .map(|summary| {
            serde_json::from_str(summary).unwrap_or_else(|_| Value::String(summary.into()))
        })
        .unwrap_or(Value::Null);
    println!(
        "{}",
        serde_json::to_string(&json!({
            "apiVersion": "multiagent.moveindustries.io/v1",
            "kind": "DirectReadResult",
            "actionId": action_id,
            "operationId": structured.get("operationId").cloned().unwrap_or(Value::Null),
            "requestedOperation": structured.get("requestedOperation").cloned().unwrap_or(Value::Null),
            "state": structured.get("state").cloned().unwrap_or(Value::Null),
            "outcome": structured.get("outcome").cloned().unwrap_or(Value::Null),
            "code": structured.get("code").cloned().unwrap_or(Value::Null),
            "message": structured.get("message").cloned().unwrap_or(Value::Null),
            "evidence": evidence,
            "requesterRole": caller_role,
            "receiptRecorded": true,
        }))
        .map_err(|error| format!("encode direct read result: {error}"))?
    );
    Ok(ExitCode::SUCCESS)
}

fn direct_requester_role(uid: u32) -> Result<&'static str, String> {
    match uid {
        0 => Ok("supervisor"),
        crate::config::ORCHESTRATOR_UID => Ok("orchestrator"),
        crate::config::WRITER_UID => Ok("writer"),
        crate::config::READER_UID => Ok("reader"),
        crate::config::OPS_UID => Ok("ops"),
        crate::config::REVIEWER_UID => Ok("reviewer"),
        _ => Err("direct reads require a confined session role identity".into()),
    }
}

fn read_direct_request(path: &Path, _caller_uid: u32) -> Result<Vec<u8>, String> {
    let logs = fs::canonicalize(required_env("MULTIAGENT_LOG_DIR")?)
        .map_err(|error| format!("resolve multiagent log directory: {error}"))?;
    let agents = fs::canonicalize(logs.join("agents"))
        .map_err(|error| format!("resolve agent trace directory: {error}"))?;
    let canonical =
        fs::canonicalize(path).map_err(|error| format!("resolve direct read request: {error}"))?;
    if !canonical.starts_with(&agents) {
        return Err("direct read request must be inside MULTIAGENT_LOG_DIR/agents".into());
    }
    let (bytes, _metadata) = read_bounded_file(&canonical, MAX_OPERATION_REQUEST_BYTES, true)?;
    if bytes.is_empty() {
        return Err("direct read request must contain between 1 and 65536 bytes".into());
    }
    #[cfg(target_os = "linux")]
    if _metadata.uid() != _caller_uid || _metadata.mode() & 0o022 != 0 {
        return Err(
            "direct read request must be caller-owned and not group- or world-writable".into(),
        );
    }
    Ok(bytes)
}

fn direct_request_runbook(unbound: &Value) -> Result<&str, String> {
    let object = unbound
        .as_object()
        .ok_or("direct read request must be an object")?;
    let allowed = ["operation", "parameters", "runbook", "runbookDocument"];
    if object.len() != allowed.len() || object.keys().any(|key| !allowed.contains(&key.as_str())) {
        return Err("direct read request may contain only operation, parameters, runbook, and runbookDocument".into());
    }
    object
        .get("runbookDocument")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "direct read request requires runbookDocument".into())
}

fn bind_direct_request(bytes: &[u8], execution_context: &Value) -> Result<Value, String> {
    let unbound: Value = serde_json::from_slice(bytes)
        .map_err(|error| format!("decode direct read request: {error}"))?;
    let runbook_document = direct_request_runbook(&unbound)?.to_string();
    let task_bound = bind_authenticated_task(unbound, execution_context)?;
    let encoded = serde_json::to_vec(&task_bound)
        .map_err(|error| format!("encode task-bound direct read request: {error}"))?;
    let (template, _) = bind_request_template(&encoded, &runbook_document, false)?;
    verified_runbook_content(&template)?;
    Ok(template)
}

fn bind_authenticated_task(
    mut template: Value,
    execution_context: &Value,
) -> Result<Value, String> {
    let workflow_id = required_env("MULTIAGENT_WORKFLOW_ID")?;
    let envelope = crate::workflow::semantic_envelope(&workflow_id)?;
    let task = envelope.original_task.trim();
    if task.is_empty() || task.len() > 32_768 {
        return Err("supervisor-sealed original task must contain 1 to 32768 bytes".into());
    }
    let thread_id = execution_context
        .get("threadId")
        .and_then(Value::as_str)
        .ok_or("execution context has no thread ID")?;
    let object = template
        .as_object_mut()
        .ok_or("direct read request template must be an object")?;
    object.insert("taskId".into(), Value::String(thread_id.into()));
    object.insert("goal".into(), Value::String(task.into()));
    object.remove("history");
    Ok(template)
}

fn validate_direct_capability(
    capability: &Value,
    template: &Value,
) -> Result<DirectAccess, String> {
    let access = match capability.get("access").and_then(Value::as_str) {
        Some("read") => DirectAccess::Read,
        Some("materialize") => DirectAccess::Materialize,
        _ => {
            return Err(
                "direct operation must be advertised with read or materialize access".into(),
            )
        }
    };
    if capability.get("mutation").and_then(Value::as_bool) != Some(false) {
        return Err("direct operation must be advertised as non-mutating".into());
    }
    if capability
        .get("requiredApprovalRoles")
        .and_then(Value::as_array)
        .is_none_or(|roles| !roles.is_empty())
    {
        return Err("direct operation must not require approval roles".into());
    }
    if capability.get("version") != template.pointer("/operation/version") {
        return Err("direct operation version does not match prod-mcp capability".into());
    }
    let runbook = format!(
        "{}@{}",
        template
            .pointer("/runbook/id")
            .and_then(Value::as_str)
            .unwrap_or(""),
        template
            .pointer("/runbook/version")
            .and_then(Value::as_str)
            .unwrap_or("")
    );
    let allowed = capability
        .get("allowedRunbooks")
        .and_then(Value::as_array)
        .is_some_and(|values| values.iter().any(|value| value.as_str() == Some(&runbook)));
    if !allowed {
        return Err("direct operation is not allowed by the bound runbook".into());
    }
    Ok(access)
}

fn reject_arbitrary_urls(value: &Value) -> Result<(), String> {
    match value {
        Value::String(value) => {
            let normalized = value.to_ascii_lowercase();
            if normalized.contains("://")
                || normalized.starts_with("git@")
                || normalized.starts_with("file:")
            {
                Err("direct operation parameters must not contain an arbitrary URL".into())
            } else {
                Ok(())
            }
        }
        Value::Array(values) => {
            for value in values {
                reject_arbitrary_urls(value)?;
            }
            Ok(())
        }
        Value::Object(values) => {
            for (key, value) in values {
                if matches!(
                    key.to_ascii_lowercase().as_str(),
                    "url" | "uri" | "endpoint" | "hostname" | "host"
                ) {
                    return Err(
                        "direct operation parameters must not select a network destination".into(),
                    );
                }
                reject_arbitrary_urls(value)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn successful_operation_receipt(result: &Value) -> Result<&serde_json::Map<String, Value>, String> {
    let outer = result
        .get("result")
        .and_then(Value::as_object)
        .ok_or("prod-mcp execution response has no result object")?;
    if outer.get("isError").and_then(Value::as_bool) == Some(true) {
        return Err("prod-mcp rejected the direct operation".into());
    }
    let structured = outer
        .get("structuredContent")
        .and_then(Value::as_object)
        .ok_or("prod-mcp execution response has no structured receipt")?;
    if structured.get("state").and_then(Value::as_str) != Some("succeeded") {
        return Err("prod-mcp direct operation did not succeed".into());
    }
    Ok(structured)
}

fn persist_direct_receipt(
    state: &Path,
    action_id: &str,
    caller_uid: u32,
    caller_role: &str,
    template: &Value,
    result: &Value,
) -> Result<(), String> {
    let operation = state.join("operations").join(action_id);
    fs::create_dir(&operation)
        .map_err(|error| format!("create direct operation receipt directory: {error}"))?;
    secure_publication_path(&operation, true)?;
    let request_path = operation.join("direct-request.json");
    let receipt_path = operation.join("receipt.redacted.json");
    write_private_file(
        &request_path,
        &serde_json::to_vec_pretty(&json!({
            "requesterUid": caller_uid,
            "requesterRole": caller_role,
            "template": template,
        }))
        .map_err(|error| format!("encode direct read request record: {error}"))?,
    )?;
    let redacted = redacted_direct_receipt(template, result)?;
    write_private_file(
        &receipt_path,
        &serde_json::to_vec_pretty(&json!({
            "rawReceiptSha256": digest(&canonical(result)?),
            "receipt": redacted,
        }))
        .map_err(|error| format!("encode redacted direct read receipt: {error}"))?,
    )?;
    secure_publication_path(&request_path, false)?;
    secure_publication_path(&receipt_path, false)
}

fn redacted_direct_receipt(template: &Value, result: &Value) -> Result<Value, String> {
    if template.pointer("/operation/id").and_then(Value::as_str) != Some("github.clone") {
        return Ok(result.clone());
    }
    let outer = result
        .get("result")
        .and_then(Value::as_object)
        .ok_or("prod-mcp execution response has no result object")?;
    let structured = outer.get("structuredContent").and_then(Value::as_object);
    let succeeded = outer.get("isError").and_then(Value::as_bool) != Some(true)
        && structured
            .and_then(|value| value.get("state"))
            .and_then(Value::as_str)
            == Some("succeeded");
    let summary = serde_json::to_string(&json!({
        "operation": "github.clone",
        "repository": template.pointer("/parameters/repository").cloned().unwrap_or(Value::Null),
        "disposition": if succeeded { "succeeded" } else { "failed" },
        "cloneUrl": "[REDACTED]",
        "authentication": "[REDACTED]",
    }))
    .map_err(|error| format!("redact github.clone receipt: {error}"))?;
    // Do not copy unknown response fields: prod-mcp duplicates tool text in
    // `content`, and future representations could repeat the protected URL or
    // transport headers. Persist only this audited allowlist.
    Ok(json!({
        "jsonrpc": result.get("jsonrpc").cloned().unwrap_or_else(|| Value::String("2.0".into())),
        "id": result.get("id").cloned().unwrap_or(Value::Null),
        "result": {
            "isError": !succeeded,
            "content": [{
                "type": "text",
                "text": if succeeded {
                    "github.clone succeeded; protected transport fields redacted"
                } else {
                    "github.clone failed; protected transport fields redacted"
                }
            }],
            "structuredContent": {
                "operationId": structured.and_then(|value| value.get("operationId")).cloned().unwrap_or(Value::Null),
                "requestedOperation": structured.and_then(|value| value.get("requestedOperation")).cloned().unwrap_or(Value::Null),
                "state": if succeeded { "succeeded" } else { "failed" },
                "summary": summary
            }
        }
    }))
}

struct CloneSummary {
    clone_url: String,
}

fn clone_summary(
    receipt: &serde_json::Map<String, Value>,
    expected_repository: &str,
    prod_mcp_url: &str,
) -> Result<CloneSummary, String> {
    let summary = receipt
        .get("summary")
        .and_then(Value::as_str)
        .ok_or("github.clone receipt has no summary")?;
    let summary: Value = serde_json::from_str(summary)
        .map_err(|error| format!("decode github.clone summary: {error}"))?;
    let object = summary
        .as_object()
        .ok_or("github.clone summary must be an object")?;
    if object.len() != 4
        || object.get("operation").and_then(Value::as_str) != Some("github.clone")
        || object.get("repository").and_then(Value::as_str) != Some(expected_repository)
    {
        return Err("github.clone summary does not match the requested repository".into());
    }
    let authentication = object
        .get("authentication")
        .and_then(Value::as_object)
        .ok_or("github.clone summary has no authentication contract")?;
    if authentication.len() != 2
        || authentication.get("bearerHeader").and_then(Value::as_str) != Some("Authorization")
        || authentication.get("permitHeader").and_then(Value::as_str) != Some("X-Prod-MCP-Permit")
    {
        return Err("github.clone authentication contract is unsupported".into());
    }
    let clone_url = object
        .get("cloneUrl")
        .and_then(Value::as_str)
        .ok_or("github.clone summary has no cloneUrl")?;
    validate_clone_url(clone_url, prod_mcp_url, expected_repository)?;
    Ok(CloneSummary {
        clone_url: clone_url.into(),
    })
}

fn validate_clone_url(clone_url: &str, prod_mcp_url: &str, repository: &str) -> Result<(), String> {
    let (clone_origin, clone_path) = http_origin_and_path(clone_url)?;
    let (mcp_origin, _) = http_origin_and_path(prod_mcp_url)?;
    if !clone_origin.eq_ignore_ascii_case(&mcp_origin) {
        return Err("github.clone URL origin differs from PROD_MCP_URL".into());
    }
    if clone_path != format!("/git/{repository}.git") {
        return Err("github.clone URL path differs from the signed repository".into());
    }
    Ok(())
}

fn http_origin_and_path(value: &str) -> Result<(String, &str), String> {
    if value.contains(['\\', '\n', '\r', '\t', '#', '?']) {
        return Err("prod-mcp URL contains unsupported characters".into());
    }
    let (scheme, remainder) = value
        .split_once("://")
        .ok_or("prod-mcp URL must use HTTP or HTTPS")?;
    if !matches!(scheme, "http" | "https") {
        return Err("prod-mcp URL must use HTTP or HTTPS".into());
    }
    let slash = remainder.find('/').unwrap_or(remainder.len());
    let authority = &remainder[..slash];
    if authority.is_empty() || authority.contains('@') {
        return Err("prod-mcp URL authority is invalid".into());
    }
    let path = if slash == remainder.len() {
        "/"
    } else {
        &remainder[slash..]
    };
    Ok((format!("{}://{}", scheme, authority), path))
}

fn validate_repository(repository: &str) -> Result<(), String> {
    let mut parts = repository.split('/');
    let owner = parts.next().unwrap_or("");
    let name = parts.next().unwrap_or("");
    if parts.next().is_some()
        || owner.is_empty()
        || name.is_empty()
        || ![owner, name].iter().all(|part| {
            part.chars().all(|character| {
                character.is_ascii_alphanumeric() || matches!(character, '_' | '.' | '-')
            })
        })
    {
        return Err("GitHub repository must be owner/name".into());
    }
    Ok(())
}

fn materialize_repository(
    state: &Path,
    repository: &str,
    clone_url: &str,
    token: &str,
    permit: &str,
) -> Result<(PathBuf, String), String> {
    validate_repository(repository)?;
    if token.contains(['\n', '\r']) || permit.contains(['\n', '\r']) {
        return Err("repository authentication values contain an invalid newline".into());
    }
    let root = state.join("materialized-repositories");
    ensure_materialization_root(&root)?;
    materialization_usage(&root, MAX_MATERIALIZATION_FILES, MAX_MATERIALIZATION_BYTES)?;
    let name = repository
        .split_once('/')
        .map(|(owner, name)| format!("{owner}-{name}-{}", &digest(repository.as_bytes())[7..23]))
        .ok_or("GitHub repository must be owner/name")?;
    let destination = root.join(&name);
    let manifest = root.join(format!(".{name}.json"));
    if destination.exists() {
        return existing_materialization(&destination, &manifest, repository);
    }

    let temporary = root.join(format!(".{name}.tmp-{}", std::process::id()));
    if temporary.exists() {
        return Err("repository materialization temporary path already exists".into());
    }
    let config = state.join("tmp").join(format!(
        "git-auth-{}-{}.config",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|error| error.to_string())?
            .as_nanos()
    ));
    let config_contents = git_auth_config(clone_url, token, permit)?;
    write_private_file(&config, config_contents.as_bytes())?;
    let clone_result = run_bounded_materialization(
        git_clone_command(&temporary, &config),
        &root,
        MATERIALIZATION_TIMEOUT,
        MAX_MATERIALIZATION_FILES,
        MAX_MATERIALIZATION_BYTES,
    );
    let _ = fs::remove_file(&config);
    let status = match clone_result {
        Ok(status) => status,
        Err(error) => {
            if temporary.starts_with(&root) {
                let _ = fs::remove_dir_all(&temporary);
            }
            return Err(error);
        }
    };
    if !status.success() {
        if temporary.starts_with(&root) {
            let _ = fs::remove_dir_all(&temporary);
        }
        return Err("repository clone through prod-mcp failed".into());
    }

    let public_url = format!("https://github.com/{repository}.git");
    let sanitized =
        sanitized_git_command(&temporary, &["remote", "set-url", "origin", &public_url])
            .output()
            .map_err(|error| format!("sanitize materialized repository remote: {error}"))?;
    if !sanitized.status.success() {
        let _ = fs::remove_dir_all(&temporary);
        return Err("could not remove authenticated URL from materialized repository".into());
    }
    let commit_output = sanitized_git_command(&temporary, &["rev-parse", "--verify", "HEAD"])
        .output()
        .map_err(|error| format!("resolve materialized repository commit: {error}"))?;
    if !commit_output.status.success() {
        let _ = fs::remove_dir_all(&temporary);
        return Err("materialized repository has no resolved HEAD".into());
    }
    let commit = String::from_utf8(commit_output.stdout)
        .map_err(|_| "materialized repository HEAD is not UTF-8".to_string())?
        .trim()
        .to_string();
    if commit.len() != 40
        || !commit
            .chars()
            .all(|character| character.is_ascii_hexdigit())
    {
        let _ = fs::remove_dir_all(&temporary);
        return Err("materialized repository HEAD is invalid".into());
    }
    fs::rename(&temporary, &destination)
        .map_err(|error| format!("publish materialized repository: {error}"))?;
    make_repository_read_only(&destination)?;
    write_materialization_manifest(&manifest, repository, &destination, &commit)?;
    Ok((destination, commit))
}

fn ensure_materialization_root(path: &Path) -> Result<(), String> {
    if !path.exists() {
        fs::create_dir(path)
            .map_err(|error| format!("create repository materialization root: {error}"))?;
    }
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect repository materialization root: {error}"))?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("repository materialization root must be a real directory".into());
    }
    #[cfg(target_os = "linux")]
    if metadata.uid() != crate::config::SUPERVISOR_UID {
        return Err("repository materialization root must be supervisor-owned".into());
    }
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o750))
        .map_err(|error| format!("protect repository materialization root: {error}"))?;
    Ok(())
}

fn materialization_usage(
    root: &Path,
    max_files: u64,
    max_bytes: u64,
) -> Result<(u64, u64), String> {
    let mut pending = vec![root.to_path_buf()];
    let mut files = 0_u64;
    let mut bytes = 0_u64;
    while let Some(path) = pending.pop() {
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && path != root => continue,
            Err(error) => {
                return Err(format!("inspect repository materialization quota: {error}"));
            }
        };
        if path != root {
            files = files.saturating_add(1);
            bytes = bytes.saturating_add(metadata.len());
            if files > max_files || bytes > max_bytes {
                return Err(format!(
                    "repository materialization exceeds supervisor quota ({max_files} files, {max_bytes} bytes)"
                ));
            }
        }
        if metadata.is_dir() && !metadata.file_type().is_symlink() {
            let entries = match fs::read_dir(&path) {
                Ok(entries) => entries,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound && path != root => {
                    continue
                }
                Err(error) => {
                    return Err(format!("scan repository materialization quota: {error}"));
                }
            };
            for entry in entries {
                match entry {
                    Ok(entry) => pending.push(entry.path()),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(error) => {
                        return Err(format!("scan repository materialization entry: {error}"));
                    }
                }
            }
        }
    }
    Ok((files, bytes))
}

fn run_bounded_materialization(
    mut command: Command,
    root: &Path,
    timeout: StdDuration,
    max_files: u64,
    max_bytes: u64,
) -> Result<std::process::ExitStatus, String> {
    let mut child = command
        .spawn()
        .map_err(|error| format!("execute repository clone: {error}"))?;
    let started = Instant::now();
    loop {
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("wait for repository clone: {error}"))?
        {
            materialization_usage(root, max_files, max_bytes)?;
            return Ok(status);
        }
        if let Err(error) = materialization_usage(root, max_files, max_bytes) {
            terminate_materialization_process(&mut child);
            let _ = child.wait();
            return Err(error);
        }
        if started.elapsed() >= timeout {
            terminate_materialization_process(&mut child);
            let _ = child.wait();
            return Err(format!(
                "repository clone exceeded the {}-second supervisor deadline",
                timeout.as_secs()
            ));
        }
        thread::sleep(StdDuration::from_millis(100));
    }
}

#[cfg(unix)]
fn terminate_materialization_process(child: &mut std::process::Child) {
    let pid = child.id() as i32;
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
        libc::kill(pid, libc::SIGKILL);
    }
}

#[cfg(not(unix))]
fn terminate_materialization_process(child: &mut std::process::Child) {
    let _ = child.kill();
}

fn existing_materialization(
    destination: &Path,
    manifest: &Path,
    repository: &str,
) -> Result<(PathBuf, String), String> {
    let destination_metadata = fs::symlink_metadata(destination)
        .map_err(|error| format!("inspect existing materialization: {error}"))?;
    if !destination_metadata.is_dir() || destination_metadata.file_type().is_symlink() {
        return Err("existing repository materialization is not a real directory".into());
    }
    let (bytes, _) = read_bounded_file(manifest, 4096, true)?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode repository materialization manifest: {error}"))?;
    if value.get("repository").and_then(Value::as_str) != Some(repository)
        || value.get("path").and_then(Value::as_str) != destination.to_str()
    {
        return Err("existing repository materialization manifest does not match".into());
    }
    let commit = value
        .get("commit")
        .and_then(Value::as_str)
        .filter(|value| {
            value.len() == 40 && value.chars().all(|character| character.is_ascii_hexdigit())
        })
        .ok_or("existing repository materialization manifest has an invalid commit")?;
    Ok((destination.to_path_buf(), commit.into()))
}

fn write_materialization_manifest(
    path: &Path,
    repository: &str,
    destination: &Path,
    commit: &str,
) -> Result<(), String> {
    fs::write(
        path,
        serde_json::to_vec_pretty(&json!({
            "repository": repository,
            "path": destination,
            "commit": commit,
        }))
        .map_err(|error| format!("encode repository materialization manifest: {error}"))?,
    )
    .map_err(|error| format!("write repository materialization manifest: {error}"))?;
    #[cfg(unix)]
    fs::set_permissions(path, fs::Permissions::from_mode(0o440))
        .map_err(|error| format!("protect repository materialization manifest: {error}"))?;
    Ok(())
}

fn git_auth_config(clone_url: &str, token: &str, permit: &str) -> Result<String, String> {
    for value in [clone_url, token, permit] {
        if value.contains(['\n', '\r', '\0']) {
            return Err("git authentication value contains an invalid character".into());
        }
    }
    Ok(format!(
        "[url {}]\n\tinsteadOf = prod-mcp-materialize:\n[http {}]\n\textraHeader = {}\n\textraHeader = {}\n\tfollowRedirects = false\n[credential]\n\thelper =\n[protocol \"file\"]\n\tallow = never\n[protocol \"ext\"]\n\tallow = never\n[filter \"lfs\"]\n\trequired = false\n\tsmudge =\n\tclean =\n[core]\n\thooksPath = /dev/null\n",
        git_config_quote(clone_url),
        git_config_quote(clone_url),
        git_config_quote(&format!("Authorization: Bearer {token}")),
        git_config_quote(&format!("X-Prod-MCP-Permit: {permit}")),
    ))
}

fn git_config_quote(value: &str) -> String {
    format!(
        "\"{}\"",
        value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('\t', "\\t")
    )
}

fn git_clone_command(destination: &Path, config: &Path) -> Command {
    let mut command = Command::new("git");
    command
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .env("LANG", "C")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", config)
        .env("GIT_LFS_SKIP_SMUDGE", "1")
        .args([
            "-c",
            "http.lowSpeedLimit=1",
            "-c",
            "http.lowSpeedTime=30",
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
        ])
        .arg("prod-mcp-materialize:")
        .arg(destination)
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    command
}

fn sanitized_git_command(repository: &Path, args: &[&str]) -> Command {
    let mut command = Command::new("git");
    command
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .env("LANG", "C")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .arg("-C")
        .arg(repository)
        .args(args)
        .stderr(Stdio::null());
    command
}

fn make_repository_read_only(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|error| format!("inspect materialized repository path: {error}"))?;
    if metadata.file_type().is_symlink() {
        return Ok(());
    }
    if metadata.is_dir() {
        for entry in fs::read_dir(path)
            .map_err(|error| format!("read materialized repository directory: {error}"))?
        {
            make_repository_read_only(
                &entry
                    .map_err(|error| format!("read materialized repository entry: {error}"))?
                    .path(),
            )?;
        }
        #[cfg(unix)]
        fs::set_permissions(path, fs::Permissions::from_mode(0o550))
            .map_err(|error| format!("protect materialized repository directory: {error}"))?;
    } else if metadata.is_file() {
        #[cfg(unix)]
        {
            let executable = metadata.permissions().mode() & 0o111 != 0;
            fs::set_permissions(
                path,
                fs::Permissions::from_mode(if executable { 0o550 } else { 0o440 }),
            )
            .map_err(|error| format!("protect materialized repository file: {error}"))?;
        }
    }
    Ok(())
}

fn execute(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let caller_uid = env::var("MULTIAGENT_AUTHORITY_CALLER_UID")
        .map_err(|_| "ops execute must be mediated by the authority supervisor")?
        .parse::<u32>()
        .map_err(|_| "authority caller UID is invalid")?;
    match execute_mode(caller_uid, options.contains_key("--reviewed-request"))? {
        ExecuteMode::ReviewerRead => execute_reviewer_read(args),
        ExecuteMode::ReviewedOperation => execute_reviewed_operation(args),
    }
}

#[derive(Debug, Eq, PartialEq)]
enum ExecuteMode {
    ReviewerRead,
    ReviewedOperation,
}

fn execute_mode(caller_uid: u32, has_reviewed_request: bool) -> Result<ExecuteMode, String> {
    if has_reviewed_request {
        if matches!(caller_uid, 0 | crate::config::REVIEWER_UID) {
            return Ok(ExecuteMode::ReviewerRead);
        }
        return Err("reviewer evidence reads require the reviewer identity".into());
    }
    if matches!(caller_uid, 0 | crate::config::OPS_UID) {
        return Ok(ExecuteMode::ReviewedOperation);
    }
    Err("operation execution requires the ops identity".into())
}

fn execute_reviewed_operation(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let reviewer = required(&options, "--reviewer")?;
    let request_file = PathBuf::from(required(&options, "--request-file")?);
    let (state, template, reviewer_approval) = load_reviewed_request(&request_file, reviewer)?;
    enforce_authority_scope(&template)?;
    let now = Utc::now();
    let caller_subject = env::var("MULTIAGENT_CALLER_SUBJECT")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "multiagent-control".into());
    validate_id("caller subject", &caller_subject)?;
    let caller_approved_at = env::var("MULTIAGENT_CALLER_APPROVED_AT")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| now.to_rfc3339_opts(SecondsFormat::Millis, true));
    let caller_approval = TrustedApproval {
        subject: caller_subject,
        role: "safety-reviewer",
        evidence_sha256: digest_json(
            template
                .get("goal")
                .ok_or("ops request template requires goal")?,
        )?,
        approved_at: caller_approved_at,
    };
    let execution_context = execution_context_from_environment()?;
    let request = build_request(
        &template,
        &[&caller_approval, &reviewer_approval],
        "runbook-operator",
        "multiagent-supervisor",
        &execution_context,
        now,
    )?;
    let action_id = request["actionId"]
        .as_str()
        .ok_or("generated operation has no action ID")?
        .to_string();
    let payload = canonical(&json!({
        "apiVersion":"prod.moveindustries.io/v1",
        "kind":"ActionPermit",
        "request": request
    }))?;
    let permit = sign_permit(&payload)?;
    let result = call_prod_mcp(&permit).unwrap_or_else(|message| {
        json!({
            "jsonrpc":"2.0",
            "id":2,
            "result":{
                "isError":true,
                "structuredContent":{
                    "code":"prod_mcp_transport_failure",
                    "message":message,
                    "outcome":{
                        "disposition":"failed",
                        "terminal":true,
                        "retryable":true,
                        "code":"prod_mcp_transport_failure",
                        "requiredActor":"service-operator"
                    }
                }
            }
        })
    });
    let operation_dir = state.join("operations").join(&action_id);
    fs::create_dir_all(&operation_dir)
        .map_err(|error| format!("create operation receipt directory: {error}"))?;
    fs::write(
        operation_dir.join("request.json"),
        serde_json::to_vec_pretty(&template).map_err(|error| error.to_string())?,
    )
    .map_err(|error| format!("persist operation request: {error}"))?;
    fs::write(
        operation_dir.join("receipt.json"),
        serde_json::to_vec_pretty(&result).map_err(|error| error.to_string())?,
    )
    .map_err(|error| format!("persist operation receipt: {error}"))?;
    let structured = result
        .pointer("/result/structuredContent")
        .cloned()
        .unwrap_or(Value::Null);
    let evidence = structured
        .get("summary")
        .and_then(Value::as_str)
        .map(|summary| {
            serde_json::from_str(summary).unwrap_or_else(|_| Value::String(summary.into()))
        })
        .unwrap_or(Value::Null);
    let compact = json!({
        "apiVersion": "multiagent.moveindustries.io/v1",
        "kind": "OperationExecutionResult",
        "actionId": action_id,
        "operationId": structured.get("operationId").cloned().unwrap_or(Value::Null),
        "requestedOperation": structured.get("requestedOperation").cloned().unwrap_or(Value::Null),
        "state": structured.get("state").cloned().unwrap_or(Value::Null),
        "outcome": structured.get("outcome").cloned().unwrap_or(Value::Null),
        "code": structured.get("code").cloned().unwrap_or(Value::Null),
        "message": structured.get("message").cloned().unwrap_or(Value::Null),
        "evidence": evidence,
        "receiptPath": operation_dir.join("receipt.json"),
    });
    println!(
        "{}",
        serde_json::to_string(&compact).map_err(|error| error.to_string())?
    );
    Ok(ExitCode::SUCCESS)
}

fn build_request(
    template: &Value,
    approvals: &[&TrustedApproval],
    delegated_role: &str,
    delegated_subject: &str,
    execution_context: &Value,
    now: chrono::DateTime<Utc>,
) -> Result<Value, String> {
    validate_request_template(template)?;
    validate_id("delegated subject", delegated_subject)?;
    let object = template
        .as_object()
        .ok_or("ops request template must be a JSON object")?;
    let task_id = object
        .get("taskId")
        .and_then(Value::as_str)
        .ok_or("ops request template requires taskId")?;
    validate_id("task ID", task_id)?;
    let operation = required_object(object, "operation")?;
    let operation_id = operation
        .get("id")
        .and_then(Value::as_str)
        .ok_or("ops request operation requires id")?;
    validate_id("operation ID", operation_id)?;
    if operation.get("version").and_then(Value::as_str).is_none() {
        return Err("ops request operation requires version".into());
    }
    let target = required_object(object, "target")?;
    let parameters = required_object(object, "parameters")?;
    let runbook = required_object(object, "runbook")?;
    let runbook_value = Value::Object(runbook.clone());
    let runbook_content_sha256 = object
        .get("runbookContentSha256")
        .and_then(Value::as_str)
        .ok_or("ops request template requires runbookContentSha256")?;
    let goal = object
        .get("goal")
        .ok_or("ops request template requires goal")?;
    if object.contains_key("approvals") {
        return Err("ops request approvals are derived by the supervisor and cannot be supplied by an agent".into());
    }
    let mut subjects = std::collections::BTreeSet::new();
    for approval in approvals {
        if !subjects.insert(approval.subject.as_str()) {
            return Err("trusted approval subjects must be distinct".into());
        }
        let approved_at = chrono::DateTime::parse_from_rfc3339(&approval.approved_at)
            .map_err(|_| "trusted approval has an invalid timestamp")?;
        if approved_at > now {
            return Err("trusted approval cannot postdate permit issuance".into());
        }
    }
    let history = object.get("history").unwrap_or(&Value::Null);
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    let approval_values = approvals
        .iter()
        .map(|approval| {
            json!({
                "reviewerSubject": approval.subject,
                "reviewerRole": approval.role,
                "decision": "approve",
                "evidenceSha256": approval.evidence_sha256,
                "approvedAt": approval.approved_at
            })
        })
        .collect::<Vec<_>>();
    let mut request = json!({
        "actionId": format!("ops-{unique}-{}", std::process::id()),
        "apiVersion": "prod.moveindustries.io/v1",
        "approvals": approval_values,
        "delegatedRole": delegated_role,
        "delegatedSubject": delegated_subject,
        "authorityProxy": {
            "subject": "multiagent-supervisor",
            "credentialSource": "deployment",
            "signingBackend": "aws-kms",
            "transportAuth": "service-token"
        },
        "expiresAt": (now + Duration::minutes(4)).to_rfc3339_opts(SecondsFormat::Millis, true),
        "executionContext": execution_context,
        "historySha256": digest_json(history)?,
        "intentSha256": digest_json(goal)?,
        "issuedAt": now.to_rfc3339_opts(SecondsFormat::Millis, true),
        "kind": "OperationRequest",
        "nonce": format!("nonce-{unique}-{}", std::process::id()),
        "operation": operation,
        "parameters": parameters,
        "runbook": runbook,
        "runbookContextSha256": digest_json(&runbook_value)?,
        "runbookContentSha256": runbook_content_sha256,
        "target": target,
        "taskId": task_id
    });
    if let Some(change_ticket) = object.get("changeTicket") {
        request
            .as_object_mut()
            .expect("operation request is an object")
            .insert("changeTicket".into(), change_ticket.clone());
    }
    Ok(request)
}

fn verify_reviewer(
    state: &Path,
    reviewer: &str,
    template: &Value,
    runbook_content_sha256: &str,
) -> Result<TrustedApproval, String> {
    let directory = state.join("reviewer-evidence").join(reviewer);
    let metadata = crate::state::read_env(&directory.join("evidence.env"))?;
    if metadata.get("role").map(String::as_str) != Some("reviewer")
        || metadata.get("access").map(String::as_str) != Some("read-only")
        || metadata.get("state").map(String::as_str) != Some("completed")
    {
        return Err("ops execution requires completed supervisor-sealed reviewer evidence".into());
    }
    let workflow = env::var("MULTIAGENT_WORKFLOW_ID").unwrap_or_default();
    if !workflow.is_empty() && metadata.get("workflow_id") != Some(&workflow) {
        return Err("ops reviewer evidence belongs to a different workflow".into());
    }
    let evidence_path = directory.join("last-message.txt");
    let evidence = fs::read_to_string(&evidence_path)
        .map_err(|error| format!("read ops reviewer evidence: {error}"))?;
    let expected_output = metadata
        .get("output_sha256")
        .ok_or("ops reviewer evidence has no supervisor seal")?;
    let actual_output = format!("{:x}", Sha256::digest(evidence.as_bytes()));
    if !actual_output.eq_ignore_ascii_case(expected_output) {
        return Err("ops reviewer evidence failed its supervisor seal".into());
    }
    let accepted = reviewer_accepted(&evidence);
    if !accepted {
        return Err("ops reviewer did not accept the operation".into());
    }
    let binding_path = directory.join("review-binding.json");
    let evidence_sha256 = if binding_path.is_file() {
        let binding_bytes = fs::read(&binding_path)
            .map_err(|error| format!("read sealed ops review binding: {error}"))?;
        let expected_binding = metadata
            .get("binding_sha256")
            .ok_or("ops review binding has no supervisor seal")?;
        let actual_binding = format!("{:x}", Sha256::digest(&binding_bytes));
        if !actual_binding.eq_ignore_ascii_case(expected_binding) {
            return Err("ops review binding failed its supervisor seal".into());
        }
        let binding: Value = serde_json::from_slice(&binding_bytes)
            .map_err(|error| format!("decode sealed ops review binding: {error}"))?;
        if !review_binding_matches(&binding, template, runbook_content_sha256)? {
            return Err("ops review binding does not match the request, goal, and runbook".into());
        }
        digest_json(&json!({
            "reviewerOutputSha256": format!("sha256:{actual_output}"),
            "reviewBindingSha256": format!("sha256:{actual_binding}"),
        }))?
    } else {
        // Compatibility for evidence sealed by an already-running older
        // session. New reviewers always use the supervisor-sealed artifact.
        if !review_evidence_is_bound(&evidence, template, runbook_content_sha256)? {
            return Err(
                "ops reviewer evidence is not bound to the request, goal, and runbook".into(),
            );
        }
        format!("sha256:{actual_output}")
    };
    let approved_at = metadata
        .get("completed_at")
        .cloned()
        .ok_or("ops reviewer evidence has no completion timestamp")?;
    Ok(TrustedApproval {
        subject: reviewer.into(),
        role: "operations-reviewer",
        evidence_sha256,
        approved_at,
    })
}

fn reviewer_accepted(evidence: &str) -> bool {
    let mut accepted = false;
    for line in evidence
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
    {
        let mut value = line;
        for wrapper in ["`", "**", "__"] {
            if value.starts_with(wrapper)
                && value.ends_with(wrapper)
                && value.len() >= wrapper.len() * 2
            {
                value = &value[wrapper.len()..value.len() - wrapper.len()];
            }
        }
        let Some((label, verdict)) = value.split_once(':') else {
            continue;
        };
        if !label.trim().eq_ignore_ascii_case("verdict") {
            continue;
        }
        if verdict.trim().eq_ignore_ascii_case("accepted") {
            accepted = true;
        } else {
            return false;
        }
    }
    accepted
}

fn review_binding_value(template: &Value, runbook_content_sha256: &str) -> Result<Value, String> {
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;
    Ok(json!({
        "apiVersion": "multiagent.moveindustries.io/v1",
        "kind": "OpsReviewBinding",
        "requestTemplateSha256": digest_json(template)?,
        "goalSha256": digest_json(
            object
                .get("goal")
                .ok_or("ops request template requires goal")?
        )?,
        "runbookSha256": digest_json(
            object
                .get("runbook")
                .ok_or("ops request template requires runbook")?
        )?,
        "runbookContentSha256": runbook_content_sha256,
    }))
}

fn review_binding_marker(template: &Value, runbook_content_sha256: &str) -> Result<String, String> {
    let binding = review_binding_value(template, runbook_content_sha256)?;
    Ok(format!("review-binding-sha256={}", digest_json(&binding)?))
}

fn review_binding_matches(
    binding: &Value,
    template: &Value,
    runbook_content_sha256: &str,
) -> Result<bool, String> {
    Ok(binding == &review_binding_value(template, runbook_content_sha256)?)
}

fn review_binding_artifact_path() -> Result<PathBuf, String> {
    let reviewer = required_env("MULTIAGENT_SUBAGENT_NAME")?;
    validate_id("reviewer name", &reviewer)?;
    let logs = fs::canonicalize(required_env("MULTIAGENT_LOG_DIR")?)
        .map_err(|error| format!("resolve multiagent log directory: {error}"))?;
    let trace_dir = fs::canonicalize(logs.join("agents").join(&reviewer))
        .map_err(|error| format!("resolve reviewer trace directory: {error}"))?;
    if !trace_dir.starts_with(&logs) {
        return Err("reviewer trace directory escaped MULTIAGENT_LOG_DIR".into());
    }
    Ok(trace_dir.join("review-binding.json"))
}

fn write_review_binding_artifact(path: &Path, contents: &[u8]) -> Result<(), String> {
    let mut options = OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(target_os = "linux")]
    options.mode(0o600).custom_flags(libc::O_NOFOLLOW);
    let mut file = options
        .open(path)
        .map_err(|error| format!("create ops review binding: {error}"))?;
    file.write_all(contents)
        .map_err(|error| format!("write ops review binding: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("sync ops review binding: {error}"))
}

fn review_evidence_is_bound(
    evidence: &str,
    template: &Value,
    runbook_content_sha256: &str,
) -> Result<bool, String> {
    let binding = review_binding_marker(template, runbook_content_sha256)?;
    if evidence.lines().map(str::trim).any(|line| line == binding) {
        return Ok(true);
    }

    // Keep accepting already-sealed evidence from reviewers launched by older
    // images while new reviewers use the single deterministic binding marker.
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;
    let legacy_markers = [
        format!("request-template-sha256={}", digest_json(template)?),
        format!(
            "goal-sha256={}",
            digest_json(
                object
                    .get("goal")
                    .ok_or("ops request template requires goal")?
            )?
        ),
        format!(
            "runbook-sha256={}",
            digest_json(
                object
                    .get("runbook")
                    .ok_or("ops request template requires runbook")?
            )?
        ),
        format!("runbook-content-sha256={runbook_content_sha256}"),
    ];
    Ok(legacy_markers
        .iter()
        .all(|marker| evidence.contains(marker)))
}

fn required_object<'a>(
    object: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a serde_json::Map<String, Value>, String> {
    object
        .get(key)
        .and_then(Value::as_object)
        .ok_or_else(|| format!("ops request template requires object field {key}"))
}

fn validate_request_template(template: &Value) -> Result<(), String> {
    validate_request_envelope(template)?;
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;

    let target = required_object(object, "target")?;
    if target.len() != 4 {
        return Err(
            "ops request target must contain environment, cluster, namespace, and service".into(),
        );
    }
    let environment = required_template_string(target, "environment")?;
    if !matches!(environment, "development" | "staging" | "production") {
        return Err("ops request target environment is invalid".into());
    }
    for key in ["cluster", "namespace", "service"] {
        validate_id(
            &format!("target {key}"),
            required_template_string(target, key)?,
        )?;
    }

    required_template_string(object, "runbookDocument")?;
    let digest = required_template_string(object, "runbookContentSha256")?;
    let hex = digest
        .strip_prefix("sha256:")
        .ok_or("ops request runbookContentSha256 is invalid")?;
    if hex.len() != 64
        || !hex
            .chars()
            .all(|ch| ch.is_ascii_hexdigit() && !ch.is_ascii_uppercase())
    {
        return Err("ops request runbookContentSha256 is invalid".into());
    }
    Ok(())
}

fn validate_request_envelope(template: &Value) -> Result<(), String> {
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;
    let task_id = required_template_string(object, "taskId")?;
    validate_id("task ID", task_id)?;
    if object.get("goal").is_none_or(Value::is_null) {
        return Err("ops request template requires goal".into());
    }
    if object.contains_key("approvals") {
        return Err("ops request approvals are derived by the supervisor and cannot be supplied by an agent".into());
    }

    let operation = required_object(object, "operation")?;
    if operation.len() != 2 {
        return Err("ops request operation must contain only id and version".into());
    }
    validate_id("operation ID", required_template_string(operation, "id")?)?;
    validate_semver(
        "operation version",
        required_template_string(operation, "version")?,
    )?;

    required_object(object, "parameters")?;
    let runbook = required_object(object, "runbook")?;
    if runbook.len() != 3 {
        return Err("ops request runbook must contain id, version, and phase".into());
    }
    validate_id("runbook ID", required_template_string(runbook, "id")?)?;
    validate_semver(
        "runbook version",
        required_template_string(runbook, "version")?,
    )?;
    validate_id("runbook phase", required_template_string(runbook, "phase")?)?;
    Ok(())
}

fn required_template_string<'a>(
    object: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("ops request template requires string field {key}"))
}

fn validate_semver(label: &str, value: &str) -> Result<(), String> {
    let parts = value.split('.').collect::<Vec<_>>();
    if parts.len() != 3
        || parts
            .iter()
            .any(|part| part.is_empty() || !part.chars().all(|ch| ch.is_ascii_digit()))
    {
        return Err(format!("{label} is invalid"));
    }
    Ok(())
}

fn verified_runbook_content(template: &Value) -> Result<String, String> {
    let object = template
        .as_object()
        .ok_or("ops request template must be an object")?;
    let relative = object
        .get("runbookDocument")
        .and_then(Value::as_str)
        .ok_or("ops request template requires runbookDocument")?;
    let bytes = exact_runbook_bytes(relative)?;
    let actual = runbook_content_digest(&bytes);
    let declared = object
        .get("runbookContentSha256")
        .and_then(Value::as_str)
        .ok_or("ops request template requires runbookContentSha256")?;
    if declared != actual {
        return Err("runbookContentSha256 does not match the exact Markdown runbook bytes".into());
    }
    if let Some(target) = canonical_runbook_target(&bytes)? {
        if object.get("target") != Some(&target) {
            return Err(
                "ops request target does not match the exact Markdown runbook bytes".into(),
            );
        }
    }
    Ok(actual)
}

fn exact_runbook_content_sha256(relative: &str) -> Result<String, String> {
    let bytes = exact_runbook_bytes(relative)?;
    Ok(runbook_content_digest(&bytes))
}

fn canonical_runbook_target(bytes: &[u8]) -> Result<Option<Value>, String> {
    const PREFIX: &str = "- Set `target` to `";
    const SUFFIX: &str = "`.";
    let markdown = std::str::from_utf8(bytes)
        .map_err(|error| format!("decode runbook document as UTF-8: {error}"))?;
    let declarations = markdown
        .lines()
        .filter_map(|line| line.strip_prefix(PREFIX))
        .collect::<Vec<_>>();
    if declarations.len() > 1 {
        return Err(
            "runbook document must contain at most one canonical target declaration".into(),
        );
    }
    let Some(declaration) = declarations.first() else {
        return Ok(None);
    };
    let encoded = declaration
        .strip_suffix(SUFFIX)
        .ok_or("canonical runbook target declaration is malformed")?;
    let target: Value = serde_json::from_str(encoded)
        .map_err(|error| format!("decode canonical runbook target: {error}"))?;
    if !target.is_object() {
        return Err("canonical runbook target must be a JSON object".into());
    }
    Ok(Some(target))
}

fn exact_runbook_bytes(relative: &str) -> Result<Vec<u8>, String> {
    let relative_path = Path::new(relative);
    if relative_path.is_absolute()
        || relative_path
            .components()
            .any(|component| !matches!(component, std::path::Component::Normal(_)))
    {
        return Err("runbookDocument must be a normalized path relative to MULTIAGENT_FRAMEWORK_ROOT, for example runbooks/name.md".into());
    }
    let framework_root = fs::canonicalize(required_env("MULTIAGENT_FRAMEWORK_ROOT")?)
        .map_err(|error| format!("resolve multiagent framework root: {error}"))?;
    let runbooks_root = fs::canonicalize(framework_root.join("runbooks"))
        .map_err(|error| format!("resolve runbooks directory: {error}"))?;
    let document = fs::canonicalize(framework_root.join(relative_path))
        .map_err(|error| format!("resolve runbook document: {error}"))?;
    if !document.starts_with(&runbooks_root) {
        return Err("runbookDocument must resolve inside the framework runbooks directory".into());
    }
    let (bytes, metadata) = read_bounded_file(&document, MAX_RUNBOOK_BYTES, true)?;
    if metadata.len() == 0 {
        return Err("runbook document must be a regular file between 1 byte and 1 MiB".into());
    }
    Ok(bytes)
}

fn runbook_content_digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn sign_permit(payload: &[u8]) -> Result<String, String> {
    let kid = required_env("MULTIAGENT_KMS_KEY_KID")?;
    let header = canonical(&json!({"alg":"ES256","kid":kid,"typ":"prod-mcp-action-permit+jws"}))?;
    let signing_input = format!(
        "{}.{}",
        base64url_encode(&header),
        base64url_encode(payload)
    );
    let state = PathBuf::from(required_env("MULTIAGENT_STATE_DIR")?);
    let temporary_dir = state.join("tmp");
    fs::create_dir_all(&temporary_dir)
        .map_err(|error| format!("create KMS temporary directory: {error}"))?;
    let temporary = private_temp_path(&temporary_dir, "kms-sign", "bin")?;
    write_private_file(&temporary, signing_input.as_bytes())
        .map_err(|error| format!("write KMS signing input: {error}"))?;
    let message = format!("fileb://{}", temporary.display());
    let output = Command::new("aws")
        .args([
            "kms",
            "sign",
            "--key-id",
            &required_env("MULTIAGENT_KMS_KEY_ID")?,
            "--message",
            &message,
            "--message-type",
            "RAW",
            "--signing-algorithm",
            "ECDSA_SHA_256",
            "--output",
            "json",
        ])
        .output()
        .map_err(|error| format!("execute aws kms sign: {error}"));
    let _ = fs::remove_file(&temporary);
    let output = output?;
    if !output.status.success() {
        return Err(format!(
            "aws kms sign failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let response: Value = serde_json::from_slice(&output.stdout)
        .map_err(|error| format!("decode aws kms sign response: {error}"))?;
    let der = base64_decode(
        response
            .get("Signature")
            .and_then(Value::as_str)
            .ok_or("aws kms sign returned no signature")?,
    )?;
    let raw = ecdsa_der_to_raw(&der)?;
    Ok(format!("{signing_input}.{}", base64url_encode(&raw)))
}

fn call_prod_mcp(permit: &str) -> Result<Value, String> {
    call_prod_mcp_tool("operations_execute", json!({"permit": permit}))
}

fn call_prod_mcp_tool(name: &str, arguments: Value) -> Result<Value, String> {
    let url = required_env("PROD_MCP_URL")?;
    if !(url.starts_with("http://") || url.starts_with("https://")) {
        return Err("PROD_MCP_URL must use HTTP or HTTPS".into());
    }
    let token = required_env("PROD_MCP_BEARER_TOKEN")?;
    let state = PathBuf::from(required_env("MULTIAGENT_STATE_DIR")?);
    let temporary_dir = state.join("tmp");
    fs::create_dir_all(&temporary_dir)
        .map_err(|error| format!("create prod-mcp temporary directory: {error}"))?;
    let request_headers = private_temp_path(&temporary_dir, "prod-mcp-request-headers", "txt")?;
    let response_headers = private_temp_path(&temporary_dir, "prod-mcp-response-headers", "txt")?;
    let call = json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"arguments":arguments,"name":name}});
    let result = (|| {
        write_mcp_headers(&request_headers, &token, None)?;
        write_private_file(&response_headers, b"")?;
        curl_mcp(&url, &call, &response_headers, &request_headers)
    })();
    let _ = fs::remove_file(request_headers);
    let _ = fs::remove_file(response_headers);
    let result = result?;
    if let Some(error) = result.get("error") {
        return Err(format!("prod-mcp tool {name} failed: {error}"));
    }
    Ok(result)
}

fn curl_mcp(
    url: &str,
    body: &Value,
    response_headers: &Path,
    request_headers: &Path,
) -> Result<Value, String> {
    let mut command = curl_command(url, response_headers, request_headers);
    let body = canonical_string(body)?;
    let mut child = command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("execute prod-mcp request: {error}"))?;
    let write_result = child
        .stdin
        .as_mut()
        .ok_or_else(|| "open prod-mcp request stdin".to_string())
        .and_then(|stdin| {
            stdin
                .write_all(body.as_bytes())
                .map_err(|error| format!("write prod-mcp request body: {error}"))
        });
    drop(child.stdin.take());
    if let Err(error) = write_result {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    let output = child
        .wait_with_output()
        .map_err(|error| format!("wait for prod-mcp request: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "prod-mcp HTTP request failed: {}{}",
            String::from_utf8_lossy(&output.stderr),
            String::from_utf8_lossy(&output.stdout)
        ));
    }
    parse_mcp_body(&String::from_utf8_lossy(&output.stdout))
}

fn curl_command(url: &str, response_headers: &Path, request_headers: &Path) -> Command {
    let mut command = Command::new("curl");
    command
        .args([
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            "40",
            "--dump-header",
        ])
        .arg(response_headers)
        .args(["--header"])
        .arg(format!("@{}", request_headers.display()))
        .args([
            "--header",
            "Content-Type: application/json",
            "--header",
            "Accept: application/json, text/event-stream",
            "--data-binary",
            "@-",
            url,
        ]);
    command
}

fn write_mcp_headers(path: &Path, token: &str, session: Option<&str>) -> Result<(), String> {
    let mut contents = format!("Authorization: Bearer {token}\n");
    if let Some(session) = session {
        contents.push_str(&format!("Mcp-Session-Id: {session}\n"));
    }
    write_private_file(path, contents.as_bytes())
}

fn private_temp_path(directory: &Path, prefix: &str, extension: &str) -> Result<PathBuf, String> {
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    Ok(directory.join(format!(
        "{prefix}-{}-{unique}.{extension}",
        std::process::id()
    )))
}

fn write_private_file(path: &Path, contents: &[u8]) -> Result<(), String> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(target_os = "linux")]
    options.mode(0o600);
    let mut file = options
        .open(path)
        .map_err(|error| format!("create private temporary file: {error}"))?;
    file.write_all(contents)
        .map_err(|error| format!("write private temporary file: {error}"))
}

fn parse_mcp_body(body: &str) -> Result<Value, String> {
    if let Ok(value) = serde_json::from_str(body) {
        return Ok(value);
    }
    for line in body.lines() {
        if let Some(data) = line.strip_prefix("data: ") {
            if let Ok(value) = serde_json::from_str(data) {
                return Ok(value);
            }
        }
    }
    Err(format!(
        "prod-mcp returned an invalid MCP response: {}",
        body.chars().take(512).collect::<String>()
    ))
}

fn session_id(path: &Path) -> Result<String, String> {
    let text =
        fs::read_to_string(path).map_err(|error| format!("read prod-mcp headers: {error}"))?;
    text.lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("mcp-session-id")
                .then(|| value.trim().to_string())
        })
        .filter(|value| !value.is_empty())
        .ok_or_else(|| "prod-mcp returned no MCP session ID".into())
}

fn ecdsa_der_to_raw(der: &[u8]) -> Result<Vec<u8>, String> {
    if der.len() < 8 || der[0] != 0x30 {
        return Err("KMS returned an invalid ECDSA DER signature".into());
    }
    let mut offset = 1;
    let sequence_len = der_len(der, &mut offset)?;
    if offset + sequence_len != der.len() {
        return Err("KMS ECDSA DER sequence length is invalid".into());
    }
    let r = der_integer(der, &mut offset)?;
    let s = der_integer(der, &mut offset)?;
    let mut raw = vec![0u8; 64];
    copy_integer(&r, &mut raw[..32])?;
    copy_integer(&s, &mut raw[32..])?;
    Ok(raw)
}

fn der_len(bytes: &[u8], offset: &mut usize) -> Result<usize, String> {
    let first = *bytes.get(*offset).ok_or("truncated DER length")?;
    *offset += 1;
    if first & 0x80 == 0 {
        return Ok(first as usize);
    }
    let count = (first & 0x7f) as usize;
    if count == 0 || count > 2 || *offset + count > bytes.len() {
        return Err("unsupported DER length".into());
    }
    let mut value = 0usize;
    for byte in &bytes[*offset..*offset + count] {
        value = (value << 8) | *byte as usize;
    }
    *offset += count;
    Ok(value)
}

fn der_integer(bytes: &[u8], offset: &mut usize) -> Result<Vec<u8>, String> {
    if bytes.get(*offset) != Some(&0x02) {
        return Err("invalid DER integer".into());
    }
    *offset += 1;
    let len = der_len(bytes, offset)?;
    let value = bytes
        .get(*offset..*offset + len)
        .ok_or("truncated DER integer")?
        .to_vec();
    *offset += len;
    Ok(value)
}

fn copy_integer(value: &[u8], target: &mut [u8]) -> Result<(), String> {
    let value = if value.first() == Some(&0) {
        &value[1..]
    } else {
        value
    };
    if value.len() > target.len() {
        return Err("ECDSA integer exceeds P-256 width".into());
    }
    let offset = target.len() - value.len();
    target[offset..].copy_from_slice(value);
    Ok(())
}

fn options(args: &[String]) -> Result<std::collections::BTreeMap<String, String>, String> {
    let mut result = std::collections::BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let key = args
            .get(index)
            .filter(|value| value.starts_with("--"))
            .ok_or("expected an option")?
            .clone();
        let value = args
            .get(index + 1)
            .ok_or_else(|| format!("{key} requires a value"))?
            .clone();
        if result.insert(key.clone(), value).is_some() {
            return Err(format!("duplicate option: {key}"));
        }
        index += 2;
    }
    Ok(result)
}

fn required<'a>(
    options: &'a std::collections::BTreeMap<String, String>,
    key: &str,
) -> Result<&'a str, String> {
    options
        .get(key)
        .map(String::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{key} is required"))
}

fn validate_id(label: &str, value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 128
        || !value.chars().enumerate().all(|(index, ch)| {
            ch.is_ascii_alphanumeric() || index > 0 && matches!(ch, '.' | '_' | ':' | '-')
        })
    {
        return Err(format!("{label} is invalid"));
    }
    Ok(())
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} is required"))
}

fn execution_context_from_environment() -> Result<Value, String> {
    const NAMES: [&str; 4] = [
        "MULTIAGENT_THREAD_ID",
        "MULTIAGENT_SESSION",
        "MULTIAGENT_LEASE_GENERATION",
        "MULTIAGENT_AUTHORIZING_EVENT_ID",
    ];
    let values = NAMES.map(|name| env::var(name).ok().filter(|value| !value.is_empty()));
    if values.iter().any(Option::is_none) {
        return Err("thread execution attribution requires thread, session, lease generation, and authorizing event together".into());
    }
    let [thread_id, session_id, lease_generation, authorizing_event_id] =
        values.map(Option::unwrap);
    validate_id("thread ID", &thread_id)?;
    validate_id("session ID", &session_id)?;
    validate_id("authorizing event ID", &authorizing_event_id)?;
    let lease_generation = lease_generation
        .parse::<u64>()
        .map_err(|_| "MULTIAGENT_LEASE_GENERATION must be a positive integer")?;
    if lease_generation == 0 {
        return Err("MULTIAGENT_LEASE_GENERATION must be a positive integer".into());
    }
    Ok(json!({
        "threadId": thread_id,
        "sessionId": session_id,
        "leaseGeneration": lease_generation,
        "authorizingEventId": authorizing_event_id,
    }))
}

fn canonical(value: &Value) -> Result<Vec<u8>, String> {
    serde_json::to_vec(value).map_err(|error| error.to_string())
}
fn canonical_string(value: &Value) -> Result<String, String> {
    serde_json::to_string(value).map_err(|error| error.to_string())
}
fn digest_json(value: &Value) -> Result<String, String> {
    Ok(digest(&canonical(value)?))
}
fn digest(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}

fn base64url_encode(bytes: &[u8]) -> String {
    base64_encode(bytes, true).trim_end_matches('=').to_string()
}
fn base64_encode(bytes: &[u8], url: bool) -> String {
    let alphabet = if url {
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    } else {
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    };
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let value = (chunk[0] as u32) << 16
            | (chunk.get(1).copied().unwrap_or(0) as u32) << 8
            | chunk.get(2).copied().unwrap_or(0) as u32;
        out.push(alphabet[((value >> 18) & 63) as usize] as char);
        out.push(alphabet[((value >> 12) & 63) as usize] as char);
        out.push(if chunk.len() > 1 {
            alphabet[((value >> 6) & 63) as usize] as char
        } else {
            '='
        });
        out.push(if chunk.len() > 2 {
            alphabet[(value & 63) as usize] as char
        } else {
            '='
        });
    }
    out
}

fn base64_decode(value: &str) -> Result<Vec<u8>, String> {
    let mut buffer = 0u32;
    let mut bits = 0u8;
    let mut output = Vec::new();
    for byte in value
        .bytes()
        .filter(|byte| !byte.is_ascii_whitespace() && *byte != b'=')
    {
        let digit = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return Err("invalid base64 from KMS".into()),
        } as u32;
        buffer = (buffer << 6) | digit;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            output.push((buffer >> bits) as u8);
            buffer &= (1u32 << bits).saturating_sub(1);
        }
    }
    Ok(output)
}

#[cfg(test)]
mod tests {
    use super::{
        base64_decode, base64url_encode, build_request, canonical, clone_summary, curl_command,
        direct_request_runbook, ecdsa_der_to_raw, execute_mode, git_auth_config, git_clone_command,
        materialization_usage, operation_capability, parse_mcp_body, persist_direct_receipt,
        private_temp_path, redacted_direct_receipt, reject_arbitrary_urls, review_binding_marker,
        review_binding_matches, review_binding_value, review_evidence_is_bound, reviewer_accepted,
        runbook_content_digest, validate_diagnosis_capability, validate_direct_capability,
        validate_evidence_scope, validate_read_capability, validate_request_template,
        write_mcp_headers, DirectAccess, ExecuteMode, TrustedApproval,
    };
    use chrono::{TimeZone, Utc};
    use serde_json::json;
    use std::fs;
    #[cfg(target_os = "linux")]
    use std::os::unix::fs::PermissionsExt;
    #[test]
    fn converts_p256_der_to_jose_signature() {
        let mut der = vec![0x30, 0x44, 0x02, 0x20];
        der.extend([1u8; 32]);
        der.extend([0x02, 0x20]);
        der.extend([2u8; 32]);
        let raw = ecdsa_der_to_raw(&der).unwrap();
        assert_eq!(&raw[..32], &[1u8; 32]);
        assert_eq!(&raw[32..], &[2u8; 32]);
    }
    #[test]
    fn parses_sse_mcp_response() {
        assert_eq!(
            parse_mcp_body("event: message\ndata: {\"id\":1}\n\n").unwrap()["id"],
            1
        );
    }
    #[test]
    fn base64_round_trip_fixture() {
        assert_eq!(base64_decode("AQIDBA==").unwrap(), [1, 2, 3, 4]);
        assert_eq!(base64url_encode(&[251, 255]), "-_8");
    }

    #[test]
    fn certified_runbook_digest_uses_prefixed_exact_bytes() {
        assert_eq!(
            runbook_content_digest(b"# Runbook\n"),
            "sha256:a0bd8567ec5da5c4c78ef8370994af0b34e5c83c1ebdd28359d297096f8efa75"
        );
    }

    #[test]
    fn reviewer_acceptance_allows_only_cosmetic_markdown_wrapping() {
        assert!(reviewer_accepted("Verdict: ACCEPTED\n"));
        assert!(reviewer_accepted(
            "**Verdict: ACCEPTED**\n\nReview analysis"
        ));
        assert!(reviewer_accepted(
            "# Operations review\n\n**Verdict: ACCEPTED**\n"
        ));
        assert!(reviewer_accepted("`verdict: accepted`"));
        assert!(!reviewer_accepted("Review result: verdict: accepted"));
        assert!(!reviewer_accepted("**Verdict: REJECTED**"));
        assert!(!reviewer_accepted(
            "Verdict: ACCEPTED\n\nVerdict: REJECTED\n"
        ));
    }

    #[test]
    fn reviewer_binding_uses_one_exact_deterministic_marker() {
        let template = json!({
            "goal": "read Slack",
            "runbook": {"id":"slack.workspace-access","version":"1.0.0","phase":"read"}
        });
        let runbook_digest = format!("sha256:{}", "4".repeat(64));
        let marker = review_binding_marker(&template, &runbook_digest).unwrap();
        assert!(review_evidence_is_bound(
            &format!("Verdict: ACCEPTED\n{marker}\n"),
            &template,
            &runbook_digest
        )
        .unwrap());
        assert!(!review_evidence_is_bound(
            &format!("Verdict: ACCEPTED\n{}0\n", &marker[..marker.len() - 1]),
            &template,
            &runbook_digest
        )
        .unwrap());
    }

    #[test]
    fn reviewer_binding_artifact_is_machine_verified_without_model_hash_text() {
        let template = json!({
            "goal": "read Slack",
            "runbook": {"id":"slack.workspace-access","version":"1.0.0","phase":"read"}
        });
        let runbook_digest = format!("sha256:{}", "4".repeat(64));
        let binding = review_binding_value(&template, &runbook_digest).unwrap();
        assert!(review_binding_matches(&binding, &template, &runbook_digest).unwrap());

        let mut mistyped = binding;
        mistyped["runbookContentSha256"] = format!("sha256:{}", "5".repeat(64)).into();
        assert!(!review_binding_matches(&mistyped, &template, &runbook_digest).unwrap());
    }
    #[test]
    fn operation_and_target_come_from_runbook_request_data() {
        let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
        let caller = TrustedApproval {
            subject: "caller-1".into(),
            role: "safety-reviewer",
            evidence_sha256: format!("sha256:{}", "1".repeat(64)),
            approved_at: now.to_rfc3339(),
        };
        let reviewer = TrustedApproval {
            subject: "reviewer-1".into(),
            role: "operations-reviewer",
            evidence_sha256: format!("sha256:{}", "2".repeat(64)),
            approved_at: now.to_rfc3339(),
        };
        let request = build_request(&json!({
            "taskId":"task-1",
            "goal":{"summary":"follow the supplied runbook"},
            "operation":{"id":"service.custom-operation","version":"7.0.0"},
            "target":{"environment":"production","cluster":"cluster-a","namespace":"service-a","service":"api"},
            "parameters":{"custom":true},
            "runbook":{"id":"custom.runbook","version":"3.0.0","phase":"execute"},
            "runbookDocument":"runbooks/custom-runbook.md",
            "runbookContentSha256":format!("sha256:{}", "4".repeat(64)),
            "changeTicket":"OPS-123"
        }), &[&caller, &reviewer], "runbook-operator", "multiagent-supervisor", &json!({
            "threadId": "thread-1",
            "sessionId": "session-1",
            "leaseGeneration": 1,
            "authorizingEventId": "message-1"
        }), now).unwrap();
        assert_eq!(request["operation"]["id"], "service.custom-operation");
        assert_eq!(request["target"]["service"], "api");
        assert_eq!(request["parameters"]["custom"], true);
        assert_eq!(request["changeTicket"], "OPS-123");
        assert_eq!(request["approvals"][0]["reviewerSubject"], "caller-1");
        assert_eq!(request["approvals"][1]["reviewerSubject"], "reviewer-1");
    }

    #[test]
    fn reviewer_evidence_scope_and_live_capability_are_fail_closed() {
        let reviewed = json!({
            "taskId":"task-1",
            "goal":"investigate service",
            "target":{"environment":"production","cluster":"cluster-a","namespace":"service-a","service":"api"},
            "runbook":{"id":"observability.investigation","version":"1.1.0","phase":"observe"},
            "runbookDocument":"runbooks/observability.md",
            "runbookContentSha256":format!("sha256:{}", "4".repeat(64)),
            "operation":{"id":"observability.query","version":"1.0.0"},
            "parameters":{}
        });
        let evidence = reviewed.clone();
        assert!(validate_evidence_scope(&reviewed, &evidence).is_ok());
        let mut widened = evidence.clone();
        widened["target"]["service"] = "other".into();
        assert!(validate_evidence_scope(&reviewed, &widened)
            .unwrap_err()
            .contains("/target"));

        let capability = json!({
            "id":"observability.query",
            "version":"1.0.0",
            "access":"read",
            "mutation":false,
            "allowedRunbooks":["observability.investigation@1.1.0"],
            "requiredApprovalRoles":[]
        });
        assert!(validate_diagnosis_capability(&capability).is_ok());
        let mut materialize = capability.clone();
        materialize["access"] = "materialize".into();
        assert!(validate_diagnosis_capability(&materialize).is_ok());
        let mut requires_approval = capability.clone();
        requires_approval["requiredApprovalRoles"] = json!(["human"]);
        assert!(validate_diagnosis_capability(&requires_approval).is_err());
        let mut diagnosis_mutation = capability.clone();
        diagnosis_mutation["mutation"] = true.into();
        assert!(validate_diagnosis_capability(&diagnosis_mutation).is_err());
        assert!(validate_read_capability(&capability, &evidence).is_ok());
        let mut mutating = capability.clone();
        mutating["mutation"] = true.into();
        assert!(validate_read_capability(&mutating, &evidence).is_err());
        let mut wrong_runbook = capability;
        wrong_runbook["allowedRunbooks"] = json!(["other@1.0.0"]);
        assert!(validate_read_capability(&wrong_runbook, &evidence).is_err());
    }

    #[test]
    fn reviewer_observation_permit_uses_observer_role_and_no_model_approval() {
        let now = Utc.with_ymd_and_hms(2026, 8, 22, 12, 0, 0).unwrap();
        let caller = TrustedApproval {
            subject: "caller-1".into(),
            role: "safety-reviewer",
            evidence_sha256: format!("sha256:{}", "1".repeat(64)),
            approved_at: now.to_rfc3339(),
        };
        let request = build_request(&json!({
            "taskId":"task-1",
            "goal":"observe",
            "operation":{"id":"observability.query","version":"1.0.0"},
            "target":{"environment":"production","cluster":"cluster-a","namespace":"service-a","service":"api"},
            "parameters":{},
            "runbook":{"id":"observability.investigation","version":"1.1.0","phase":"observe"},
            "runbookDocument":"runbooks/observability.md",
            "runbookContentSha256":format!("sha256:{}", "4".repeat(64))
        }), &[&caller], "runbook-observer", "reviewer-1", &json!({
            "threadId":"thread-1","sessionId":"session-1","leaseGeneration":1,"authorizingEventId":"message-1"
        }), now).unwrap();
        assert_eq!(request["delegatedRole"], "runbook-observer");
        assert_eq!(request["delegatedSubject"], "reviewer-1");
        assert_eq!(request["approvals"].as_array().unwrap().len(), 1);
        assert_eq!(request["approvals"][0]["reviewerSubject"], "caller-1");
    }

    #[test]
    fn shared_execute_path_keeps_reviewer_and_operator_authority_disjoint() {
        assert_eq!(
            execute_mode(crate::config::REVIEWER_UID, true).unwrap(),
            ExecuteMode::ReviewerRead
        );
        assert!(execute_mode(crate::config::REVIEWER_UID, false).is_err());
        assert_eq!(
            execute_mode(crate::config::OPS_UID, false).unwrap(),
            ExecuteMode::ReviewedOperation
        );
        assert!(execute_mode(crate::config::OPS_UID, true).is_err());
    }

    #[test]
    fn request_template_validation_rejects_non_executable_envelopes() {
        let digest = format!("sha256:{}", "4".repeat(64));
        let valid = json!({
            "taskId":"task-1",
            "goal":"follow the supplied runbook",
            "operation":{"id":"provider.read","version":"1.0.0"},
            "target":{"environment":"production","cluster":"external-services","namespace":"provider","service":"configured-service"},
            "parameters":{"action":"list"},
            "runbook":{"id":"provider.access","version":"1.0.0","phase":"read"},
            "runbookDocument":"runbooks/provider-access.md",
            "runbookContentSha256":digest
        });
        validate_request_template(&valid).unwrap();

        let mut invalid_operation = valid.clone();
        invalid_operation["operation"] = json!("provider.read");
        assert_eq!(
            validate_request_template(&invalid_operation).unwrap_err(),
            "ops request template requires object field operation"
        );

        let mut invalid_target = valid;
        invalid_target["target"] = json!("list");
        assert_eq!(
            validate_request_template(&invalid_target).unwrap_err(),
            "ops request template requires object field target"
        );
    }

    #[test]
    fn operation_capability_selects_the_exact_live_contract() {
        let response = json!({
            "result": {
                "structuredContent": {
                    "operations": [
                        {"id":"github.read","parameterSchema":{"type":"object"}},
                        {"id":"slack.read","parameterSchema":{"type":"object"}}
                    ]
                }
            }
        });
        let operation = operation_capability(&response, "github.read").unwrap();
        assert_eq!(operation["id"], "github.read");
        assert_eq!(operation["parameterSchema"]["type"], "object");
        assert_eq!(
            operation_capability(&response, "github.write").unwrap_err(),
            "prod-mcp does not advertise operation github.write"
        );
    }

    #[test]
    fn direct_request_shape_excludes_caller_supplied_authority_fields() {
        let request = json!({
            "operation": {"id":"github.clone","version":"1.0.0"},
            "parameters": {"repository":"MoveIndustries/InternalServices"},
            "runbook": {"id":"github.repository-work","version":"1.1.0","phase":"materialize"},
            "runbookDocument": "runbooks/github-repository-work.md"
        });
        assert_eq!(
            direct_request_runbook(&request).unwrap(),
            "runbooks/github-repository-work.md"
        );
        let mut injected = request;
        injected["goal"] = "caller-selected goal".into();
        assert!(direct_request_runbook(&injected).is_err());
    }

    #[test]
    fn direct_capability_gate_accepts_only_non_mutating_reads_and_materialization() {
        let template = json!({
            "operation":{"id":"github.clone","version":"1.0.0"},
            "runbook":{"id":"github.repository-work","version":"1.1.0","phase":"materialize"}
        });
        let materialize = json!({
            "id":"github.clone",
            "version":"1.0.0",
            "access":"materialize",
            "mutation":false,
            "allowedRunbooks":["github.repository-work@1.1.0"],
            "requiredApprovalRoles":[]
        });
        assert_eq!(
            validate_direct_capability(&materialize, &template).unwrap(),
            DirectAccess::Materialize
        );
        let mut read = materialize.clone();
        read["access"] = "read".into();
        assert_eq!(
            validate_direct_capability(&read, &template).unwrap(),
            DirectAccess::Read
        );
        let mut write = materialize.clone();
        write["access"] = "write".into();
        assert!(validate_direct_capability(&write, &template).is_err());
        let mut mutating = materialize.clone();
        mutating["mutation"] = true.into();
        assert!(validate_direct_capability(&mutating, &template).is_err());
        let mut approval_bearing = materialize.clone();
        approval_bearing["requiredApprovalRoles"] = json!(["operations-reviewer"]);
        assert!(validate_direct_capability(&approval_bearing, &template).is_err());
        let mut wrong_version = materialize.clone();
        wrong_version["version"] = "2.0.0".into();
        assert!(validate_direct_capability(&wrong_version, &template).is_err());
    }

    #[test]
    fn direct_parameters_reject_caller_selected_network_destinations() {
        assert!(reject_arbitrary_urls(&json!({"repository":"MoveIndustries/prod-mcp"})).is_ok());
        assert!(reject_arbitrary_urls(&json!({"url":"prod-mcp.internal"})).is_err());
        assert!(reject_arbitrary_urls(&json!({"query":"https://attacker.invalid"})).is_err());
        assert!(reject_arbitrary_urls(&json!({"query":"HTTPS://attacker.invalid"})).is_err());
        assert!(reject_arbitrary_urls(&json!({"source":"GiT@attacker.invalid:repo"})).is_err());
        assert!(reject_arbitrary_urls(&json!({"source":"FiLe:/tmp/repo"})).is_err());
    }

    #[test]
    fn clone_summary_requires_the_exact_repository_origin_path_and_headers() {
        let receipt = json!({
            "summary": serde_json::to_string(&json!({
                "operation":"github.clone",
                "repository":"MoveIndustries/prod-mcp",
                "cloneUrl":"http://prod-mcp.test:3000/git/MoveIndustries/prod-mcp.git",
                "authentication":{
                    "bearerHeader":"Authorization",
                    "permitHeader":"X-Prod-MCP-Permit"
                }
            })).unwrap()
        });
        assert_eq!(
            clone_summary(
                receipt.as_object().unwrap(),
                "MoveIndustries/prod-mcp",
                "http://prod-mcp.test:3000/mcp"
            )
            .unwrap()
            .clone_url,
            "http://prod-mcp.test:3000/git/MoveIndustries/prod-mcp.git"
        );
        assert!(clone_summary(
            receipt.as_object().unwrap(),
            "MoveIndustries/other",
            "http://prod-mcp.test:3000/mcp"
        )
        .is_err());
        assert!(clone_summary(
            receipt.as_object().unwrap(),
            "MoveIndustries/prod-mcp",
            "https://prod-mcp.test:3000/mcp"
        )
        .is_err());
        let mut wrong_header = receipt.clone();
        let mut summary: serde_json::Value =
            serde_json::from_str(wrong_header["summary"].as_str().unwrap()).unwrap();
        summary["authentication"]["permitHeader"] = "Authorization".into();
        wrong_header["summary"] = serde_json::to_string(&summary).unwrap().into();
        assert!(clone_summary(
            wrong_header.as_object().unwrap(),
            "MoveIndustries/prod-mcp",
            "http://prod-mcp.test:3000/mcp"
        )
        .is_err());
    }

    #[test]
    fn persisted_clone_receipt_redacts_the_protected_transport_contract() {
        let template = json!({
            "operation":{"id":"github.clone"},
            "parameters":{"repository":"MoveIndustries/prod-mcp"}
        });
        let protected = serde_json::to_string(&json!({
            "operation":"github.clone",
            "repository":"MoveIndustries/prod-mcp",
            "cloneUrl":"http://prod-mcp.test/git/MoveIndustries/prod-mcp.git",
            "authentication":{"bearerHeader":"Authorization","permitHeader":"X-Prod-MCP-Permit"}
        }))
        .unwrap();
        let result = json!({
            "jsonrpc":"2.0",
            "id":2,
            "result":{
                "isError":false,
                "content":[{"type":"text","text":protected}],
                "structuredContent":{
                    "operationId":"github.clone",
                    "requestedOperation":"github.clone",
                    "state":"succeeded",
                    "summary":protected
                }
            }
        });
        let redacted = redacted_direct_receipt(&template, &result).unwrap();
        let serialized = serde_json::to_string(&redacted).unwrap();
        assert!(!serialized.contains("http://prod-mcp.test/git"));
        assert!(!serialized.contains("X-Prod-MCP-Permit"));
        assert!(serialized.contains("[REDACTED]"));
    }

    #[test]
    fn rejected_clone_receipt_is_allowlisted_and_persistence_is_owner_gated() {
        let state = private_temp_path(
            &std::env::temp_dir(),
            "multiagent-rejected-clone-receipt",
            "dir",
        )
        .unwrap();
        fs::create_dir_all(state.join("operations")).unwrap();
        let template = json!({
            "operation":{"id":"github.clone"},
            "parameters":{"repository":"MoveIndustries/private"}
        });
        let result = json!({
            "jsonrpc":"2.0",
            "id":2,
            "result":{
                "isError":true,
                "content":[{"type":"text","text":"failed http://prod-mcp.test/git/private X-Prod-MCP-Permit"}],
                "structuredContent":{"state":"failed","message":"protected failure detail"}
            }
        });
        let persistence = persist_direct_receipt(
            &state,
            "action-rejected",
            10005,
            "reader",
            &template,
            &result,
        );
        #[cfg(target_os = "linux")]
        if unsafe { libc::geteuid() } != 0
            && unsafe { libc::geteuid() } != crate::config::SUPERVISOR_UID
        {
            assert_eq!(
                persistence.unwrap_err(),
                "operation request store must be supervisor-owned"
            );
            let redacted = redacted_direct_receipt(&template, &result).unwrap();
            let receipt = serde_json::to_string(&redacted).unwrap();
            assert!(!receipt.contains("http://prod-mcp.test/git"));
            assert!(!receipt.contains("X-Prod-MCP-Permit"));
            assert!(!receipt.contains("protected failure detail"));
            assert!(receipt.contains("github.clone failed"));
            assert!(!receipt.contains("github.clone succeeded"));
            fs::remove_dir_all(state).unwrap();
            return;
        }
        persistence.unwrap();
        let receipt =
            fs::read_to_string(state.join("operations/action-rejected/receipt.redacted.json"))
                .unwrap();
        assert!(!receipt.contains("http://prod-mcp.test/git"));
        assert!(!receipt.contains("X-Prod-MCP-Permit"));
        assert!(!receipt.contains("protected failure detail"));
        assert!(receipt.contains("github.clone failed"));
        assert!(!receipt.contains("github.clone succeeded"));
        fs::remove_dir_all(state).unwrap();
    }

    #[test]
    fn git_clone_process_does_not_expose_authentication_or_protected_url() {
        let protected_url = "http://prod-mcp.test:3000/git/MoveIndustries/prod-mcp.git";
        let token = "bearer-secret-marker";
        let permit = "permit-secret-marker";
        let config = git_auth_config(protected_url, token, permit).unwrap();
        assert!(config.contains(protected_url));
        assert!(config.contains(token));
        assert!(config.contains(permit));

        let command = git_clone_command(
            std::path::Path::new("/session/materialized/repository"),
            std::path::Path::new("/session/private/git.config"),
        );
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join(" ");
        let environment = command
            .get_envs()
            .filter_map(|(key, value)| {
                value.map(|value| format!("{}={}", key.to_string_lossy(), value.to_string_lossy()))
            })
            .collect::<Vec<_>>()
            .join(" ");
        let process = format!("{arguments} {environment}");
        assert!(!process.contains(protected_url));
        assert!(!process.contains(token));
        assert!(!process.contains(permit));
        assert!(process.contains("prod-mcp-materialize:"));
    }

    #[test]
    fn materialization_quota_counts_the_whole_supervisor_owned_root() {
        let root = private_temp_path(
            &std::env::temp_dir(),
            "multiagent-materialization-quota",
            "dir",
        )
        .unwrap();
        fs::create_dir(&root).unwrap();
        fs::write(root.join("one"), b"1").unwrap();
        fs::write(root.join("two"), b"2").unwrap();
        assert!(materialization_usage(&root, 1, 1024).is_err());
        assert!(materialization_usage(&root, 2, 1).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn materialization_quota_tolerates_concurrent_git_style_renames() {
        let root = private_temp_path(
            &std::env::temp_dir(),
            "multiagent-materialization-churn",
            "dir",
        )
        .unwrap();
        fs::create_dir(&root).unwrap();
        let churn_root = root.clone();
        let churn = std::thread::spawn(move || {
            for index in 0..500 {
                let temporary = churn_root.join(format!("tmp-pack-{index}"));
                let published = churn_root.join(format!("pack-{index}"));
                let _ = fs::write(&temporary, b"pack");
                let _ = fs::rename(&temporary, &published);
                let _ = fs::remove_file(&published);
            }
        });
        for _ in 0..500 {
            materialization_usage(&root, 10_000, 1024 * 1024).unwrap();
        }
        churn.join().unwrap();
        fs::remove_dir_all(root).unwrap();
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn stalled_materialization_process_group_is_killed_at_deadline() {
        use super::run_bounded_materialization;
        use std::os::unix::process::CommandExt;
        use std::process::{Command, Stdio};
        use std::time::{Duration, Instant};

        let root = private_temp_path(
            &std::env::temp_dir(),
            "multiagent-materialization-timeout",
            "dir",
        )
        .unwrap();
        fs::create_dir(&root).unwrap();
        let mut command = Command::new("sh");
        command
            .args(["-c", "sleep 30"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        let started = Instant::now();
        let error =
            run_bounded_materialization(command, &root, Duration::from_millis(50), 100, 1024)
                .unwrap_err();
        assert!(error.contains("supervisor deadline"));
        assert!(started.elapsed() < Duration::from_secs(5));
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn shared_action_permit_fixture_matches_the_rust_contract() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../contracts/prod-mcp-action-permit-v1.json"
        ))
        .unwrap();
        let request = fixture.get("request").unwrap();

        assert_eq!(
            request["authorityProxy"]["subject"],
            "multiagent-supervisor"
        );
        assert_eq!(request["operation"]["version"], "1.1.0");
        assert_eq!(request["runbook"]["version"], "1.1.0");
        assert_eq!(request["executionContext"]["threadId"], "thread-contract-1");
        assert!(String::from_utf8(canonical(request).unwrap())
            .unwrap()
            .contains("\"authorityProxy\""));
    }

    #[test]
    fn prod_mcp_secrets_and_body_are_not_in_curl_arguments() {
        let directory = std::env::temp_dir();
        let request_headers =
            private_temp_path(&directory, "multiagent-test-request", "txt").unwrap();
        let response_headers =
            private_temp_path(&directory, "multiagent-test-response", "txt").unwrap();
        let token = "prod-mcp-secret-marker";
        write_mcp_headers(&request_headers, token, Some("session-secret-marker")).unwrap();
        fs::write(&response_headers, []).unwrap();
        let command = curl_command(
            "http://prod-mcp.test/mcp",
            &response_headers,
            &request_headers,
        );
        let arguments = command
            .get_args()
            .map(|value| value.to_string_lossy())
            .collect::<Vec<_>>()
            .join(" ");
        assert!(!arguments.contains(token));
        assert!(!arguments.contains("session-secret-marker"));
        assert_eq!(
            arguments.matches("@-").count(),
            1,
            "the request body must be provided through stdin"
        );
        #[cfg(target_os = "linux")]
        assert_eq!(
            fs::metadata(&request_headers).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let _ = fs::remove_file(request_headers);
        let _ = fs::remove_file(response_headers);
    }
}
