<script lang="ts">
  // "Update available" dialog: asked once per launch when GitHub has a newer
  // release, or when the user checks by hand from the sidebar.
  import { openUrl } from "@tauri-apps/plugin-opener";
  import Icon from "./Icon.svelte";
  import Notice from "./Notice.svelte";
  import { api, errorText, events, type UpdateCheck, type UpdateProgress } from "../lib/api";

  let { check, onclose }: { check: UpdateCheck; onclose: () => void } = $props();

  let progress = $state<UpdateProgress | null>(null);
  let error = $state("");
  const busy = $derived(progress !== null && !error);
  const pct = $derived(progress && progress.total ? Math.round((100 * progress.done) / progress.total) : 0);

  async function update() {
    error = "";
    progress = { stage: "downloading", done: 0, total: 0 };
    const stop = await events.updateProgress((p) => (progress = p));
    try {
      await api.installUpdate(); // the app exits and relaunches on success
    } catch (e) {
      error = errorText(e);
    } finally {
      stop();
    }
  }
</script>

<div class="overlay" role="presentation">
  <div class="dialog card" role="dialog" aria-modal="true" aria-labelledby="update-title">
    <div class="head">
      <span class="badge"><Icon name="download" size={18} /></span>
      <h2 id="update-title">Update available</h2>
    </div>
    <p>
      Openphalanx <strong>{check.latest}</strong> is available; you have {check.current}.
      {#if check.can_install}Would you like to download it?{/if}
    </p>
    <button class="link" onclick={() => openUrl(check.url)}>What's new <Icon name="external" size={12} /></button>

    {#if check.note}
      <p class="muted note">{check.note}</p>
    {:else}
      <p class="muted note">
        The app restarts when it's done. A running server keeps serving meanwhile.
        {#if check.install.kind === "deb"}You'll be asked for your password to install the package.{/if}
      </p>
    {/if}

    {#if progress && !error}
      <div class="progress" aria-live="polite">
        {#if progress.stage === "downloading"}
          <div class="bar"><div style="width: {pct}%"></div></div>
          <span class="muted">Downloading… {pct}%</span>
        {:else if progress.stage === "installing"}
          <span class="muted">Installing…</span>
        {:else}
          <span class="muted">Restarting…</span>
        {/if}
      </div>
    {/if}
    {#if error}<Notice onclose={() => (error = "")}>{error}</Notice>{/if}

    <div class="actions">
      <button class="ghost" disabled={busy} onclick={onclose}>{check.can_install ? "Later" : "Close"}</button>
      {#if check.can_install}
        <button class="primary" disabled={busy} onclick={update}>
          <Icon name="download" size={14} /> {error ? "Try again" : "Download and install"}
        </button>
      {/if}
    </div>
  </div>
</div>

<style>
  .overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); display: grid; place-items: center; z-index: 50; padding: 24px; }
  .dialog { width: min(460px, 100%); display: flex; flex-direction: column; gap: 10px; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.4); }
  .head { display: flex; align-items: center; gap: 10px; }
  .badge { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; color: var(--on); background: var(--on-soft); }
  h2 { margin: 0; font-size: 18px; }
  p { margin: 0; line-height: 1.5; }
  .note { font-size: 13px; }
  .link { align-self: flex-start; display: inline-flex; gap: 5px; align-items: center; border: none; background: none; padding: 0; color: var(--on); font-size: 13px; }
  .progress { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
  .bar { height: 6px; border-radius: 3px; background: var(--surface-3); overflow: hidden; }
  .bar div { height: 100%; background: var(--on); transition: width 0.2s; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 6px; }
</style>
