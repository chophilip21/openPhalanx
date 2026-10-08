<script lang="ts">
  import { api } from "../lib/api";
  import { app } from "../lib/store.svelte";

  let filter = $state("");
  let follow = $state(true);
  let box: HTMLDivElement | undefined = $state();

  // Which machine's logs: this one (live stream), or a cluster member (its
  // backend and cluster events, forwarded with its reports every ~5 s).
  const LOCAL = "local";
  let source = $state(LOCAL);
  let memberLines = $state<string[]>([]);
  const cluster = $derived(app.snapshot?.cluster ?? null);
  const members = $derived(cluster?.role.role === "host" ? cluster.members : []);
  const thisName = $derived(cluster ? `${cluster.name} (this machine)` : "This machine");
  // A member that left falls back to this machine.
  $effect(() => {
    if (source !== LOCAL && !members.some((m) => m.id === source)) source = LOCAL;
  });
  $effect(() => {
    if (source === LOCAL) return;
    const id = source;
    const load = () => api.clusterMemberLogs(id).then((l) => { if (source === id) memberLines = l; }).catch(() => {});
    memberLines = [];
    load();
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  });
  const lines = $derived(source === LOCAL ? app.logs : memberLines);
  const notStarted = $derived(app.snapshot?.server.state === "stopped");

  // Seed with recent history the first time the page opens.
  $effect(() => {
    if (app.logs.length === 0) {
      api.logs(300).then((text) => {
        if (app.logs.length === 0 && text) app.logs.push(...text.split("\n").filter(Boolean));
      }).catch(() => {});
    }
  });

  const shown = $derived(
    filter ? lines.filter((l) => l.toLowerCase().includes(filter.toLowerCase())) : lines,
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
      <p class="sub">
        {#if source === LOCAL}
          SGLang and gateway output from this machine's backend container.
        {:else}
          Backend output and cluster events from this member, forwarded with its reports (about every 5 s).
        {/if}
      </p>
    </div>
    <div class="controls">
      {#if members.length}
        <select bind:value={source} aria-label="Machine">
          <option value={LOCAL}>{thisName}</option>
          {#each members as m (m.id)}
            <option value={m.id}>{m.name}{m.online ? "" : " (offline)"}</option>
          {/each}
        </select>
      {/if}
      <input placeholder="Filter (e.g. cache hit)" bind:value={filter} />
      <label><input type="checkbox" bind:checked={follow} /> Follow</label>
      {#if source === LOCAL}<button class="ghost" onclick={() => (app.logs.length = 0)}>Clear</button>{/if}
    </div>
  </div>
  <div class="box mono" bind:this={box}>
    {#each shown as line}
      <div class={level(line)}>{line}</div>
    {:else}
      <div class="muted">
        {#if source === LOCAL && notStarted}
          The server hasn't been started yet. Its output shows up here once you press Start on the Server page.
        {:else}
          No log output yet.
        {/if}
      </div>
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
    background: var(--log-bg);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 12px 14px;
    font-size: 12px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-all;
    color: var(--log-text);
  }
  .err { color: var(--bad); }
  .warn { color: var(--busy); }
  .hit { color: var(--on); }
</style>
