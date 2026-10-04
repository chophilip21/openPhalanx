//! Client for the backend's loopback-only admin API.

use std::time::Duration;

use anyhow::{Context, Result};
use serde::{de::DeserializeOwned, Deserialize, Serialize};

use crate::docker::ADMIN_PORT;

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Pairing {
    pub active: bool,
    pub code: Option<String>,
    pub expires_at: Option<f64>,
    pub attempts_left: Option<u32>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct GatewayMetrics {
    pub started_at: f64,
    /// Authenticated client requests through the gateway.
    pub requests_total: u64,
    pub requests_failed: u64,
    pub requests_active: u64,
    pub last_request_at: Option<f64>,
    #[serde(default)]
    pub web_searches: u64,
    /// Requests that went through the automatic search router / that searched.
    #[serde(default)]
    pub auto_routed: u64,
    #[serde(default)]
    pub auto_searched: u64,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct InferenceMetrics {
    pub prompt_tokens_total: Option<f64>,
    pub generation_tokens_total: Option<f64>,
    pub cached_tokens_total: Option<f64>,
    pub cache_hit_ratio: Option<f64>,
    pub gen_throughput: Option<f64>,
    pub running_requests: Option<f64>,
    pub queued_requests: Option<f64>,
    pub token_usage: Option<f64>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Status {
    pub sglang: String,
    pub model: String,
    pub gateway: GatewayMetrics,
    #[serde(default)]
    pub inference: InferenceMetrics,
    pub pairing: Pairing,
    pub devices: u32,
    pub tls_fingerprint: String,
    /// Whether the gateway has a SearXNG instance configured.
    #[serde(default)]
    pub web_search: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub created_at: f64,
    pub last_seen: Option<f64>,
    pub requests: u64,
    /// Counted when the backend reports usage (always for non-streaming calls).
    #[serde(default)]
    pub prompt_tokens: u64,
    #[serde(default)]
    pub completion_tokens: u64,
    #[serde(default)]
    pub web_searches: u64,
}

pub struct AdminClient {
    http: reqwest::Client,
    base: String,
    token: String,
}

impl AdminClient {
    pub fn new(token: String) -> Self {
        Self {
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .expect("reqwest client"),
            base: format!("http://127.0.0.1:{ADMIN_PORT}"),
            token,
        }
    }

    async fn call<T: DeserializeOwned>(&self, method: reqwest::Method, path: &str) -> Result<T> {
        self.http
            .request(method, format!("{}{path}", self.base))
            .header("x-admin-token", &self.token)
            .send()
            .await
            .context("backend admin API is not reachable yet")?
            .error_for_status()?
            .json()
            .await
            .context("unexpected admin API response")
    }

    pub async fn status(&self) -> Result<Status> {
        self.call(reqwest::Method::GET, "/admin/status").await
    }

    pub async fn new_pairing(&self) -> Result<Pairing> {
        self.call(reqwest::Method::POST, "/admin/pairing").await
    }

    pub async fn clear_pairing(&self) -> Result<Pairing> {
        self.call(reqwest::Method::DELETE, "/admin/pairing").await
    }

    pub async fn devices(&self) -> Result<Vec<Device>> {
        self.call(reqwest::Method::GET, "/admin/devices").await
    }

    pub async fn revoke(&self, id: &str) -> Result<serde_json::Value> {
        self.call(reqwest::Method::DELETE, &format!("/admin/devices/{id}")).await
    }
}

pub fn new_admin_token() -> String {
    use rand::RngCore;
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    hex::encode(bytes)
}
