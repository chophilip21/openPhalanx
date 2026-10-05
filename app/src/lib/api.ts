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
    web_searches: number;
    auto_routed: number;
    auto_searched: number;
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
  web_search: boolean;
};

export type ServerState = "stopped" | "starting" | "running" | "stopping" | "error" | "paused" | "external";

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
  web_search: boolean;
  /** Days a client pairing lasts; null: never expires. */
  pairing_ttl_days: number | null;
};

/** A server's hardware, as it reports it. */
export type NodeInventory = {
  hostname: string;
  os: string;
  arch: string;
  cpus: number;
  memory_bytes: number;
  gpus: GpuInfo[];
  docker_version: string | null;
  docker_error: string | null;
  nvidia_runtime: boolean;
  version: string;
};

/** What a member serves (its backend's stats). */
export type Serving = {
  model: string;
  sglang: string;
  gateway: AdminStatus["gateway"];
  inference: AdminStatus["inference"];
};

export type Peer = { id: string; name: string; url: string; fingerprint: string };

/** The model a host serves, which its members keep on disk too. */
export type ModelSpec = { key: string; label: string; repo: string; revision: string; weight_bytes: number };

/** A member's copy of the host's model. */
export type ModelSync = {
  label: string;
  repo: string;
  revision: string;
  /** "missing": not on that machine; it waits for the host to approve a download. */
  state: "checking" | "missing" | "downloading" | "ready" | "error";
  done_bytes: number;
  total_bytes: number;
  bytes_per_sec: number;
  error: string | null;
};

export type ClusterRole = { role: "standalone" } | { role: "host" } | { role: "member"; host: Peer };

export type ClusterMember = {
  id: string;
  name: string;
  url: string;
  fingerprint: string;
  joined_at: number;
  last_seen: number | null;
  online: boolean;
  report: { inventory: NodeInventory; serving: Serving | null; model_sync: ModelSync | null } | null;
};

export type ClusterCandidate = {
  id: string;
  name: string;
  url: string;
  fingerprint: string;
  role: string;
  busy: boolean;
  version: string;
};

/** How a cluster uses its GPUs (the host decides). */
export type ClusterStrategy = "split" | "replicas";

export type ClusterState = {
  id: string;
  name: string;
  url: string | null;
  fingerprint: string;
  port: number;
  role: ClusterRole;
  strategy: ClusterStrategy;
  members: ClusterMember[];
  candidates: ClusterCandidate[];
  invites: { host: Peer; invite_id: string; received_at: number }[];
  host_requests: { from: Peer; members: Peer[]; received_at: number }[];
  host_link: { connected: boolean; last_ok: number | null; error: string | null } | null;
  discovery_error: string | null;
  desired_model: ModelSpec | null;
  /** As host: members that lack the model may download it. */
  download_approved: boolean;
  model_sync: ModelSync | null;
  /** This machine is a member and the host is serving: it can't start a server. */
  locked_by_host: boolean;
  error: string | null;
};

/** What the backend is serving (or loading); fixed until the server stops. */
export type RunningModel = {
  key: string;
  label: string;
  quant: string | null;
  context_len: number | null;
  /** Split across the cluster: "rig-3090: 40 layers (14.2 GiB) · laptop-4090: …". */
  split: string | null;
};

export type Snapshot = {
  server: { state: ServerState; detail: string | null; model_key: string | null; model: RunningModel | null };
  gpus: GpuInfo[];
  admin: AdminStatus | null;
  endpoint: string | null;
  downloads: DownloadView[];
  settings: Settings;
  warnings: string[];
  cluster: ClusterState | null;
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
  /** The backend is already running. */
  running: boolean;
  gpu: GpuInfo | null;
  requirement: Requirement | null;
  fit: FitCheck | null;
  model: { key: string; label: string; quant: string | null } | null;
};

export type ModelRow = {
  key: string;
  name: string;
  /** Catalog family ("Qwen", "Gemma", …); null for custom models. */
  family: string | null;
  /** Who made a community quantization; null for official repos. */
  quantized_by: string | null;
  /** Longest context that fits the available VRAM; null if not even 2k does. */
  max_fit_context: number | null;
  repo: string | null;
  source_url: string | null;
  revision: string | null;
  params: string | null;
  quant: string | null;
  license: string | null;
  /** Year the model was published, e.g. "2025". */
  released: string | null;
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
  /** Split cluster: the VRAM each server adds (available_bytes is the sum). */
  pool: PoolNode[] | null;
};

export type PoolNode = {
  id: string;
  name: string;
  this: boolean;
  gpu: string | null;
  available_bytes: number;
  total_bytes: number;
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
  prompt_tokens: number;
  completion_tokens: number;
  web_searches: number;
  /** When its pairing expires (unix seconds); null: never. */
  expires_at: number | null;
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
  setPairingTtl: (days: number | null) => invoke<Settings>("set_pairing_ttl", { days }),
  models: () => invoke<ModelsView>("get_models"),
  selectModel: (key: string) => invoke<Settings>("select_model", { key }),
  setContextLen: (contextLen: number) => invoke<Settings>("set_context_len", { contextLen }),
  setGpu: (index: number) => invoke<Settings>("set_gpu", { index }),
  setWebSearch: (enabled: boolean) => invoke<Settings>("set_web_search", { enabled }),
  download: (key: string) => invoke<void>("download_model", { key }),
  cancelDownload: (key: string) => invoke<void>("cancel_download", { key }),
  clearDownload: (key: string) => invoke<void>("clear_download", { key }),
  deleteModel: (key: string) => invoke<void>("delete_model", { key }),
  inspectCustom: (input: string) => invoke<CustomInspect>("inspect_custom", { input }),
  addCustom: (input: string) => invoke<string>("add_custom", { input }),
  removeCustom: (key: string) => invoke<Settings>("remove_custom", { key }),
  clusterInvite: (id: string) => invoke<void>("cluster_invite", { id }),
  clusterMakeHost: (id: string) => invoke<void>("cluster_make_host", { id }),
  clusterApproveInvite: (hostId: string) => invoke<void>("cluster_approve_invite", { hostId }),
  clusterApproveHost: (fromId: string) => invoke<string[]>("cluster_approve_host", { fromId }),
  clusterDecline: (id: string) => invoke<void>("cluster_decline", { id }),
  clusterRemove: (id: string) => invoke<void>("cluster_remove", { id }),
  clusterLeave: () => invoke<void>("cluster_leave"),
  clusterDissolve: () => invoke<void>("cluster_dissolve"),
  clusterRename: (name: string) => invoke<void>("cluster_rename", { name }),
  clusterMemberLogs: (id: string) => invoke<string[]>("cluster_member_logs", { id }),
  clusterSetStrategy: (strategy: ClusterStrategy) => invoke<void>("cluster_set_strategy", { strategy }),
  clusterApproveDownload: () => invoke<void>("cluster_approve_download"),
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
