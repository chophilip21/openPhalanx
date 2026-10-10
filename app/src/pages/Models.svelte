<script lang="ts">
  import { ask, open } from "@tauri-apps/plugin-dialog";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import ContextSlider from "../components/ContextSlider.svelte";
  import Icon from "../components/Icon.svelte";
  import Notice from "../components/Notice.svelte";
  import VramBar from "../components/VramBar.svelte";
  import VramPool from "../components/VramPool.svelte";
  import RunningModel from "../components/RunningModel.svelte";
  import { api, errorText, type CustomInspect, type ModelRow, type ModelsView } from "../lib/api";
  import { gb, gib, rate, tokens } from "../lib/format";
  import { fuzzyFilter } from "../lib/search";
  import { app } from "../lib/store.svelte";
  import { suggestStart } from "../lib/nav.svelte";

  // Context window steps for the slider (tokens).
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
    // Show progress at once: the first bytes can be seconds away.
    app.downloads[row.key] = { key: row.key, done_bytes: 0, total_bytes: 0, bytes_per_sec: 0, current_file: "Starting…", error: null, finished: false };
    await run(() => api.download(row.key));
    if (error) delete app.downloads[row.key];
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
        : row.app_managed
          ? `Delete the downloaded weights for ${row.name} (${gb(row.weight_bytes)})?`
          : `${row.name} is in your Hugging Face cache (~/.cache/huggingface), which other tools on this machine may use too. ` +
            `Delete it from the cache (${gb(row.weight_bytes)}, every revision)?`,
      { title: row.custom ? "Remove model" : "Delete model", kind: "warning" },
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

  // The saved context window (the slider is the shared ContextSlider).
  const ctxValue = $derived(view?.context_len ?? 32768);
  // Locked whenever a backend is starting, running or stopping (the app refuses changes then).
  const ctxLocked = $derived(["starting", "running", "stopping", "external"].includes(app.snapshot?.server.state ?? "stopped"));
  const MAX_CTX = 262144;
  const selectedRow = $derived(view?.rows.find((r) => r.key === view?.selected) ?? null);
  const pooled = $derived(!!view?.pool);
  const live = $derived(running ? (app.snapshot?.server.model ?? null) : null);
  const where = $derived(pooled ? "on this cluster" : "on this GPU");

  // Filters.
  let family = $state("All");
  let fitsOnly = $state(false);
  let downloadedOnly = $state(false);
  // Search over everything a row shows as text; typos are forgiven (see fuzzyFilter).
  let query = $state("");
  const found = $derived(
    fuzzyFilter(view?.rows ?? [], query, (r) =>
      [r.name, r.family ?? "Custom", r.repo, r.params, r.quant, r.quantized_by, r.license]
        .filter(Boolean)
        .join(" "),
    ),
  );
  const families = $derived([
    "All",
    ...new Set((view?.rows ?? []).map((r) => r.family ?? "Custom")),
  ]);
  const shown = $derived(
    found.filter(
      (r) =>
        (family === "All" || (r.family ?? "Custom") === family) &&
        (!fitsOnly || (r.fit != null && r.fit.fit !== "insufficient")) &&
        (!downloadedOnly || r.installed_dir != null),
    ),
  );
  const downloadedCount = $derived((view?.rows ?? []).filter((r) => r.installed_dir != null).length);
  const fitting = $derived((view?.rows ?? []).filter((r) => r.fit != null && r.fit.fit !== "insufficient").length);

  // Sorting (newest first by default) and pages of PAGE_SIZE.
  type SortKey = "released" | "size";
  const PAGE_SIZE = 15;
  let sortKey = $state<SortKey>("released");
  let descending = $state(true);
  let page = $state(0);
  let tableTop: HTMLElement | undefined = $state();
  function sortBy(key: SortKey) {
    if (sortKey === key) descending = !descending;
    else [sortKey, descending] = [key, true];
  }
  const sorted = $derived(
    [...shown].sort((a, b) => {
      // Rows without a release date (custom models) stay last either way.
      const date = (r: ModelRow) => r.released_on ?? r.released ?? "";
      if (sortKey === "released" && !date(a) !== !date(b)) return date(a) ? -1 : 1;
      const by = sortKey === "released" ? date(a).localeCompare(date(b)) : a.weight_bytes - b.weight_bytes;
      return (descending ? -by : by) || a.name.localeCompare(b.name);
    }),
  );
  const pages = $derived(Math.max(1, Math.ceil(sorted.length / PAGE_SIZE)));
  const pageRows = $derived(sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE));
  // A new filter or order starts from the first page.
  $effect(() => {
    void [query, family, fitsOnly, downloadedOnly, sortKey, descending];
    page = 0;
  });
  function goPage(p: number) {
    page = Math.min(Math.max(p, 0), pages - 1);
    tableTop?.scrollIntoView({ block: "start", behavior: "smooth" });
  }
  const arrow = (key: SortKey) => (sortKey === key ? (descending ? "↓" : "↑") : "");
  // "other" / "see repo" say nothing: show a license only when it names one.
  const knownLicense = (l: string | null) => !!l && !["other", "see repo", "unknown"].includes(l.trim().toLowerCase());
  const familyCount = (f: string) => (view?.rows ?? []).filter((r) => f === "All" || (r.family ?? "Custom") === f).length;
</script>

<div class="page">
  <h1>Models</h1>
  <p class="sub">
    Openphalanx never redistributes weights. Downloads come straight from the publisher's Hugging Face repo, pinned to
    a commit and checked against its SHA-256 hashes.
  </p>

  {#if live}
    <div class="live-banner">
      <RunningModel model={live} loading={app.snapshot?.server.state === "starting"} />
      <span class="muted">The server is live, so this model is locked in. Stop the server to switch models or change
        the context window.</span>
    </div>
  {/if}

  <section class="card ctx-card">
    <div class="ctx-head">
      <div>
        <span class="eyebrow">Server setting · Context window</span>
      </div>
      {#if view?.pool}
        <div class="avail">
          <Icon name="users" size={14} />
          <span>{view.pool.length} servers</span>
          <span class="muted">· {gib(view.available_bytes)} {view.available_basis}</span>
        </div>
      {:else if view?.gpu}
        <div class="avail">
          <Icon name="cpu" size={14} />
          <span>{view.gpu.name}</span>
          <span class="muted">· {gib(view.available_bytes)} available ({view.available_basis})</span>
        </div>
      {/if}
    </div>
    <ContextSlider value={ctxValue} locked={ctxLocked} onchange={(ctx) => run(() => api.setContextLen(ctx))} />
    <p class="muted ctx-help">
      How much text the model can work with in one request: your code, the conversation and its answer. A longer
      window lets the coding agent see more of your repository, but its KV cache needs more VRAM. This setting
      doesn't change which models exist; it changes how much memory each one needs, so the VRAM column and the
      <em>Fits</em> badges below follow it.
      {#if ctxLocked}<strong>Stop the server to change it; the running model keeps the context it started with.</strong>{/if}
    </p>
    {#if selectedRow}
      <div class="ctx-model">
        <span class="muted">Selected model</span>
        <span>{selectedRow.name} · {selectedRow.quant}</span>
        <span class="muted">·</span>
        {#if selectedRow.max_fit_context}
          <span>fits up to <strong>{tokens(selectedRow.max_fit_context)}</strong> {where}</span>
          {#if selectedRow.max_fit_context !== ctxValue && selectedRow.max_fit_context < MAX_CTX}
            <button class="ghost small" onclick={() => run(() => api.setContextLen(Math.min(selectedRow!.max_fit_context!, selectedRow!.max_context)))}>
              Use {tokens(selectedRow.max_fit_context)}
            </button>
          {/if}
        {:else}
          <span class="warn-note">doesn't fit {where} at any context</span>
        {/if}
      </div>
    {/if}
  </section>

  {#if view?.pool}
    <VramPool nodes={view.pool} selected={selectedRow} />
  {/if}

  <p class="muted estimate">
    <strong>VRAM needed</strong> is deliberately conservative: weights as loaded, the KV cache for the context window
    above plus 25%, and runtime memory{pooled ? " on every server" : ""}. Models marked <em>Won't fit</em> can't be started {where}.
  </p>

  <div class="filters">
    <label class="search">
      <Icon name="search" size={14} />
      <input type="search" placeholder="Search models" aria-label="Search models" bind:value={query}
        onkeydown={(e) => e.key === "Escape" && (query = "")} />
    </label>
    <label class="pick">
      <span class="muted small">Family</span>
      <select bind:value={family}>
        {#each families as f}<option value={f}>{f === "All" ? "All models" : f} ({familyCount(f)})</option>{/each}
      </select>
    </label>
    <label class="pick">
      <span class="muted small">Sort</span>
      <select value={`${sortKey}:${descending ? "desc" : "asc"}`}
        onchange={(e) => { const [k, d] = e.currentTarget.value.split(":"); sortKey = k as SortKey; descending = d === "desc"; }}>
        <option value="released:desc">Newest first</option>
        <option value="released:asc">Oldest first</option>
        <option value="size:desc">Largest first</option>
        <option value="size:asc">Smallest first</option>
      </select>
    </label>
    <div class="checks">
      <label class="fits-only">
        <input type="checkbox" bind:checked={fitsOnly} />
        Only models that fit ({fitting})
      </label>
      <label class="fits-only">
        <input type="checkbox" bind:checked={downloadedOnly} />
        Only downloaded models ({downloadedCount})
      </label>
    </div>
  </div>

  {#if error}<Notice onclose={() => (error = "")}>{error}</Notice>{/if}

  <div class="table card" bind:this={tableTop}>
    <div class="tr th">
      <span>Model</span>
      <button class="th-sort" onclick={() => sortBy("size")}>Download {arrow("size")}</button>
      <span>VRAM needed</span>
      <button class="th-sort" onclick={() => sortBy("released")}>Released {arrow("released")}</button><span></span>
    </div>
    {#each pageRows as row (row.key)}
      {@const dl = app.downloads[row.key]}
      {@const downloading = dl && !dl.finished && !dl.error}
      {@const selected = view?.selected === row.key}
      <div class="tr" class:selected class:live={live?.key === row.key}>
        <span class="name">
          <span class="title">
            {row.name}
            {#if row.best_fit}<span class="badge ok" title="Largest model that fits this GPU with headroom at the selected context">Best fit for this GPU</span>{/if}
            {#if row.tested}<span class="badge neutral" title="Verified end to end on real hardware">Tested</span>{/if}
            {#if row.custom}<span class="badge neutral">Custom</span>{/if}
            {#if row.quantized_by}<span class="badge community" title="Quantized by {row.quantized_by}, not by the model's publisher">Community · {row.quantized_by}</span>{/if}
          </span>
          <!-- Size, quantization and license under the name (not columns, which crowded small windows). -->
          <span class="meta">
            {[row.params, row.quant].filter(Boolean).join(" · ") || "–"}{#if knownLicense(row.license)}<span class="lic">{` · ${row.license}`}</span>{/if}
          </span>
          {#if row.source_url}
            <button class="link mono" onclick={() => openUrl(row.source_url!)} title="View on Hugging Face">
              {row.repo}{row.revision ? `@${row.revision.slice(0, 7)}` : ""} <Icon name="external" size={11} />
            </button>
          {:else if row.installed_dir}
            <span class="link mono plain">{row.installed_dir}</span>
          {/if}
          {#if row.notes}<span class="note">{row.notes}</span>{/if}
          {#if row.broken}<span class="note broken">Broken: {row.broken}. Delete it and download it again; the server can't start with it.</span>{/if}
        </span>
        <span class="cell num muted" data-label="Download">{gb(row.requirement.download_bytes)}</span>
        <span class="cell need" data-label="VRAM needed">
          <span class="need-top" title="{gib(row.requirement.weight_bytes)} weights + {gib(row.requirement.kv_bytes)} KV cache + {gib(row.requirement.overhead_bytes)} runtime at {tokens(row.requirement.context_len)} context">
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
        <span class="cell muted num date" data-label="Released">{row.released ?? "–"}</span>
        <span class="actions">
          {#if downloading}
            {@const pct = dl.total_bytes ? Math.floor((dl.done_bytes / dl.total_bytes) * 100) : null}
            <div class="progress" aria-live="polite">
              <!-- No size yet (listing, starting): an animated bar instead of an empty one. -->
              <div class="pbar" class:indeterminate={pct == null}><div style:width="{pct ?? 0}%"></div></div>
              <span class="muted small" title={dl.current_file}>
                {pct == null ? dl.current_file : `${pct}% · ${dl.bytes_per_sec > 0 ? rate(dl.bytes_per_sec) : dl.current_file}`}
              </span>
            </div>
            <button class="ghost icon" title="Cancel download" onclick={() => api.cancelDownload(row.key)}><Icon name="x" size={14} /></button>
          {:else if row.installed_dir}
            {#if row.broken}
              <span class="badge insufficient" title="Its files are damaged ({row.broken}). Delete it and download it again."><Icon name="alert" size={12} stroke={2.6} /> Broken</span>
            {:else if live?.key === row.key}
              <span class="badge ok" title="The server is running this model. Stop the server to switch models."><Icon name="lock" size={12} stroke={2.6} /> Running</span>
            {:else if selected}
              <span class="badge ok"><Icon name="check" size={12} stroke={3} /> Selected</span>
            {:else}
              <button disabled={running} title={running ? "Stop the server to switch models" : ""} onclick={() => use(row)}>Use</button>
            {/if}
            {#if live?.key !== row.key}
              <button class="ghost icon danger" title={row.app_managed || row.custom ? "Delete" : "Delete from the Hugging Face cache"}
                aria-label="Delete {row.name}" onclick={() => remove(row)}><Icon name="trash" size={14} /></button>
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
    {:else}
      <div class="empty muted">
        {query.trim() && !found.length ? `No models match "${query.trim()}".` : "No models match these filters."}
      </div>
    {/each}
  </div>

  {#if pages > 1}
    <nav class="pager" aria-label="Model pages">
      <span class="muted small">
        {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, sorted.length)} of {sorted.length} models
      </span>
      <div class="pages">
        <button class="ghost" disabled={page === 0} onclick={() => goPage(page - 1)} aria-label="Previous page">‹ Prev</button>
        {#each Array.from({ length: pages }, (_, i) => i) as i}
          <button class:active={i === page} aria-current={i === page ? "page" : undefined} onclick={() => goPage(i)}>{i + 1}</button>
        {/each}
        <button class="ghost" disabled={page === pages - 1} onclick={() => goPage(page + 1)} aria-label="Next page">Next ›</button>
      </div>
    </nav>
  {/if}

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
    {#if customError}<Notice onclose={() => (customError = "")}>{customError}</Notice>{/if}
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
  .ctx-help { margin: 4px 0 0; font-size: 12.5px; max-width: 900px; }
  .ctx-model { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; font-size: 13px; border-top: 1px solid var(--border); padding-top: 10px; }
  .filters { display: flex; align-items: center; gap: 10px 20px; flex-wrap: wrap; margin-bottom: 4px; }
  /* The search box grows with the window, between 280 and 460px; whatever
     doesn't fit beside it wraps to the next line, left-aligned. */
  .fits-only { display: flex; gap: 8px; align-items: center; font-size: 13px; cursor: pointer; }
  .checks { display: flex; gap: 8px 20px; flex-wrap: wrap; }
  .search { position: relative; display: flex; align-items: center; flex: 1 1 280px; max-width: 460px; min-width: 0; }
  .search :global(svg) { position: absolute; left: 9px; color: var(--muted); pointer-events: none; }
  .search input { width: 100%; min-width: 0; padding: 5px 8px 5px 30px; font-size: 13px; }
  .pick { display: flex; gap: 8px; align-items: center; min-width: 0; }
  .pick select { padding: 5px 8px; font-size: 13px; min-width: 0; max-width: 100%; }
  /* Narrow: the search box fills what the pickers leave of the first line
     (the whole line once they no longer fit beside it), the pickers share a
     line evenly, and the checkboxes go under them. */
  @media (max-width: 1100px) {
    .search { max-width: none; }
    .pick { flex: 1 1 170px; }
    .pick select { flex: 1; }
    .checks { flex-basis: 100%; }
  }
  .th-sort { border: none; background: none; padding: 0; font: inherit; letter-spacing: inherit; text-transform: inherit; color: inherit; text-align: left; cursor: pointer; }
  .th-sort:hover { color: var(--text); }
  .empty { padding: 18px; text-align: center; }
  .pager { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; margin: -8px 0 20px; }
  .pages { display: flex; gap: 4px; flex-wrap: wrap; }
  .pages button { min-width: 34px; padding: 5px 10px; font-size: 13px; }
  .pages button.active { background: var(--on-soft); border-color: var(--on); color: var(--on); font-weight: 600; }
  :global(.badge.community) { color: var(--violet); background: var(--surface-2); }
  .avail { display: flex; align-items: center; gap: 6px; font-size: 13px; }
  .table { padding: 6px 0; margin: 12px 0 20px; container-type: inline-size; }
  .tr {
    display: grid;
    /* Every row the same columns (a content-sized last column moved a row
       with a download in progress out of line with the rest). */
    grid-template-columns: minmax(240px, 3fr) 80px minmax(160px, 1.6fr) 84px 150px;
    gap: 12px;
    align-items: center;
    padding: 12px 18px;
    border-top: 1px solid var(--border);
  }
  .tr.th { border-top: none; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--faint); font-weight: 600; padding: 8px 18px; }
  .tr.selected { background: var(--on-soft); box-shadow: inset 3px 0 0 var(--on); }
  .tr.live { box-shadow: inset 4px 0 0 var(--on), 0 0 0 1px color-mix(in srgb, var(--on) 40%, transparent); }
  .live-banner { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; margin: 0 0 16px; font-size: 13px; }
  .name { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .meta { font-size: 12.5px; color: var(--text); }
  .meta .lic { color: var(--muted); }
  .title { font-weight: 600; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .link { border: none; background: none; padding: 0; color: var(--link); font-size: 11.5px; text-align: left; display: inline-flex; gap: 4px; align-items: center; }
  .link.plain { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .note { font-size: 11.5px; color: var(--faint); }
  .note.broken { color: var(--bad-text); }
  .num { font-variant-numeric: tabular-nums; }
  .need { display: flex; flex-direction: column; gap: 2px; }
  .need-top { display: flex; gap: 4px 6px; align-items: center; flex-wrap: wrap; }
  .need .total { white-space: nowrap; }
  .need .total { font-weight: 600; }
  .warn-note { color: var(--busy); }
  .estimate { margin: 0 0 12px; font-size: 12.5px; }
  .actions { display: flex; align-items: center; justify-content: flex-end; gap: 6px; }
  .actions button { display: inline-flex; align-items: center; gap: 6px; padding: 5px 12px; }
  .actions .danger:hover { color: var(--bad-text); border-color: var(--bad); }
  .icon { padding: 6px !important; }
  .progress { display: flex; flex-direction: column; gap: 4px; min-width: 80px; flex: 1; }
  .pbar { height: 6px; border-radius: 4px; background: var(--bg); overflow: hidden; }
  .pbar div { height: 100%; background: var(--on); transition: width 0.3s; }
  /* Size not known yet: the empty track pulses. (A sliding fill looked like
     progress that kept going up and falling back to 0.) */
  .pbar.indeterminate { animation: waiting 1.4s ease-in-out infinite; }
  .pbar.indeterminate div { width: 0 !important; }
  @keyframes waiting { 50% { opacity: 0.45; } }
  .progress .small { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px; font-size: 11.5px; }
  .small { font-size: 12px; }
  .date { white-space: nowrap; }
  .cell::before { display: none; }
  /* Narrow: each model becomes a card (name, then a wrapping line of facts,
     then its buttons), so nothing is squeezed or cut off at the right edge. */
  /* Narrow: a compact card. Name, its description, repo and notes on top;
     then one row with the download size, the VRAM need and the year on the
     left and the buttons on the right. The VRAM breakdown moves to a tooltip. */
  @container (max-width: 900px) {
    .tr.th { display: none; }
    .tr { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; padding: 14px 16px; }
    .tr .name { flex: 1 0 100%; }
    .cell { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
    .cell + .cell::before { display: inline; content: "·"; color: var(--faint); margin-right: 8px; }
    .need { flex-direction: row; }
    .need > :not(.need-top) { display: none; }
    .need-top { flex-wrap: nowrap; }
    .need-top::before { content: "needs"; color: var(--muted); font-weight: 400; }
    .cell[data-label="Download"]::after { content: "download"; color: var(--muted); }
    .actions { margin-left: auto; }
    .dl-error { flex: 1 0 100%; }
  }

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
