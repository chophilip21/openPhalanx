<script lang="ts">
  import { api } from "../lib/api";
  import { app } from "../lib/store.svelte";

  let filter = $state("");
  let follow = $state(true);
  let box: HTMLDivElement | undefined = $state();

  // Seed with recent history the first time the page opens.
  $effect(() => {
    if (app.logs.length === 0) {
      api.logs(300).then((text) => {
        if (app.logs.length === 0 && text) app.logs.push(...text.split("\n").filter(Boolean));
      }).catch(() => {});
    }
  });

  const shown = $derived(
    filter ? app.logs.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : app.logs,
  );

  $effect(() => {
    shown.length;
    if (follow && box) queueMicrotask(() => box && (box.scrollTop = box.scrollHeight));
  });

  const level = (l: string) =>
    /error|traceback|exception|out of memory/i.test(l) ? "err" : /warn/i.test(l) ? "warn" : /cache hit|#cached-token/i.test(l) ? "hit" : "";
</script>

<div class="page logs">
  <div class="head">
    <div>
      <h1>Logs</h1>
      <p class="sub">SGLang and agent output from the backend container.</p>
    </div>
    <div class="controls">
      <input placeholder="Filter (e.g. cache hit)" bind:value={filter} />
      <label><input type="checkbox" bind:checked={follow} /> Follow</label>
      <button class="ghost" onclick={() => (app.logs.length = 0)}>Clear</button>
    </div>
  </div>
  <div class="box mono" bind:this={box}>
    {#each shown as line}
      <div class={level(line)}>{line}</div>
    {:else}
      <div class="muted">No log output yet.</div>
    {/each}
  </div>
</div>

<style>
  .logs { display: flex; flex-direction: column; height: 100%; }
  .head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
  .controls { display: flex; gap: 10px; align-items: center; }
  .controls label { display: flex; gap: 6px; align-items: center; color: var(--muted); font-size: 13px; }
  .box {
    flex: 1;
    min-height: 0;
    overflow: auto;
    background: #070a10;
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 12px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-all;
    color: #c4cce0;
  }
  .err { color: var(--bad); }
  .warn { color: var(--busy); }
  .hit { color: var(--on); }
</style>
