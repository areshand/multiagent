use chrono::{Duration, SecondsFormat, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(target_os = "linux")]
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

struct TrustedApproval {
    subject: String,
    role: &'static str,
    evidence_sha256: String,
    approved_at: String,
}

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    match args.first().map(String::as_str) {
        Some("bind-runbook") => bind_runbook(&args[1..]),
        Some("execute") => execute(&args[1..]),
        Some("review-bind") => review_bind(&args[1..]),
        _ => Err("usage: multiagent ops bind-runbook --request-file PATH --runbook-document PATH | multiagent ops review-bind --request-file PATH | multiagent ops execute --request-file PATH --reviewer NAME".into()),
    }
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
    let bytes = fs::read(&request_file).map_err(|error| format!("read ops request: {error}"))?;
    if bytes.is_empty() || bytes.len() > 65_536 {
        return Err("ops request must contain between 1 and 65536 bytes".into());
    }
    let mut template: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("decode ops request template: {error}"))?;
    let relative = required(&options, "--runbook-document")?;
    let digest = exact_runbook_content_sha256(relative)?;
    let canonical_target = exact_runbook_target(relative)?;
    let object = template
        .as_object_mut()
        .ok_or("ops request template must be an object")?;
    if let Some(target) = canonical_target {
        object.insert("target".into(), target);
    }
    object.insert("runbookDocument".into(), Value::String(relative.into()));
    object.insert("runbookContentSha256".into(), Value::String(digest.clone()));
    validate_request_template(&template)?;
    let encoded = serde_json::to_vec_pretty(&template)
        .map_err(|error| format!("encode bound ops request: {error}"))?;
    fs::write(&request_file, encoded)
        .map_err(|error| format!("write bound ops request: {error}"))?;
    println!("request-template-sha256={}", digest_json(&template)?);
    println!("runbook-content-sha256={digest}");
    Ok(ExitCode::SUCCESS)
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
    let request_file = fs::canonicalize(request_file)
        .map_err(|error| format!("resolve ops request file: {error}"))?;
    if !request_file.starts_with(&state) {
        return Err("ops request file must be inside MULTIAGENT_STATE_DIR".into());
    }
    let bytes = fs::read(&request_file).map_err(|error| format!("read ops request: {error}"))?;
    if bytes.is_empty() || bytes.len() > 65_536 {
        return Err("ops request must contain between 1 and 65536 bytes".into());
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        let metadata =
            fs::metadata(&request_file).map_err(|error| format!("inspect ops request: {error}"))?;
        if metadata.uid() != crate::config::OPS_UID || metadata.permissions().mode() & 0o022 != 0 {
            return Err("ops request must be owned by the ops UID and not group-writable".into());
        }
    }
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

fn execute(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let reviewer = required(&options, "--reviewer")?;
    let request_file = PathBuf::from(required(&options, "--request-file")?);
    let (state, template, reviewer_approval) = load_reviewed_request(&request_file, reviewer)?;
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
    let request = build_request(&template, &caller_approval, &reviewer_approval, now)?;
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
    let result = call_prod_mcp(&permit)?;
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
    println!(
        "{}",
        serde_json::to_string_pretty(&result).map_err(|error| error.to_string())?
    );
    Ok(ExitCode::SUCCESS)
}

fn build_request(
    template: &Value,
    caller: &TrustedApproval,
    reviewer: &TrustedApproval,
    now: chrono::DateTime<Utc>,
) -> Result<Value, String> {
    validate_request_template(template)?;
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
    if caller.subject == reviewer.subject {
        return Err("caller and operations reviewer must be distinct subjects".into());
    }
    for approval in [caller, reviewer] {
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
    let mut request = json!({
        "actionId": format!("ops-{unique}-{}", std::process::id()),
        "apiVersion": "prod.moveindustries.io/v1",
        "approvals": [
            {
                "reviewerSubject": caller.subject,
                "reviewerRole": caller.role,
                "decision": "approve",
                "evidenceSha256": caller.evidence_sha256,
                "approvedAt": caller.approved_at
            },
            {
                "reviewerSubject": reviewer.subject,
                "reviewerRole": reviewer.role,
                "decision": "approve",
                "evidenceSha256": reviewer.evidence_sha256,
                "approvedAt": reviewer.approved_at
            }
        ],
        "delegatedRole": "runbook-operator",
        "delegatedSubject": "multiagent-supervisor",
        "authorityProxy": {
            "subject": "multiagent-supervisor",
            "credentialSource": "deployment",
            "signingBackend": "aws-kms",
            "transportAuth": "service-token"
        },
        "expiresAt": (now + Duration::minutes(4)).to_rfc3339_opts(SecondsFormat::Millis, true),
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
    let actual = exact_runbook_content_sha256(relative)?;
    let declared = object
        .get("runbookContentSha256")
        .and_then(Value::as_str)
        .ok_or("ops request template requires runbookContentSha256")?;
    if declared != actual {
        return Err("runbookContentSha256 does not match the exact Markdown runbook bytes".into());
    }
    Ok(actual)
}

fn exact_runbook_content_sha256(relative: &str) -> Result<String, String> {
    let bytes = exact_runbook_bytes(relative)?;
    Ok(runbook_content_digest(&bytes))
}

fn exact_runbook_target(relative: &str) -> Result<Option<Value>, String> {
    canonical_runbook_target(&exact_runbook_bytes(relative)?)
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
        return Err("runbookDocument must be a normalized relative path".into());
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
    let metadata =
        fs::metadata(&document).map_err(|error| format!("inspect runbook document: {error}"))?;
    if !metadata.is_file() || metadata.len() == 0 || metadata.len() > 1_048_576 {
        return Err("runbook document must be a regular file between 1 byte and 1 MiB".into());
    }
    let bytes = fs::read(&document).map_err(|error| format!("read runbook document: {error}"))?;
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
    let call = json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"arguments":{"permit":permit},"name":"operations_execute"}});
    let result = (|| {
        write_mcp_headers(&request_headers, &token, None)?;
        write_private_file(&response_headers, b"")?;
        curl_mcp(&url, &call, &response_headers, &request_headers)
    })();
    let _ = fs::remove_file(request_headers);
    let _ = fs::remove_file(response_headers);
    let result = result?;
    if let Some(error) = result.get("error") {
        return Err(format!("prod-mcp execution failed: {error}"));
    }
    if result.pointer("/result/isError").and_then(Value::as_bool) == Some(true) {
        return Err(format!(
            "prod-mcp rejected the operation: {}",
            result["result"]["structuredContent"]
        ));
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
        base64_decode, base64url_encode, build_request, canonical, curl_command, ecdsa_der_to_raw,
        parse_mcp_body, private_temp_path, review_binding_marker, review_binding_matches,
        review_binding_value, review_evidence_is_bound, reviewer_accepted, runbook_content_digest,
        validate_request_template, write_mcp_headers, TrustedApproval,
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
        }), &caller, &reviewer, now).unwrap();
        assert_eq!(request["operation"]["id"], "service.custom-operation");
        assert_eq!(request["target"]["service"], "api");
        assert_eq!(request["parameters"]["custom"], true);
        assert_eq!(request["changeTicket"], "OPS-123");
        assert_eq!(request["approvals"][0]["reviewerSubject"], "caller-1");
        assert_eq!(request["approvals"][1]["reviewerSubject"], "reviewer-1");
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
    fn shared_action_permit_fixture_matches_the_rust_contract() {
        let fixture: serde_json::Value =
            serde_json::from_str(include_str!("../contracts/prod-mcp-action-permit-v1.json"))
                .unwrap();
        let request = fixture.get("request").unwrap();

        assert_eq!(
            request["authorityProxy"]["subject"],
            "multiagent-supervisor"
        );
        assert_eq!(request["operation"]["version"], "1.1.0");
        assert_eq!(request["runbook"]["version"], "1.1.0");
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
