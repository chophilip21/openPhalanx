//! Prints the pre-flight report for the current settings:
//! `cargo run -p openphalanx-core --example preflight`

use openphalanx_core::{server, settings::Settings, vram};

#[tokio::main]
async fn main() {
    let settings = Settings::load();
    let pf = server::preflight(&settings).await;
    for c in &pf.checks {
        println!("{:<5} {:<26} {}", format!("{:?}", c.status).to_uppercase(), c.label, c.detail);
    }
    if let (Some(m), Some(r)) = (&pf.model, &pf.requirement) {
        println!(
            "\n{}: weights {} + KV {} ({} tokens) + overhead {} = {}",
            m.label,
            vram::fmt_gib(r.weight_bytes),
            vram::fmt_gib(r.kv_bytes),
            r.context_len,
            vram::fmt_gib(r.overhead_bytes),
            vram::fmt_gib(r.total_bytes)
        );
        println!("installed at: {:?}", m.installed_dir);
    }
    println!("can_start: {}", pf.can_start);
}
