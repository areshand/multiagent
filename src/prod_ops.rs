use chrono::{Duration, SecondsFormat, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::env;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::time::{SystemTime, UNIX_EPOCH};

pub fn run(args: &[String]) -> Result<ExitCode, String> {
    match args.first().map(String::as_str) {
        Some("grafana-read") => grafana_read(&args[1..]),
        _ => Err("usage: multiagent prod-ops grafana-read --task-id ID --logql QUERY [--lookback-minutes N] [--limit N]".into()),
    }
}

fn grafana_read(args: &[String]) -> Result<ExitCode, String> {
    let options = options(args)?;
    let task_id = required(&options, "--task-id")?;
    validate_id("task ID", task_id)?;
    let logql = required(&options, "--logql")?;
    if logql.len() > 4096 {
        return Err("LogQL query exceeds 4096 characters".into());
    }
    let lookback = number(&options, "--lookback-minutes", 30, 1, 120)?;
    let limit = number(&options, "--limit", 100, 1, 100)?;
    let now = Utc::now();
    let unique = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_millis();
    let action_id = format!("grafana-{unique}-{}", std::process::id());
    let parameters = json!({
        "action": "query-loki-logs",
        "datasourceUid": env_default("PROD_MCP_GRAFANA_DATASOURCE_UID", "mi-loki"),
        "direction": "backward",
        "limit": limit,
        "logql": logql,
        "lookbackMinutes": lookback
    });
    let runbook = json!({"id":"observability.investigation","phase":"read-logs","version":"1.0.0"});
    let request = json!({
        "actionId": action_id,
        "apiVersion": "prod.moveindustries.io/v1",
        "approvals": [],
        "delegatedRole": "runbook-observer",
        "delegatedSubject": "multiagent-supervisor",
        "expiresAt": (now + Duration::minutes(4)).to_rfc3339_opts(SecondsFormat::Millis, true),
        "historySha256": history_digest(),
        "intentSha256": digest_json(&parameters)?,
        "issuedAt": now.to_rfc3339_opts(SecondsFormat::Millis, true),
        "kind": "OperationRequest",
        "nonce": format!("nonce-{unique}-{}", std::process::id()),
        "operation": {"id":"grafana.read","version":"1.0.0"},
        "parameters": parameters,
        "runbook": runbook,
        "runbookContextSha256": digest_json(&runbook)?,
        "target": {
            "cluster": env_default("PROD_MCP_GRAFANA_CLUSTER", "internal-tools"),
            "environment": "production",
            "namespace": env_default("PROD_MCP_GRAFANA_NAMESPACE", "grafana"),
            "service": env_default("PROD_MCP_GRAFANA_SERVICE", "grafana")
        },
        "taskId": task_id
    });
    let payload = canonical(&json!({
        "apiVersion":"prod.moveindustries.io/v1",
        "kind":"ActionPermit",
        "request": request
    }))?;
    let permit = sign_permit(&payload)?;
    let result = call_prod_mcp(&permit)?;
    println!(
        "{}",
        serde_json::to_string_pretty(&result).map_err(|error| error.to_string())?
    );
    Ok(ExitCode::SUCCESS)
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
    let temporary = state
        .join("tmp")
        .join(format!("kms-sign-{}.bin", std::process::id()));
    fs::create_dir_all(temporary.parent().expect("temporary parent"))
        .map_err(|error| format!("create KMS temporary directory: {error}"))?;
    fs::write(&temporary, signing_input.as_bytes())
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
    if !url.starts_with("http://") {
        return Err("PROD_MCP_URL must use the internal http:// service endpoint".into());
    }
    let token = required_env("PROD_MCP_BEARER_TOKEN")?;
    let state = PathBuf::from(required_env("MULTIAGENT_STATE_DIR")?);
    let headers = state
        .join("tmp")
        .join(format!("prod-mcp-headers-{}.txt", std::process::id()));
    let initialize = json!({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"capabilities":{},"clientInfo":{"name":"multiagent-supervisor","version":env!("CARGO_PKG_VERSION")},"protocolVersion":"2025-03-26"}});
    let initialized = curl_mcp(&url, &token, None, &initialize, &headers)?;
    if initialized.get("error").is_some() {
        return Err(format!("prod-mcp initialize failed: {initialized}"));
    }
    let session = session_id(&headers)?;
    let call = json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"arguments":{"permit":permit},"name":"operations_execute"}});
    let result = curl_mcp(&url, &token, Some(&session), &call, &headers)?;
    let _ = fs::remove_file(headers);
    if let Some(error) = result.get("error") {
        return Err(format!("prod-mcp execution failed: {error}"));
    }
    Ok(result)
}

fn curl_mcp(
    url: &str,
    token: &str,
    session: Option<&str>,
    body: &Value,
    headers: &Path,
) -> Result<Value, String> {
    let authorization = format!("Authorization: Bearer {token}");
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
        .arg(headers)
        .args([
            "--header",
            &authorization,
            "--header",
            "Content-Type: application/json",
            "--header",
            "Accept: application/json, text/event-stream",
        ]);
    if let Some(session) = session {
        command.args(["--header", &format!("Mcp-Session-Id: {session}")]);
    }
    let output = command
        .args(["--data-binary", &canonical_string(body)?, url])
        .output()
        .map_err(|error| format!("execute prod-mcp request: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "prod-mcp HTTP request failed: {}{}",
            String::from_utf8_lossy(&output.stderr),
            String::from_utf8_lossy(&output.stdout)
        ));
    }
    parse_mcp_body(&String::from_utf8_lossy(&output.stdout))
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

fn number(
    options: &std::collections::BTreeMap<String, String>,
    key: &str,
    default: u32,
    min: u32,
    max: u32,
) -> Result<u32, String> {
    let value = options
        .get(key)
        .map(|value| value.parse::<u32>())
        .transpose()
        .map_err(|_| format!("{key} must be an integer"))?
        .unwrap_or(default);
    if !(min..=max).contains(&value) {
        return Err(format!("{key} must be between {min} and {max}"));
    }
    Ok(value)
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

fn env_default(name: &str, default: &str) -> String {
    env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.into())
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

fn history_digest() -> String {
    let path = env::var("MULTIAGENT_STATE_DIR")
        .ok()
        .zip(env::var("MULTIAGENT_WORKFLOW_ID").ok())
        .map(|(state, workflow)| {
            PathBuf::from(state)
                .join("workflows")
                .join(workflow)
                .join("lifecycle/events.log")
        });
    path.and_then(|path| fs::read(path).ok())
        .map(|bytes| digest(&bytes))
        .unwrap_or_else(|| digest(&[]))
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
    use super::{base64_decode, base64url_encode, ecdsa_der_to_raw, parse_mcp_body};
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
}
