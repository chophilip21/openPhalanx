//! Embeds the git commit oppx was built from (shown by `oppx --version`).
fn main() {
    let commit = std::process::Command::new("git")
        .args(["rev-parse", "--short", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string())
        .unwrap_or_else(|| "unknown".into());
    println!("cargo:rustc-env=OPPX_GIT_COMMIT={commit}");
    // Set by the release workflow: the asset target `oppx --update` downloads.
    println!("cargo:rerun-if-env-changed=OPPX_RELEASE_TARGET");
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/refs/heads");
}
