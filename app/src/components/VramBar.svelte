<script lang="ts">
  import { gib } from "../lib/format";
  import type { Fit } from "../lib/api";

  // Required vs. available VRAM, with weights / KV cache / overhead segments.
  let {
    weights,
    kv,
    overhead,
    available,
    total,
    fit,
  }: { weights: number; kv: number; overhead: number; available: number | null; total: number; fit: Fit | null } =
    $props();
  const scale = $derived(Math.max(total, weights + kv + overhead));
  const w = (b: number) => `${Math.min(100, (b / scale) * 100)}%`;
</script>

<div class="bar" title="Weights {gib(weights)} · KV cache {gib(kv)} · runtime {gib(overhead)}">
  <div class="seg weights" style:width={w(weights)}></div>
  <div class="seg kv" style:width={w(kv)}></div>
  <div class="seg overhead {fit}" style:width={w(overhead)}></div>
  {#if available != null}
    <div class="avail" style:left={w(available)}></div>
  {/if}
</div>
<div class="legend">
  <span><i class="weights"></i>Weights {gib(weights)}</span>
  <span><i class="kv"></i>KV cache {gib(kv)}</span>
  <span><i class="overhead"></i>Runtime {gib(overhead)}</span>
  {#if available != null}<span><i class="line"></i>Available {gib(available)}</span>{/if}
</div>

<style>
  .bar {
    position: relative;
    display: flex;
    height: 10px;
    border-radius: 6px;
    background: var(--bg);
    border: 1px solid var(--border);
    overflow: visible;
  }
  .seg { height: 100%; }
  .seg:first-child { border-radius: 5px 0 0 5px; }
  .weights { background: var(--link); }
  .kv { background: var(--violet); }
  .overhead { background: var(--slate); border-radius: 0 5px 5px 0; }
  .overhead.insufficient { background: var(--bad); }
  .avail {
    position: absolute;
    top: -5px;
    bottom: -5px;
    width: 2px;
    margin-left: -1px;
    background: var(--text);
    border-radius: 2px;
  }
  .legend { display: flex; flex-wrap: wrap; gap: 4px 14px; margin-top: 8px; font-size: 12px; color: var(--muted); }
  .legend i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; vertical-align: 0; }
  .legend i.line { width: 2px; height: 10px; background: var(--text); vertical-align: -1px; }
</style>
