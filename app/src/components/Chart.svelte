<script lang="ts">
  // A dashboard panel with a small time-series chart (Grafana-style): title,
  // current value, and one or more series over the last WINDOW_SECONDS.
  import { WINDOW_SECONDS } from "../lib/metrics.svelte";

  type Point = { t: number; v: number | null };
  type Series = { label: string; color: string; points: Point[] };

  let {
    title,
    value,
    series,
    max = null,
    note = "",
    sub = "",
  }: { title: string; value: string; series: Series[]; max?: number | null; note?: string; sub?: string } = $props();

  const W = 300;
  const H = 90;

  const now = $derived(Math.max(Date.now(), ...series.flatMap((s) => s.points.map((p) => p.t))));
  const top = $derived(
    max ?? Math.max(1e-9, ...series.flatMap((s) => s.points.map((p) => p.v ?? 0))) * 1.15,
  );
  const x = (t: number) => W - ((now - t) / (WINDOW_SECONDS * 1000)) * W;
  const y = (v: number) => H - (Math.min(v, top) / top) * (H - 4) - 2;

  /** Line segments, broken where there is no data. */
  function paths(points: Point[]): { line: string; area: string }[] {
    const out: { line: string; area: string }[] = [];
    let run: Point[] = [];
    const flush = () => {
      if (run.length) {
        const line = run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v!).toFixed(1)}`).join(" ");
        const area = `${line} L${x(run.at(-1)!.t).toFixed(1)},${H} L${x(run[0].t).toFixed(1)},${H} Z`;
        out.push({ line, area });
      }
      run = [];
    };
    for (const p of points) {
      if (p.v == null) flush();
      else run.push(p);
    }
    flush();
    return out;
  }
</script>

<div class="card panel">
  <div class="head">
    <span class="title">{title}</span>
    {#if series.length > 1}
      <span class="legend">
        {#each series as s}<span><i style="background:{s.color}"></i>{s.label}</span>{/each}
      </span>
    {/if}
  </div>
  <div class="value">{value}{#if sub}<span class="sub">{sub}</span>{/if}</div>
  <svg viewBox="0 0 {W} {H}" preserveAspectRatio="none" aria-hidden="true">
    <line x1="0" x2={W} y1={H / 2} y2={H / 2} class="grid" />
    {#each series as s}
      {#each paths(s.points) as seg}
        <path d={seg.area} fill={s.color} opacity="0.12" />
        <path d={seg.line} fill="none" stroke={s.color} stroke-width="1.8" vector-effect="non-scaling-stroke" />
      {/each}
    {/each}
  </svg>
  <div class="foot"><span>{Math.round(WINDOW_SECONDS / 60)} min ago</span><span>{note}</span><span>now</span></div>
</div>

<style>
  .panel { padding: 14px 16px 10px; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
  .head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .title { font-size: 12.5px; color: var(--muted); font-weight: 600; }
  .legend { display: flex; gap: 10px; font-size: 11px; color: var(--muted); }
  .legend span { display: inline-flex; align-items: center; gap: 4px; }
  .legend i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
  .value { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
  /* Secondary figures next to the headline one, so a panel with several doesn't crowd the big line. */
  .sub { margin-left: 10px; font-size: 12px; font-weight: 500; color: var(--muted); white-space: nowrap; }
  svg { width: 100%; height: 72px; display: block; }
  .grid { stroke: var(--border); stroke-dasharray: 3 4; vector-effect: non-scaling-stroke; }
  .foot { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--faint); }
</style>
