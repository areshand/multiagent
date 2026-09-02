use multiagent_logger::{
    config::Config,
    model::{ArtifactReference, Event},
    AppState,
};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::{self, Read},
};

#[tokio::main]
async fn main() {
    #[cfg(unix)]
    unsafe {
        libc::umask(0o077);
    }
    if let Err(error) = run().await {
        eprintln!("logger failed: {error}");
        std::process::exit(1)
    }
}
async fn run() -> Result<(), String> {
    let mut args = env::args().skip(1);
    match args.next().as_deref().unwrap_or("serve") {
        "serve" => serve().await,
        "hash-token" => {
            let token = args.next().ok_or("usage: logger hash-token TOKEN")?;
            if args.next().is_some() {
                return Err("usage: logger hash-token TOKEN".into());
            }
            println!("sha256:{:x}", Sha256::digest(token));
            Ok(())
        }
        "submit-event" => submit(false, args.collect()).await,
        "submit-trace-commitment" => submit(true, args.collect()).await,
        other => Err(format!("unknown logger command {other}")),
    }
}
async fn serve() -> Result<(), String> {
    let config = Config::from_env()?;
    let state = AppState::from_config(&config)?;
    if config.projection_dir.is_some() {
        let projection = state.clone();
        let interval = config.projection_interval_ms;
        tokio::spawn(async move {
            let mut timer = tokio::time::interval(std::time::Duration::from_millis(interval));
            loop {
                timer.tick().await;
                projection.flush_projections();
            }
        });
    }
    let address = format!("{}:{}", config.host, config.port);
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .map_err(|e| format!("bind {address}: {e}"))?;
    println!("logger listening on {address}");
    axum::serve(listener, multiagent_logger::router(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await
        .map_err(|e| e.to_string())
}
async fn submit(trace: bool, args: Vec<String>) -> Result<(), String> {
    let (
        mut url,
        mut token_file,
        mut session,
        mut event_id,
        mut file,
        mut storage_reference,
        mut media_type,
    ) = (None, None, None, None, None, None, None);
    let mut index = 0;
    while index < args.len() {
        let target = match args[index].as_str() {
            "--url" => &mut url,
            "--token-file" => &mut token_file,
            "--session-id" => &mut session,
            "--event-id" => &mut event_id,
            "--file" => &mut file,
            "--storage-reference" => &mut storage_reference,
            "--media-type" => &mut media_type,
            flag => return Err(format!("unknown option {flag}")),
        };
        index += 1;
        *target = Some(args.get(index).ok_or("option value is required")?.clone());
        index += 1;
    }
    let url = url.ok_or("--url is required")?;
    let token = fs::read_to_string(token_file.ok_or("--token-file is required")?)
        .map_err(|e| e.to_string())?
        .trim()
        .to_string();
    let event = if trace {
        let path = file.ok_or("--file is required")?;
        let (digest, size) = digest_file(&path)?;
        Event {
            event_id: event_id.ok_or("--event-id is required")?,
            session_id: session.ok_or("--session-id is required")?,
            event_type: "trace.commitment".into(),
            payload_digest: digest.clone(),
            artifact_references: vec![ArtifactReference {
                uri: storage_reference.ok_or("--storage-reference is required")?,
                digest: Some(digest),
                size: Some(size),
                media_type: Some(media_type.ok_or("--media-type is required")?),
            }],
        }
    } else {
        let mut raw = Vec::new();
        io::stdin()
            .read_to_end(&mut raw)
            .map_err(|e| e.to_string())?;
        serde_json::from_slice(&raw).map_err(|e| format!("decode event: {e}"))?
    };
    let response = reqwest::Client::new()
        .post(format!("{}/v1/events", url.trim_end_matches('/')))
        .bearer_auth(token)
        .json(&event)
        .send()
        .await
        .map_err(|e| e.to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "logger rejected event: {} {}",
            response.status(),
            response.text().await.unwrap_or_default()
        ));
    }
    Ok(())
}

fn digest_file(path: &str) -> Result<(String, u64), String> {
    let mut input =
        fs::File::open(path).map_err(|error| format!("open trace artifact: {error}"))?;
    let mut digest = Sha256::new();
    let mut size = 0u64;
    let mut buffer = [0u8; 64 * 1024];
    loop {
        let count = input
            .read(&mut buffer)
            .map_err(|error| format!("read trace artifact: {error}"))?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
        size = size
            .checked_add(count as u64)
            .ok_or("trace artifact is too large")?;
    }
    Ok((format!("sha256:{:x}", digest.finalize()), size))
}
