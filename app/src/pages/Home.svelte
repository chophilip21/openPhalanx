<script lang="ts">
  import Checklist from "../components/Checklist.svelte";
  import ContextSlider from "../components/ContextSlider.svelte";
  import Dashboard from "../components/Dashboard.svelte";
  import Icon from "../components/Icon.svelte";
  import PairingCard from "../components/PairingCard.svelte";
  import PowerButton from "../components/PowerButton.svelte";
  import RunningModel from "../components/RunningModel.svelte";
  import VramBar from "../components/VramBar.svelte";
  import { api, errorText, type Preflight } from "../lib/api";
  import { gib, tokens } from "../lib/format";
  import { app } from "../lib/store.svelte";
  import { forceLoad, goto as navigate, nav } from "../lib/nav.svelte";
  import { condition, type NoteBody } from "../lib/notify.svelte";
  import { onDestroy } from "svelte";

  let { goto }: { goto: (page: string) => void } = $props();

  let preflight = $state<Preflight | null>(null);
  let actionError = $state("");
  // What this page has to say goes to the window's notification stack, not
  // into the page: a state that holds (a member lacks the model, GPU memory is
  // almost full) is one note that updates in place and leaves by itself; only
  // hints and errors can be closed. Leaving the page takes them along.
  const mine = new Set<string>();
  function note(id: string, body: () => NoteBody | null) {
    mine.add(id);
    $effect(() => condition(id, body()));
  }
  onDestroy(() => mine.forEach((id) => condition(id, null)));
  let acting = $state(false);

  const snap = $derived(app.snapshot);
  const st = $derived(snap?.server.state ?? "stopped");
  const admin = $derived(snap?.admin ?? null);
  const gpu = $derived(snap?.gpus.find((g) => g.index === snap?.settings.gpu_index) ?? null);
  const idle = $derived(st === "stopped" || st === "error" || st === "paused");
  const cluster = $derived(snap?.cluster ?? null);
  // A member whose host is serving: only the host starts the cluster's server.
  const locked = $derived(idle && !!cluster?.locked_by_host);
  // As host: members that report the cluster's model missing, or fetching it.
  const wanted = $derived(cluster?.role.role === "host" ? cluster.desired_model : null);
  const copies = $derived(
    (cluster?.members ?? [])
      .filter((m) => m.online)
      .map((m) => ({ name: m.name, sync: m.report?.model_sync ?? null }))
      .filter((m) => wanted && m.sync?.repo === wanted.repo && m.sync?.revision === wanted.revision),
  );
  const missing = $derived(copies.filter((m) => m.sync?.state === "missing"));
  const fetching = $derived(copies.filter((m) => m.sync?.state === "downloading"));
  let approving = $state(false);
  async function approveDownload() {
    approving = true;
    actionError = "";
    try {
      await api.clusterApproveDownload();
    } catch (e) {
      actionError = errorText(e);
    } finally {
      approving = false;
    }
  }
  const pct = (s: { done_bytes: number; total_bytes: number }) =>
    s.total_bytes ? Math.floor((100 * s.done_bytes) / s.total_bytes) : 0;
  const hostName = $derived(cluster?.role.role === "member" ? cluster.role.host.name : "");

  const headline = $derived(
    locked ? `${hostName} is running the cluster` : {
      stopped: "Server is off",
      starting: "Starting…",
      running: "Server is running",
      stopping: "Stopping…",
      error: "Server stopped with an error",
      paused: "Serving paused",
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
        if (st === "error" || st === "paused") await api.dismissError();
        await api.start(forced);
        forceLoad.on = false;
        forceLoad.restore = null;
      } else {
        await api.stop();
      }
    } catch (e) {
      actionError = errorText(e);
    } finally {
      acting = false;
    }
  }

  const blocking = $derived(preflight?.checks.find((c) => c.status === "fail") ?? null);

  note("start-hint", () =>
    showHint
      ? {
          kind: canToggle || !blocking ? "info" : "warn",
          text: `${nav.startHint} is selected. ` + (canToggle
            ? "Press the power button to start the server."
            : blocking ? `It can't start yet: ${blocking.detail}` : "Checking whether it can start…"),
          onclose: () => (nav.startHint = null),
        }
      : null);
  note("cluster-download", () =>
    idle && wanted && missing.length
      ? {
          kind: "info",
          text: `${missing.map((m) => m.name).join(", ")} ${missing.length === 1 ? "doesn't" : "don't"} have ${wanted.label} `
            + `(${gib(wanted.weight_bytes)}). Download it there, so the model can be split across the cluster?`,
          action: { label: `Download on ${missing.length === 1 ? missing[0].name : "them"}`, run: approveDownload, busy: approving },
        }
      : null);
  note("cluster-fetching", () =>
    idle && wanted && !missing.length && fetching.length
      ? {
          kind: "info",
          text: fetching.map((m) => `${m.name} is downloading ${wanted.label}: ${pct(m.sync!)}%`).join(" · ") + ". Start when it's done.",
        }
      : null);
  note("logs-hint", () =>
    showLogsHint
      ? {
          kind: "info",
          text: "Loading the model can take up to a few minutes. Follow its progress live in Logs.",
          action: { label: "Open Logs", run: () => navigate("logs") },
          onclose: () => (logsHintClosed = true),
        }
      : null);
  note("context-note", () => (ctxNote ? { kind: "warn", text: ctxNote, onclose: () => (ctxNote = "") } : null));
  note("server-error", () => (actionError ? { kind: "error", text: actionError, onclose: () => (actionError = "") } : null));
  // The server's own warnings (GPU memory almost full). The number in the text
  // changes with every snapshot; the note is the same one.
  note("server-warning", () => {
    const w = snap?.warnings ?? [];
    return w.length ? { kind: "warn", text: w.join(" ") } : null;
  });
  // Not enough VRAM is the one check the user may override, by ticking a box.
  const forceable = $derived(
    idle && !locked && !preflight?.running && preflight?.checks.filter((c) => c.status === "fail").every((c) => c.id === "vram") === true && blocking?.id === "vram",
  );
  // The tick lives outside the page (it survives a visit to another tab) and
  // is dropped once the checks are in and no longer ask for it: the model
  // fits now, or something else blocks the start.
  $effect(() => {
    if (preflight && !forceable && forceLoad.on) {
      forceLoad.on = false;
      forceLoad.restore = null;
    }
  });
  const forced = $derived(forceable && forceLoad.on);
  let ctxNote = $state("");
  // Ticking it brings the context window down to the longest that fits (the 2k
  // minimum when none does), so the server isn't asked for a window the GPU
  // can't hold; unticking puts the earlier window back.
  async function setForce(on: boolean) {
    forceLoad.on = on;
    ctxNote = "";
    const now = snap?.settings.context_len ?? 32768;
    if (!on) {
      const back = forceLoad.restore;
      forceLoad.restore = null;
      if (back != null && back !== now) await setContext(back);
      return;
    }
    try {
      const limit = await api.contextLimit();
      const target = Math.min(now, limit.max_fit ?? MIN_CONTEXT);
      if (target < now) {
        forceLoad.restore = now;
        await setContext(target);
        ctxNote = limit.max_fit != null
          ? `Context window set to ${tokens(target)}, the longest ${limit.model ?? "the model"} fits right now.`
          : `Context window set to ${tokens(target)}, the minimum: ${limit.model ?? "the model"} doesn't fit this GPU by our estimate.`;
      }
    } catch (e) {
      actionError = errorText(e);
    }
  }
  const MIN_CONTEXT = 2048;
  // Forced: the VRAM check is overridden, so it reads as a warning, not a stop.
  const checks = $derived((preflight?.checks ?? []).map((c) => (forced && c.id === "vram" ? { ...c, status: "warn" as const } : c)));
  const canToggle = $derived(
    !acting &&
      (idle ? !locked && (!!preflight?.can_start || forced) : st === "running" || st === "starting" || st === "external"),
  );
  // The same setting as the Models page's slider; locked while a backend runs.
  const ctxLocked = $derived(["starting", "running", "stopping", "external"].includes(st));
  async function setContext(ctx: number) {
    try {
      const settings = await api.setContextLen(ctx);
      if (app.snapshot) app.snapshot.settings = settings; // both pages see it at once
      actionError = "";
    } catch (e) {
      actionError = errorText(e);
    }
    await refreshPreflight();
  }
  const passed = $derived(preflight?.checks.filter((c) => c.status === "pass").length ?? 0);
</script>

<div class="page">
  <div class="home">
    <section class="hero">
      <div class="status-line">
        <span class="dot {st}"></span>
        <span class="eyebrow">{snap?.endpoint ? `Agent API · ${snap.endpoint}` : "Agent API"}</span>
      </div>

      <div class="power-wrap" class:beckon={showHint && canToggle}>
        <PowerButton state={st} {locked} disabled={!canToggle} onclick={toggle} />
      </div>

      <h2 class="headline">{headline}</h2>
      {#if snap?.server.model && !idle}
        <div class="running-model"><RunningModel model={snap.server.model} loading={st === "starting"} /></div>
      {/if}
      <p class="detail">
        {#if locked}
          <span class="locked-note">
            <Icon name="lock" size={14} /> {hostName} started the server and controls this cluster, so this machine can't
            start one too. To run on your own, leave the cluster from the dashboard below.
          </span>
        {:else if st === "paused" && snap?.server.detail}
          <span class="paused-note"><Icon name="alert" size={14} /> {snap.server.detail}</span>
        {:else if snap?.server.detail}
          {snap.server.detail}
        {:else if idle && blocking && forced}
          <span class="forced"><Icon name="alert" size={14} /> Force load is on. {blocking.detail}</span>
        {:else if idle && blocking}
          <span class="blocked"><Icon name="alert" size={14} /> {blocking.detail}</span>
        {:else if idle}
          Press the button to start the backend.
        {:else if st === "running"}
          Clients can connect with a pairing code.
        {/if}
      </p>

      {#if forceable}
        <label class="force">
          <input type="checkbox" checked={forceLoad.on} onchange={(e) => setForce(e.currentTarget.checked)} />
          <span>
            <strong>Force load.</strong> I understand it may fail to load, or crash when it runs out of GPU memory, and
            I would still like to proceed.
          </span>
        </label>
      {/if}

      {#if snap?.cluster?.role.role === "member"}
        <p class="member-note">
          This server is a member of <strong>{snap.cluster.role.host.name}</strong>'s cluster. Clients pair with the host
          ({snap.cluster.role.host.url.replace("https://", "").replace(":9092", "")}), not here.
        </p>
      {:else if admin && (st === "running" || st === "starting")}
        <PairingCard pairing={admin.pairing} endpoint={snap?.endpoint ?? null} />
      {/if}
    </section>

    {#if idle}
      <section class="setup">
        <div class="card model">
          <div class="row">
            <span class="eyebrow titled"><Icon name="cpu" size={14} /> Model</span>
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
                  fit={forced ? "tight" : (preflight.fit?.fit ?? null)}
                />
              </div>
            {/if}
          {:else}
            <div class="muted">No model selected.</div>
          {/if}
          <div class="ctx">
            <span class="muted small-text">Context window</span>
            <ContextSlider compact value={snap?.settings.context_len ?? 32768} locked={ctxLocked} {forced} onchange={setContext} />
          </div>
        </div>

        {#if preflight}
          <div class="card">
            <div class="row">
              <span class="eyebrow titled"><Icon name="checklist" size={14} /> Pre-flight checks</span>
              <span class="muted small-text">{passed} of {preflight.checks.length} passed</span>
            </div>
            <Checklist {checks} />
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
  .dot.error, .dot.paused { background: var(--bad); }
  .force {
    display: flex; gap: 10px; align-items: flex-start; max-width: 560px; margin: 4px auto 0; padding: 10px 14px;
    border: 1px solid var(--busy-glow); border-radius: 10px; background: var(--busy-soft); color: var(--warn-text);
    font-size: 13px; text-align: left; cursor: pointer;
  }
  .force input { margin-top: 2px; flex: none; }
  .running-model { display: flex; justify-content: center; margin: 2px 0 6px; }
  .locked-note { color: var(--violet); display: inline-flex; gap: 6px; align-items: baseline; max-width: 560px; }
  .paused-note { color: var(--bad); display: inline-flex; gap: 6px; align-items: baseline; max-width: 560px; font-weight: 500; }
  .headline { margin: 4px 0 0; font-size: 26px; font-weight: 650; }
  .detail { margin: 0; color: var(--muted); max-width: 460px; min-height: 20px; }
  .forced { color: var(--warn-text); display: inline-flex; gap: 6px; align-items: flex-start; text-align: left; }
  .blocked { color: var(--bad-text); display: inline-flex; gap: 6px; align-items: flex-start; text-align: left; }
  /* Side by side, the model card and the pre-flight list are always the same height. */
  .setup { width: 100%; display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; align-items: stretch; }
  .setup > .card { display: flex; flex-direction: column; }
  .row { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
  .small { padding: 3px 10px; font-size: 12.5px; }
  .model-name { font-size: 16px; font-weight: 600; }
  .small-text { font-size: 12.5px; }
  .vram { margin-top: 14px; }
  .ctx { margin-top: 14px; display: flex; flex-direction: column; gap: 2px; }
  .power-wrap { border-radius: 50%; }
  .power-wrap.beckon { animation: beckon 1.6s ease-in-out infinite; }
  @keyframes beckon {
    0%, 100% { filter: drop-shadow(0 0 0 transparent); }
    50% { filter: drop-shadow(0 0 22px var(--on-glow)); }
  }
  .member-note { max-width: 560px; margin: 0; padding: 12px 16px; border-radius: 12px; background: var(--surface); border: 1px solid var(--border); font-size: 13.5px; }
</style>
