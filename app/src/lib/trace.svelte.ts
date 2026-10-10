// The gateway's request trace, kept across page switches: polled while the
// Trace page is open and a backend runs. Records are merged by sequence number
// (an unfinished one is sent again until it finishes).
import { api, type TraceBatch, type TraceNode, type TracePool, type TraceRecord } from "./api";

export const trace = $state<{
  records: TraceRecord[];
  latest: number;
  content: boolean;
  context: number;
  pool: TracePool;
  nodes: TraceNode[];
  startedAt: number;
  error: string;
  loaded: boolean;
}>({ records: [], latest: 0, content: false, context: 0, pool: {}, nodes: [], startedAt: 0, error: "", loaded: false });

const MAX = 2000;

function merge(batch: TraceBatch) {
  if (batch.started_at !== trace.startedAt || batch.latest < trace.latest) {
    // Another backend (a restart), or the trace was cleared: start over.
    trace.records = [];
    trace.startedAt = batch.started_at;
  }
  const at = new Map(trace.records.map((r, i) => [r.seq, i]));
  for (const r of batch.records) {
    const i = at.get(r.seq);
    if (i == null) trace.records.push(r);
    else trace.records[i] = r;
  }
  if (trace.records.length > MAX) trace.records.splice(0, trace.records.length - MAX);
  trace.latest = batch.latest;
  trace.content = batch.content;
  trace.context = batch.context;
  trace.pool = batch.pool ?? {};
}

export async function refreshTrace() {
  try {
    // Unfinished records come back whatever `after` is.
    merge(await api.trace(trace.records.length ? trace.latest : 0));
    trace.error = "";
  } catch (e) {
    trace.error = String(e);
  }
  trace.loaded = true;
}

export async function refreshTree() {
  try {
    trace.nodes = (await api.traceTree()).nodes;
  } catch {
    /* the records poll reports problems */
  }
}

export function resetTrace() {
  trace.records = [];
  trace.nodes = [];
  trace.latest = 0;
}

/** A turn of an agent session (or one unlabelled request), as the page lists it. */
export type Turn = {
  key: string;
  label: string;
  device: string;
  at: number;
  records: TraceRecord[];
  durationMs: number;
  peakPrompt: number;
  prompt: number;
  cached: number;
  output: number;
  events: Record<string, number>;
  running: boolean;
  failed: number;
};

export function turns(records: TraceRecord[]): Turn[] {
  const by = new Map<string, TraceRecord[]>();
  for (const r of records) {
    const key = r.session && r.turn != null ? `${r.session}:${r.turn}` : `#${r.seq}`;
    (by.get(key) ?? by.set(key, []).get(key)!).push(r);
  }
  const out: Turn[] = [];
  for (const [key, recs] of by) {
    recs.sort((a, b) => a.seq - b.seq);
    const first = recs[0];
    const end = Math.max(...recs.map((r) => r.at * 1000 + (r.total_ms ?? 0) + (r.tool_ms ?? 0)));
    const events: Record<string, number> = {};
    for (const r of recs) for (const [k, v] of Object.entries(r.events ?? {})) events[k] = (events[k] ?? 0) + v;
    out.push({
      key,
      label: first.session ? `Turn ${first.turn}` : "Request",
      device: first.device_name || first.device,
      at: first.at,
      records: recs,
      durationMs: Math.max(0, end - first.at * 1000),
      peakPrompt: Math.max(0, ...recs.map((r) => r.prompt_tokens ?? 0)),
      prompt: recs.reduce((n, r) => n + (r.prompt_tokens ?? 0), 0),
      cached: recs.reduce((n, r) => n + (r.cached_tokens ?? 0), 0),
      output: recs.reduce((n, r) => n + (r.completion_tokens ?? 0), 0),
      events,
      running: recs.some((r) => r.state === "queued" || r.state === "running"),
      failed: recs.filter((r) => r.state === "failed").length,
    });
  }
  return out.sort((a, b) => b.at - a.at);
}

/** Milliseconds the model takes to write one token, from the streamed
 *  requests (the only ones whose first token is timed). Null without any. */
export function msPerToken(records: TraceRecord[]): number | null {
  const rates = records
    .filter((r) => r.ttft_ms != null && r.total_ms != null && (r.completion_tokens ?? 0) >= 8)
    .map((r) => (r.total_ms! - r.ttft_ms!) / r.completion_tokens!)
    .filter((v) => v > 0)
    .sort((a, b) => a - b);
  return rates.length ? rates[Math.floor(rates.length / 2)] : null;
}

/** A request's time split: waiting for a slot, the model reading the prompt,
 *  the model writing. Measured when streamed; otherwise the writing part is
 *  estimated from the tokens written (`estimated`). */
export function phases(r: TraceRecord, rate: number | null) {
  const total = r.total_ms ?? 0;
  const queue = Math.min(total, r.queue_ms ?? 0);
  const model = total - queue;
  if (r.ttft_ms != null) {
    const read = Math.max(0, Math.min(model, r.ttft_ms - queue));
    return { queue, read, write: model - read, estimated: false, split: true };
  }
  if (rate != null && r.completion_tokens != null) {
    const write = Math.min(model, r.completion_tokens * rate);
    return { queue, read: model - write, write, estimated: true, split: true };
  }
  return { queue, read: model, write: 0, estimated: false, split: false };
}

export const median = (values: number[]) => {
  const v = values.filter((x) => Number.isFinite(x)).sort((a, b) => a - b);
  return v.length ? v[Math.floor(v.length / 2)] : null;
};
