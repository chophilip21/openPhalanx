//! Prints the catalog's conservative VRAM estimates for this machine's GPU:
//! `cargo run -p openphalanx-core --example catalog -- [context_len]`

use openphalanx_core::{catalog, gpu, vram};

#[tokio::main]
async fn main() {
    let ctx: u32 = std::env::args().nth(1).and_then(|s| s.parse().ok()).unwrap_or(32_768);
    let gpu = gpu::query().await.ok().and_then(|g| g.into_iter().next());
    let (free, cc) = gpu.as_ref().map_or((None, None), |g| (Some(g.free_bytes), g.compute_capability));
    if let Some(g) = &gpu {
        println!("{}: {} free, compute capability {:?}, context {ctx}\n", g.name, vram::fmt_gib(g.free_bytes), cc);
    }
    println!("{:<44} {:>9} {:>9} {:>9} {:>9} {:>9}  fit", "model", "download", "weights", "KV", "runtime", "TOTAL");
    for e in catalog::catalog() {
        let r = vram::requirement(e.weight_bytes, Some(&e.quant), &e.arch, ctx.min(e.max_context), cc);
        let fit = free.map(|f| format!("{:?}", vram::check(&r, f).fit)).unwrap_or_default();
        let g = |b: u64| format!("{:.1}", b as f64 / vram::GIB as f64);
        println!(
            "{:<44} {:>9} {:>9} {:>9} {:>9} {:>9}  {fit}{}",
            format!("{} {}", e.name, e.quant),
            g(r.download_bytes), g(r.weight_bytes), g(r.kv_bytes), g(r.overhead_bytes), g(r.total_bytes),
            if r.fp8_upcast { "  (FP8 counted as 16-bit)" } else { "" }
        );
    }
}
