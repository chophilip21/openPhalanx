<script lang="ts">
  import Icon from "./Icon.svelte";
  import { api, errorText, type Pairing } from "../lib/api";
  import { countdown } from "../lib/format";

  let { pairing, endpoint }: { pairing: Pairing | null; endpoint: string | null } = $props();

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

<section class="pairing">
  <div class="head">
    <span class="eyebrow titled"><Icon name="laptop" size={14} /> Pair a client</span>
    {#if active}<span class="expires">expires in {countdown(pairing?.expires_at, now)}</span>{/if}
  </div>

  {#if active && pairing?.code}
    <button class="code mono" onclick={() => copy(pairing!.code!, "code")} title="Copy code">
      <span>{pairing.code}</span>
      <span class="copy">{#if copied === "code"}<Icon name="check" size={20} />{:else}<Icon name="copy" size={20} />{/if}</span>
    </button>
    <button class="cmd mono" onclick={() => copy(command, "cmd")} title="Copy command">
      <span>{command}</span>
      {#if copied === "cmd"}<Icon name="check" size={14} />{:else}<Icon name="copy" size={14} />{/if}
    </button>
  {:else}
    <p class="muted hint">No active code. Paired clients keep working.</p>
  {/if}

  <div class="actions">
    <button onclick={regenerate} disabled={busy}>
      <Icon name="refresh" size={14} />
      {active ? "New code" : "Generate code"}
    </button>
    {#if active}
      <button class="ghost" onclick={() => api.clearPairingCode()}>Cancel</button>
    {/if}
  </div>
  {#if error}<p class="err">{error}</p>{/if}
</section>

<style>
  .pairing {
    width: 100%;
    max-width: 560px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 14px;
    padding: 22px clamp(16px, 4vw, 32px) 20px;
    text-align: center;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 20px;
    box-shadow: var(--shadow), 0 0 0 4px var(--on-soft);
  }
  .head { display: flex; gap: 12px; align-items: baseline; justify-content: center; flex-wrap: wrap; }
  .expires { font-size: 12.5px; color: var(--busy); font-variant-numeric: tabular-nums; }
  .code {
    font-size: clamp(30px, 6vw, 54px);
    font-weight: 650;
    letter-spacing: 0.12em;
    padding: 12px clamp(14px, 3vw, 26px);
    border-radius: 14px;
    background: var(--bg);
    border: 1px dashed var(--on);
    display: inline-flex;
    justify-content: center;
    align-items: center;
    gap: 14px;
    color: var(--on);
    max-width: 100%;
  }
  .copy { color: var(--muted); display: inline-flex; }
  .hint { margin: 0; font-size: 13px; }
  .cmd {
    font-size: 12.5px;
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 7px 12px;
    background: var(--bg);
    color: var(--muted);
    max-width: 100%;
  }
  .cmd span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .actions { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; width: 100%; }
  .actions button { display: inline-flex; align-items: center; gap: 6px; }
  .err { color: var(--bad); margin: 0; font-size: 12.5px; }
</style>
