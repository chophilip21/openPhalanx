//! Calls to the OpenPhalanx gateway. Every function takes a client from
//! [`crate::tls`], so requests only ever reach the expected server.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use reqwest::StatusCode;
use serde::Deserialize;
use serde_json::json;

const TIMEOUT: Duration = Duration::from_secs(20);

#[derive(Debug, Deserialize)]
pub struct Paired {
    pub device_id: String,
    pub token: String,
}

#[derive(Debug, Deserialize)]
pub struct Health {
    pub sglang: String,
}

#[derive(Debug, Deserialize)]
pub struct WhoAmI {
    pub device_id: String,
    pub device_name: String,
    pub model: String,
}

/// Error detail from a FastAPI (`{"detail": …}`) or OpenAI (`{"error": {"message": …}}`) body.
async fn detail(resp: reqwest::Response) -> String {
    let status = resp.status();
    let body: serde_json::Value = resp.json().await.unwrap_or_default();
    body["detail"]
        .as_str()
        .or_else(|| body["error"]["message"].as_str())
        .map(str::to_string)
        .unwrap_or_else(|| format!("HTTP {status}"))
}

pub async fn pair(client: &reqwest::Client, url: &str, code: &str, device_name: &str) -> Result<Paired> {
    let resp = client
        .post(format!("{url}/v1/pair"))
        .json(&json!({ "code": code, "device_name": device_name }))
        .timeout(TIMEOUT)
        .send()
        .await
        .with_context(|| format!("cannot reach {url}"))?;
    match resp.status() {
        StatusCode::OK => Ok(resp.json().await.context("unexpected pairing response")?),
        StatusCode::FORBIDDEN => bail!(
            "the server rejected the pairing code: it is wrong, already used, expired (10 min), \
             or was burned after 5 wrong tries. Generate a new one in the app."
        ),
        _ => bail!("pairing failed: {}", detail(resp).await),
    }
}

pub async fn health(client: &reqwest::Client, url: &str) -> Result<Health> {
    let resp = client.get(format!("{url}/health")).timeout(TIMEOUT).send().await?;
    if !resp.status().is_success() {
        bail!("health check failed: {}", detail(resp).await);
    }
    Ok(resp.json().await?)
}

/// `None` when the server no longer accepts this device's token.
pub async fn whoami(client: &reqwest::Client, url: &str, token: &str) -> Result<Option<WhoAmI>> {
    let resp = client.get(format!("{url}/v1/whoami")).bearer_auth(token).timeout(TIMEOUT).send().await?;
    match resp.status() {
        StatusCode::OK => Ok(Some(resp.json().await?)),
        StatusCode::UNAUTHORIZED => Ok(None),
        _ => bail!("whoami failed: {}", detail(resp).await),
    }
}

/// The served model's context window, when the model is loaded.
pub async fn context_len(client: &reqwest::Client, url: &str, token: &str) -> Result<Option<u64>> {
    let resp = client.get(format!("{url}/v1/models")).bearer_auth(token).timeout(TIMEOUT).send().await?;
    if !resp.status().is_success() {
        return Ok(None);
    }
    let body: serde_json::Value = resp.json().await?;
    Ok(body["data"][0]["max_model_len"].as_u64())
}

/// Asks the server to revoke this device's token. `Ok(false)` if it was
/// already revoked.
pub async fn unpair(client: &reqwest::Client, url: &str, token: &str) -> Result<bool> {
    let resp = client.post(format!("{url}/v1/unpair")).bearer_auth(token).timeout(TIMEOUT).send().await?;
    match resp.status() {
        StatusCode::OK => Ok(true),
        StatusCode::UNAUTHORIZED => Ok(false),
        _ => bail!("unpair failed: {}", detail(resp).await),
    }
}

#[derive(Debug, Deserialize)]
pub struct SearchResult {
    pub title: String,
    pub url: String,
    pub snippet: String,
}

/// Web search through the server's private SearXNG.
pub async fn search(client: &reqwest::Client, url: &str, token: &str, query: &str, max: u8) -> Result<Vec<SearchResult>> {
    let resp = client
        .post(format!("{url}/v1/search"))
        .bearer_auth(token)
        .json(&json!({ "query": query, "max_results": max }))
        .timeout(TIMEOUT)
        .send()
        .await
        .with_context(|| format!("cannot reach {url}"))?;
    match resp.status() {
        StatusCode::OK => {
            #[derive(Deserialize)]
            struct Body {
                results: Vec<SearchResult>,
            }
            Ok(resp.json::<Body>().await?.results)
        }
        StatusCode::UNAUTHORIZED => bail!("this device was revoked; pair again"),
        _ => bail!("search failed: {}", detail(resp).await),
    }
}
