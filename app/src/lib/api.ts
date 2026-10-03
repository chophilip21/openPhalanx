// Typed wrappers around the Rust commands and events in src-tauri/src/lib.rs.
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type GpuInfo = {
  index: number;
  name: string;
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  utilization_pct: number | null;
  temperature_c: number | null;
  compute_capability: number | null;
};

export type Pairing = {
  active: boolean;
  code?: string | null;
  expires_at?: number | null;
  attempts_left?: number | null;
};

export type AdminStatus = {
  sglang: string;
  model: string;
  gateway: {
    started_at: number;
    requests_total: number;
    requests_failed: number;
    requests_active: number;
    last_request_at: number | null;
  };
  inference: {
    prompt_tokens_total?: number | null;
    generation_tokens_total?: number | null;
    cached_tokens_total?: number | null;
    cache_hit_ratio?: number | null;
    gen_throughput?: number | null;
    running_requests?: number | null;
    queued_requests?: number | null;
    token_usage?: number | null;
  };
  pairing: Pairing;
  devices: number;
  tls_fingerprint: string;
};

export type ServerState = "stopped" | "starting" | "running" | "stopping" | "error" | "external";

export type DownloadView = {
  key: string;
  done_bytes: number;
  total_bytes: number;
  bytes_per_sec: number;
  current_file: string;
  error: string | null;
  finished: boolean;
};

export type Settings = {
  selected_model: string | null;
  context_len: number;
  gpu_index: number;
  agent_port: number;
  image: string | null;
};

export type Snapshot = {
  server: { state: ServerState; detail: string | null; model_key: string | null };
  gpus: GpuInfo[];
  admin: AdminStatus | null;
  endpoint: string | null;
  downloads: DownloadView[];
  settings: Settings;
  warnings: string[];
};

export type Requirement = {
  context_len: number;
  download_bytes: number;
  /** Estimated weights once loaded (download + repacking; 2x for FP8 without native support). */
  weight_bytes: number;
  kv_bytes: number;
  overhead_bytes: number;
  total_bytes: number;
  fp8_upcast: boolean;
};

export type Fit = "ok" | "tight" | "insufficient";

export type FitCheck = {
  fit: Fit;
  required_bytes: number;
  free_bytes: number;
  headroom_bytes: number;
  message: string;
};

export type Check = {
  id: string;
  label: string;
  status: "pass" | "warn" | "fail";
  detail: string;
};

export type Preflight = {
  checks: Check[];
  can_start: boolean;
  gpu: GpuInfo | null;
  requirement: Requirement | null;
  fit: FitCheck | null;
  model: { key: string; label: string; quant: string | null } | null;
};

export type ModelRow = {
  key: string;
  name: string;
  repo: string | null;
  source_url: string | null;
  revision: string | null;
  params: string | null;
  quant: string | null;
  license: string | null;
  notes: string | null;
  tested: boolean;
  best_fit: boolean;
  custom: boolean;
  weight_bytes: number;
  max_context: number;
  requirement: Requirement;
  fit: FitCheck | null;
  installed_dir: string | null;
  app_managed: boolean;
};

export type ModelsView = {
  rows: ModelRow[];
  selected: string | null;
  context_len: number;
  gpu: GpuInfo | null;
  available_bytes: number | null;
  available_basis: string;
};

export type CustomInspect = {
  key: string;
  label: string;
  local: boolean;
  dir: string;
  repo: string | null;
  revision: string | null;
  info: { architecture: string | null; max_context: number; weight_bytes: number; quant: string | null };
  requirement: Requirement;
  fit: FitCheck | null;
};

export type Device = {
  id: string;
  name: string;
  created_at: number;
  last_seen: number | null;
  requests: number;
};

export const api = {
  snapshot: () => invoke<Snapshot>("get_snapshot"),
  preflight: () => invoke<Preflight>("get_preflight"),
  start: () => invoke<void>("start_server"),
  stop: () => invoke<void>("stop_server"),
  dismissError: () => invoke<void>("dismiss_error"),
  logs: (tail: number) => invoke<string>("get_logs", { tail }),
  newPairingCode: () => invoke<Pairing>("new_pairing_code"),
  clearPairingCode: () => invoke<Pairing>("clear_pairing_code"),
  devices: () => invoke<Device[]>("list_devices"),
  revokeDevice: (id: string) => invoke<void>("revoke_device", { id }),
  models: () => invoke<ModelsView>("get_models"),
  selectModel: (key: string) => invoke<Settings>("select_model", { key }),
  setContextLen: (contextLen: number) => invoke<Settings>("set_context_len", { contextLen }),
  setGpu: (index: number) => invoke<Settings>("set_gpu", { index }),
  download: (key: string) => invoke<void>("download_model", { key }),
  cancelDownload: (key: string) => invoke<void>("cancel_download", { key }),
  clearDownload: (key: string) => invoke<void>("clear_download", { key }),
  deleteModel: (key: string) => invoke<void>("delete_model", { key }),
  inspectCustom: (input: string) => invoke<CustomInspect>("inspect_custom", { input }),
  addCustom: (input: string) => invoke<string>("add_custom", { input }),
  removeCustom: (key: string) => invoke<Settings>("remove_custom", { key }),
};

export const events = {
  snapshot: (cb: (s: Snapshot) => void): Promise<UnlistenFn> => listen<Snapshot>("snapshot", (e) => cb(e.payload)),
  download: (cb: (d: DownloadView) => void): Promise<UnlistenFn> => listen<DownloadView>("download", (e) => cb(e.payload)),
  log: (cb: (lines: string[]) => void): Promise<UnlistenFn> => listen<string[]>("log", (e) => cb(e.payload)),
};

/** Tauri rejects with the Rust error string. */
export function errorText(e: unknown): string {
  return typeof e === "string" ? e : e instanceof Error ? e.message : JSON.stringify(e);
}
