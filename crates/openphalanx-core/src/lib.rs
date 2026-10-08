//! Core logic for the Openphalanx server GUI, kept free of Tauri so it can be
//! unit-tested on machines without a desktop toolchain.

pub mod admin;
pub mod app_update;
pub mod catalog;
pub mod cluster;
pub mod docker;
pub mod download;
pub mod gpu;
pub mod model;
pub mod net;
pub mod paths;
pub mod server;
pub mod settings;
pub mod split;
pub mod vram;
