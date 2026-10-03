<script lang="ts">
  import { onMount } from "svelte";
  import Icon from "./components/Icon.svelte";
  import Devices from "./pages/Devices.svelte";
  import Home from "./pages/Home.svelte";
  import Logs from "./pages/Logs.svelte";
  import Models from "./pages/Models.svelte";
  import { app, connect } from "./lib/store.svelte";

  const NAV = [
    { id: "home", label: "Server", icon: "power" },
    { id: "models", label: "Models", icon: "box" },
    { id: "devices", label: "Devices", icon: "laptop" },
    { id: "logs", label: "Logs", icon: "terminal" },
  ];

  let page = $state("home");
  let failed = $state("");

  onMount(() => {
    connect().catch((e) => (failed = String(e)));
  });

  const st = $derived(app.snapshot?.server.state ?? "stopped");
  const goto = (p: string) => (page = p);
</script>

<div class="shell">
  <nav>
    <div class="brand">
      <span class="logo"><Icon name="shield" size={20} stroke={2.2} /></span>
      <span>Openphalanx</span>
    </div>
    {#each NAV as item}
      <button class="nav-item" class:active={page === item.id} onclick={() => (page = item.id)}>
        <Icon name={item.icon} size={18} />
        <span>{item.label}</span>
        {#if item.id === "home"}<span class="pip {st}"></span>{/if}
      </button>
    {/each}
    <div class="spacer"></div>
    <div class="version muted">v{__APP_VERSION__} · Linux</div>
  </nav>

  <main>
    {#if failed}
      <div class="page"><div class="error-banner">{failed}</div></div>
    {:else if !app.snapshot}
      <div class="page muted">Connecting…</div>
    {:else if page === "home"}
      <Home {goto} />
    {:else if page === "models"}
      <Models />
    {:else if page === "devices"}
      <Devices />
    {:else}
      <Logs />
    {/if}
  </main>
</div>

<style>
  .shell { display: grid; grid-template-columns: 210px 1fr; height: 100%; }
  nav {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 18px 12px;
    background: linear-gradient(180deg, #0d1320, #0a0e16);
    border-right: 1px solid var(--border);
  }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 16px; padding: 4px 10px 20px; }
  .logo { width: 32px; height: 32px; border-radius: 9px; display: grid; place-items: center; background: rgba(52, 211, 153, 0.12); color: var(--on); }
  .nav-item { display: flex; align-items: center; gap: 12px; border: none; background: transparent; color: var(--muted); padding: 10px 12px; text-align: left; }
  .nav-item:hover { color: var(--text); }
  .nav-item.active { background: var(--surface-2); color: var(--text); }
  .pip { margin-left: auto; width: 7px; height: 7px; border-radius: 50%; background: var(--faint); }
  .pip.running { background: var(--on); box-shadow: 0 0 8px var(--on); }
  .pip.starting, .pip.stopping, .pip.external { background: var(--busy); }
  .pip.error { background: var(--bad); }
  .spacer { flex: 1; }
  .version { font-size: 11.5px; padding: 0 12px; }
  main { min-width: 0; height: 100%; overflow: hidden; }
</style>
