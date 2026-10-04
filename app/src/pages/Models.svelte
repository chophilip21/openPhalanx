<script lang="ts">
  import { ask, open } from "@tauri-apps/plugin-dialog";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import Icon from "../components/Icon.svelte";
  import VramBar from "../components/VramBar.svelte";
  import { api, errorText, type CustomInspect, type ModelRow, type ModelsView } from "../lib/api";
  import { gb, gib, rate, tokens } from "../lib/format";
  import { app } from "../lib/store.svelte";
  import { suggestStart } from "../lib/nav.svelte";

  // Context window steps for the slider (tokens).
  const STEPS = [4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144];
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

  async function use(row: ModelRow) {
    error = "";
    try {
      await api.selectModel(row.key);
      suggestStart(`${row.name} · ${row.quant ?? ""}`.replace(/ · $/, ""));
    } catch (e) {
      error = errorText(e);
    }
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
      if (inspected?.local) {
        await api.selectModel(key);
        suggestStart(inspected.label);
      }
      inspected = null;
      custom = "";
    } catch (e) {
      customError = errorText(e);
    }
    await refresh();
  }

  const running = $derived(app.snapshot?.server.state === "running" || app.snapshot?.server.state === "starting");

  // Context slider: moves locally, saved when released.
  const stepOf = (ctx: number) => {
    let best = 0;
    STEPS.forEach((s, i) => {
      if (Math.abs(s - ctx) < Math.abs(STEPS[best] - ctx)) best = i;
    });
    return best;
  };
  let dragging = $state<number | null>(null);
  const stepIndex = $derived(dragging ?? stepOf(view?.context_len ?? 32768));
  const ctxValue = $derived(STEPS[stepIndex]);
  async function commitContext() {
    if (dragging == null) return;
    const ctx = STEPS[dragging];
    await run(() => api.setContextLen(ctx));
    dragging = null;
  }
  const selectedRow = $derived(view?.rows.find((r) => r.key === view?.selected) ?? null);

  // Filters.
  let family = $state("All");
  let fitsOnly = $state(false);
  const families = $derived([
    "All",
    ...new Set((view?.rows ?? []).map((r) => r.family ?? "Custom")),
  ]);
  const shown = $derived(
    (view?.rows ?? []).filter(
      (r) =>
        (family === "All" || (r.family ?? "Custom") === family) &&
        (!fitsOnly || (r.fit != null && r.fit.fit !== "insufficient")),
    ),
  );
  const fitting = $derived((view?.rows ?? []).filter((r) => r.fit != null && r.fit.fit !== "insufficient").length);
</script>

<div class="page">
  <h1>Models</h1>
  <p class="sub">
    Openphalanx never redistributes weights. Downloads come straight from the publisher's Hugging Face repo, pinned to
    a commit and checked against its SHA-256 hashes.
  </p>

  <section class="card ctx-card">
    <div class="ctx-head">
      <div>
        <span class="eyebrow">Server setting · Context window</span>
        <div class="ctx-value">{tokens(ctxValue)} <span class="muted">tokens</span></div>
      </div>
      {#if view?.gpu}
        <div class="avail">
          <Icon name="cpu" size={14} />
          <span>{view.gpu.name}</span>
          <span class="muted">· {gib(view.available_bytes)} available ({view.available_basis})</span>
        </div>
      {/if}
    </div>
    <input
      class="slider"
      type="range"
      min="0"
      max={STEPS.length - 1}
      step="1"
      value={stepIndex}
      oninput={(e) => (dragging = Number(e.currentTarget.value))}
      onchange={commitContext}
      aria-label="Context window"
    />
    <div class="ticks">
      {#each STEPS as s, i}
        <span class:on={i === stepIndex} style="left:{(100 * i) / (STEPS.length - 1)}%">{i % 2 === 0 || i === STEPS.length - 1 ? tokens(s) : ""}</span>
      {/each}
    </div>
    <p class="muted ctx-help">
      How much text the model can work with in one request: your code, the conversation and its answer. A longer
      window lets the coding agent see more of your repository, but its KV cache needs more VRAM. This setting
      doesn't change which models exist; it changes how much memory each one needs, so the VRAM column and the
      <em>Fits</em> badges below follow it.
      {#if running}<strong>Applies the next time the server starts.</strong>{/if}
    </p>
    {#if selectedRow}
      <div class="ctx-model">
        <span class="muted">Selected model</span>
        <span>{selectedRow.name} · {selectedRow.quant}</span>
        <span class="muted">·</span>
        {#if selectedRow.max_fit_context}
          <span>fits up to <strong>{tokens(selectedRow.max_fit_context)}</strong> on this GPU</span>
          {#if selectedRow.max_fit_context !== ctxValue && selectedRow.max_fit_context < (STEPS.at(-1) ?? 0)}
            <button class="ghost small" onclick={() => run(() => api.setContextLen(Math.min(selectedRow!.max_fit_context!, selectedRow!.max_context)))}>
              Use {tokens(selectedRow.max_fit_context)}
            </button>
          {/if}
        {:else}
          <span class="warn-note">doesn't fit on this GPU at any context</span>
        {/if}
      </div>
    {/if}
  </section>

  <p class="muted estimate">
    <strong>VRAM needed</strong> is deliberately conservative: weights as loaded, the KV cache for the context window
    above plus 25%, and runtime memory. Models marked <em>Won't fit</em> can't be started on this GPU.
  </p>

  <div class="filters">
    <div class="chips">
      {#each families as f}
        <button class:active={family === f} onclick={() => (family = f)}>{f}</button>
      {/each}
    </div>
    <label class="fits-only">
      <input type="checkbox" bind:checked={fitsOnly} />
      Only models that fit ({fitting})
    </label>
  </div>

  {#if error}<div class="error-banner"><Icon name="alert" size={16} /><span class="selectable">{error}</span></div>{/if}

  <div class="table card">
    <div class="tr th">
      <span>Model</span><span>Quantization</span><span>Download</span><span>VRAM needed</span><span>License</span><span>Released</span><span></span>
    </div>
    {#each shown as row (row.key)}
      {@const dl = app.downloads[row.key]}
      {@const downloading = dl && !dl.finished && !dl.error}
      {@const selected = view?.selected === row.key}
      <div class="tr" class:selected>
        <span class="name">
          <span class="title">
            {row.name}
            {#if row.best_fit}<span class="badge ok" title="Largest model that fits this GPU with headroom at the selected context">Best fit for this GPU</span>{/if}
            {#if row.tested}<span class="badge neutral" title="Verified end to end on real hardware">Tested</span>{/if}
            {#if row.custom}<span class="badge neutral">Custom</span>{/if}
            {#if row.quantized_by}<span class="badge community" title="Quantized by {row.quantized_by}, not by the model's publisher">Community · {row.quantized_by}</span>{/if}
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
        <span class="num muted">{gb(row.requirement.download_bytes)}</span>
        <span class="need">
          <span class="need-top">
            <span class="num total">{gib(row.requirement.total_bytes)}</span>
            {#if row.fit}<span class="badge {row.fit.fit}" title={row.fit.message}>{FIT_LABEL[row.fit.fit]}</span>{/if}
          </span>
          <span class="note num" title="Weights as loaded + KV cache ({tokens(row.requirement.context_len)} context + 25%) + runtime">
            {gib(row.requirement.weight_bytes)} + {gib(row.requirement.kv_bytes)} KV + {gib(row.requirement.overhead_bytes)} runtime
          </span>
          {#if row.requirement.fp8_upcast}
            <span class="note warn-note">FP8 counted at 16-bit size: this GPU has no native FP8</span>
          {/if}
          {#if row.requirement.context_len < (view?.context_len ?? 0)}
            <span class="note">model's maximum is {tokens(row.requirement.context_len)}</span>
          {/if}
          {#if row.max_fit_context && row.fit?.fit === "insufficient"}
            <span class="note">fits at {tokens(row.max_fit_context)} or less</span>
          {:else if !row.max_fit_context && view?.available_bytes}
            <span class="note">too large for this GPU</span>
          {/if}
        </span>
        <span class="muted">{row.license ?? "–"}</span>
        <span class="muted num">{row.released ?? "–"}</span>
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
              <button disabled={running} title={running ? "Stop the server to switch models" : ""} onclick={() => use(row)}>Use</button>
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
        {#if inspected.requirement.fp8_upcast}<p class="small warn-note">FP8 weights are counted at 16-bit size because this GPU has no native FP8.</p>{/if}
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
  .ctx-card { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
  .ctx-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
  .ctx-value { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; }
  .ctx-value .muted { font-size: 14px; font-weight: 500; }
  .slider { width: 100%; accent-color: var(--on); margin: 6px 0 0; height: 22px; cursor: pointer; }
  .ticks { position: relative; height: 16px; margin: 0 9px; font-size: 11px; color: var(--faint); }
  .ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .ticks span.on { color: var(--text); font-weight: 600; }
  .ctx-help { margin: 4px 0 0; font-size: 12.5px; max-width: 900px; }
  .ctx-model { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; border-top: 1px solid var(--border); padding-top: 10px; }
  .filters { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 4px; }
  .chips { display: flex; gap: 6px; flex-wrap: wrap; }
  .chips button { padding: 5px 12px; border-radius: 999px; font-size: 12.5px; color: var(--muted); }
  .chips button.active { background: var(--on-soft); border-color: var(--on); color: var(--on); }
  .fits-only { display: flex; gap: 8px; align-items: center; font-size: 13px; cursor: pointer; }
  :global(.badge.community) { color: var(--violet); background: var(--surface-2); }
  .avail { display: flex; align-items: center; gap: 6px; font-size: 13px; }
  .table { padding: 6px 0; margin: 12px 0 20px; }
  .tr {
    display: grid;
    grid-template-columns: minmax(220px, 2.4fr) 1.3fr 0.8fr 1.3fr 0.8fr 0.6fr minmax(150px, 1.3fr);
    gap: 12px;
    align-items: center;
    padding: 12px 18px;
    border-top: 1px solid var(--border);
  }
  .tr.th { border-top: none; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); font-weight: 600; padding: 8px 18px; }
  .tr.selected { background: var(--on-soft); box-shadow: inset 3px 0 0 var(--on); }
  .name { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .title { font-weight: 600; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .link { border: none; background: none; padding: 0; color: var(--link); font-size: 11.5px; text-align: left; display: inline-flex; gap: 4px; align-items: center; }
  .link.plain { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .note { font-size: 11.5px; color: var(--faint); }
  .num { font-variant-numeric: tabular-nums; }
  .need { display: flex; flex-direction: column; gap: 2px; }
  .need-top { display: flex; gap: 6px; align-items: center; }
  .need .total { font-weight: 600; }
  .warn-note { color: var(--busy); }
  .estimate { margin: 0 0 12px; font-size: 12.5px; }
  .actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
  .actions button { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; }
  .icon { padding: 6px !important; }
  .progress { display: flex; flex-direction: column; gap: 4px; min-width: 110px; flex: 1; }
  .pbar { height: 6px; border-radius: 4px; background: var(--bg); overflow: hidden; }
  .pbar div { height: 100%; background: var(--on); transition: width 0.3s; }
  .small { font-size: 12px; }
  .dl-error { grid-column: 1 / -1; display: flex; gap: 8px; align-items: center; color: var(--bad-text); font-size: 12.5px; }
  .custom { display: flex; flex-direction: column; gap: 10px; }
  .custom p { margin: 0; }
  .custom-row { display: flex; gap: 8px; }
  .custom-row input { flex: 1; }
  .custom-row button { display: inline-flex; align-items: center; gap: 6px; }
  .inspect { display: flex; flex-direction: column; gap: 12px; border-top: 1px solid var(--border); padding-top: 14px; }
  .inspect .row { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .inspect p { margin: 0; }
</style>
