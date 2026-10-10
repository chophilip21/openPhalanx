<script lang="ts">
  // Server statistics under the power button: cluster management, cluster-wide
  // tiles, a machines table, then the charts for ONE machine at a time (a
  // dropdown), so a cluster doesn't pile every machine into the same charts.
  import { ask } from "@tauri-apps/plugin-dialog";
  import Chart from "./Chart.svelte";
  import ClusterPanel from "./ClusterPanel.svelte";
  import Icon from "./Icon.svelte";
  import Notice from "./Notice.svelte";
  import { api, errorText } from "../lib/api";
  import { LOCAL_NODE, nodeSeries, nodesOf, WINDOW_SECONDS } from "../lib/metrics.svelte";
  import { gib, pct, rate } from "../lib/format";
  import { app } from "../lib/store.svelte";

  const snap = $derived(app.snapshot);
  const cluster = $derived(snap?.cluster ?? null);
  const nodes = $derived(nodesOf(snap));
  const online = $derived(nodes.filter((n) => n.online).length);
  const serving = $derived(nodes.filter((n) => n.state === "running").length);
  const isHost = $derived(cluster?.role.role === "host");
  let nodeError = $state("");

  // Charts show one machine; default to this one.
  let chosen = $state(LOCAL_NODE);
  const charted = $derived(nodes.filter((n) => n.charted));
  const selected = $derived(charted.find((n) => n.id === chosen) ?? charted[0]);
  const sel = $derived(selected?.id ?? LOCAL_NODE);

  const tps = $derived(nodeSeries(sel, (s) => s.tokensPerSec));
  const running = $derived(nodeSeries(sel, (s) => s.running));
  const queued = $derived(nodeSeries(sel, (s) => s.queued));
  const waiting = $derived(nodeSeries(sel, (s) => s.waiting));
  const p50 = $derived(nodeSeries(sel, (s) => s.ttftP50));
  const p95 = $derived(nodeSeries(sel, (s) => s.ttftP95));
  const p99 = $derived(nodeSeries(sel, (s) => s.ttftP99));
  const secs = (v: number | null) => (v == null ? "–" : v < 10 ? `${v.toFixed(2)} s` : `${v.toFixed(1)} s`);
  const rpm = $derived(nodeSeries(sel, (s) => s.requestsPerMin));
  const hit = $derived(nodeSeries(sel, (s) => (s.cacheHit == null ? null : s.cacheHit * 100)));
  const kv = $derived(nodeSeries(sel, (s) => (s.kvUsage == null ? null : s.kvUsage * 100)));
  const util = $derived(nodeSeries(sel, (s) => s.gpuUtil));
  const temp = $derived(nodeSeries(sel, (s) => s.gpuTemp));

  const lastOf = (pts: { v: number | null }[]) => [...pts].reverse().find((p) => p.v != null)?.v ?? null;
  const fmt = (v: number | null, digits = 0, unit = "") => (v == null ? "–" : `${v.toFixed(digits)}${unit}`);

  // Tiles: the whole cluster.
  const allGpus = $derived(nodes.filter((n) => n.online).flatMap((n) => n.gpus));
  const gpuTotal = $derived(allGpus.reduce((a, g) => a + g.total_bytes, 0));
  const gpuUsedNow = $derived(allGpus.reduce((a, g) => a + g.used_bytes, 0));
  const clients = $derived(nodes.reduce((a, n) => a + n.clients, 0));
  const sumLatest = (pick: (id: string) => number | null) => {
    const v = charted.map((n) => pick(n.id)).filter((x): x is number => x != null);
    return v.length ? v.reduce((a, b) => a + b, 0) : null;
  };
  const totalTps = $derived(sumLatest((id) => lastOf(nodeSeries(id, (s) => s.tokensPerSec))));
  const totalRpm = $derived(sumLatest((id) => lastOf(nodeSeries(id, (s) => s.requestsPerMin))));

  /** Inference on a machine (the Serving column). */
  const SERVING_LABEL: Record<string, string> = {
    running: "Serving",
    starting: "Starting",
    stopping: "Stopping",
    stopped: "Off",
    error: "Error",
    external: "External",
  };

  async function removeMember(id: string, name: string) {
    const yes = await ask(`Remove ${name} from the cluster? It becomes standalone, and needs a new invitation to come back.`, {
      title: "Remove server",
      kind: "warning",
    });
    if (!yes) return;
    nodeError = "";
    try {
      await api.clusterRemove(id);
    } catch (e) {
      nodeError = errorText(e);
    }
  }

  async function makeHost(id: string, name: string) {
    const yes = await ask(
      `Make ${name} the host? It must approve on that machine; then this machine and the other members move to it. Clients will need to pair with ${name}.`,
      { title: "Change host", kind: "warning" },
    );
    if (!yes) return;
    nodeError = "";
    try {
      await api.clusterMakeHost(id);
    } catch (e) {
      nodeError = errorText(e);
    }
  }
</script>

<section class="dash">
  <div class="dash-head">
    <div>
      <span class="eyebrow titled"><Icon name="server" size={14} /> Cluster</span>
      <h2>{nodes.length} server{nodes.length === 1 ? "" : "s"} · {online} online · {serving} serving</h2>
    </div>
    <span class="muted small">Live · updates every 2 s · last {WINDOW_SECONDS / 60} min</span>
  </div>

  {#if cluster}<ClusterPanel {cluster} />{/if}
  {#if nodeError}<Notice onclose={() => (nodeError = "")}>{nodeError}</Notice>{/if}

  <div class="tiles">
    <div class="tile"><span class="k">Servers online</span><span class="v">{online}<small>/{nodes.length}</small></span></div>
    <div class="tile"><span class="k">Paired clients</span><span class="v">{clients}</span></div>
    <div class="tile"><span class="k">Tokens / s</span><span class="v">{fmt(totalTps)}</span></div>
    <div class="tile"><span class="k">Requests / min</span><span class="v">{fmt(totalRpm)}</span></div>
    <div class="tile"><span class="k">Prefix cache hit</span><span class="v">{pct(snap?.admin?.inference.cache_hit_ratio)}</span></div>
    <div class="tile">
      <span class="k">GPU memory</span>
      <span class="v">{gpuTotal ? `${Math.round((100 * gpuUsedNow) / gpuTotal)}%` : "–"}</span>
      {#if gpuTotal}<span class="k2">{gib(gpuUsedNow)} of {gib(gpuTotal)}</span>{/if}
    </div>
  </div>

  <div class="card table-card">
    <div class="table-head"><span class="eyebrow titled"><Icon name="list" size={14} /> Machines</span></div>
    <div class="table-scroll">
      <table>
        <thead>
          <tr><th>Server</th><th>Status</th><th>Serving</th><th>GPU</th><th class="vram-col">VRAM</th><th>Load</th></tr>
        </thead>
        <tbody>
          {#each nodes as n (n.id)}
            {@const span = Math.max(1, n.gpus.length)}
            {#each n.gpus.length ? n.gpus : [null] as g, gi}
              <tr class:offline={!n.online}>
                {#if gi === 0}
                  <td rowspan={span}>
                    <div class="node-cell">
                      <span class="node-icon"><Icon name="server" size={15} /></span>
                      <span class="node-title">
                        <span class="name">{n.name}</span>
                        <span class="muted mono tiny">{n.endpoint ?? "no network address"}</span>
                        {#if n.detail}<span class="muted tiny">{n.detail}</span>{/if}
                        {#if n.sync}
                          {@const s = n.sync}
                          <span class="sync {s.state}" title="{s.repo}@{s.revision.slice(0, 7)}{s.error ? ` · ${s.error}` : ''}">
                            {#if s.state === "ready"}
                              ✓ {s.label} on disk
                            {:else if s.state === "downloading"}
                              ↓ {s.label} · {s.total_bytes ? Math.floor((100 * s.done_bytes) / s.total_bytes) : 0}% · {rate(s.bytes_per_sec)}
                              <span class="sync-bar"><i style="width:{s.total_bytes ? (100 * s.done_bytes) / s.total_bytes : 0}%"></i></span>
                            {:else if s.state === "missing"}
                              ✗ {s.label} not on this machine (download from the Server page)
                            {:else if s.state === "error"}
                              ! {s.label}: download failed, retrying
                            {:else}
                              … checking for {s.label}
                            {/if}
                          </span>
                        {/if}
                        {#if n.role === "member" && isHost}
                          <span class="row-actions">
                            <button class="ghost link-btn" onclick={() => makeHost(n.id, n.name)}>Make host</button>
                            <button class="ghost link-btn danger" onclick={() => removeMember(n.id, n.name)}>Remove</button>
                          </span>
                        {/if}
                      </span>
                    </div>
                  </td>
                  <td rowspan={span}>
                    {#if n.role === "this"}
                      <span class="pill this">{cluster?.role.role === "host" ? "Host" : cluster?.role.role === "member" ? "Member" : "Standalone"}</span>
                    {:else if n.role === "host"}
                      <span class="pill {n.online ? 'this' : 'stopped'}">Host</span>
                    {:else if n.online}
                      <span class="pill running">Online</span>
                    {:else}
                      <span class="pill stopped">Offline</span>
                    {/if}
                  </td>
                  <td rowspan={span} class="model" title={n.model ?? ""}>
                    {#if n.role === "host"}
                      <span class="muted">–</span>
                    {:else if n.state === "running" && n.model}
                      {n.model.split("/").at(-1)}
                    {:else}
                      <span class="muted">{SERVING_LABEL[n.state] ?? n.state}</span>
                    {/if}
                  </td>
                {/if}
                <td>{g ? `${g.index} · ${g.name.replace("NVIDIA GeForce ", "")}` : n.online && n.role !== "host" ? "none" : "–"}</td>
                <td class="vram-col">
                  {#if g}
                    <span class="vram">
                      <span class="bar"><i style="width:{(100 * g.used_bytes) / Math.max(1, g.total_bytes)}%"></i></span>
                      <span class="mono tiny">{gib(g.used_bytes)} / {gib(g.total_bytes)}</span>
                    </span>
                  {:else}–{/if}
                </td>
                <td>{g?.utilization_pct != null ? `${g.utilization_pct}%` : "–"}{g?.temperature_c != null ? ` · ${g.temperature_c}°C` : ""}</td>
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
    </div>
  </div>

  <div class="charts-head">
    <span class="eyebrow titled"><Icon name="activity" size={14} /> Charts</span>
    <label class="picker">
      <span class="muted small">Machine</span>
      <select bind:value={chosen}>
        {#each charted as n (n.id)}
          <option value={n.id}>{n.name}{n.online ? "" : " (offline)"}</option>
        {/each}
      </select>
    </label>
  </div>

  {#key sel}
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
      <Chart
        title="Time to first token"
        value={secs(lastOf(p50))}
        sub={lastOf(p50) == null ? "" : `median · p95 ${secs(lastOf(p95))} · p99 ${secs(lastOf(p99))}`}
        note="streamed requests, last 5 min"
        series={[
          { label: "p50", color: "var(--on)", points: p50 },
          { label: "p95", color: "var(--busy)", points: p95 },
          { label: "p99", color: "var(--bad)", points: p99 },
        ]}
      />
      <Chart
        title="Queue depth"
        value={lastOf(waiting) == null ? "–" : `${fmt(lastOf(waiting))} waiting · ${fmt(lastOf(queued))} queued`}
        note="waiting: for a slot · queued: in SGLang"
        series={[
          { label: "waiting", color: "var(--violet)", points: waiting },
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
  {/key}
</section>

<style>
  .dash { width: 100%; display: flex; flex-direction: column; gap: 14px; }
  .dash-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; border-top: 1px solid var(--border); padding-top: 22px; }
  .dash-head h2 { margin: 2px 0 0; font-size: 18px; font-weight: 650; }
  .small { font-size: 12px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 12px 14px; display: flex; flex-direction: column; gap: 2px; box-shadow: var(--shadow); }
  .tile .k { font-size: 11.5px; color: var(--muted); }
  .tile .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .tile .k2 { font-size: 11px; color: var(--faint); }
  .tile small { font-size: 13px; color: var(--muted); font-weight: 500; }
  .table-card { padding: 12px 0 4px; }
  .table-head { padding: 0 16px 8px; display: flex; justify-content: space-between; align-items: center; }
  .sync { font-size: 11.5px; display: flex; flex-direction: column; gap: 3px; max-width: 280px; white-space: normal; }
  .sync.ready { color: var(--on); }
  .sync.downloading { color: var(--link); }
  .sync.error { color: var(--bad); }
  .sync.missing { color: var(--busy); }
  .sync-bar { height: 4px; border-radius: 3px; background: var(--surface-2); overflow: hidden; }
  .sync-bar i { display: block; height: 100%; background: var(--link); }
  .row-actions { display: flex; gap: 2px; margin: 2px 0 0 -6px; }
  .link-btn { padding: 1px 6px; font-size: 12px; color: var(--link); }
  .link-btn.danger { color: var(--bad); }
  tr.offline td { opacity: 0.65; }
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
  .pill.this { color: var(--link); background: var(--surface-2); }
  .vram { display: flex; align-items: center; gap: 10px; }
  .bar { width: 90px; height: 8px; border-radius: 6px; background: var(--surface-2); overflow: hidden; flex-shrink: 0; }
  .bar i { display: block; height: 100%; background: var(--link); border-radius: 6px; }
  .charts-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 6px; }
  .picker { display: inline-flex; align-items: center; gap: 8px; }
  .picker select { min-width: 220px; }
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }
</style>
