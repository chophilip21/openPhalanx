<script lang="ts">
  import Checklist from "../components/Checklist.svelte";
  import Dashboard from "../components/Dashboard.svelte";
  import Icon from "../components/Icon.svelte";
  import PairingCard from "../components/PairingCard.svelte";
  import PowerButton from "../components/PowerButton.svelte";
  import VramBar from "../components/VramBar.svelte";
  import { api, errorText, type Preflight } from "../lib/api";
  import { gib, tokens } from "../lib/format";
  import { app } from "../lib/store.svelte";
  import { goto as navigate, nav } from "../lib/nav.svelte";

  let { goto }: { goto: (page: string) => void } = $props();

  let preflight = $state<Preflight | null>(null);
  let actionError = $state("");
  let acting = $state(false);

  const snap = $derived(app.snapshot);
  const st = $derived(snap?.server.state ?? "stopped");
  const admin = $derived(snap?.admin ?? null);
  const gpu = $derived(snap?.gpus.find((g) => g.index === snap?.settings.gpu_index) ?? null);
  const idle = $derived(st === "stopped" || st === "error");

  const headline = $derived(
    {
      stopped: "Server is off",
      starting: "Starting…",
      running: "Server is running",
      stopping: "Stopping…",
      error: "Server stopped with an error",
      external: "Running outside the app",
    }[st],
  );

  async function refreshPreflight() {
    try {
      preflight = await api.preflight();
    } catch (e) {
      actionError = errorText(e);
    }
  }

  $effect(() => {
    // Re-check while idle; the snapshot already covers the running state.
    if (!idle) return;
    refreshPreflight();
    const t = setInterval(refreshPreflight, 5000);
    return () => clearInterval(t);
  });

  // "Press start" hint after choosing a model; gone once the server moves.
  $effect(() => {
    if (!idle && nav.startHint) nav.startHint = null;
  });
  const showHint = $derived(!!nav.startHint && idle);

  // While starting, point to the Logs page (loading takes a few minutes).
  let logsHintClosed = $state(false);
  $effect(() => {
    if (st !== "starting") logsHintClosed = false; // show again on the next start
  });
  const showLogsHint = $derived(st === "starting" && !logsHintClosed);

  async function toggle() {
    nav.startHint = null;
    actionError = "";
    acting = true;
    try {
      if (idle) {
        if (st === "error") await api.dismissError();
        await api.start();
      } else {
        await api.stop();
      }
    } catch (e) {
      actionError = errorText(e);
    } finally {
      acting = false;
    }
  }

  const canToggle = $derived(
    !acting && (idle ? !!preflight?.can_start : st === "running" || st === "starting" || st === "external"),
  );
  const blocking = $derived(preflight?.checks.find((c) => c.status === "fail") ?? null);
</script>

<div class="page">
  <div class="home">
    <section class="hero">
      <div class="status-line">
        <span class="dot {st}"></span>
        <span class="eyebrow">{snap?.endpoint ? `Agent API · ${snap.endpoint}` : "Agent API"}</span>
      </div>

      {#if showHint}
        <div class="hint" class:blocked-hint={!canToggle} role="status">
          <span>
            <strong>{nav.startHint}</strong> is selected.
            {#if canToggle}
              Press the power button to start the server.
            {:else if blocking}
              It can't start yet: {blocking.detail}
            {:else}
              Checking whether it can start…
            {/if}
          </span>
          <button class="ghost close" onclick={() => (nav.startHint = null)} aria-label="Dismiss"><Icon name="x" size={14} /></button>
        </div>
      {/if}

      <div class="power-wrap" class:beckon={showHint && canToggle}>
        <PowerButton state={st} disabled={!canToggle} onclick={toggle} />
      </div>

      <h2 class="headline">{headline}</h2>
      <p class="detail">
        {#if snap?.server.detail}
          {snap.server.detail}
        {:else if idle && blocking}
          <span class="blocked"><Icon name="alert" size={14} /> {blocking.detail}</span>
        {:else if idle}
          Press the button to start the backend.
        {:else if st === "running"}
          Clients can connect with a pairing code.
        {/if}
      </p>

      {#if showLogsHint}
      <div class="hint info" role="status">
        <Icon name="terminal" size={16} />
        <span>Loading the model can take up to a few minutes. Follow its progress live in Logs.</span>
        <button class="logs-btn" onclick={() => navigate("logs")}>Open Logs</button>
        <button class="ghost close" onclick={() => (logsHintClosed = true)} aria-label="Dismiss"><Icon name="x" size={14} /></button>
      </div>
    {/if}

    {#if actionError}
        <div class="error-banner"><Icon name="alert" size={16} /><span class="selectable">{actionError}</span></div>
      {/if}
      {#each snap?.warnings ?? [] as w}
        <div class="error-banner warn"><Icon name="alert" size={16} /><span>{w}</span></div>
      {/each}

      {#if admin && (st === "running" || st === "starting")}
        <PairingCard pairing={admin.pairing} endpoint={snap?.endpoint ?? null} />
      {/if}
    </section>

    {#if idle}
      <section class="setup">
        <div class="card model">
          <div class="row">
            <span class="eyebrow">Model</span>
            <button class="ghost small" onclick={() => goto("models")}>Change</button>
          </div>
          {#if preflight?.model}
            <div class="model-name">{preflight.model.label}</div>
            <div class="muted small-text">
              {[preflight.model.quant, preflight.requirement && `${tokens(preflight.requirement.context_len)} context`, preflight.requirement && `needs ${gib(preflight.requirement.total_bytes)}`].filter(Boolean).join(" · ")}
            </div>
            {#if preflight.requirement && gpu}
              <div class="vram">
                <VramBar
                  weights={preflight.requirement.weight_bytes}
                  kv={preflight.requirement.kv_bytes}
                  overhead={preflight.requirement.overhead_bytes}
                  available={gpu.free_bytes}
                  total={gpu.total_bytes}
                  fit={preflight.fit?.fit ?? null}
                />
              </div>
            {/if}
          {:else}
            <div class="muted">No model selected.</div>
          {/if}
          <label class="toggle">
            <input type="checkbox" checked={snap?.settings.web_search ?? true}
              onchange={(e) => api.setWebSearch(e.currentTarget.checked).then(refreshPreflight)} />
            <span>
              Web search for clients
              <span class="muted small-text">private SearXNG on this server</span>
            </span>
          </label>
        </div>

        {#if preflight}
          <div class="card">
            <div class="row"><span class="eyebrow">Pre-flight checks</span></div>
            <Checklist checks={preflight.checks} />
          </div>
        {/if}
      </section>
    {/if}

    <Dashboard />
  </div>
</div>

<style>
  .home { max-width: 1240px; margin: 0 auto; display: flex; flex-direction: column; align-items: center; gap: 28px; }
  .hero { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 14px; padding-top: 12px; width: 100%; }
  .status-line { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
  .dot.running { background: var(--on); box-shadow: 0 0 10px var(--on); }
  .dot.starting, .dot.stopping, .dot.external { background: var(--busy); }
  .dot.error { background: var(--bad); }
  .headline { margin: 4px 0 0; font-size: 26px; font-weight: 650; }
  .detail { margin: 0; color: var(--muted); max-width: 460px; min-height: 20px; }
  .blocked { color: var(--bad-text); display: inline-flex; gap: 6px; align-items: flex-start; text-align: left; }
  .error-banner { max-width: 560px; text-align: left; }
  .error-banner.warn { border-color: var(--busy-glow); background: var(--busy-soft); color: var(--warn-text); }
  .setup { width: 100%; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; align-items: start; }
  .row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .small { padding: 3px 10px; font-size: 12.5px; }
  .model-name { font-size: 16px; font-weight: 600; }
  .small-text { font-size: 12.5px; }
  .vram { margin-top: 14px; }
  .toggle { display: flex; gap: 10px; align-items: flex-start; margin-top: 14px; font-size: 13px; cursor: pointer; }
  .toggle input { margin-top: 3px; }
  .toggle span { display: flex; flex-direction: column; }
  .hint {
    position: relative;
    display: flex;
    align-items: center;
    gap: 10px;
    max-width: 520px;
    padding: 10px 12px 10px 16px;
    border-radius: 12px;
    background: var(--on-soft);
    border: 1px solid var(--on);
    color: var(--text);
    font-size: 13.5px;
    text-align: left;
    box-shadow: var(--shadow);
    animation: drop 0.35s ease-out;
  }
  .hint::after {
    content: "";
    position: absolute;
    left: 50%;
    bottom: -7px;
    width: 12px;
    height: 12px;
    transform: translateX(-50%) rotate(45deg);
    background: inherit;
    border-right: 1px solid var(--on);
    border-bottom: 1px solid var(--on);
    background-color: var(--surface);
    background-image: linear-gradient(var(--on-soft), var(--on-soft));
  }
  .hint.blocked-hint { background: var(--busy-soft); border-color: var(--busy); }
  .hint.blocked-hint::after { border-color: var(--busy); background-image: linear-gradient(var(--busy-soft), var(--busy-soft)); }
  .hint .close { padding: 4px; margin-left: auto; }
  .power-wrap { border-radius: 50%; }
  .power-wrap.beckon { animation: beckon 1.6s ease-in-out infinite; }
  @keyframes beckon {
    0%, 100% { filter: drop-shadow(0 0 0 transparent); }
    50% { filter: drop-shadow(0 0 22px var(--on-glow)); }
  }
  @keyframes drop {
    from { opacity: 0; transform: translateY(-6px); }
    to { opacity: 1; transform: none; }
  }
  .hint.info { background: var(--surface); border-color: var(--busy); animation: drop 0.35s ease-out; }
  .hint.info::after { bottom: auto; top: -7px; transform: translateX(-50%) rotate(225deg); border-color: var(--busy); background-image: none; }
  .hint.info :global(svg) { color: var(--busy); flex-shrink: 0; }
  .logs-btn { padding: 5px 12px; font-size: 12.5px; white-space: nowrap; }
</style>
