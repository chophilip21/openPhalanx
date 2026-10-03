<script lang="ts">
  import { ask, open } from "@tauri-apps/plugin-dialog";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import Icon from "../components/Icon.svelte";
  import VramBar from "../components/VramBar.svelte";
  import { api, errorText, type CustomInspect, type ModelRow, type ModelsView } from "../lib/api";
  import { gb, gib, rate, tokens } from "../lib/format";
  import { app } from "../lib/store.svelte";

  const CONTEXTS = [8192, 16384, 32768, 65536, 131072];
  const FIT_LABEL = { ok: "Fits", tight: "Tight", insufficient: "Won't fit" } as const;

  let view = $state<ModelsView | null>(null);
  let error = $state("");
  let custom = $state("");
  let inspecting = $state(false);
  let inspected = $state<CustomInspect | null>(null);
  let customError = $state("");

  async function refresh() {
    try {
      view = await api.models();
    } catch (e) {
      error = errorText(e);
    }
  }

  $effect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  });

  // Refresh when a download finishes so "Installed" appears immediately.
  let finishedSeen = new Set<string>();
  $effect(() => {
    for (const d of Object.values(app.downloads)) {
      if (d.finished && !finishedSeen.has(d.key)) {
        finishedSeen.add(d.key);
        refresh();
      }
    }
  });

  async function run(f: () => Promise<unknown>) {
    error = "";
    try {
      await f();
    } catch (e) {
      error = errorText(e);
    }
    await refresh();
  }

  async function download(row: ModelRow) {
    if (row.fit?.fit === "insufficient") {
      const yes = await ask(
        `${row.name} (${row.quant}) needs ${gib(row.requirement.total_bytes)} of VRAM at ${tokens(row.requirement.context_len)} context, ` +
          `but this GPU can offer ${gib(view?.available_bytes)}. You won't be able to start it here. Download anyway?`,
        { title: "Model won't fit", kind: "warning" },
      );
      if (!yes) return;
    }
    await run(() => api.download(row.key));
  }

  async function remove(row: ModelRow) {
    const yes = await ask(
      row.custom
        ? `Remove ${row.name} from the list${row.app_managed ? " and delete its downloaded weights" : ""}?`
        : `Delete the downloaded weights for ${row.name} (${gb(row.weight_bytes)})?`,
      { title: "Remove model", kind: "warning" },
    );
    if (!yes) return;
    await run(() => (row.custom ? api.removeCustom(row.key) : api.deleteModel(row.key)));
  }

  async function browse() {
    const dir = await open({ directory: true, title: "Choose a model folder (with config.json and .safetensors)" });
    if (typeof dir === "string") {
      custom = dir;
      await inspect();
    }
  }

  async function inspect() {
    customError = "";
    inspected = null;
    inspecting = true;
    try {
      inspected = await api.inspectCustom(custom);
    } catch (e) {
      customError = errorText(e);
    } finally {
      inspecting = false;
    }
  }

  async function addCustom() {
    customError = "";
    try {
      const key = await api.addCustom(custom);
      if (inspected?.local) await api.selectModel(key);
      inspected = null;
      custom = "";
    } catch (e) {
      customError = errorText(e);
    }
    await refresh();
  }

  const running = $derived(app.snapshot?.server.state === "running" || app.snapshot?.server.state === "starting");
</script>

<div class="page">
  <h1>Models</h1>
  <p class="sub">
    Openphalanx never redistributes weights. Downloads come straight from the publisher's Hugging Face repo, pinned to
    a commit and checked against its SHA-256 hashes.
  </p>

  <div class="toolbar">
    <div class="ctx">
      <span class="eyebrow">Context</span>
      <div class="seg">
        {#each CONTEXTS as c}
          <button class:active={view?.context_len === c} onclick={() => run(() => api.setContextLen(c))}>{tokens(c)}</button>
        {/each}
      </div>
    </div>
    {#if view?.gpu}
      <div class="avail">
        <Icon name="cpu" size={14} />
        <span>{view.gpu.name}</span>
        <span class="muted">· {gib(view.available_bytes)} available ({view.available_basis})</span>
      </div>
    {/if}
  </div>

  {#if error}<div class="error-banner"><Icon name="alert" size={16} /><span class="selectable">{error}</span></div>{/if}

  <div class="table card">
    <div class="tr th">
      <span>Model</span><span>Quantization</span><span>Weights</span><span>VRAM needed</span><span>License</span><span></span>
    </div>
    {#each view?.rows ?? [] as row (row.key)}
      {@const dl = app.downloads[row.key]}
      {@const downloading = dl && !dl.finished && !dl.error}
      {@const selected = view?.selected === row.key}
      <div class="tr" class:selected>
        <span class="name">
          <span class="title">
            {row.name}
            {#if row.recommended}<span class="badge ok">Recommended</span>{/if}
            {#if row.custom}<span class="badge neutral">Custom</span>{/if}
          </span>
          {#if row.source_url}
            <button class="link mono" onclick={() => openUrl(row.source_url!)} title="View on Hugging Face">
              {row.repo}{row.revision ? `@${row.revision.slice(0, 7)}` : ""} <Icon name="external" size={11} />
            </button>
          {:else if row.installed_dir}
            <span class="link mono plain">{row.installed_dir}</span>
          {/if}
          {#if row.notes}<span class="note">{row.notes}</span>{/if}
        </span>
        <span>{row.params ? `${row.params} · ` : ""}{row.quant ?? "–"}</span>
        <span class="num">{gb(row.weight_bytes)}</span>
        <span class="need">
          <span class="num">{gib(row.requirement.total_bytes)}</span>
          {#if row.fit}<span class="badge {row.fit.fit}" title={row.fit.message}>{FIT_LABEL[row.fit.fit]}</span>{/if}
          {#if row.requirement.context_len < (view?.context_len ?? 0)}
            <span class="note">capped at {tokens(row.requirement.context_len)}</span>
          {/if}
        </span>
        <span class="muted">{row.license ?? "–"}</span>
        <span class="actions">
          {#if downloading}
            <div class="progress">
              <div class="pbar"><div style:width="{dl.total_bytes ? (dl.done_bytes / dl.total_bytes) * 100 : 0}%"></div></div>
              <span class="muted small">
                {dl.total_bytes ? `${Math.floor((dl.done_bytes / dl.total_bytes) * 100)}% · ${rate(dl.bytes_per_sec)}` : dl.current_file}
              </span>
            </div>
            <button class="ghost icon" title="Cancel download" onclick={() => api.cancelDownload(row.key)}><Icon name="x" size={14} /></button>
          {:else if row.installed_dir}
            {#if selected}
              <span class="badge ok"><Icon name="check" size={12} stroke={3} /> Selected</span>
            {:else}
              <button disabled={running} title={running ? "Stop the server to switch models" : ""} onclick={() => run(() => api.selectModel(row.key))}>Use</button>
            {/if}
            {#if row.app_managed || row.custom}
              <button class="ghost icon" title="Remove" onclick={() => remove(row)}><Icon name="trash" size={14} /></button>
            {/if}
          {:else}
            <button onclick={() => download(row)}><Icon name="download" size={14} /> Download</button>
          {/if}
        </span>
        {#if dl?.error}
          <div class="dl-error">
            <Icon name="alert" size={14} /> <span class="selectable">{dl.error}</span>
            <button class="ghost small" onclick={() => api.clearDownload(row.key)}>Dismiss</button>
          </div>
        {/if}
      </div>
    {/each}
  </div>

  <div class="card custom">
    <span class="eyebrow">Add your own model</span>
    <p class="muted small">
      A local folder with <span class="mono">config.json</span> and <span class="mono">.safetensors</span> weights, or a
      Hugging Face model URL / repo id (e.g. <span class="mono">Qwen/Qwen2.5-Coder-3B-Instruct</span>). The VRAM need is
      checked before anything is downloaded.
    </p>
    <div class="custom-row">
      <input placeholder="/path/to/model  or  https://huggingface.co/owner/name" bind:value={custom}
        onkeydown={(e) => e.key === "Enter" && inspect()} />
      <button onclick={browse}><Icon name="folder" size={14} /> Browse</button>
      <button onclick={inspect} disabled={!custom.trim() || inspecting}><Icon name="search" size={14} /> {inspecting ? "Checking…" : "Check"}</button>
    </div>
    {#if customError}<div class="error-banner"><Icon name="alert" size={16} /><span class="selectable">{customError}</span></div>{/if}
    {#if inspected}
      <div class="inspect">
        <div class="row">
          <div>
            <div class="title">{inspected.label}</div>
            <div class="muted small">
              {inspected.info.architecture ?? "unknown architecture"} · {inspected.info.quant ?? "unquantized"} ·
              {gb(inspected.info.weight_bytes)} weights · up to {tokens(inspected.info.max_context)} context
              {#if inspected.revision} · pinned to <span class="mono">{inspected.revision.slice(0, 7)}</span>{/if}
            </div>
          </div>
          {#if inspected.fit}<span class="badge {inspected.fit.fit}">{FIT_LABEL[inspected.fit.fit]}</span>{/if}
        </div>
        <VramBar weights={inspected.requirement.weight_bytes} kv={inspected.requirement.kv_bytes}
          overhead={inspected.requirement.overhead_bytes} available={inspected.fit?.free_bytes ?? null}
          total={view?.gpu?.total_bytes ?? inspected.requirement.total_bytes} fit={inspected.fit?.fit ?? null} />
        {#if inspected.fit}<p class="muted small">{inspected.fit.message}</p>{/if}
        <div>
          <button class="primary" onclick={addCustom}>
            {inspected.local ? "Add model" : `Add and download (${gb(inspected.info.weight_bytes)})`}
          </button>
        </div>
      </div>
    {/if}
  </div>
</div>

<style>
  .toolbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }
  .ctx { display: flex; align-items: center; gap: 12px; }
  .seg { display: inline-flex; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 3px; }
  .seg button { border: none; background: transparent; padding: 5px 12px; border-radius: 7px; color: var(--muted); }
  .seg button.active { background: var(--surface-3); color: var(--text); }
  .avail { display: flex; align-items: center; gap: 6px; font-size: 13px; }
  .table { padding: 6px 0; margin: 12px 0 20px; }
  .tr {
    display: grid;
    grid-template-columns: minmax(220px, 2.4fr) 1.3fr 0.8fr 1.3fr 0.8fr minmax(150px, 1.3fr);
    gap: 12px;
    align-items: center;
    padding: 12px 18px;
    border-top: 1px solid var(--border);
  }
  .tr.th { border-top: none; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); font-weight: 600; padding: 8px 18px; }
  .tr.selected { background: rgba(52, 211, 153, 0.04); box-shadow: inset 3px 0 0 var(--on); }
  .name { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .title { font-weight: 600; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .link { border: none; background: none; padding: 0; color: var(--link); font-size: 11.5px; text-align: left; display: inline-flex; gap: 4px; align-items: center; }
  .link.plain { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .note { font-size: 11.5px; color: var(--faint); }
  .num { font-variant-numeric: tabular-nums; }
  .need { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
  .actions button { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; }
  .icon { padding: 6px !important; }
  .progress { display: flex; flex-direction: column; gap: 4px; min-width: 110px; flex: 1; }
  .pbar { height: 6px; border-radius: 4px; background: var(--bg); overflow: hidden; }
  .pbar div { height: 100%; background: var(--on); transition: width 0.3s; }
  .small { font-size: 12px; }
  .dl-error { grid-column: 1 / -1; display: flex; gap: 8px; align-items: center; color: #fecaca; font-size: 12.5px; }
  .custom { display: flex; flex-direction: column; gap: 10px; }
  .custom p { margin: 0; }
  .custom-row { display: flex; gap: 8px; }
  .custom-row input { flex: 1; }
  .custom-row button { display: inline-flex; align-items: center; gap: 6px; }
  .inspect { display: flex; flex-direction: column; gap: 12px; border-top: 1px solid var(--border); padding-top: 14px; }
  .inspect .row { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .inspect p { margin: 0; }
</style>
