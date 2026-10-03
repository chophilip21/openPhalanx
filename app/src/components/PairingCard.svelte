<script lang="ts">
  import Icon from "./Icon.svelte";
  import { api, errorText, type Pairing } from "../lib/api";
  import { countdown } from "../lib/format";

  let { pairing, endpoint, fingerprint }: { pairing: Pairing | null; endpoint: string | null; fingerprint: string } =
    $props();

  let now = $state(Date.now());
  let busy = $state(false);
  let error = $state("");
  let copied = $state("");

  $effect(() => {
    const t = setInterval(() => (now = Date.now()), 1000);
    return () => clearInterval(t);
  });

  const active = $derived(pairing?.active && (pairing.expires_at ?? 0) * 1000 > now);
  const command = $derived(endpoint && pairing?.code ? `oppx pair https://${endpoint} ${pairing.code}` : "");

  async function regenerate() {
    busy = true;
    error = "";
    try {
      await api.newPairingCode();
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = false;
    }
  }

  async function copy(text: string, what: string) {
    await navigator.clipboard.writeText(text);
    copied = what;
    setTimeout(() => (copied = ""), 1500);
  }
</script>

<div class="card pairing">
  <div class="head">
    <span class="eyebrow">Pair a client</span>
    {#if active}<span class="expires">expires in {countdown(pairing?.expires_at, now)}</span>{/if}
  </div>

  {#if active && pairing?.code}
    <button class="code mono" onclick={() => copy(pairing!.code!, "code")} title="Copy code">
      {pairing.code}
      <span class="copy">{#if copied === "code"}<Icon name="check" size={16} />{:else}<Icon name="copy" size={16} />{/if}</span>
    </button>
    <p class="muted hint">
      Single-use, valid for 10 minutes, and burned after 5 wrong attempts. Run this on the client:
    </p>
    <button class="cmd mono" onclick={() => copy(command, "cmd")} title="Copy command">
      <span>{command}</span>
      {#if copied === "cmd"}<Icon name="check" size={14} />{:else}<Icon name="copy" size={14} />{/if}
    </button>
  {:else}
    <p class="muted hint">
      No active pairing code. Already-paired devices keep working; generate a code only to add a new one.
    </p>
  {/if}

  <div class="foot">
    <button onclick={regenerate} disabled={busy}>
      <Icon name="refresh" size={14} />
      {active ? "New code" : "Generate code"}
    </button>
    {#if active}
      <button class="ghost" onclick={() => api.clearPairingCode()}>Cancel code</button>
    {/if}
  </div>

  <div class="fp">
    <span class="eyebrow">Server fingerprint</span>
    <span class="mono fpv" title="Clients verify this TLS certificate fingerprint when pairing">
      {fingerprint ? fingerprint.split(":").slice(0, 8).join(":") + "…" : "–"}
    </span>
  </div>
  {#if error}<p class="err">{error}</p>{/if}
</div>

<style>
  .pairing { display: flex; flex-direction: column; gap: 10px; }
  .head { display: flex; justify-content: space-between; align-items: center; }
  .expires { font-size: 12px; color: var(--busy); font-variant-numeric: tabular-nums; }
  .code {
    font-size: 34px;
    font-weight: 600;
    letter-spacing: 0.12em;
    padding: 12px 16px;
    border-radius: 12px;
    background: var(--bg);
    border: 1px dashed var(--border);
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 14px;
    color: var(--on);
  }
  .copy { color: var(--muted); display: inline-flex; }
  .hint { margin: 0; font-size: 12.5px; }
  .cmd {
    font-size: 12px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    background: var(--bg);
    text-align: left;
    color: var(--muted);
  }
  .cmd span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .foot { display: flex; gap: 8px; }
  .foot button { display: inline-flex; align-items: center; gap: 6px; }
  .fp { display: flex; justify-content: space-between; align-items: center; border-top: 1px solid var(--border); padding-top: 10px; }
  .fpv { font-size: 11.5px; color: var(--muted); }
  .err { color: var(--bad); margin: 0; font-size: 12.5px; }
</style>
