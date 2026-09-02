use crate::{
    auth::{AuthError, Authorizer, Client},
    config::Config,
    model::{validate_event, validate_identifier, Event},
    signer::Ed25519Signer,
    store::{AppendResult, Store, StoreError},
};
use axum::{
    body::Bytes,
    extract::{Path, Query, State},
    http::{header::AUTHORIZATION, HeaderMap, Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        Arc, Mutex,
    },
};

#[derive(Clone)]
pub struct AppState(Arc<Inner>);
struct Inner {
    store: Mutex<Store>,
    authorizer: Authorizer,
    public_key: Value,
    ready: AtomicBool,
    max_event_bytes: usize,
    projection_dir: Option<PathBuf>,
    appends: AtomicU64,
    duplicates: AtomicU64,
    rejected: AtomicU64,
    projection_success: AtomicU64,
    projection_failures: AtomicU64,
}

impl AppState {
    pub fn from_config(config: &Config) -> Result<Self, String> {
        let signer = Ed25519Signer::load(
            &config.signing_key_file,
            config.signing_key_id.clone(),
            config.logger_id.clone(),
        )?;
        let public_key = signer.public_descriptor()?;
        let store = Store::open(
            &config.ledger_file,
            signer,
            config.checkpoint_interval,
            config.projection_dir.is_some(),
        )?;
        let authorizer = Authorizer::load(&config.clients_file)?;
        Ok(Self(Arc::new(Inner {
            store: Mutex::new(store),
            authorizer,
            public_key,
            ready: AtomicBool::new(true),
            max_event_bytes: config.max_event_bytes,
            projection_dir: config.projection_dir.clone(),
            appends: 0.into(),
            duplicates: 0.into(),
            rejected: 0.into(),
            projection_success: 0.into(),
            projection_failures: 0.into(),
        })))
    }
    pub fn flush_projections(&self) {
        let Some(directory) = &self.0.projection_dir else {
            return;
        };
        let result = self
            .0
            .store
            .lock()
            .expect("logger store lock")
            .flush_projections(directory);
        match result {
            Ok((ok, failed)) => {
                self.0.projection_success.fetch_add(ok, Ordering::Relaxed);
                self.0
                    .projection_failures
                    .fetch_add(failed, Ordering::Relaxed);
            }
            Err(error) => {
                self.0.projection_failures.fetch_add(1, Ordering::Relaxed);
                eprintln!("logger projection failed: {error}")
            }
        }
    }
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/healthz", get(health))
        .route("/readyz", get(ready))
        .route("/v1/public-key", get(public_key))
        .route("/metrics", get(metrics))
        .route("/v1/events", post(events))
        .route("/v1/logs/{log_id}/head", get(head))
        .route("/v1/logs/{log_id}/entries", get(entries))
        .route("/v1/logs/{log_id}/checkpoints", get(checkpoints))
        .route("/v1/checkpoints/{checkpoint_id}", get(checkpoint))
        .route("/v1/verify", post(verify))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            response_policy,
        ))
        .with_state(state)
}

async fn response_policy(
    State(state): State<AppState>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Response {
    let mut response = next.run(request).await;
    response.headers_mut().insert(
        axum::http::header::CACHE_CONTROL,
        axum::http::HeaderValue::from_static("no-store"),
    );
    response.headers_mut().insert(
        axum::http::header::X_CONTENT_TYPE_OPTIONS,
        axum::http::HeaderValue::from_static("nosniff"),
    );
    if response.status().is_client_error() || response.status().is_server_error() {
        state.0.rejected.fetch_add(1, Ordering::Relaxed);
    }
    response
}
async fn health() -> Json<Value> {
    Json(json!({"live":true}))
}
async fn ready(State(state): State<AppState>) -> Result<Json<Value>, ApiError> {
    if state.0.ready.load(Ordering::Relaxed) {
        Ok(Json(json!({"ready":true})))
    } else {
        Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "integrity_unavailable",
            "logger integrity is unavailable",
        ))
    }
}
async fn public_key(State(state): State<AppState>) -> Json<Value> {
    Json(state.0.public_key.clone())
}
async fn events(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<StatusCode, ApiError> {
    if !state.0.ready.load(Ordering::Relaxed) {
        return Err(ApiError::new(
            StatusCode::SERVICE_UNAVAILABLE,
            "integrity_unavailable",
            "authoritative append is disabled until ledger integrity is restored",
        ));
    }
    let client = authorize(&state, &headers, "append")?;
    if body.is_empty() {
        return Err(ApiError::bad("request body is required"));
    }
    if body.len() > state.0.max_event_bytes {
        return Err(ApiError::new(
            StatusCode::PAYLOAD_TOO_LARGE,
            "request_too_large",
            "request body exceeds size limit",
        ));
    }
    let event: Event = serde_json::from_slice(&body).map_err(|error| {
        ApiError::bad(format!("request body must be valid event JSON: {error}"))
    })?;
    validate_event(&event).map_err(ApiError::bad)?;
    client
        .authorize_session(&event.session_id)
        .map_err(ApiError::from_auth)?;
    client
        .authorize_event(&event.event_type)
        .map_err(ApiError::from_auth)?;
    let result = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .append(event, client.id);
    if matches!(result, Err(StoreError::Internal(_))) {
        state.0.ready.store(false, Ordering::Relaxed);
    }
    let result = result.map_err(ApiError::from_store)?;
    match result {
        AppendResult::Appended => {
            state.0.appends.fetch_add(1, Ordering::Relaxed);
        }
        AppendResult::Duplicate => {
            state.0.duplicates.fetch_add(1, Ordering::Relaxed);
        }
    }
    Ok(StatusCode::NO_CONTENT)
}
async fn head(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(log_id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    let client = read_client(&state, &headers, &log_id)?;
    drop(client);
    let value = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .head(&log_id)
        .map_err(ApiError::internal)?
        .ok_or_else(ApiError::not_found)?;
    Ok(Json(
        serde_json::to_value(value).map_err(|e| ApiError::internal(e.to_string()))?,
    ))
}
#[derive(Deserialize)]
struct ListQuery {
    after: Option<u64>,
    limit: Option<usize>,
}
async fn entries(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(log_id): Path<String>,
    Query(query): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    read_client(&state, &headers, &log_id)?;
    let (after, limit) = list_query(query)?;
    let values = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .entries(&log_id, after, limit)
        .map_err(ApiError::internal)?;
    Ok(Json(json!({"logId":log_id,"entries":values})))
}
async fn checkpoints(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(log_id): Path<String>,
    Query(query): Query<ListQuery>,
) -> Result<Json<Value>, ApiError> {
    read_client(&state, &headers, &log_id)?;
    let (after, limit) = list_query(query)?;
    let values = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .checkpoints(&log_id, after, limit)
        .map_err(ApiError::internal)?;
    Ok(Json(json!({"logId":log_id,"checkpoints":values})))
}
async fn checkpoint(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<String>,
) -> Result<Json<Value>, ApiError> {
    validate_identifier(&id, "checkpointId").map_err(ApiError::bad)?;
    let client = authorize(&state, &headers, "read")?;
    let store = state.0.store.lock().expect("logger store lock");
    let log_id = store
        .checkpoint_session(&id)
        .map_err(ApiError::internal)?
        .ok_or_else(ApiError::not_found)?;
    client
        .authorize_session(&log_id)
        .map_err(ApiError::from_auth)?;
    let value = store
        .checkpoint(&id)
        .map_err(ApiError::internal)?
        .ok_or_else(ApiError::not_found)?;
    Ok(Json(
        serde_json::to_value(value).map_err(|e| ApiError::internal(e.to_string()))?,
    ))
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct VerifyRequest {
    log_id: Option<String>,
}
async fn verify(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let client = authorize(&state, &headers, "verify")?;
    let request: VerifyRequest = serde_json::from_slice(&body)
        .map_err(|error| ApiError::bad(format!("invalid verify request: {error}")))?;
    if let Some(id) = &request.log_id {
        validate_identifier(id, "logId").map_err(ApiError::bad)?;
        client.authorize_session(id).map_err(ApiError::from_auth)?;
    }
    let result = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .verify(request.log_id.as_deref())
        .map_err(ApiError::internal)?;
    state.0.ready.store(result.ok, Ordering::Relaxed);
    let status = if result.ok {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    Ok((
        status,
        Json(serde_json::to_value(result).map_err(|e| ApiError::internal(e.to_string()))?),
    ))
}
async fn metrics(State(state): State<AppState>, headers: HeaderMap) -> Result<String, ApiError> {
    authorize(&state, &headers, "read")?;
    let counts: BTreeMap<String, u64> = state
        .0
        .store
        .lock()
        .expect("logger store lock")
        .projection_counts()
        .map_err(ApiError::internal)?;
    Ok(format!("logger_appends_total {}\nlogger_duplicate_appends_total {}\nlogger_rejected_requests_total {}\nlogger_integrity_ok {}\nlogger_projection_success_total {}\nlogger_projection_failures_total {}\nlogger_projection_pending {}\n",state.0.appends.load(Ordering::Relaxed),state.0.duplicates.load(Ordering::Relaxed),state.0.rejected.load(Ordering::Relaxed),u8::from(state.0.ready.load(Ordering::Relaxed)),state.0.projection_success.load(Ordering::Relaxed),state.0.projection_failures.load(Ordering::Relaxed),counts.get("pending").copied().unwrap_or(0)))
}
fn authorize(state: &AppState, headers: &HeaderMap, permission: &str) -> Result<Client, ApiError> {
    let value = headers.get(AUTHORIZATION).and_then(|v| v.to_str().ok());
    let client = state
        .0
        .authorizer
        .authenticate(value)
        .map_err(ApiError::from_auth)?;
    client.require(permission).map_err(ApiError::from_auth)?;
    Ok(client)
}
fn read_client(state: &AppState, headers: &HeaderMap, log_id: &str) -> Result<Client, ApiError> {
    validate_identifier(log_id, "logId").map_err(ApiError::bad)?;
    let client = authorize(state, headers, "read")?;
    client
        .authorize_session(log_id)
        .map_err(ApiError::from_auth)?;
    Ok(client)
}
fn list_query(query: ListQuery) -> Result<(u64, usize), ApiError> {
    let limit = query.limit.unwrap_or(100);
    if !(1..=1000).contains(&limit) {
        return Err(ApiError::bad("limit is outside the allowed range"));
    }
    Ok((query.after.unwrap_or(0), limit))
}

#[derive(Debug)]
struct ApiError {
    status: StatusCode,
    code: &'static str,
    message: String,
}
impl ApiError {
    fn new(status: StatusCode, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }
    fn bad(message: impl Into<String>) -> Self {
        Self::new(StatusCode::BAD_REQUEST, "invalid_request", message)
    }
    fn internal(message: impl Into<String>) -> Self {
        eprintln!("logger request failed: {}", message.into());
        Self::new(
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal_error",
            "internal server error",
        )
    }
    fn not_found() -> Self {
        Self::new(StatusCode::NOT_FOUND, "not_found", "resource not found")
    }
    fn from_auth(value: AuthError) -> Self {
        Self::new(
            StatusCode::from_u16(value.status).unwrap_or(StatusCode::UNAUTHORIZED),
            value.code,
            value.message,
        )
    }
    fn from_store(value: StoreError) -> Self {
        match value {
            StoreError::Conflict(message) => {
                Self::new(StatusCode::CONFLICT, "event_id_conflict", message)
            }
            StoreError::Internal(message) => Self::internal(message),
        }
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(json!({"error":{"code":self.code,"message":self.message}})),
        )
            .into_response()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::Request};
    use ed25519_dalek::{
        pkcs8::{spki::der::pem::LineEnding, EncodePrivateKey},
        SigningKey,
    };
    use sha2::{Digest, Sha256};
    use std::fs;
    use tower::ServiceExt;

    fn application() -> (tempfile::TempDir, Router) {
        let directory = tempfile::tempdir().unwrap();
        let key = SigningKey::from_bytes(&[9; 32])
            .to_pkcs8_pem(LineEnding::LF)
            .unwrap();
        let key_file = directory.path().join("signing-key.pem");
        fs::write(&key_file, key.as_bytes()).unwrap();
        let token = "test-token-0123456789abcdef";
        let clients_file = directory.path().join("clients.json");
        fs::write(
            &clients_file,
            serde_json::to_vec(&json!({"clients":[{
                "id":"test-client",
                "tokenSha256":format!("sha256:{:x}", Sha256::digest(token)),
                "permissions":["append","read","verify"],
                "eventTypes":["*"],
                "sessions":["session-*"]
            }]}))
            .unwrap(),
        )
        .unwrap();
        let config = Config {
            ledger_file: directory.path().join("ledger.jsonl"),
            signing_key_file: key_file,
            signing_key_id: "test-key".into(),
            logger_id: "test-logger".into(),
            clients_file,
            checkpoint_interval: 1,
            max_event_bytes: 65_536,
            projection_dir: None,
            projection_interval_ms: 1_000,
            host: "127.0.0.1".into(),
            port: 8090,
        };
        let state = AppState::from_config(&config).unwrap();
        (directory, router(state))
    }

    #[tokio::test]
    async fn append_and_read_require_scoped_auth_and_return_no_receipt() {
        let (_directory, app) = application();
        let event = json!({
            "eventId":"event-1",
            "sessionId":"session-1",
            "eventType":"reviewer.verdict",
            "payloadDigest":format!("sha256:{}", "1".repeat(64)),
            "artifactReferences":[]
        });
        let unauthorized = app
            .clone()
            .oneshot(
                Request::post("/v1/events")
                    .header("content-type", "application/json")
                    .body(Body::from(event.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(unauthorized.status(), StatusCode::UNAUTHORIZED);

        let appended = app
            .clone()
            .oneshot(
                Request::post("/v1/events")
                    .header("authorization", "Bearer test-token-0123456789abcdef")
                    .header("content-type", "application/json")
                    .body(Body::from(event.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(appended.status(), StatusCode::NO_CONTENT);
        assert_eq!(appended.headers()["cache-control"], "no-store");
        assert_eq!(
            axum::body::to_bytes(appended.into_body(), 1024)
                .await
                .unwrap()
                .len(),
            0
        );

        let head = app
            .oneshot(
                Request::get("/v1/logs/session-1/head")
                    .header("authorization", "Bearer test-token-0123456789abcdef")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(head.status(), StatusCode::OK);
        let body = axum::body::to_bytes(head.into_body(), 4096).await.unwrap();
        assert_eq!(
            serde_json::from_slice::<Value>(&body).unwrap()["sequence"],
            1
        );
    }
}
