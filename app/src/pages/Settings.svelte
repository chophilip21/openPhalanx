<script lang="ts">
  import Icon from "../components/Icon.svelte";
  import Notice from "../components/Notice.svelte";
  import { api, errorText, type PortInfo, type Settings, type StoragePath } from "../lib/api";
  import { app } from "../lib/store.svelte";
  import { setThemeMode, theme } from "../lib/theme.svelte";

  // Every setting here is saved in settings.json at once and kept across
  // restarts and updates; nothing changes unless it is changed here.
  const snap = $derived(app.snapshot);
  const settings = $derived(snap?.settings);
  const st = $derived(snap?.server.state ?? "stopped");
  // What the backend started with can't change under it.
  const locked = $derived(st !== "stopped" && st !== "error" && st !== "paused");
  let error = $state("");
  let saved = $state("");

  function adopt(s: Settings) {
    if (app.snapshot) app.snapshot.settings = s;
  }

  async function save(what: string, call: () => Promise<Settings>) {
    error = "";
    saved = "";
    try {
      adopt(await call());
      saved = what;
      setTimeout(() => { if (saved === what) saved = ""; }, 2500);
    } catch (e) {
      error = errorText(e);
    }
  }

  // ---- Ports: one row is edited at a time ---------------------------------
  type Movable = NonNullable<PortInfo["setting"]>;
  const PORT_NAMES: Record<Movable, string> = {
    agent_port: "Client API port",
    admin_port: "Admin API port",
    control_port: "oppxs control port",
  };
  let editing = $state<Movable | null>(null);
  let draft = $state("");
  const draftNumber = $derived(Number(draft));
  const draftValid = $derived(Number.isInteger(draftNumber) && draftNumber >= 1 && draftNumber <= 65535);

  /** Why a row's Change button is greyed out, or "" when it can be used. */
  function cannotChange(p: PortInfo): string {
    if (!p.setting) return p.fixed_because ?? "This port is fixed.";
    // The backend was started with the client and admin ports.
    if (locked && p.setting !== "control_port") return "Stop the server to change this port; it applies at the next start.";
    return "";
  }

  function edit(p: PortInfo) {
    if (!p.setting) return;
    editing = p.setting;
    draft = String(p.port);
  }

  async function savePort(p: PortInfo) {
    if (!p.setting || !draftValid) return;
    if (draftNumber === p.port) {
      editing = null;
      return;
    }
    await save(PORT_NAMES[p.setting], () => api.setPort(p.setting!, draftNumber));
    if (!error) {
      editing = null;
      loadPorts();
    }
  }

  // ---- Ports -------------------------------------------------------------
  let ports = $state<PortInfo[]>([]);
  let portsError = $state("");
  let portsLoading = $state(false);
  async function loadPorts() {
    portsLoading = true;
    try {
      ports = await api.portOverview();
      portsError = "";
    } catch (e) {
      portsError = errorText(e);
    } finally {
      portsLoading = false;
    }
  }

  // ---- Storage -----------------------------------------------------------
  let paths = $state<StoragePath[]>([]);
  let copied = $state("");
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      copied = text;
      setTimeout(() => { if (copied === text) copied = ""; }, 1500);
    } catch {
      // Clipboard unavailable: the path is selectable.
    }
  }

  $effect(() => {
    loadPorts();
    api.storagePaths().then((p) => (paths = p)).catch(() => {});
    const t = setInterval(loadPorts, 10_000);
    return () => clearInterval(t);
  });

  // ---- Appearance ----------------------------------------------------------
  const themeMode = $derived(settings?.theme ?? "system");
  function pickTheme(mode: "light" | "dark" | "system") {
    setThemeMode(mode);
    save("Theme", () => api.setTheme(mode === "system" ? null : mode));
  }

  const REACH: Record<string, string> = {
    clients: "your laptops",
    "this machine": "this machine only",
    cluster: "other servers",
  };
</script>

<div class="page">
  <h1>Settings</h1>
  <p class="sub">
    Saved on this machine as soon as you change them, and kept across restarts and updates.
    {#if saved}<span class="saved"><Icon name="check" size={13} /> {saved} saved</span>{/if}
  </p>
  {#if error}<Notice onclose={() => (error = "")}>{error}</Notice>{/if}

  {#if settings}
    <section class="card">
      <span class="eyebrow titled"><Icon name="server" size={14} /> Server</span>
      {#if locked}
        <p class="note"><Icon name="lock" size={13} /> The server is running. The GPU and the client and admin ports apply at the next start; stop it to change them.</p>
      {/if}

      {#if snap && snap.gpus.length > 0}
        <div class="row">
          <div class="label">
            <span>GPU</span>
            <span class="muted small">The card the model runs on. A split model uses each server's own.</span>
          </div>
          <div class="control">
            <select value={String(settings.gpu_index)} disabled={locked || snap.gpus.length < 2} aria-label="GPU"
              onchange={(e) => save("GPU", () => api.setGpu(Number(e.currentTarget.value)))}>
              {#each snap.gpus as g}
                <option value={String(g.index)}>{g.index}: {g.name} ({(g.total_bytes / 2 ** 30).toFixed(0)} GB)</option>
              {/each}
            </select>
          </div>
        </div>
      {/if}

      <label class="row toggle">
        <div class="label">
          <span>Web search for clients</span>
          <span class="muted small">A private SearXNG next to the backend, so the model can look things up. Queries leave this server for public search engines. Applies at the next start.</span>
        </div>
        <input type="checkbox" checked={settings.web_search}
          onchange={(e) => save("Web search", () => api.setWebSearch(e.currentTarget.checked))} />
      </label>
    </section>

    <section class="card">
      <span class="eyebrow titled"><Icon name="shield" size={14} /> Privacy</span>
      <label class="row toggle">
        <div class="label">
          <span>Hide IP addresses</span>
          <span class="muted small">
            Shows addresses with their first digits hidden, like <span class="mono" data-real>xxx.xx8.1.74</span> for
            <span class="mono" data-real>192.168.1.74</span>, everywhere in the app: for screenshots and screen sharing.
            Copy buttons still copy the real address.
          </span>
        </div>
        <input type="checkbox" checked={settings.hide_ips}
          onchange={(e) => save("Privacy", () => api.setHideIps(e.currentTarget.checked))} />
      </label>
    </section>

    <section class="card">
      <div class="head">
        <span class="eyebrow titled"><Icon name="list" size={14} /> Ports</span>
        <button class="ghost" disabled={portsLoading} onclick={loadPorts} title="Check again"><Icon name="refresh" size={13} /></button>
      </div>
      <p class="muted small">
        What this server listens on and who holds each port now. Open the client API port to your laptops' network,
        and the cluster ports between servers. Never expose the ports marked "this machine only". The client, admin
        and oppxs control ports can move (at the next start; after moving the client port, laptops pair again with
        the new address). The cluster's ports can't: every server must use the same ones.
      </p>
      {#if portsError}<p class="bad small">{portsError}</p>{/if}
      <div class="ports">
        {#each ports as p}
          {@const why = cannotChange(p)}
          <div class="port-row" class:held={p.holders.length > 0}>
            {#if editing && editing === p.setting}
              <span class="edit">
                <input class="port mono" inputmode="numeric" bind:value={draft} aria-label={PORT_NAMES[editing]}
                  onkeydown={(e) => { if (e.key === "Enter") savePort(p); if (e.key === "Escape") editing = null; }} />
              </span>
            {:else}
              <span class="mono num">{p.port}{p.last ? `–${p.last}` : ""}<span class="muted">/{p.proto}</span></span>
            {/if}
            <span class="what">
              <span>{p.purpose}</span>
              <span class="muted small">{REACH[p.reach] ?? p.reach} · {p.when}</span>
            </span>
            <span class="who small">
              {#if p.holders.length}
                <span class="dot on"></span>{p.holders.join(", ")}
              {:else}
                <span class="dot"></span><span class="muted">free</span>
              {/if}
            </span>
            <span class="act">
              {#if editing && editing === p.setting}
                <button class="primary small-btn" disabled={!draftValid} onclick={() => savePort(p)}>Save</button>
                <button class="small-btn" onclick={() => (editing = null)}>Cancel</button>
              {:else}
                <!-- Greyed out when the port can't move; the tooltip says why. -->
                <span class="why" title={why}>
                  <button class="small-btn" disabled={!!why || editing !== null} onclick={() => edit(p)}>Change</button>
                </span>
              {/if}
            </span>
          </div>
        {/each}
      </div>
    </section>

    <section class="card">
      <span class="eyebrow titled"><Icon name="folder" size={14} /> Storage</span>
      <div class="paths">
        {#each paths as p}
          <div class="path-row">
            <div class="label">
              <span>{p.label}</span>
              <span class="muted small">{p.detail}</span>
            </div>
            <div class="path">
              <span class="mono small selectable" class:missing={!p.exists}>{p.path}{p.exists ? "" : " (not created yet)"}</span>
              <button class="ghost" title="Copy the path" onclick={() => copy(p.path)}>
                <Icon name={copied === p.path ? "check" : "copy"} size={13} />
              </button>
            </div>
          </div>
        {/each}
      </div>
    </section>

    <section class="card">
      <span class="eyebrow titled"><Icon name={theme.current === "dark" ? "moon" : "sun"} size={14} /> Appearance</span>
      <div class="row">
        <div class="label">
          <span>Theme</span>
          <span class="muted small">System follows your desktop's light or dark setting.</span>
        </div>
        <div class="segmented" role="radiogroup" aria-label="Theme">
          {#each [["system", "System"], ["light", "Light"], ["dark", "Dark"]] as [mode, label]}
            <button role="radio" aria-checked={themeMode === mode} class:active={themeMode === mode}
              onclick={() => pickTheme(mode as "light" | "dark" | "system")}>{label}</button>
          {/each}
        </div>
      </div>
    </section>
  {/if}
</div>

<style>
  section { display: flex; flex-direction: column; gap: 4px; padding: 16px 18px; margin-bottom: 16px; }
  .head { display: flex; align-items: center; justify-content: space-between; }
  .row { display: flex; align-items: center; gap: 18px; padding: 12px 0; border-top: 1px solid var(--border); flex-wrap: wrap; }
  .eyebrow + .row, .eyebrow + .note + .row { border-top: none; }
  .label { display: flex; flex-direction: column; gap: 3px; flex: 1; min-width: 240px; }
  .control { display: flex; gap: 8px; align-items: center; }
  .toggle { cursor: pointer; }
  .toggle input { width: 18px; height: 18px; flex: none; }
  .small { font-size: 12.5px; }
  .port { width: 92px; text-align: right; }
  select { min-width: 220px; }
  .note { display: flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--warn-text); margin: 6px 0 2px; }
  .saved { display: inline-flex; align-items: center; gap: 4px; color: var(--on); margin-left: 8px; font-size: 12.5px; }
  .bad { color: var(--bad-text); }
  .ports { display: flex; flex-direction: column; margin-top: 6px; }
  .port-row { display: grid; grid-template-columns: 120px 1fr minmax(140px, 0.7fr) 150px; gap: 12px; align-items: center; padding: 9px 0; border-top: 1px solid var(--border); }
  .port-row:first-child { border-top: none; }
  .num { font-size: 13px; }
  .what { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
  .who { display: flex; align-items: center; gap: 7px; overflow-wrap: anywhere; }
  .act { display: flex; gap: 6px; justify-content: flex-end; }
  .small-btn { padding: 4px 10px; font-size: 12.5px; }
  .small-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .why { display: inline-flex; }
  .edit .port { width: 92px; }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--faint); flex: none; }
  .dot.on { background: var(--on); box-shadow: 0 0 6px var(--on-glow); }
  .paths { display: flex; flex-direction: column; }
  .path-row { display: flex; gap: 18px; padding: 10px 0; border-top: 1px solid var(--border); flex-wrap: wrap; align-items: center; }
  .eyebrow + .paths .path-row:first-child { border-top: none; }
  .path { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 1.3; }
  .path .mono { overflow-wrap: anywhere; }
  .missing { color: var(--faint); }
  .ghost { border: none; background: none; color: var(--muted); padding: 4px; display: inline-grid; place-items: center; }
  .ghost:hover:not(:disabled) { color: var(--text); }
  .segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 9px; overflow: hidden; }
  .segmented button { border: none; border-radius: 0; background: transparent; color: var(--muted); padding: 6px 14px; }
  .segmented button + button { border-left: 1px solid var(--border); }
  .segmented button.active { background: var(--surface-2); color: var(--text); font-weight: 600; }
  @media (max-width: 760px) {
    .port-row { grid-template-columns: 1fr; gap: 4px; }
    .act { justify-content: flex-start; }
  }
</style>
