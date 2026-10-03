//! End-to-end check without the GUI: start the backend exactly as the app
//! does, wait for SGLang, pair a fake client over TLS, and check its token.
//! `cargo run -p openphalanx-core --example lifecycle`

use std::time::Duration;

use openphalanx_core::{admin::AdminClient, docker, server, settings::Settings};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let settings = Settings::load();
    let pf = server::preflight(&settings).await;
    for c in &pf.checks {
        println!("[{:?}] {}: {}", c.status, c.label, c.detail);
    }
    server::start(&settings, |p| println!("progress: {p:?}")).await?;

    let token = docker::inspect().await?.and_then(|c| c.admin_token).expect("admin token");
    let admin = AdminClient::new(token);
    let t0 = std::time::Instant::now();
    loop {
        match admin.status().await {
            Ok(s) if s.sglang == "ready" => break,
            Ok(_) | Err(_) => tokio::time::sleep(Duration::from_secs(5)).await,
        }
        anyhow::ensure!(t0.elapsed() < Duration::from_secs(900), "SGLang not ready after 15 minutes");
        if let Some(c) = docker::inspect().await? {
            anyhow::ensure!(c.state.running, "container exited: {:?}", c.state);
        }
    }
    println!("sglang ready after {:.0}s", t0.elapsed().as_secs_f64());

    let pairing = admin.new_pairing().await?;
    let code = pairing.code.clone().unwrap();
    println!("pairing code {code}");
    let http = reqwest::Client::builder().danger_accept_invalid_certs(true).build()?;
    let base = format!("https://127.0.0.1:{}", settings.agent_port);
    let paired: serde_json::Value = http
        .post(format!("{base}/v1/pair"))
        .json(&serde_json::json!({"code": code, "device_name": "lifecycle-test"}))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let device_token = paired["token"].as_str().unwrap();
    let whoami = |token: String| {
        let (http, base) = (http.clone(), base.clone());
        async move { http.get(format!("{base}/v1/whoami")).bearer_auth(token).send().await }
    };
    let me: serde_json::Value = whoami(device_token.into()).await?.error_for_status()?.json().await?;
    println!("whoami with device token: {me}");
    println!("whoami with bad token: HTTP {}", whoami("bogus".into()).await?.status());

    // Inference proxy.
    let marker = "ZEBRA-MARKER-7731"; // must never appear in the container log
    let chat = |token: &str, body: serde_json::Value| {
        http.post(format!("{base}/v1/chat/completions")).bearer_auth(token.to_string()).json(&body).send()
    };
    let unauth = http.post(format!("{base}/v1/chat/completions")).json(&serde_json::json!({})).send().await?;
    println!("chat without token: HTTP {}", unauth.status());
    let models: serde_json::Value =
        http.get(format!("{base}/v1/models")).bearer_auth(device_token).send().await?.error_for_status()?.json().await?;
    println!("models: {}", models["data"][0]["id"]);
    let messages = serde_json::json!([{"role": "user", "content": format!("Reply with one word: ok. {marker}")}]);
    let r: serde_json::Value = chat(device_token, serde_json::json!({"model": "gpt-4", "messages": messages, "max_tokens": 5}))
        .await?
        .error_for_status()?
        .json()
        .await?;
    println!("non-stream: model={} usage={}", r["model"], r["usage"]);
    let t0 = std::time::Instant::now();
    let sse = chat(device_token, serde_json::json!({"messages": messages, "max_tokens": 20, "stream": true,
        "stream_options": {"include_usage": true}}))
        .await?
        .error_for_status()?
        .text()
        .await?;
    let chunks = sse.lines().filter(|l| l.starts_with("data: {")).count();
    println!("stream with include_usage: {chunks} chunks, usage events = {}, ends with [DONE]: {}, {:.2}s", sse.lines().filter(|l| l.contains("\"usage\":{")).count(), sse.trim_end().ends_with("data: [DONE]"), t0.elapsed().as_secs_f64());
    let plain = chat(device_token, serde_json::json!({"messages": messages, "max_tokens": 20, "stream": true}))
        .await?
        .error_for_status()?
        .text()
        .await?;
    println!(
        "stream without include_usage: usage events seen by client = {}, ends with [DONE]: {}",
        plain.lines().filter(|l| l.contains("\"usage\":{")).count(),
        plain.trim_end().ends_with("data: [DONE]")
    );
    let big = "x".repeat(5 * 1024 * 1024);
    let r = chat(device_token, serde_json::json!({"messages": [{"role": "user", "content": big}]})).await?;
    println!("5 MiB body: HTTP {}", r.status());
    let dev = admin.devices().await?.into_iter().find(|d| d.id == paired["device_id"].as_str().unwrap()).unwrap();
    println!("device usage: requests={} prompt_tokens={} completion_tokens={}", dev.requests, dev.prompt_tokens, dev.completion_tokens);
    let logs = docker::logs_tail(100_000).await?;
    println!("prompt marker in container log: {} occurrence(s)", logs.matches(marker).count());
    let st = admin.status().await?;
    println!("gateway: requests_total={} failed={} active={}", st.gateway.requests_total, st.gateway.requests_failed, st.gateway.requests_active);

    // Leave the test device out of the real device list.
    admin.revoke(paired["device_id"].as_str().unwrap()).await?;
    println!("whoami after revoke: HTTP {}", whoami(device_token.into()).await?.status());
    let status = admin.status().await?;
    println!("devices={} cache_hit={:?}", status.devices, status.inference.cache_hit_ratio);
    Ok(())
}
