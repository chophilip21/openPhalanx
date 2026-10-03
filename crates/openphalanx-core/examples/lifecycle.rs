//! End-to-end check without the GUI: start the backend exactly as the app
//! does, wait for SGLang, pair a fake client over TLS, and run one task.
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
    let run: serde_json::Value = http
        .post(format!("{base}/v1/run"))
        .bearer_auth(device_token)
        .json(&serde_json::json!({
            "prompt": "Add a function is_even(n) to util.py.",
            "files": {"util.py": "def add(a, b):\n    return a + b\n"}
        }))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    println!("exit_code {} diff:\n{}", run["exit_code"], run["diff"].as_str().unwrap_or(""));
    let status = admin.status().await?;
    println!("tasks_total={} devices={} cache_hit={:?}", status.agent.tasks_total, status.devices, status.inference.cache_hit_ratio);
    // Leave the test device out of the real device list.
    admin.revoke(paired["device_id"].as_str().unwrap()).await?;
    Ok(())
}
