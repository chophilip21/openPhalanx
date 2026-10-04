// Dashboard data: a rolling history of samples per server node, taken from
// each snapshot (every ~2 s), plus cluster-wide totals.
//
// Today the app manages one node (this machine). Everything here is keyed by
// node id so a cluster only adds nodes; the dashboard already renders a list.
import type { GpuInfo, ServerState, Snapshot } from "./api";

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
  name: string; // shown in the dashboard
  endpoint: string | null;
  state: ServerState;
  model: string | null;
  gpus: GpuInfo[];
  clients: number;
  requestsTotal: number;
  latest: Sample | null;
};

type Counters = { t: number; requests: number; prompt: number | null; cached: number | null };

export const metrics = $state<{ history: Record<string, Sample[]> }>({ history: {} });
const last: Record<string, Counters> = {};

export const LOCAL_NODE = "local";

/** The nodes in a snapshot. One for now: this machine. */
export function nodesOf(snap: Snapshot | null): NodeView[] {
  if (!snap) return [];
  const gpus = snap.gpus.filter((g) => g.index === snap.settings.gpu_index);
  const history = metrics.history[LOCAL_NODE] ?? [];
  return [
    {
      id: LOCAL_NODE,
      name: "This machine",
      endpoint: snap.endpoint,
      state: snap.server.state,
      model: snap.admin?.model ?? null,
      gpus: gpus.length ? gpus : snap.gpus,
      clients: snap.admin?.devices ?? 0,
      requestsTotal: snap.admin?.gateway.requests_total ?? 0,
      latest: history.at(-1) ?? null,
    },
  ];
}

export function record(snap: Snapshot) {
  const now = Date.now();
  const id = LOCAL_NODE;
  const a = snap.admin;
  const gpu = snap.gpus.find((g) => g.index === snap.settings.gpu_index) ?? snap.gpus[0] ?? null;
  const inf = a?.inference;
  const prev = last[id];
  const cur: Counters = {
    t: now,
    requests: a?.gateway.requests_total ?? 0,
    prompt: inf?.prompt_tokens_total ?? null,
    cached: inf?.cached_tokens_total ?? null,
  };
  let requestsPerMin: number | null = null;
  let cacheHit: number | null = null;
  if (a && prev && now > prev.t) {
    const dt = (now - prev.t) / 60000;
    requestsPerMin = Math.max(0, cur.requests - prev.requests) / dt;
    if (cur.prompt != null && prev.prompt != null && cur.cached != null && prev.cached != null) {
      const dp = cur.prompt - prev.prompt;
      if (dp > 0) cacheHit = Math.min(1, Math.max(0, (cur.cached - prev.cached) / dp));
    }
  }
  last[id] = cur;
  const sample: Sample = {
    t: now,
    tokensPerSec: a ? (inf?.gen_throughput ?? 0) : null,
    running: a ? (inf?.running_requests ?? a.gateway.requests_active) : null,
    queued: a ? (inf?.queued_requests ?? 0) : null,
    requestsPerMin,
    cacheHit,
    kvUsage: inf?.token_usage ?? null,
    gpuUsedBytes: gpu?.used_bytes ?? null,
    gpuTotalBytes: gpu?.total_bytes ?? null,
    gpuUtil: gpu?.utilization_pct ?? null,
    gpuTemp: gpu?.temperature_c ?? null,
  };
  // Read back through the state proxy (an `??=` result is the raw array,
  // and pushes to it wouldn't update the charts).
  if (!metrics.history[id]) metrics.history[id] = [];
  const h = metrics.history[id];
  h.push(sample);
  const cutoff = now - WINDOW_SECONDS * 1000;
  while (h.length > MAX_SAMPLES || (h.length && h[0].t < cutoff)) h.shift();
}

/** One series across all nodes, summed (or averaged) per sample index. */
export function clusterSeries(pick: (s: Sample) => number | null, mode: "sum" | "avg" = "sum"): { t: number; v: number | null }[] {
  const all = Object.values(metrics.history);
  if (!all.length) return [];
  const longest = all.reduce((a, b) => (b.length > a.length ? b : a));
  return longest.map((s, i) => {
    const vals = all.map((h) => h[h.length - longest.length + i]).filter(Boolean).map((x) => pick(x!)).filter((v): v is number => v != null);
    if (!vals.length) return { t: s.t, v: null };
    const sum = vals.reduce((a, b) => a + b, 0);
    return { t: s.t, v: mode === "sum" ? sum : sum / vals.length };
  });
}
