//! Loopback proxy between a local coding agent and the OpenPhalanx gateway.
//!
//! Agents speak plain OpenAI-compatible HTTP to `127.0.0.1`. The proxy
//! forwards only the inference endpoints over TLS pinned to the server's
//! certificate and adds the device token, so the agent never needs to trust a
//! self-signed certificate or see the token. Other local processes are kept
//! out by a random per-run key that only the launched agent receives.

use std::net::SocketAddr;

use anyhow::{Context, Result};
use axum::body::Body;
use axum::extract::{DefaultBodyLimit, State};
use axum::http::{header, HeaderMap, Method, StatusCode, Uri};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use rand::RngCore;
use serde_json::json;

use crate::config::Server;
use crate::tls;

/// Slightly above the gateway's own 4 MiB limit, so the gateway decides.
const MAX_BODY_BYTES: usize = 5 * 1024 * 1024;

#[derive(Clone)]
struct Upstream {
    client: reqwest::Client,
    url: String,
    token: String,
    local_key: String,
    /// Ask the gateway to route chat requests through automatic web search.
    web_search: bool,
}

/// A bound, not yet running proxy.
pub struct Proxy {
    pub addr: SocketAddr,
    /// API key local clients must send (`Authorization: Bearer …`).
    pub local_key: String,
    listener: tokio::net::TcpListener,
    app: Router,
}

impl Proxy {
    /// Binds `127.0.0.1:port` (`0` picks a free port).
    pub async fn bind(server: &Server, port: u16, web_search: bool) -> Result<Proxy> {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", port))
            .await
            .with_context(|| format!("cannot listen on 127.0.0.1:{port}"))?;
        let addr = listener.local_addr()?;
        let mut key = [0u8; 24];
        rand::rng().fill_bytes(&mut key);
        let local_key = format!("oppx-local-{}", key.iter().map(|b| format!("{b:02x}")).collect::<String>());
        let state = Upstream {
            client: tls::pinned_client(&server.fingerprint)?,
            url: server.url.clone(),
            token: server.token.clone(),
            local_key: local_key.clone(),
            web_search,
        };
        let app = Router::new()
            .route("/v1/models", get(forward))
            .route("/v1/chat/completions", post(forward))
            .route("/v1/tokenize", post(forward))
            .route("/v1/search", post(forward)) // the agent's web_search tool
            .fallback(|| async {
                error(StatusCode::NOT_FOUND, "oppx proxy only serves /v1/models, /v1/chat/completions, /v1/tokenize and /v1/search")
            })
            .layer(DefaultBodyLimit::max(MAX_BODY_BYTES))
            .with_state(state);
        Ok(Proxy { addr, local_key, listener, app })
    }

    /// `http://127.0.0.1:<port>/v1`, for `OPENAI_API_BASE`.
    pub fn base_url(&self) -> String {
        format!("http://{}/v1", self.addr)
    }

    /// Serves until the future is dropped or the process exits.
    pub async fn serve(self) -> Result<()> {
        axum::serve(self.listener, self.app).await.context("proxy stopped")
    }
}

fn error(status: StatusCode, message: &str) -> Response {
    (status, Json(json!({ "error": { "message": message, "type": "oppx_proxy_error" } }))).into_response()
}

async fn forward(State(up): State<Upstream>, method: Method, uri: Uri, headers: HeaderMap, body: Body) -> Response {
    let presented = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .unwrap_or_default();
    if presented != up.local_key {
        return error(StatusCode::UNAUTHORIZED, "wrong or missing local API key for the oppx proxy");
    }
    let bytes = match axum::body::to_bytes(body, MAX_BODY_BYTES).await {
        Ok(b) => b,
        Err(_) => return error(StatusCode::PAYLOAD_TOO_LARGE, "request body too large"),
    };

    let path = uri.path_and_query().map(|p| p.as_str()).unwrap_or(uri.path());
    let mut req = up.client.request(method, format!("{}{path}", up.url)).bearer_auth(&up.token);
    if let Some(ct) = headers.get(header::CONTENT_TYPE) {
        req = req.header(header::CONTENT_TYPE, ct.clone());
    }
    if up.web_search {
        req = req.header("x-oppx-web-search", "auto");
    }
    let resp = match req.body(bytes).send().await {
        Ok(r) => r,
        Err(e) if tls::is_pin_mismatch(&e) => {
            eprintln!("oppx: the server's TLS certificate changed; refusing to send anything. Run `oppx status`.");
            return error(StatusCode::BAD_GATEWAY, "the OpenPhalanx server's certificate changed; run `oppx status`");
        }
        Err(e) => return error(StatusCode::BAD_GATEWAY, &format!("cannot reach the OpenPhalanx server: {e}")),
    };

    let mut builder = Response::builder().status(resp.status().as_u16());
    for name in [header::CONTENT_TYPE, header::CACHE_CONTROL] {
        if let Some(v) = resp.headers().get(name.as_str()) {
            builder = builder.header(name, v.as_bytes());
        }
    }
    // Streamed through as it arrives; dropping it (client disconnect) closes
    // the upstream connection, which cancels the generation on the server.
    builder
        .body(Body::from_stream(resp.bytes_stream()))
        .unwrap_or_else(|_| error(StatusCode::BAD_GATEWAY, "bad upstream response"))
}
