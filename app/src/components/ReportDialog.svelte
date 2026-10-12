<script lang="ts">
  // "Report a problem": the app gathers what a bug report needs (version,
  // settings, the machine, the backend's state and log, the kernel log's
  // signs of trouble, what the app recorded), shows it, and opens a filled-in
  // issue on GitHub. Nothing is sent from here: the user reads it and posts it.
  import Icon from "./Icon.svelte";
  import Notice from "./Notice.svelte";
  import { api, errorText, openLink, type ProblemReport } from "../lib/api";

  let { onclose, intro = "" }: { onclose: () => void; intro?: string } = $props();

  let description = $state("");
  let report = $state<ProblemReport | null>(null);
  let busy = $state(false);
  let error = $state("");
  let copied = $state(false);

  async function gather() {
    busy = true;
    error = "";
    copied = false;
    try {
      report = await api.makeReport(description.trim());
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = false;
    }
  }

  async function copy() {
    if (!report) return;
    try {
      await navigator.clipboard.writeText(report.markdown);
      copied = true;
    } catch (e) {
      error = `Couldn't copy it (${errorText(e)}). It is saved in ${report.path ?? "the app's data folder"}.`;
    }
  }

  // The link holds a summary; the whole report goes on the clipboard, to paste into the issue.
  async function openIssue() {
    if (!report) return;
    await copy();
    openLink(report.url).catch((e) => (error = errorText(e)));
  }
</script>

<div class="overlay" role="presentation">
  <div class="dialog card" role="dialog" aria-modal="true" aria-labelledby="report-title">
    <div class="head">
      <span class="badge"><Icon name="alert" size={18} /></span>
      <h2 id="report-title">Report a problem</h2>
    </div>
    {#if !report}
      {#if intro}<p class="intro">{intro}</p>{/if}
      <p>
        Say what you did and what you saw. The app adds its version and settings, this machine's hardware, the
        backend's state and the last lines of its log, and what it recorded before the problem.
      </p>
      <textarea bind:value={description} rows="5" disabled={busy}
        placeholder="For example: I pressed Start with the 27B model split across two machines. After about three minutes the whole computer froze."></textarea>
      <p class="muted note">
        Left out: addresses, your user name, tokens, pairing codes and anything a client sent. You see the whole
        report before anything leaves this machine.
      </p>
    {:else}
      <p>
        This is the report. <strong>Open GitHub issue</strong> copies it and opens a new issue with the start of it
        filled in; paste the rest there.
      </p>
      <pre class="selectable preview">{report.markdown}</pre>
      {#if report.path}<p class="muted note">Saved as <span class="selectable" data-real>{report.path}</span></p>{/if}
      {#if copied}<p class="ok note">Copied to the clipboard.</p>{/if}
    {/if}
    {#if error}<Notice kind="error" onclose={() => (error = "")}>{error}</Notice>{/if}
    <div class="actions">
      <button class="ghost" disabled={busy} onclick={onclose}>{report ? "Close" : "Cancel"}</button>
      {#if !report}
        <button class="primary" disabled={busy} onclick={gather}>{busy ? "Gathering…" : "Gather the report"}</button>
      {:else}
        <button class="ghost" onclick={() => (report = null)}>Back</button>
        <button onclick={copy}>Copy report</button>
        <button class="primary" onclick={openIssue}>Open GitHub issue <Icon name="external" size={12} /></button>
      {/if}
    </div>
  </div>
</div>

<style>
  .overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); display: grid; place-items: center; z-index: 50; padding: 24px; }
  .dialog { width: min(640px, 100%); max-height: calc(100vh - 48px); display: flex; flex-direction: column; gap: 10px; box-shadow: 0 16px 48px rgba(0, 0, 0, 0.4); }
  .head { display: flex; align-items: center; gap: 10px; }
  .badge { display: grid; place-items: center; width: 34px; height: 34px; border-radius: 10px; color: var(--busy); background: var(--busy-glow); }
  h2 { margin: 0; font-size: 18px; }
  p { margin: 0; line-height: 1.5; }
  .intro { font-weight: 600; }
  .note { font-size: 12.5px; }
  textarea { width: 100%; resize: vertical; min-height: 96px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); color: var(--text); font: inherit; line-height: 1.45; box-sizing: border-box; }
  .preview { margin: 0; flex: 1; min-height: 160px; max-height: 46vh; overflow: auto; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-2); font-size: 12px; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
  .actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; flex-wrap: wrap; }
  .ok { color: var(--on); }
</style>
