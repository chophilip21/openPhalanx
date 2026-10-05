<script lang="ts">
  // Split cluster: how much VRAM each server adds to the pool, as a donut.
  // The outer ring shows the selected model's need against the pool.
  import type { ModelRow, PoolNode } from "../lib/api";
  import { gib } from "../lib/format";

  let { nodes, selected }: { nodes: PoolNode[]; selected: ModelRow | null } = $props();

  const COLORS = ["var(--on)", "var(--violet)", "var(--link)", "var(--busy)", "var(--bad)", "var(--slate)"];
  const R = 70;
  const C = 2 * Math.PI * R;
  const R2 = 86;
  const C2 = 2 * Math.PI * R2;

  const total = $derived(nodes.reduce((a, n) => a + n.available_bytes, 0));
  const slices = $derived.by(() => {
    let offset = 0;
    return nodes.map((n, i) => {
      const len = total > 0 ? (n.available_bytes / total) * C : 0;
      const s = { node: n, color: COLORS[i % COLORS.length], len, offset, pct: total > 0 ? (100 * n.available_bytes) / total : 0 };
      offset += len;
      return s;
    });
  });
  const need = $derived(selected?.requirement.total_bytes ?? 0);
  const needFrac = $derived(total > 0 ? Math.min(need / total, 1) : 0);
  const over = $derived(need > total);
</script>

<section class="card pool">
  <div class="chart">
    <svg viewBox="0 0 200 200" role="img" aria-label="Pooled VRAM by server">
      <circle cx="100" cy="100" r={R} class="track" />
      {#each slices as s (s.node.id)}
        <circle cx="100" cy="100" r={R} class="slice" stroke={s.color}
          stroke-dasharray="{Math.max(s.len - 2, 0)} {C}" stroke-dashoffset={-s.offset}>
          <title>{s.node.name}: {gib(s.node.available_bytes)}</title>
        </circle>
      {/each}
      {#if need > 0}
        <circle cx="100" cy="100" r={R2} class="need-track" />
        <circle cx="100" cy="100" r={R2} class="need" class:over stroke-dasharray="{needFrac * C2} {C2}">
          <title>{selected?.name} needs {gib(need)}</title>
        </circle>
      {/if}
      <text x="100" y="96" class="big">{gib(total)}</text>
      <text x="100" y="116" class="small">pooled VRAM</text>
    </svg>
  </div>
  <div class="legend">
    <span class="eyebrow">Split cluster · {nodes.length} servers</span>
    {#each slices as s (s.node.id)}
      <div class="row">
        <span class="sw" style="background:{s.color}"></span>
        <span class="name">{s.node.name}{s.node.this ? " (this machine)" : ""}</span>
        <span class="muted gpu">{s.node.gpu ?? "no GPU"}</span>
        <span class="num"><strong>{gib(s.node.available_bytes)}</strong> <span class="muted">of {gib(s.node.total_bytes)} · {Math.round(s.pct)}%</span></span>
      </div>
    {/each}
    {#if selected && need > 0}
      <div class="row need-row" class:over>
        <span class="sw ring"></span>
        <span class="name">{selected.name} needs <strong>{gib(need)}</strong></span>
        <span class="muted">{over ? `${gib(need - total)} more than the pool` : `${Math.round(needFrac * 100)}% of the pool`}</span>
      </div>
    {/if}
    <p class="muted note">
      Free VRAM on each server's largest GPU, as last reported. With the split strategy the weights and KV cache are
      shared out, but every server needs its own runtime memory, so each extra server adds that to the need below.
      A model that fits this machine's GPU runs here alone (faster, and it keeps serving if a member drops out); one
      that doesn't is split across the servers when you start it.
    </p>
  </div>
</section>

<style>
  .pool { display: grid; grid-template-columns: 200px 1fr; gap: 24px; align-items: center; margin-bottom: 16px; }
  .chart svg { width: 200px; height: 200px; display: block; }
  circle { fill: none; transform: rotate(-90deg); transform-origin: 100px 100px; }
  .track { stroke: var(--surface-3); stroke-width: 22; }
  .slice { stroke-width: 22; transition: stroke-dasharray 0.4s, stroke-dashoffset 0.4s; }
  .need-track { stroke: var(--surface-2); stroke-width: 5; }
  .need { stroke: var(--text); stroke-width: 5; stroke-linecap: round; opacity: 0.75; transition: stroke-dasharray 0.4s; }
  .need.over { stroke: var(--bad); opacity: 1; }
  text { text-anchor: middle; fill: var(--text); }
  .big { font-size: 22px; font-weight: 650; }
  .small { font-size: 11px; fill: var(--muted); }
  .legend { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
  .row { display: grid; grid-template-columns: 12px minmax(0, 1fr) auto auto; gap: 10px; align-items: center; font-size: 13px; }
  .sw { width: 12px; height: 12px; border-radius: 3px; }
  .sw.ring { background: none; border: 2px solid var(--text); border-radius: 50%; opacity: 0.75; }
  .over .sw.ring { border-color: var(--bad); opacity: 1; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gpu { font-size: 12px; }
  .need-row { grid-template-columns: 12px minmax(0, 1fr) auto; border-top: 1px solid var(--border); padding-top: 8px; }
  .need-row.over .name { color: var(--bad); }
  .note { font-size: 12px; margin: 4px 0 0; }
  @media (max-width: 640px) {
    .pool { grid-template-columns: 1fr; justify-items: center; }
    .row { grid-template-columns: 12px minmax(0, 1fr) auto; }
    .gpu { display: none; }
  }
</style>
