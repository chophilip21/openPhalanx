//! GPU inventory via `nvidia-smi`.

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};
use tokio::process::Command;

const MIB: u64 = 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct GpuInfo {
    pub index: u32,
    pub name: String,
    pub total_bytes: u64,
    pub used_bytes: u64,
    pub free_bytes: u64,
    pub utilization_pct: Option<u32>,
    pub temperature_c: Option<u32>,
    pub compute_capability: Option<f32>,
}

const QUERY: &str =
    "index,name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,compute_cap";

/// How long `nvidia-smi` may take. It answers in about 50 ms; one that
/// doesn't is stuck in the driver, and is killed instead of waited for.
const QUERY_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(8);

pub async fn query() -> Result<Vec<GpuInfo>> {
    // The app asks every couple of seconds, from its monitor and from every
    // page that shows VRAM. Without a limit, a driver that stops answering
    // blocks the monitor (so nothing feeds the backend's watchdog, and a
    // cluster member stops reporting) and leaves one more stuck process
    // behind with each question from a page.
    let mut cmd = Command::new("nvidia-smi");
    cmd.args([&format!("--query-gpu={QUERY}"), "--format=csv,noheader,nounits"]).kill_on_drop(true);
    let out = tokio::time::timeout(QUERY_TIMEOUT, cmd.output())
        .await
        .map_err(|_| anyhow::anyhow!("nvidia-smi did not answer in {} s; the GPU driver may be stuck", QUERY_TIMEOUT.as_secs()))?
        .context("nvidia-smi not found; is the NVIDIA driver installed?")?;
    if !out.status.success() {
        bail!("nvidia-smi failed: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    parse(&String::from_utf8_lossy(&out.stdout))
}

pub fn parse(csv: &str) -> Result<Vec<GpuInfo>> {
    csv.lines()
        .filter(|l| !l.trim().is_empty())
        .map(|line| {
            let f: Vec<&str> = line.split(',').map(str::trim).collect();
            if f.len() < 8 {
                bail!("unexpected nvidia-smi line: {line}");
            }
            let mib = |s: &str| -> Result<u64> { Ok(s.parse::<u64>()? * MIB) };
            Ok(GpuInfo {
                index: f[0].parse()?,
                name: f[1].to_string(),
                total_bytes: mib(f[2])?,
                used_bytes: mib(f[3])?,
                free_bytes: mib(f[4])?,
                utilization_pct: f[5].parse().ok(),
                temperature_c: f[6].parse().ok(),
                compute_capability: f[7].parse().ok(),
            })
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_nvidia_smi_csv() {
        let gpus = parse("0, NVIDIA GeForce RTX 3090, 24576, 512, 23651, 3, 41, 8.6\n").unwrap();
        assert_eq!(gpus.len(), 1);
        let g = &gpus[0];
        assert_eq!(g.name, "NVIDIA GeForce RTX 3090");
        assert_eq!(g.total_bytes, 24576 * MIB);
        assert_eq!(g.free_bytes, 23651 * MIB);
        assert_eq!(g.compute_capability, Some(8.6));
    }

    #[test]
    fn tolerates_not_supported_fields() {
        let gpus = parse("1, Tesla T4, 15360, 0, 15360, [N/A], [N/A], 7.5").unwrap();
        assert_eq!(gpus[0].utilization_pct, None);
        assert_eq!(gpus[0].index, 1);
    }
}
