<script lang="ts">
  import { ask } from "@tauri-apps/plugin-dialog";
  import Icon from "../components/Icon.svelte";
  import { api, errorText, type Device } from "../lib/api";
  import { ago, tokens } from "../lib/format";
  import { app } from "../lib/store.svelte";

  let devices = $state<Device[]>([]);
  let error = $state("");
  const available = $derived(!!app.snapshot?.admin);

  async function refresh() {
    if (!available) return;
    try {
      devices = await api.devices();
      error = "";
    } catch (e) {
      error = errorText(e);
    }
  }

  $effect(() => {
    if (!available) return;
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  });

  async function revoke(d: Device) {
    const yes = await ask(`Revoke "${d.name}"? It will need a new pairing code to connect again.`, {
      title: "Revoke device",
      kind: "warning",
    });
    if (!yes) return;
    try {
      await api.revokeDevice(d.id);
    } catch (e) {
      error = errorText(e);
    }
    await refresh();
  }
</script>

<div class="page">
  <h1>Devices</h1>
  <p class="sub">
    Clients that have paired with this server. Each holds its own token, stored here only as a hash. Revoking cuts
    a device off immediately.
  </p>

  {#if !available}
    <div class="card empty muted">
      <Icon name="laptop" size={28} />
      <span>Start the server to see and manage paired devices.</span>
    </div>
  {:else}
    {#if error}<div class="error-banner"><Icon name="alert" size={16} /><span>{error}</span></div>{/if}
    {#if devices.length === 0}
      <div class="card empty muted">
        <Icon name="laptop" size={28} />
        <span>No devices yet. Use the pairing code on the Home page to connect one.</span>
      </div>
    {:else}
      <div class="card list">
        {#each devices as d (d.id)}
          <div class="dev">
            <span class="ic"><Icon name="laptop" size={18} /></span>
            <span class="name">
              <span class="title">{d.name}</span>
              <span class="muted small mono">{d.id}</span>
            </span>
            <span class="muted small">Paired {ago(d.created_at)}</span>
            <span class="muted small">Last seen {ago(d.last_seen)}</span>
            <span class="muted small" title="Prompt / generated tokens (counted when the client reports usage)">
              {d.requests} request{d.requests === 1 ? "" : "s"} · {tokens(d.prompt_tokens)} in / {tokens(d.completion_tokens)} out
            </span>
            <button class="danger" onclick={() => revoke(d)}>Revoke</button>
          </div>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .empty { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 48px; text-align: center; }
  .list { padding: 4px 0; }
  .dev { display: grid; grid-template-columns: 36px 1.5fr 0.9fr 0.9fr 1.3fr auto; gap: 12px; align-items: center; padding: 12px 18px; border-top: 1px solid var(--border); }
  .dev:first-child { border-top: none; }
  .ic { width: 32px; height: 32px; border-radius: 9px; display: grid; place-items: center; background: var(--surface-2); color: var(--on); }
  .name { display: flex; flex-direction: column; }
  .title { font-weight: 600; }
  .small { font-size: 12.5px; }
</style>
