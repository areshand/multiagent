use crate::{config, state::atomic_write};
use base64::{engine::general_purpose, Engine as _};
use chrono::{DateTime, Duration, Utc};
use p256::ecdsa::{signature::Signer as _, Signature, SigningKey};
use p256::pkcs8::DecodePrivateKey;
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

const API_VERSION: &str = "prod.moveindustries.io/v1";
const PERMIT_TYPE: &str = "prod-mcp-action-permit+jws";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
enum OperationsRole {
    RunbookObserver,
    RunbookOperator,
    ServiceDeployer,
}

impl OperationsRole {
    fn as_str(&self) -> &'static str {
        match self {
            Self::RunbookObserver => "runbook-observer",
            Self::RunbookOperator => "runbook-operator",
            Self::ServiceDeployer => "service-deployer",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct ApprovalV1 {
    reviewer_subject: String,
    reviewer_role: String,
    decision: String,
    evidence_sha256: String,
    approved_at: DateTime<Utc>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RunbookReference {
    id: String,
    version: String,
    phase: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OperationReference {
    id: String,
    version: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct TargetReference {
    environment: String,
    cluster: String,
    namespace: String,
    service: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct OperationRequestV1 {
    api_version: String,
    kind: String,
    action_id: String,
    task_id: String,
    delegated_subject: String,
    delegated_role: OperationsRole,
    intent_sha256: String,
    runbook: RunbookReference,
    runbook_context_sha256: String,
    history_sha256: String,
    operation: OperationReference,
    target: TargetReference,
    parameters: Value,
    approvals: Vec<ApprovalV1>,
    #[serde(skip_serializing_if = "Option::is_none")]
    change_ticket: Option<String>,
    issued_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
    nonce: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ActionPermitV1<'a> {
    api_version: &'static str,
    kind: &'static str,
    request: &'a OperationRequestV1,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct RoleAssignment {
    api_version: String,
    kind: String,
    delegated_subject: String,
    delegated_role: OperationsRole,
    task_id: String,
    assigned_at: DateTime<Utc>,
    expires_at: DateTime<Utc>,
}

trait SigningBackend {
    fn key_id(&self) -> &str;
    fn sign(&self, signing_input: &[u8]) -> Result<Vec<u8>, String>;
}

struct FileSigningBackend {
    key_id: String,
    key: SigningKey,
}

impl FileSigningBackend {
    fn load(key_id: String, path: &Path) -> Result<Self, String> {
        let pem = fs::read_to_string(path)
            .map_err(|error| format!("read local signing key {}: {error}", path.display()))?;
        let key = SigningKey::from_pkcs8_pem(&pem)
            .map_err(|error| format!("parse local P-256 PKCS#8 key: {error}"))?;
        Ok(Self { key_id, key })
    }
}

impl SigningBackend for FileSigningBackend {
    fn key_id(&self) -> &str {
        &self.key_id
    }

    fn sign(&self, signing_input: &[u8]) -> Result<Vec<u8>, String> {
        let signature: Signature = self.key.sign(signing_input);
        Ok(signature.to_bytes().to_vec())
    }
}

struct AwsKmsSigningBackend {
    key_id: String,
    aws_bin: String,
}

impl SigningBackend for AwsKmsSigningBackend {
    fn key_id(&self) -> &str {
        &self.key_id
    }

    fn sign(&self, signing_input: &[u8]) -> Result<Vec<u8>, String> {
        let output = Command::new(&self.aws_bin)
            .args([
                "kms",
                "sign",
                "--key-id",
                &self.key_id,
                "--message-type",
                "RAW",
                "--signing-algorithm",
                "ECDSA_SHA_256",
                "--message",
                &general_purpose::STANDARD.encode(signing_input),
                "--output",
                "json",
            ])
            .output()
            .map_err(|error| format!("invoke AWS KMS signer: {error}"))?;
        if !output.status.success() {
            return Err(format!(
                "AWS KMS signer failed: {}",
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        let response: Value = serde_json::from_slice(&output.stdout)
            .map_err(|error| format!("decode AWS KMS response: {error}"))?;
        let encoded = response
            .get("Signature")
            .and_then(Value::as_str)
            .ok_or_else(|| "AWS KMS response omitted Signature".to_string())?;
        let der = general_purpose::STANDARD
            .decode(encoded)
            .map_err(|error| format!("decode AWS KMS signature: {error}"))?;
        let signature = Signature::from_der(&der)
            .map_err(|error| format!("decode AWS KMS ECDSA signature: {error}"))?;
        Ok(signature.to_bytes().to_vec())
    }
}

struct VaultTransitSigningBackend {
    key_id: String,
    address: String,
    mount: String,
    key_name: String,
    token: String,
    client: Client,
}

impl SigningBackend for VaultTransitSigningBackend {
    fn key_id(&self) -> &str {
        &self.key_id
    }

    fn sign(&self, signing_input: &[u8]) -> Result<Vec<u8>, String> {
        let url = format!(
            "{}/v1/{}/sign/{}",
            self.address.trim_end_matches('/'),
            self.mount.trim_matches('/'),
            self.key_name
        );
        let response: Value = self
            .client
            .post(url)
            .header("X-Vault-Token", &self.token)
            .json(&json!({
                "input": general_purpose::STANDARD.encode(signing_input),
                "hash_algorithm": "sha2-256",
                "marshaling_algorithm": "jws"
            }))
            .send()
            .map_err(|error| format!("Vault Transit sign request: {error}"))?
            .error_for_status()
            .map_err(|error| format!("Vault Transit sign response: {error}"))?
            .json()
            .map_err(|error| format!("decode Vault Transit response: {error}"))?;
        let encoded = response
            .pointer("/data/signature")
            .and_then(Value::as_str)
            .and_then(|value| value.rsplit(':').next())
            .ok_or_else(|| "Vault Transit response omitted signature".to_string())?;
        let signature = general_purpose::STANDARD
            .decode(encoded)
            .or_else(|_| general_purpose::URL_SAFE_NO_PAD.decode(encoded))
            .map_err(|error| format!("decode Vault Transit JWS signature: {error}"))?;
        if signature.len() != 64 {
            return Err("Vault Transit must return a 64-byte JWS ECDSA signature".into());
        }
        Ok(signature)
    }
}

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    let command = args.first().map(String::as_str).unwrap_or("");
    match command {
        "validate" => validate_command(&args[1..]),
        "role-assign" => {
            require_supervisor()?;
            role_assign(&args[1..])
        }
        "role-revoke" => {
            require_supervisor()?;
            role_revoke(&args[1..])
        }
        "permit-issue" => {
            require_supervisor()?;
            permit_issue(&args[1..])
        }
        "submit" => {
            require_supervisor()?;
            submit(&args[1..])
        }
        _ => Err(
            "usage: multiagent prod-ops validate --request FILE | role-assign | role-revoke | permit-issue --request FILE --output FILE | submit ..."
                .into(),
        ),
    }
}

fn validate_command(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let path = required_option(&options, "--request")?;
    let request = read_request(Path::new(path))?;
    validate_request(&request)?;
    println!(
        "{}",
        canonical_json(&serde_json::to_value(request).map_err(json_error)?)?
    );
    Ok(ExitCode::SUCCESS)
}

fn role_assign(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let subject = required_option(&options, "--agent")?;
    validate_id("agent", subject)?;
    let role: OperationsRole =
        serde_json::from_value(Value::String(required_option(&options, "--role")?.into()))
            .map_err(|_| {
                "--role must be runbook-observer, runbook-operator, or service-deployer".to_string()
            })?;
    let task_id = required_option(&options, "--task-id")?;
    validate_id("task id", task_id)?;
    let expires_at = DateTime::parse_from_rfc3339(required_option(&options, "--expires-at")?)
        .map_err(|error| format!("parse --expires-at: {error}"))?
        .with_timezone(&Utc);
    if expires_at <= Utc::now() || expires_at > Utc::now() + Duration::hours(24) {
        return Err("role assignment expiry must be within the next 24 hours".into());
    }
    let assignment = RoleAssignment {
        api_version: API_VERSION.into(),
        kind: "RoleAssignment".into(),
        delegated_subject: subject.into(),
        delegated_role: role,
        task_id: task_id.into(),
        assigned_at: Utc::now(),
        expires_at,
    };
    let path = role_path(subject)?;
    if path.exists() {
        return Err("role assignment already exists; supervisor must revoke it before assigning a different role".into());
    }
    let parent = path
        .parent()
        .ok_or_else(|| "invalid role assignment path".to_string())?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("create prod-ops role directory: {error}"))?;
    atomic_write(
        &path,
        &format!(
            "{}\n",
            serde_json::to_string_pretty(&assignment).map_err(json_error)?
        ),
    )?;
    println!(
        "role assigned\t{subject}\t{}\t{task_id}",
        assignment.delegated_role.as_str()
    );
    Ok(ExitCode::SUCCESS)
}

fn permit_issue(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let request_path = Path::new(required_option(&options, "--request")?);
    let output_path = Path::new(required_option(&options, "--output")?);
    let request = read_request(request_path)?;
    validate_request(&request)?;
    validate_assignment(&request)?;
    validate_approval_evidence(&request)?;
    let signer = signer_from_env()?;
    let permit = ActionPermitV1 {
        api_version: API_VERSION,
        kind: "ActionPermit",
        request: &request,
    };
    let payload = canonical_json(&serde_json::to_value(permit).map_err(json_error)?)?;
    let compact = compact_jws(signer.as_ref(), payload.as_bytes())?;
    atomic_write(output_path, &format!("{compact}\n"))?;
    println!("permit issued\t{}\t{}", request.action_id, signer.key_id());
    Ok(ExitCode::SUCCESS)
}

fn role_revoke(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let subject = required_option(&options, "--agent")?;
    let path = role_path(subject)?;
    fs::remove_file(&path)
        .map_err(|error| format!("revoke role assignment {}: {error}", path.display()))?;
    println!("role revoked\t{subject}");
    Ok(ExitCode::SUCCESS)
}

fn submit(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let permit = fs::read_to_string(required_option(&options, "--permit")?)
        .map_err(|error| format!("read permit: {error}"))?;
    let token = fs::read_to_string(required_env("MULTIAGENT_PROD_MCP_TOKEN_FILE")?)
        .map_err(|error| format!("read MCP access token: {error}"))?;
    let client = ProdMcpClient::new(
        required_env("MULTIAGENT_PROD_MCP_URL")?,
        token.trim().into(),
    )?;
    let preview = client.call_tool("operations_preview", json!({ "permit": permit.trim() }))?;
    if preview.get("accepted").and_then(Value::as_bool) != Some(true) {
        return Err("prod-mcp preview did not accept the permit".into());
    }
    let receipt = client.call_tool("operations_execute", json!({ "permit": permit.trim() }))?;
    println!(
        "{}",
        serde_json::to_string_pretty(&receipt).map_err(json_error)?
    );
    Ok(ExitCode::SUCCESS)
}

struct ProdMcpClient {
    url: String,
    token: String,
    client: Client,
}

impl ProdMcpClient {
    fn new(url: String, token: String) -> Result<Self, String> {
        let client = Client::builder()
            .timeout(std::time::Duration::from_secs(60))
            .build()
            .map_err(|error| format!("build prod-mcp client: {error}"))?;
        Ok(Self { url, token, client })
    }

    fn call_tool(&self, name: &str, arguments: Value) -> Result<Value, String> {
        let response = self
            .client
            .post(&self.url)
            .bearer_auth(&self.token)
            .header("accept", "application/json, text/event-stream")
            .header("mcp-protocol-version", "2025-11-25")
            .json(&json!({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": { "name": name, "arguments": arguments }
            }))
            .send()
            .map_err(|error| format!("call prod-mcp {name}: {error}"))?
            .error_for_status()
            .map_err(|error| format!("prod-mcp {name} response: {error}"))?;
        let body = response
            .text()
            .map_err(|error| format!("read prod-mcp {name} response: {error}"))?;
        let json_body = body
            .lines()
            .filter_map(|line| line.strip_prefix("data: "))
            .next_back()
            .unwrap_or(body.trim());
        let response: Value = serde_json::from_str(json_body)
            .map_err(|error| format!("decode prod-mcp {name} response: {error}"))?;
        if let Some(error) = response.get("error") {
            return Err(format!("prod-mcp {name} error: {error}"));
        }
        let result = response
            .get("result")
            .ok_or_else(|| format!("prod-mcp {name} response omitted result"))?;
        if result.get("isError").and_then(Value::as_bool) == Some(true) {
            return Err(format!("prod-mcp {name} rejected request: {result}"));
        }
        result
            .get("structuredContent")
            .cloned()
            .ok_or_else(|| format!("prod-mcp {name} response omitted structuredContent"))
    }
}

fn signer_from_env() -> Result<Box<dyn SigningBackend>, String> {
    let backend = required_env("MULTIAGENT_PROD_OPS_SIGNER")?;
    let key_id = required_env("MULTIAGENT_PROD_OPS_KEY_ID")?;
    match backend.as_str() {
        "file" => {
            if !cfg!(feature = "insecure-dev-signer")
                || env::var("MULTIAGENT_PROD_OPS_DEVELOPMENT").as_deref() != Ok("1")
            {
                return Err("file signer requires the insecure-dev-signer build feature and MULTIAGENT_PROD_OPS_DEVELOPMENT=1".into());
            }
            Ok(Box::new(FileSigningBackend::load(
                key_id,
                Path::new(&required_env("MULTIAGENT_PROD_OPS_KEY_FILE")?),
            )?))
        }
        "aws-kms" => Ok(Box::new(AwsKmsSigningBackend {
            key_id,
            aws_bin: env::var("MULTIAGENT_PROD_OPS_AWS_BIN").unwrap_or_else(|_| "aws".into()),
        })),
        "vault-transit" => {
            let token_file = required_env("MULTIAGENT_PROD_OPS_VAULT_TOKEN_FILE")?;
            let token = fs::read_to_string(&token_file)
                .map_err(|error| format!("read Vault token file {token_file}: {error}"))?
                .trim()
                .to_string();
            Ok(Box::new(VaultTransitSigningBackend {
                key_id,
                address: required_env("MULTIAGENT_PROD_OPS_VAULT_ADDR")?,
                mount: env::var("MULTIAGENT_PROD_OPS_VAULT_MOUNT")
                    .unwrap_or_else(|_| "transit".into()),
                key_name: required_env("MULTIAGENT_PROD_OPS_VAULT_KEY")?,
                token,
                client: Client::new(),
            }))
        }
        _ => Err("MULTIAGENT_PROD_OPS_SIGNER must be file, aws-kms, or vault-transit".into()),
    }
}

fn compact_jws(signer: &dyn SigningBackend, payload: &[u8]) -> Result<String, String> {
    let header = json!({ "alg": "ES256", "kid": signer.key_id(), "typ": PERMIT_TYPE });
    let header = canonical_json(&header)?;
    let protected = general_purpose::URL_SAFE_NO_PAD.encode(header.as_bytes());
    let payload = general_purpose::URL_SAFE_NO_PAD.encode(payload);
    let signing_input = format!("{protected}.{payload}");
    let signature = signer.sign(signing_input.as_bytes())?;
    if signature.len() != 64 {
        return Err("ES256 signing backend returned a non-JWS signature".into());
    }
    Ok(format!(
        "{signing_input}.{}",
        general_purpose::URL_SAFE_NO_PAD.encode(signature)
    ))
}

fn validate_assignment(request: &OperationRequestV1) -> Result<(), String> {
    let path = role_path(&request.delegated_subject)?;
    let assignment: RoleAssignment =
        serde_json::from_str(&fs::read_to_string(&path).map_err(|error| {
            format!(
                "read supervisor role assignment {}: {error}",
                path.display()
            )
        })?)
        .map_err(|error| format!("decode supervisor role assignment: {error}"))?;
    if assignment.api_version != API_VERSION || assignment.kind != "RoleAssignment" {
        return Err("role assignment contract is not supported".into());
    }
    if assignment.delegated_subject != request.delegated_subject
        || assignment.delegated_role != request.delegated_role
        || assignment.task_id != request.task_id
    {
        return Err(
            "operation request subject, role, or task does not match the supervisor assignment"
                .into(),
        );
    }
    if assignment.expires_at <= Utc::now() || request.expires_at > assignment.expires_at {
        return Err("role assignment is expired or shorter than the requested permit".into());
    }
    Ok(())
}

fn validate_approval_evidence(request: &OperationRequestV1) -> Result<(), String> {
    let state = config::state_dir()?;
    validate_approval_evidence_at(request, &state)
}

fn validate_approval_evidence_at(request: &OperationRequestV1, state: &Path) -> Result<(), String> {
    for approval in &request.approvals {
        let directory = state
            .join("reviewer-evidence")
            .join(&approval.reviewer_subject);
        let metadata = read_key_values(&directory.join("evidence.env"))?;
        if metadata.get("role").map(String::as_str) != Some("reviewer")
            || metadata.get("access").map(String::as_str) != Some("read-only")
            || metadata.get("state").map(String::as_str) != Some("completed")
        {
            return Err(format!(
                "reviewer evidence must be sealed read-only output: {}",
                approval.reviewer_subject
            ));
        }
        let message_path = directory.join("last-message.txt");
        let message = fs::read_to_string(&message_path).map_err(|error| {
            format!(
                "read reviewer evidence {}: {error}",
                message_path.display()
            )
        })?;
        let actual = format!("sha256:{:x}", Sha256::digest(message.as_bytes()));
        let recorded = metadata
            .get("output_sha256")
            .map(|value| format!("sha256:{value}"))
            .unwrap_or_default();
        if actual != approval.evidence_sha256 || recorded != approval.evidence_sha256 {
            return Err(format!(
                "review evidence hash does not match sealed reviewer output: {}",
                approval.reviewer_subject
            ));
        }
        let marker = prod_ops_review_marker(request, approval)?;
        if !message.lines().any(|line| line.trim() == marker) {
            return Err(format!(
                "reviewer {} evidence is missing marker: {marker}",
                approval.reviewer_subject
            ));
        }
    }
    Ok(())
}

fn prod_ops_review_marker(
    request: &OperationRequestV1,
    approval: &ApprovalV1,
) -> Result<String, String> {
    let parameters = canonical_json(&request.parameters)?;
    let parameters_sha256 = format!("sha256:{:x}", Sha256::digest(parameters.as_bytes()));
    Ok(format!(
        "prod-ops-review: reviewer-role={} decision=approve action-id={} task-id={} delegated-subject={} delegated-role={} intent-sha256={} runbook={}@{} phase={} operation={}@{} target={}/{}/{}/{} parameters-sha256={} change-ticket={} runbook-context-sha256={} history-sha256={}",
        approval.reviewer_role,
        request.action_id,
        request.task_id,
        request.delegated_subject,
        request.delegated_role.as_str(),
        request.intent_sha256,
        request.runbook.id,
        request.runbook.version,
        request.runbook.phase,
        request.operation.id,
        request.operation.version,
        request.target.environment,
        request.target.cluster,
        request.target.namespace,
        request.target.service,
        parameters_sha256,
        request.change_ticket.as_deref().unwrap_or("-"),
        request.runbook_context_sha256,
        request.history_sha256
    ))
}

fn validate_request(request: &OperationRequestV1) -> Result<(), String> {
    if request.api_version != API_VERSION || request.kind != "OperationRequest" {
        return Err("unsupported OperationRequest contract".into());
    }
    for (label, value) in [
        ("actionId", request.action_id.as_str()),
        ("taskId", request.task_id.as_str()),
        ("delegatedSubject", request.delegated_subject.as_str()),
        ("nonce", request.nonce.as_str()),
    ] {
        validate_id(label, value)?;
    }
    validate_digest("intentSha256", &request.intent_sha256)?;
    validate_digest("runbookContextSha256", &request.runbook_context_sha256)?;
    validate_digest("historySha256", &request.history_sha256)?;
    validate_id("runbook id", &request.runbook.id)?;
    validate_id("runbook phase", &request.runbook.phase)?;
    validate_id("operation id", &request.operation.id)?;
    if !matches!(
        request.target.environment.as_str(),
        "development" | "staging" | "production"
    ) {
        return Err("target environment must be development, staging, or production".into());
    }
    for (label, value) in [
        ("cluster", request.target.cluster.as_str()),
        ("namespace", request.target.namespace.as_str()),
        ("service", request.target.service.as_str()),
    ] {
        validate_id(label, value)?;
    }
    if request.issued_at > Utc::now() + Duration::seconds(30) {
        return Err("operation request issuedAt is in the future".into());
    }
    if request.expires_at <= Utc::now()
        || request.expires_at <= request.issued_at
        || request.expires_at - request.issued_at > Duration::minutes(5)
    {
        return Err(
            "operation request lifetime must be positive, unexpired, and at most five minutes"
                .into(),
        );
    }
    let (roles, required_parameters, required_reviews, ticket) =
        operation_contract(&request.runbook, &request.operation)?;
    if !roles.contains(&request.delegated_role.as_str()) {
        return Err("supervisor-assigned role cannot execute this operation".into());
    }
    validate_parameters(
        &request.operation.id,
        &request.parameters,
        required_parameters,
    )?;
    if ticket
        && match request.change_ticket.as_deref() {
            Some(value) => !valid_ticket(value),
            None => true,
        }
    {
        return Err("mutating operation requires a valid changeTicket".into());
    }
    let mut subjects = BTreeSet::new();
    if request.approvals.len() > 4 {
        return Err("operation request may contain at most four approvals".into());
    }
    for approval in &request.approvals {
        if !matches!(
            approval.reviewer_role.as_str(),
            "safety-reviewer" | "operations-reviewer"
        ) || approval.decision != "approve"
        {
            return Err("approval role or decision is not supported".into());
        }
        validate_id("reviewer subject", &approval.reviewer_subject)?;
        validate_digest("review evidence", &approval.evidence_sha256)?;
        if approval.approved_at > request.issued_at {
            return Err("review approval must predate permit issuance".into());
        }
    }
    for role in required_reviews {
        let approval = request
            .approvals
            .iter()
            .find(|approval| approval.reviewer_role == role && approval.decision == "approve")
            .ok_or_else(|| format!("missing {role} approval"))?;
        if !subjects.insert(&approval.reviewer_subject) {
            return Err("required reviews must come from distinct subjects".into());
        }
    }
    Ok(())
}

fn operation_contract(
    runbook: &RunbookReference,
    operation: &OperationReference,
) -> Result<
    (
        BTreeSet<&'static str>,
        BTreeSet<&'static str>,
        Vec<&'static str>,
        bool,
    ),
    String,
> {
    if runbook.version != "1.0.0" {
        return Err("runbook version is not certified".into());
    }
    if operation.version != "1.0.0" {
        return Err("operation version is not certified".into());
    }
    let contract = match operation.id.as_str() {
        "k8s.deployment-health" => (
            ["runbook-observer", "runbook-operator", "service-deployer"]
                .into_iter()
                .collect(),
            ["timeoutSeconds"].into_iter().collect(),
            vec![],
            false,
        ),
        "k8s.service-diagnostics" => (
            ["runbook-observer", "runbook-operator", "service-deployer"]
                .into_iter()
                .collect(),
            ["lookbackMinutes", "includeLogs"].into_iter().collect(),
            vec![],
            false,
        ),
        "k8s.restart-deployment" => (
            ["runbook-operator"].into_iter().collect(),
            ["reason", "waitForReadySeconds", "expectedReplicaCount"]
                .into_iter()
                .collect(),
            vec!["safety-reviewer", "operations-reviewer"],
            true,
        ),
        "service.deploy-release" => (
            ["service-deployer"].into_iter().collect(),
            [
                "imageDigest",
                "releaseId",
                "waitForReadySeconds",
                "expectedReplicaCount",
            ]
            .into_iter()
            .collect(),
            vec!["safety-reviewer", "operations-reviewer"],
            true,
        ),
        _ => return Err("request is not one of the certified operation primitives".into()),
    };
    let allowed_runbook = match operation.id.as_str() {
        "k8s.deployment-health" | "k8s.service-diagnostics" | "k8s.restart-deployment" => {
            runbook.id == "k8s.service-recovery"
        }
        "service.deploy-release" => runbook.id == "service.approved-release-deployment",
        _ => false,
    };
    if !allowed_runbook {
        return Err("operation is not allowed under the signed runbook".into());
    }
    Ok(contract)
}

fn validate_parameters(
    operation: &str,
    value: &Value,
    expected: BTreeSet<&str>,
) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or_else(|| "parameters must be an object".to_string())?;
    let actual: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    if actual != expected {
        return Err("parameters deviate from the fixed operation schema".into());
    }
    let integer = |name: &str, minimum: i64, maximum: i64| {
        let value = object
            .get(name)
            .and_then(Value::as_i64)
            .ok_or_else(|| format!("{name} must be an integer"))?;
        if !(minimum..=maximum).contains(&value) {
            return Err(format!("{name} must be between {minimum} and {maximum}"));
        }
        Ok(())
    };
    match operation {
        "k8s.deployment-health" => integer("timeoutSeconds", 5, 120)?,
        "k8s.service-diagnostics" => {
            integer("lookbackMinutes", 5, 120)?;
            if object.get("includeLogs").and_then(Value::as_bool).is_none() {
                return Err("includeLogs must be a boolean".into());
            }
        }
        "k8s.restart-deployment" => {
            let reason = object.get("reason").and_then(Value::as_str).unwrap_or("");
            if !(10..=500).contains(&reason.len()) {
                return Err("reason must contain 10 to 500 bytes".into());
            }
            integer("waitForReadySeconds", 30, 600)?;
            integer("expectedReplicaCount", 1, 500)?;
        }
        "service.deploy-release" => {
            validate_digest(
                "imageDigest",
                object
                    .get("imageDigest")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )?;
            validate_id(
                "releaseId",
                object
                    .get("releaseId")
                    .and_then(Value::as_str)
                    .unwrap_or(""),
            )?;
            integer("waitForReadySeconds", 30, 900)?;
            integer("expectedReplicaCount", 1, 500)?;
        }
        _ => return Err("unknown operation parameter contract".into()),
    }
    Ok(())
}

fn canonical_json(value: &Value) -> Result<String, String> {
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) | Value::String(_) => {
            serde_json::to_string(value).map_err(json_error)
        }
        Value::Array(values) => Ok(format!(
            "[{}]",
            values
                .iter()
                .map(canonical_json)
                .collect::<Result<Vec<_>, _>>()?
                .join(",")
        )),
        Value::Object(values) => {
            let ordered: BTreeMap<&String, &Value> = values.iter().collect();
            let entries = ordered
                .into_iter()
                .map(|(key, child)| {
                    Ok(format!(
                        "{}:{}",
                        serde_json::to_string(key).map_err(json_error)?,
                        canonical_json(child)?
                    ))
                })
                .collect::<Result<Vec<String>, String>>()?;
            Ok(format!("{{{}}}", entries.join(",")))
        }
    }
}

fn read_request(path: &Path) -> Result<OperationRequestV1, String> {
    serde_json::from_str(
        &fs::read_to_string(path)
            .map_err(|error| format!("read operation request {}: {error}", path.display()))?,
    )
    .map_err(|error| format!("decode OperationRequestV1: {error}"))
}

fn role_path(subject: &str) -> Result<PathBuf, String> {
    validate_id("agent", subject)?;
    Ok(config::state_dir()?
        .join("prod-ops/roles")
        .join(format!("{subject}.json")))
}

fn require_supervisor() -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        let uid = unsafe { libc::getuid() };
        if uid == 0 || uid == config::SUPERVISOR_UID {
            return Ok(());
        }
    }
    #[cfg(all(unix, not(target_os = "linux")))]
    if unsafe { libc::getuid() } == 0 {
        return Ok(());
    }
    if cfg!(feature = "insecure-dev-signer")
        && env::var("MULTIAGENT_PROD_OPS_DEVELOPMENT").as_deref() == Ok("1")
    {
        return Ok(());
    }
    Err("only the OS-isolated supervisor may assign operations roles, issue permits, or submit operations".into())
}

fn options(args: &[String]) -> Result<BTreeMap<String, String>, String> {
    let mut result = BTreeMap::new();
    let mut index = 0;
    while index < args.len() {
        let key = args[index].clone();
        if !key.starts_with("--") || index + 1 >= args.len() {
            return Err(format!("invalid option: {key}"));
        }
        if result
            .insert(key.clone(), args[index + 1].clone())
            .is_some()
        {
            return Err(format!("duplicate option: {key}"));
        }
        index += 2;
    }
    Ok(result)
}

fn required_option<'a>(
    options: &'a BTreeMap<String, String>,
    key: &str,
) -> Result<&'a str, String> {
    options
        .get(key)
        .filter(|value| !value.is_empty())
        .map(String::as_str)
        .ok_or_else(|| format!("missing {key}"))
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} is required"))
}

fn read_key_values(path: &Path) -> Result<BTreeMap<String, String>, String> {
    let content =
        fs::read_to_string(path).map_err(|error| format!("read {}: {error}", path.display()))?;
    let mut result = BTreeMap::new();
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let (key, value) = trimmed
            .split_once('=')
            .ok_or_else(|| format!("invalid key-value line in {}", path.display()))?;
        result.insert(key.to_string(), value.to_string());
    }
    Ok(result)
}

fn validate_id(label: &str, value: &str) -> Result<(), String> {
    let valid = !value.is_empty()
        && value.len() <= 128
        && value
            .chars()
            .next()
            .is_some_and(|character| character.is_ascii_alphanumeric())
        && value
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || ".-_:".contains(character));
    if valid {
        Ok(())
    } else {
        Err(format!("invalid {label}"))
    }
}

fn validate_digest(label: &str, value: &str) -> Result<(), String> {
    let valid = value.len() == 71
        && value.starts_with("sha256:")
        && value[7..]
            .chars()
            .all(|character| character.is_ascii_hexdigit() && !character.is_ascii_uppercase());
    if valid {
        Ok(())
    } else {
        Err(format!("invalid {label}"))
    }
}

fn valid_ticket(value: &str) -> bool {
    let mut parts = value.split('-');
    matches!((parts.next(), parts.next(), parts.next()), (Some(prefix), Some(number), None) if !prefix.is_empty() && prefix.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()) && number.chars().all(|c| c.is_ascii_digit()))
}

fn json_error(error: serde_json::Error) -> String {
    format!("JSON error: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use p256::pkcs8::EncodePrivateKey;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn request() -> OperationRequestV1 {
        let issued_at = Utc::now() - Duration::seconds(1);
        OperationRequestV1 {
            api_version: API_VERSION.into(),
            kind: "OperationRequest".into(),
            action_id: "action-123".into(),
            task_id: "task-123".into(),
            delegated_subject: "agent-123".into(),
            delegated_role: OperationsRole::RunbookOperator,
            intent_sha256: format!("sha256:{}", "1".repeat(64)),
            runbook: RunbookReference {
                id: "k8s.service-recovery".into(),
                version: "1.0.0".into(),
                phase: "restart".into(),
            },
            runbook_context_sha256: format!("sha256:{}", "4".repeat(64)),
            history_sha256: format!("sha256:{}", "5".repeat(64)),
            operation: OperationReference {
                id: "k8s.restart-deployment".into(),
                version: "1.0.0".into(),
            },
            target: TargetReference {
                environment: "production".into(),
                cluster: "mainnet-a".into(),
                namespace: "payments".into(),
                service: "api".into(),
            },
            parameters: json!({ "expectedReplicaCount": 3, "reason": "readiness checks are failing", "waitForReadySeconds": 120 }),
            approvals: vec![
                ApprovalV1 {
                    reviewer_subject: "safety-1".into(),
                    reviewer_role: "safety-reviewer".into(),
                    decision: "approve".into(),
                    evidence_sha256: format!("sha256:{}", "2".repeat(64)),
                    approved_at: issued_at - Duration::seconds(2),
                },
                ApprovalV1 {
                    reviewer_subject: "operations-1".into(),
                    reviewer_role: "operations-reviewer".into(),
                    decision: "approve".into(),
                    evidence_sha256: format!("sha256:{}", "3".repeat(64)),
                    approved_at: issued_at - Duration::seconds(1),
                },
            ],
            change_ticket: Some("OPS-123".into()),
            issued_at,
            expires_at: issued_at + Duration::minutes(5),
            nonce: "nonce-123".into(),
        }
    }

    #[test]
    fn role_and_parameter_escalation_are_rejected() {
        let mut candidate = request();
        candidate.delegated_role = OperationsRole::RunbookObserver;
        assert!(validate_request(&candidate).unwrap_err().contains("role"));
        let mut candidate = request();
        candidate.parameters = json!({ "command": "kubectl delete namespace payments" });
        assert!(validate_request(&candidate)
            .unwrap_err()
            .contains("deviate"));
        let mut candidate = request();
        candidate.runbook.id = "service.approved-release-deployment".into();
        assert!(validate_request(&candidate)
            .unwrap_err()
            .contains("not allowed"));
    }

    fn temporary_state() -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = env::temp_dir().join(format!("multiagent-prod-ops-test-{nonce}"));
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn write_review_evidence(state: &Path, request: &mut OperationRequestV1) {
        for index in 0..request.approvals.len() {
            let marker = prod_ops_review_marker(request, &request.approvals[index]).unwrap();
            let message = format!("APPROVED\n{marker}\n");
            let digest = Sha256::digest(message.as_bytes());
            let digest = format!("{digest:x}");
            request.approvals[index].evidence_sha256 = format!("sha256:{digest}");
            let directory = state
                .join("reviewer-evidence")
                .join(&request.approvals[index].reviewer_subject);
            fs::create_dir_all(&directory).unwrap();
            fs::write(directory.join("last-message.txt"), message).unwrap();
            fs::write(
                directory.join("evidence.env"),
                format!(
                    "name={}\nrole=reviewer\naccess=read-only\nworkflow_id=prod-ops\nstate=completed\noutput_sha256={digest}\n",
                    request.approvals[index].reviewer_subject
                ),
            )
            .unwrap();
        }
    }

    #[test]
    fn supervisor_signing_requires_sealed_reviewer_evidence() {
        let state = temporary_state();
        let mut candidate = request();
        assert!(validate_approval_evidence_at(&candidate, &state)
            .unwrap_err()
            .contains("read"));

        write_review_evidence(&state, &mut candidate);
        validate_approval_evidence_at(&candidate, &state).unwrap();

        candidate.parameters = json!({ "expectedReplicaCount": 4, "reason": "readiness checks are failing", "waitForReadySeconds": 120 });
        assert!(validate_approval_evidence_at(&candidate, &state)
            .unwrap_err()
            .contains("missing marker"));
        candidate.parameters = json!({ "expectedReplicaCount": 3, "reason": "readiness checks are failing", "waitForReadySeconds": 120 });

        candidate.history_sha256 = format!("sha256:{}", "9".repeat(64));
        assert!(validate_approval_evidence_at(&candidate, &state)
            .unwrap_err()
            .contains("missing marker"));
        fs::remove_dir_all(state).unwrap();
    }

    #[test]
    fn compact_file_signature_uses_jws_raw_es256_shape() {
        let key = SigningKey::random(&mut p256::elliptic_curve::rand_core::OsRng);
        let pem = key.to_pkcs8_pem(Default::default()).unwrap();
        let parsed = SigningKey::from_pkcs8_pem(pem.as_str()).unwrap();
        let signer = FileSigningBackend {
            key_id: "test-key".into(),
            key: parsed,
        };
        let permit = ActionPermitV1 {
            api_version: API_VERSION,
            kind: "ActionPermit",
            request: &request(),
        };
        let payload = canonical_json(&serde_json::to_value(permit).unwrap()).unwrap();
        let compact = compact_jws(&signer, payload.as_bytes()).unwrap();
        let parts: Vec<&str> = compact.split('.').collect();
        assert_eq!(parts.len(), 3);
        assert_eq!(
            general_purpose::URL_SAFE_NO_PAD
                .decode(parts[2])
                .unwrap()
                .len(),
            64
        );
    }

    #[test]
    fn canonical_json_sorts_nested_keys() {
        assert_eq!(
            canonical_json(&json!({"z": 1, "a": {"y": 2, "b": 3}})).unwrap(),
            r#"{"a":{"b":3,"y":2},"z":1}"#
        );
    }

    #[test]
    fn signer_hashes_are_stable() {
        let digest = Sha256::digest(b"intent");
        assert_eq!(format!("sha256:{digest:x}").len(), 71);
    }
}
