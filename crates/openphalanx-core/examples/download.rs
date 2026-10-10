//! Downloads a Hugging Face model the way the GUI does:
//! `cargo run -p openphalanx-core --example download -- <repo> <dest> [cancel_after_mb]`

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

use openphalanx_core::download;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let (repo, dest) = (&args[1], std::path::PathBuf::from(&args[2]));
    let cancel_after: Option<u64> = args.get(3).map(|s| s.parse::<u64>().unwrap() * 1_000_000);
    let client = download::client();
    let sha = download::resolve_revision(&client, repo, "main").await?;
    let (info, files) = download::inspect_remote(&client, repo, &sha).await?;
    println!("{repo}@{} -> {} files, {} weight bytes, arch {:?}", &sha[..7], files.len(), info.weight_bytes, info.arch);
    let cancel = Arc::new(AtomicBool::new(false));
    let flag = cancel.clone();
    let mut last = 0u64;
    let result = download::download(&client, repo, &sha, &files, &dest, cancel, |p| {
        if cancel_after.is_some_and(|n| p.done_bytes >= n) {
            flag.store(true, Ordering::Relaxed);
        }
        // Progress as the GUI sees it; it must never go backwards.
        if p.done_bytes < last {
            println!("PROGRESS WENT BACK: {last} -> {} ({})", p.done_bytes, p.current_file);
        }
        last = p.done_bytes;
        println!("{:>5.1}% {:>12}/{} {}", 100.0 * p.done_bytes as f64 / p.total_bytes.max(1) as f64, p.done_bytes, p.total_bytes, p.current_file);
    })
    .await;
    match result {
        Ok(dir) => println!("complete: {}", dir.display()),
        Err(e) => println!("stopped: {e:#}"),
    }
    Ok(())
}
