<script lang="ts">
  import { onMount } from "svelte";
  import Icon from "./components/Icon.svelte";
  import logo from "./assets/logo.svg";
  import Devices from "./pages/Devices.svelte";
  import Home from "./pages/Home.svelte";
  import Logs from "./pages/Logs.svelte";
  import Models from "./pages/Models.svelte";
  import { app, connect } from "./lib/store.svelte";
  import { theme, toggleTheme } from "./lib/theme.svelte";
  import { goto, nav } from "./lib/nav.svelte";

  const NAV = [
    { id: "home", label: "Server", icon: "power" },
    { id: "devices", label: "Client", icon: "laptop" },
    { id: "models", label: "Models", icon: "box" },
    { id: "logs", label: "Logs", icon: "terminal" },
  ];

  let failed = $state("");

  onMount(() => {
    connect().catch((e) => (failed = String(e)));
  });

  const st = $derived(app.snapshot?.server.state ?? "stopped");
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
    <button class="nav-item theme" onclick={toggleTheme} title="Switch between light and dark">
      <Icon name={theme.current === "dark" ? "sun" : "moon"} size={18} />
      <span>{theme.current === "dark" ? "Light mode" : "Dark mode"}</span>
    </button>
    <div class="version muted">v{__APP_VERSION__} · Linux</div>
  </nav>

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
    {:else}
      <Logs />
    {/if}
  </main>
</div>

<style>
  .shell { display: grid; grid-template-columns: 210px 1fr; height: 100%; }
  @media (max-width: 760px) {
    .shell { grid-template-columns: 64px 1fr; }
    .brand span:last-child, .nav-item span:not(.pip), .version { display: none; }
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
  .pip.error { background: var(--bad); }
  .spacer { flex: 1; }
  .version { font-size: 11.5px; padding: 6px 12px 0; }
  main { min-width: 0; height: 100%; overflow: hidden; }
</style>
