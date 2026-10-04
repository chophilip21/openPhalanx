<script lang="ts">
  // Server statistics under the power button: cluster-wide tiles, one card
  // per node, then time-series panels. Built on a list of nodes so a cluster
  // of servers only adds entries (today: this machine).
  import Chart from "./Chart.svelte";
  import Icon from "./Icon.svelte";
  import { clusterSeries, nodesOf, WINDOW_SECONDS } from "../lib/metrics.svelte";
  import { gib, pct } from "../lib/format";
  import { app } from "../lib/store.svelte";

  const snap = $derived(app.snapshot);
  const nodes = $derived(nodesOf(snap));
  const online = $derived(nodes.filter((n) => n.state === "running").length);
  const admin = $derived(snap?.admin ?? null);

  const series = (pick: Parameters<typeof clusterSeries>[0], mode: "sum" | "avg" = "sum") =>
    clusterSeries(pick, mode);

  const tps = $derived(series((s) => s.tokensPerSec));
  const running = $derived(series((s) => s.running));
  const queued = $derived(series((s) => s.queued));
  const rpm = $derived(series((s) => s.requestsPerMin));
  const hit = $derived(series((s) => (s.cacheHit == null ? null : s.cacheHit * 100), "avg"));
  const kv = $derived(series((s) => (s.kvUsage == null ? null : s.kvUsage * 100), "avg"));
  const util = $derived(series((s) => s.gpuUtil, "avg"));
  const temp = $derived(series((s) => s.gpuTemp, "avg"));

  const lastOf = (pts: { v: number | null }[]) => [...pts].reverse().find((p) => p.v != null)?.v ?? null;
  const fmt = (v: number | null, digits = 0, unit = "") => (v == null ? "–" : `${v.toFixed(digits)}${unit}`);

  const gpuTotal = $derived(nodes.flatMap((n) => n.gpus).reduce((a, g) => a + g.total_bytes, 0));
  const gpuUsedNow = $derived(nodes.flatMap((n) => n.gpus).reduce((a, g) => a + g.used_bytes, 0));
  const clients = $derived(nodes.reduce((a, n) => a + n.clients, 0));

  const STATE_LABEL: Record<string, string> = {
    running: "Online",
    starting: "Starting",
    stopping: "Stopping",
    stopped: "Offline",
    error: "Error",
    external: "External",
  };
</script>

<section class="dash">
  <div class="dash-head">
    <div>
      <span class="eyebrow">Cluster</span>
      <h2>{nodes.length} node{nodes.length === 1 ? "" : "s"} · {online} online</h2>
    </div>
    <span class="muted small">Live · updates every 2 s · last {WINDOW_SECONDS / 60} min</span>
  </div>

  <div class="tiles">
    <div class="tile"><span class="k">Nodes online</span><span class="v">{online}<small>/{nodes.length}</small></span></div>
    <div class="tile"><span class="k">Paired clients</span><span class="v">{clients}</span></div>
    <div class="tile"><span class="k">Tokens / s</span><span class="v">{fmt(lastOf(tps))}</span></div>
    <div class="tile"><span class="k">Requests / min</span><span class="v">{fmt(lastOf(rpm))}</span></div>
    <div class="tile">
      <span class="k">Prefix cache hit</span>
      <span class="v">{pct(admin?.inference.cache_hit_ratio)}</span>
    </div>
    <div class="tile">
      <span class="k">GPU memory</span>
      <span class="v">{gpuTotal ? `${Math.round((100 * gpuUsedNow) / gpuTotal)}%` : "–"}</span>
      {#if gpuTotal}<span class="k2">{gib(gpuUsedNow)} of {gib(gpuTotal)}</span>{/if}
    </div>
  </div>

  <div class="card table-card">
    <div class="table-head"><span class="eyebrow">Nodes</span></div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr><th>Node</th><th>State</th><th>Model</th><th>GPU</th><th class="vram-col">VRAM</th><th>Load</th><th>Clients</th><th>Requests</th></tr>
        </thead>
        <tbody>
          {#each nodes as n (n.id)}
            {#each n.gpus.length ? n.gpus : [null] as g, gi}
              <tr>
                {#if gi === 0}
                  <td rowspan={Math.max(1, n.gpus.length)}>
                    <div class="node-cell">
                      <span class="node-icon"><Icon name="server" size={15} /></span>
                      <span class="node-title">
                        <span class="name">{n.name}</span>
                        <span class="muted mono tiny">{n.endpoint ?? "no network address"}</span>
                      </span>
                    </div>
                  </td>
                  <td rowspan={Math.max(1, n.gpus.length)}><span class="pill {n.state}">{STATE_LABEL[n.state] ?? n.state}</span></td>
                  <td rowspan={Math.max(1, n.gpus.length)} class="model" title={n.model ?? ""}>{n.model ? n.model.split("/").at(-1) : "–"}</td>
                {/if}
                <td>{g ? `${g.index} · ${g.name.replace("NVIDIA GeForce ", "")}` : "–"}</td>
                <td class="vram-col">
                  {#if g}
                    <span class="vram">
                      <span class="bar"><i style="width:{(100 * g.used_bytes) / Math.max(1, g.total_bytes)}%"></i></span>
                      <span class="mono tiny">{gib(g.used_bytes)} / {gib(g.total_bytes)}</span>
                    </span>
                  {:else}–{/if}
                </td>
                <td>{g?.utilization_pct != null ? `${g.utilization_pct}%` : "–"}{g?.temperature_c != null ? ` · ${g.temperature_c}°C` : ""}</td>
                {#if gi === 0}
                  <td rowspan={Math.max(1, n.gpus.length)}>{n.clients}</td>
                  <td rowspan={Math.max(1, n.gpus.length)}>{n.requestsTotal}</td>
                {/if}
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
    </div>
  </div>

  <div class="charts">
    <Chart title="Generation throughput" value={fmt(lastOf(tps), 0, " tok/s")} series={[{ label: "tok/s", color: "var(--on)", points: tps }]} />
    <Chart
      title="Requests in flight"
      value={lastOf(running) == null ? "–" : `${fmt(lastOf(running))} running · ${fmt(lastOf(queued))} queued`}
      series={[
        { label: "running", color: "var(--link)", points: running },
        { label: "queued", color: "var(--busy)", points: queued },
      ]}
    />
    <Chart title="Client requests" value={fmt(lastOf(rpm), 1, " / min")} series={[{ label: "req/min", color: "var(--violet)", points: rpm }]} />
    <Chart
      title="Prefix cache hit (per interval)"
      value={fmt(lastOf(hit), 0, "%")}
      max={100}
      note="share of prompt tokens reused"
      series={[{ label: "hit %", color: "var(--on)", points: hit }]}
    />
    <Chart title="KV cache in use" value={fmt(lastOf(kv), 0, "%")} max={100} series={[{ label: "KV %", color: "var(--link)", points: kv }]} />
    <Chart
      title="GPU load and temperature"
      value={`${fmt(lastOf(util), 0, "%")} · ${fmt(lastOf(temp), 0, "°C")}`}
      max={100}
      series={[
        { label: "load %", color: "var(--on)", points: util },
        { label: "°C", color: "var(--bad)", points: temp },
      ]}
    />
  </div>
</section>

<style>
  .dash { width: 100%; display: flex; flex-direction: column; gap: 14px; }
  .dash-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; border-top: 1px solid var(--border); padding-top: 22px; }
  .dash-head h2 { margin: 2px 0 0; font-size: 18px; font-weight: 650; }
  .small { font-size: 12px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 2px; box-shadow: var(--shadow); }
  .tile .k { font-size: 11.5px; color: var(--muted); }
  .tile .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .tile .k2 { font-size: 11px; color: var(--faint); }
  .tile .v { white-space: nowrap; }
  .tile small { font-size: 13px; color: var(--muted); font-weight: 500; }
  .table-card { padding: 12px 0 4px; }
  .table-head { padding: 0 16px 8px; }
  .table-scroll { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11.5px; font-weight: 600; color: var(--muted); padding: 6px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 10px 14px; border-bottom: 1px solid var(--border); white-space: nowrap; vertical-align: middle; }
  tbody tr:last-child td { border-bottom: none; }
  .node-cell { display: flex; align-items: center; gap: 10px; }
  .node-icon { width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; background: var(--surface-2); color: var(--muted); }
  .node-title { display: flex; flex-direction: column; }
  .name { font-weight: 600; }
  .tiny { font-size: 11.5px; }
  .model { max-width: 200px; overflow: hidden; text-overflow: ellipsis; }
  .pill { font-size: 11.5px; font-weight: 600; padding: 2px 10px; border-radius: 999px; background: var(--surface-2); color: var(--muted); }
  .pill.running { color: var(--on); background: var(--on-soft); }
  .pill.starting, .pill.stopping, .pill.external { color: var(--busy); background: var(--busy-soft); }
  .pill.error { color: var(--bad); background: var(--bad-soft); }
  .vram { display: flex; align-items: center; gap: 10px; }
  .bar { width: 90px; height: 8px; border-radius: 6px; background: var(--surface-2); overflow: hidden; flex-shrink: 0; }
  .bar i { display: block; height: 100%; background: var(--link); border-radius: 6px; }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
</style>
