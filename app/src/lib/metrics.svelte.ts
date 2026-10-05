// Dashboard data: a rolling history of samples per server, taken from each
// snapshot (every ~2 s). This machine samples its own backend and GPUs; as a
// cluster host it also samples every member from the reports members send.
// The dashboard charts one machine at a time (a dropdown), so a cluster
// doesn't pile every machine into the same charts.
import type { AdminStatus, GpuInfo, ModelSync, ServerState, Snapshot } from "./api";

export const WINDOW_SECONDS = 600; // history kept for the charts
const MAX_SAMPLES = 400;

export type Sample = {
  t: number; // ms since epoch
  tokensPerSec: number | null;
  running: number | null; // requests being generated
  queued: number | null; // requests waiting in SGLang's queue
  requestsPerMin: number | null;
  cacheHit: number | null; // share of prompt tokens served from cache in this interval
  kvUsage: number | null; // KV cache in use (0..1)
  gpuUsedBytes: number | null;
  gpuTotalBytes: number | null;
  gpuUtil: number | null;
  gpuTemp: number | null;
};

export type NodeView = {
  id: string;
  name: string;
  /** This machine, a member of its cluster, or (seen from a member) its host. */
  role: "this" | "member" | "host";
  /** Reachable: this machine always; a member while it reports. */
  online: boolean;
  /** Inference on that machine. */
  state: ServerState;
  endpoint: string | null;
  /** Short facts, e.g. "16 CPUs · 15 GiB RAM". */
  detail: string | null;
  model: string | null;
  gpus: GpuInfo[];
  clients: number;
  requestsTotal: number;
  /** Whether this machine has samples for it (charts). */
  charted: boolean;
  /** A member's copy of the cluster's model. */
  sync: ModelSync | null;
};

type Counters = { t: number; requests: number; prompt: number | null; cached: number | null };

export const metrics = $state<{ history: Record<string, Sample[]> }>({ history: {} });
const last: Record<string, Counters> = {};

export const LOCAL_NODE = "local";

const facts = (inv: { cpus: number; memory_bytes: number; docker_version: string | null } | undefined) =>
  inv ? `${inv.cpus} CPUs · ${Math.round(inv.memory_bytes / 2 ** 30)} GiB RAM` + (inv.docker_version ? "" : " · no Docker") : null;

/** The machines in a snapshot: this one, then its members (as host) or its host (as member). */
export function nodesOf(snap: Snapshot | null): NodeView[] {
  if (!snap) return [];
  const gpus = snap.gpus.filter((g) => g.index === snap.settings.gpu_index);
  const c = snap.cluster;
  const self: NodeView = {
    id: LOCAL_NODE,
    name: c ? `${c.name} (this machine)` : "This machine",
    role: "this",
    online: true,
    detail: null,
    endpoint: snap.endpoint,
    state: snap.server.state,
    model: snap.admin?.model ?? null,
    gpus: gpus.length ? gpus : snap.gpus,
    clients: snap.admin?.devices ?? 0,
    requestsTotal: snap.admin?.gateway.requests_total ?? 0,
    charted: true,
    sync: c?.role.role === "member" ? c.model_sync : null,
  };
  const members: NodeView[] = (c?.members ?? []).map((m) => {
    const serving = m.report?.serving ?? null;
    return {
      id: m.id,
      name: m.name,
      role: "member",
      online: m.online,
      state: serving ? (serving.sglang === "ready" ? "running" : "starting") : "stopped",
      endpoint: m.url.replace(/^https:\/\//, ""),
      detail: facts(m.report?.inventory) ?? "hasn't reported yet",
      model: serving?.model ?? null,
      gpus: m.online ? (m.report?.inventory.gpus ?? []) : [],
      clients: 0,
      requestsTotal: serving?.gateway.requests_total ?? 0,
      charted: true,
      sync: m.report?.model_sync ?? null,
    };
  });
  const host: NodeView[] =
    c?.role.role === "member"
      ? [
          {
            id: `host:${c.role.host.id}`,
            name: c.role.host.name,
            role: "host",
            online: c.host_link?.connected ?? false,
            state: "stopped",
            endpoint: c.role.host.url.replace(/^https:\/\//, ""),
            detail: c.host_link?.connected ? "cluster host · pairs the clients" : (c.host_link?.error ?? "connecting…"),
            model: null,
            gpus: [],
            clients: 0,
            requestsTotal: 0,
            charted: false,
            sync: null,
          },
        ]
      : [];
  return [self, ...host, ...members];
}

function sample(
  id: string,
  now: number,
  gateway: AdminStatus["gateway"] | null,
  inf: AdminStatus["inference"] | null,
  gpus: GpuInfo[],
): Sample {
  const prev = last[id];
  const cur: Counters = {
    t: now,
    requests: gateway?.requests_total ?? 0,
    prompt: inf?.prompt_tokens_total ?? null,
    cached: inf?.cached_tokens_total ?? null,
  };
  let requestsPerMin: number | null = null;
  let cacheHit: number | null = null;
  if (gateway && prev && now > prev.t) {
    const dt = (now - prev.t) / 60000;
    requestsPerMin = Math.max(0, cur.requests - prev.requests) / dt;
    if (cur.prompt != null && prev.prompt != null && cur.cached != null && prev.cached != null) {
      const dp = cur.prompt - prev.prompt;
      if (dp > 0) cacheHit = Math.min(1, Math.max(0, (cur.cached - prev.cached) / dp));
    }
  }
  last[id] = cur;
  const sum = (f: (g: GpuInfo) => number) => (gpus.length ? gpus.reduce((a, g) => a + f(g), 0) : null);
  const avg = (f: (g: GpuInfo) => number | null) => {
    const v = gpus.map(f).filter((x): x is number => x != null);
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  };
  return {
    t: now,
    tokensPerSec: gateway ? (inf?.gen_throughput ?? 0) : null,
    running: gateway ? (inf?.running_requests ?? gateway.requests_active) : null,
    queued: gateway ? (inf?.queued_requests ?? 0) : null,
    requestsPerMin,
    cacheHit,
    kvUsage: inf?.token_usage ?? null,
    gpuUsedBytes: sum((g) => g.used_bytes),
    gpuTotalBytes: sum((g) => g.total_bytes),
    gpuUtil: avg((g) => g.utilization_pct),
    gpuTemp: avg((g) => g.temperature_c),
  };
}

function push(id: string, s: Sample) {
  // Read back through the state proxy (an `??=` result is the raw array,
  // and pushes to it wouldn't update the charts).
  if (!metrics.history[id]) metrics.history[id] = [];
  const h = metrics.history[id];
  h.push(s);
  const cutoff = s.t - WINDOW_SECONDS * 1000;
  while (h.length > MAX_SAMPLES || (h.length && h[0].t < cutoff)) h.shift();
}

export function record(snap: Snapshot) {
  const now = Date.now();
  const a = snap.admin;
  const gpus = snap.gpus.filter((g) => g.index === snap.settings.gpu_index);
  push(LOCAL_NODE, sample(LOCAL_NODE, now, a?.gateway ?? null, a?.inference ?? null, gpus.length ? gpus : snap.gpus.slice(0, 1)));
  for (const m of snap.cluster?.members ?? []) {
    if (!m.online || !m.report) continue;
    const s = m.report.serving;
    push(m.id, sample(m.id, now, s?.gateway ?? null, s?.inference ?? null, m.report.inventory.gpus));
  }
}

/** One machine's history for a chart. */
export function nodeSeries(id: string, pick: (s: Sample) => number | null): { t: number; v: number | null }[] {
  return (metrics.history[id] ?? []).map((s) => ({ t: s.t, v: pick(s) }));
}
