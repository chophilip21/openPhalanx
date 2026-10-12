<script lang="ts">
  import { onMount } from "svelte";
  import Icon from "./components/Icon.svelte";
  import Notifications from "./components/Notifications.svelte";
  import ReportDialog from "./components/ReportDialog.svelte";
  import UpdatePrompt from "./components/UpdatePrompt.svelte";
  import { ask } from "@tauri-apps/plugin-dialog";
  import { api, errorText, openLink, type UpdateCheck } from "./lib/api";
  import { condition, notify } from "./lib/notify.svelte";
  import logo from "./assets/logo.svg";
  import Devices from "./pages/Devices.svelte";
  import Home from "./pages/Home.svelte";
  import Logs from "./pages/Logs.svelte";
  import Trace from "./pages/Trace.svelte";
  import Models from "./pages/Models.svelte";
  import Settings from "./pages/Settings.svelte";
  import { setIpMasking } from "./lib/privacy";
  import { app, connect } from "./lib/store.svelte";
  import { adoptTheme, theme, toggleTheme } from "./lib/theme.svelte";
  import { goto, nav } from "./lib/nav.svelte";

  const NAV = [
    { id: "home", label: "Server", icon: "power" },
    { id: "devices", label: "Client", icon: "laptop" },
    { id: "models", label: "Models", icon: "box" },
    { id: "logs", label: "Logs", icon: "terminal" },
    { id: "trace", label: "Trace", icon: "activity" },
    { id: "settings", label: "Settings", icon: "settings" },
  ];

  const REPO = "https://github.com/chophilip21/openPhalanx";
  let failed = $state("");
  let update = $state<UpdateCheck | null>(null);
  let checking = $state(false);
  let updateNote = $state("");

  // The launch screen is plain markup in index.html, so it is on screen before
  // any of this code has loaded. It stays at least this long after its first
  // frame, even when the backend answers at once.
  const SPLASH_MIN_MS = 2000;
  const loaded = $derived(app.snapshot !== null || failed !== "");
  let splashClosing = false;

  $effect(() => {
    if (!loaded || splashClosing) return;
    splashClosing = true;
    const el = document.getElementById("splash");
    if (!el) return;
    const shown = performance.now() - (window.__splashShownAt ?? performance.now());
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 400);
    }, Math.max(0, SPLASH_MIN_MS - shown));
  });

  onMount(() => {
    connect().catch((e) => (failed = String(e)));
    // Ask once per launch; offline or rate-limited stays quiet.
    api.checkUpdate().then((c) => { if (c.available) update = c; }).catch(() => {});
  });

  async function checkNow() {
    checking = true;
    updateNote = "";
    try {
      const c = await api.checkUpdate();
      if (c.available) update = c;
      else updateNote = "Up to date";
    } catch (e) {
      updateNote = errorText(e);
    } finally {
      checking = false;
    }
  }

  const st = $derived(app.snapshot?.server.state ?? "stopped");

  // ---- Report a problem -----------------------------------------------------
  let reporting = $state(false);
  let reportIntro = $state("");

  function openReport(intro = "") {
    reportIntro = intro;
    reporting = true;
  }

  function closeReport() {
    reporting = false;
    // Reported or looked at: the note about the last run has done its job.
    if (app.snapshot?.unclean) api.dismissUnclean().catch(() => {});
  }

  const clock = (t: number) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  // The last run did not end cleanly: say so once, on every page, with a way to report it.
  const unclean = $derived(app.snapshot?.unclean ?? null);
  $effect(() => {
    const u = unclean;
    condition("unclean-exit", u ? {
      kind: "warn",
      text: `Openphalanx didn't shut down cleanly last time: it was last seen at ${clock(u.seen)} with the server ${u.state}. ` +
        "The app was killed, it crashed, or this machine froze. What it recorded up to then is kept.",
      action: {
        label: "Report it…",
        run: () => openReport(`The last run stopped without shutting down (last seen at ${clock(u.seen)}, server ${u.state}).`),
      },
      onclose: () => { api.dismissUnclean().catch(() => {}); },
    } : null);
  });

  // ---- Ports held by another server process on this machine ---------------------
  // The cluster service can't start while a leftover `oppxs` or a second copy
  // of the app holds its ports, and this machine is then invisible to the
  // others. That is an alert with a button, on every page: it used to be a
  // small underlined "Free the ports…" at the bottom of the cluster panel.
  const clusterError = $derived(app.snapshot?.cluster?.error ?? null);
  let freeing = $state(false);

  async function freePorts() {
    freeing = true;
    try {
      const holders = await api.portHolders();
      const ours = holders.filter((h) => h.ours && h.pid != null);
      const others = holders.filter((h) => !h.ours);
      const describe = (h: (typeof holders)[number]) =>
        h.pid != null ? `${h.name} (pid ${h.pid}) on port ${h.port}` : `another user's process on port ${h.port}`;
      if (!ours.length) {
        if (others.length) {
          notify("error", `The ports are held by something that isn't Openphalanx: ${others.map(describe).join("; ")}. Stop it yourself; this app won't.`);
        } else {
          notify("info", "Nothing is holding the ports now; the cluster service starts by itself within a few seconds.");
        }
        return;
      }
      const names = [...new Set(ours.map((h) => `${h.name} (pid ${h.pid})`))].join(", ");
      const yes = await ask(
        `Stop ${names}? It is another Openphalanx server on this machine holding the ports this app needs. If it is serving a model, that stops too.`,
        { title: "Free the ports", kind: "warning" },
      );
      if (!yes) return;
      const [stopped, left] = await api.freePorts();
      notify("info", `Stopped ${stopped.join(", ")}. The cluster service starts within a few seconds.`);
      if (left.length) notify("error", `Still held by something else: ${left.map(describe).join("; ")}.`);
    } catch (e) {
      notify("error", errorText(e), { label: "Report a problem…", run: () => openReport() });
    } finally {
      freeing = false;
    }
  }

  $effect(() => {
    const e = clusterError;
    const busy = freeing;
    condition("cluster-service", e ? {
      kind: "error",
      text: e,
      action: { label: busy ? "Freeing the ports…" : "Free the ports", run: freePorts, busy },
    } : null);
  });

  // Settings that change the whole window: IP masking and the saved theme
  // (settings.json is the record; it survives updates).
  const hideIps = $derived(app.snapshot?.settings.hide_ips ?? false);
  $effect(() => setIpMasking(hideIps));
  const savedTheme = $derived(app.snapshot ? (app.snapshot.settings.theme ?? null) : undefined);
  $effect(() => { if (savedTheme !== undefined) adoptTheme(savedTheme); });

  async function flipTheme() {
    toggleTheme();
    try {
      const s = await api.setTheme(theme.current);
      if (app.snapshot) app.snapshot.settings = s;
    } catch {
      // Not saved: it still applies to this window.
    }
  }
</script>

<div class="shell">
  <nav>
    <div class="brand">
      <img class="logo" src={logo} alt="" />
      <span>Openphalanx</span>
    </div>
    {#each NAV as item}
      <button class="nav-item" class:active={nav.page === item.id} onclick={() => goto(item.id)}>
        <Icon name={item.icon} size={18} />
        <span>{item.label}</span>
        {#if item.id === "home"}<span class="pip {st}"></span>{/if}
      </button>
    {/each}
    <div class="spacer"></div>
    <button class="nav-item theme" onclick={flipTheme} title="Switch between light and dark">
      <Icon name={theme.current === "dark" ? "sun" : "moon"} size={18} />
      <span>{theme.current === "dark" ? "Light mode" : "Dark mode"}</span>
    </button>
    <div class="version muted">v{__APP_VERSION__} · Linux</div>
    <button class="check-update" disabled={checking} onclick={checkNow} title={updateNote}>
      {checking ? "Checking…" : updateNote === "Up to date" ? "Up to date ✓" : "Check for updates"}
    </button>
    {#if updateNote && updateNote !== "Up to date"}<div class="update-error">{updateNote}</div>{/if}
    <button class="check-update" title={REPO} onclick={() => openLink(REPO).catch((e) => (updateNote = errorText(e)))}>GitHub <Icon name="external" size={11} /></button>
    <button class="check-update" title="Gather what a bug report needs and open an issue on GitHub" onclick={() => openReport()}>Report a problem</button>
  </nav>
  {#if update}<UpdatePrompt check={update} onclose={() => (update = null)} />{/if}
  {#if reporting}<ReportDialog intro={reportIntro} onclose={closeReport} />{/if}
  <Notifications />

  <main>
    {#if failed}
      <div class="page"><div class="error-banner">{failed}</div></div>
    {:else if !app.snapshot}
      <div class="page muted">Connecting…</div>
    {:else if nav.page === "home"}
      <Home {goto} />
    {:else if nav.page === "models"}
      <Models />
    {:else if nav.page === "devices"}
      <Devices />
    {:else if nav.page === "trace"}
      <Trace />
    {:else if nav.page === "settings"}
      <Settings />
    {:else}
      <Logs />
    {/if}
  </main>
</div>

<style>
  .shell { display: grid; grid-template-columns: 210px 1fr; height: 100%; }
  @media (max-width: 760px) {
    .shell { grid-template-columns: 64px 1fr; }
    .brand span:last-child, .nav-item span:not(.pip), .version, .check-update, .update-error { display: none; }
    .nav-item { justify-content: center; padding: 10px 0; }
    .pip { position: absolute; top: 7px; right: 11px; margin: 0; }
  }
  nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 18px 12px;
    background: var(--nav-bg);
    border-right: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 16px; padding: 4px 10px 20px; }
  .logo { width: 30px; height: 31px; display: block; }
  .nav-item { position: relative; display: flex; align-items: center; gap: 12px; border: none; background: transparent; color: var(--muted); padding: 10px 12px; text-align: left; }
  .nav-item:hover { color: var(--text); }
  .nav-item.active { background: var(--surface-2); color: var(--text); }
  .pip { margin-left: auto; width: 7px; height: 7px; border-radius: 50%; background: var(--faint); }
  .pip.running { background: var(--on); box-shadow: 0 0 8px var(--on); }
  .pip.starting, .pip.stopping, .pip.external { background: var(--busy); }
  .pip.error, .pip.paused { background: var(--bad); }
  .spacer { flex: 1; }
  .version { font-size: 11.5px; padding: 6px 12px 0; }
  .check-update { align-self: flex-start; border: none; background: none; padding: 2px 12px; font-size: 11.5px; color: var(--muted); text-decoration: underline; text-underline-offset: 2px; }
  .check-update:hover:not(:disabled) { color: var(--text); }
  .check-update :global(svg) { vertical-align: -1px; }
  .update-error { font-size: 11px; color: var(--bad-text); padding: 2px 12px; overflow-wrap: anywhere; }
  main { min-width: 0; height: 100%; overflow: hidden; }
</style>
