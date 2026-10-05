<script lang="ts">
  import { ask } from "@tauri-apps/plugin-dialog";
  import Icon from "../components/Icon.svelte";
  import { api, errorText, type Device } from "../lib/api";
  import { ago, tokens, until } from "../lib/format";
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

  // How long pairings last; clients pair again after that.
  const TTLS: { days: number | null; label: string }[] = [
    { days: 1, label: "1 day" },
    { days: 7, label: "1 week" },
    { days: 30, label: "1 month" },
    { days: 365, label: "1 year" },
    { days: null, label: "Never expire" },
  ];
  const ttl = $derived(app.snapshot?.settings.pairing_ttl_days === undefined ? 7 : app.snapshot.settings.pairing_ttl_days);
  let savingTtl = $state(false);
  async function setTtl(value: string) {
    const days = value === "never" ? null : Number(value);
    if (days === null) {
      const yes = await ask("Paired clients will stay paired until you revoke them. Continue?", {
        title: "Never expire pairings",
        kind: "warning",
      });
      if (!yes) return;
    }
    savingTtl = true;
    error = "";
    try {
      await api.setPairingTtl(days);
      await refresh();
    } catch (e) {
      error = errorText(e);
    } finally {
      savingTtl = false;
    }
  }

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
  <h1>Client Devices</h1>
  <p class="sub">
    Clients that have paired with this server. Each holds its own token, stored here only as a hash. Revoking cuts
    a device off immediately.
  </p>
  {#if app.snapshot?.admin?.tls_fingerprint}
    <p class="fp" title="oppx pair shows this fingerprint; check that the two match before confirming">
      <span class="muted">Server fingerprint</span>
      <span class="mono selectable">{app.snapshot.admin.tls_fingerprint}</span>
    </p>
  {/if}

  <div class="card ttl">
    <div class="ttl-text">
      <span class="eyebrow">Pairing expiry</span>
      <span class="muted small">
        A client's pairing ends after this long, counted from when it paired; it then needs a new pairing code.
        Changing it applies to every paired client at once.
      </span>
    </div>
    <select value={ttl === null ? "never" : String(ttl)} disabled={savingTtl}
      onchange={(e) => setTtl(e.currentTarget.value)} aria-label="Pairing expiry">
      {#each TTLS as t}
        <option value={t.days === null ? "never" : String(t.days)}>{t.label}{t.days === 7 ? " (default)" : ""}</option>
      {/each}
    </select>
  </div>

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
            <span class="small expiry" class:soon={d.expires_at !== null && d.expires_at - Date.now() / 1000 < 86400}
              class:expired={d.expires_at !== null && d.expires_at <= Date.now() / 1000}
              title={d.expires_at ? `Pairing expires ${new Date(d.expires_at * 1000).toLocaleString()}` : "Pairing never expires"}>
              <span class="muted">Paired {ago(d.created_at)}</span>
              <span>{d.expires_at === null ? "never expires" : until(d.expires_at) === "expired" ? "expired: pair again" : `expires ${until(d.expires_at)}`}</span>
            </span>
            <span class="muted small">Last seen {ago(d.last_seen)}</span>
            <span class="muted small" title="Prompt / generated tokens (counted when the client reports usage)">
              {d.requests} request{d.requests === 1 ? "" : "s"} · {tokens(d.prompt_tokens)} in / {tokens(d.completion_tokens)} out{d.web_searches ? ` · ${d.web_searches} searches` : ""}
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
  .ttl { display: flex; align-items: center; gap: 16px; padding: 14px 18px; margin-bottom: 16px; flex-wrap: wrap; }
  .ttl-text { display: flex; flex-direction: column; gap: 4px; flex: 1; min-width: 240px; }
  .ttl select { min-width: 170px; }
  .expiry { display: flex; flex-direction: column; }
  .expiry.soon span:last-child { color: var(--warn-text, var(--busy)); }
  .expiry.expired span:last-child { color: var(--bad); font-weight: 600; }
  .fp { display: flex; gap: 10px; flex-wrap: wrap; font-size: 12px; margin: -12px 0 20px; }
  .fp .mono { word-break: break-all; }
</style>
