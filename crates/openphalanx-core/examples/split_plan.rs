//! How the selected model would be split across servers, using the app's own
//! settings and planner:
//! `cargo run -p openphalanx-core --example split_plan -- <name>=<free GiB> [<name>=<free GiB> …]`
//! (the host first).

use openphalanx_core::{server, settings::Settings, split};

fn main() -> anyhow::Result<()> {
    let settings = Settings::load();
    let key = settings.selected_model.clone().ok_or_else(|| anyhow::anyhow!("no model selected"))?;
    let m = server::resolve(&settings, &key).ok_or_else(|| anyhow::anyhow!("unknown model {key}"))?;
    let dir = m.installed_dir.clone().ok_or_else(|| anyhow::anyhow!("{} isn't downloaded", m.label))?;
    let req = m.requirement(settings.context_len, Some(8.6));
    let shape = split::ModelShape::read(&dir)?;
    let nodes: Vec<split::Capacity> = std::env::args()
        .skip(1)
        .map(|a| {
            let (name, gib) = a.split_once('=').expect("<name>=<free GiB>");
            let free_bytes = (gib.parse::<f64>().expect("GiB") * (1u64 << 30) as f64) as u64;
            split::Capacity { id: name.into(), name: name.into(), free_bytes }
        })
        .collect();
    println!("{} at {} context: {:?}", m.label, settings.context_len, req);
    println!("{shape:?}");
    match split::plan(&req, &shape, &nodes) {
        Ok(stages) => {
            println!("{}", split::describe(&stages));
            println!("SGLANG_PP_LAYER_PARTITION={}", split::partition(&stages));
        }
        Err(e) => println!("can't split: {e}"),
    }
    Ok(())
}
