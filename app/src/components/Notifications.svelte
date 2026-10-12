<script lang="ts">
  // The window's notification stack (lib/notify.svelte.ts), top right.
  import Icon from "./Icon.svelte";
  import { dismiss, notes } from "../lib/notify.svelte";
</script>

<div class="stack" aria-live="polite">
  {#each notes.list as n (n.id)}
    <div class="note {n.kind}" role={n.kind === "error" ? "alert" : "status"}>
      <Icon name={n.kind === "info" ? "info" : "alert"} size={16} />
      <div class="body">
        <span class="selectable text">{n.text}</span>
        {#if n.action}
          <button class="action" disabled={n.action.busy} onclick={n.action.run}>{n.action.label}</button>
        {/if}
      </div>
      {#if n.onclose}
        <button class="ghost close" onclick={() => dismiss(n.id)} aria-label="Dismiss" title="Dismiss"><Icon name="x" size={14} /></button>
      {/if}
    </div>
  {/each}
</div>

<style>
  .stack {
    position: fixed;
    top: 14px;
    right: 18px;
    z-index: 40;
    width: min(380px, calc(100vw - 32px));
    display: flex;
    flex-direction: column;
    gap: 8px;
    pointer-events: none; /* the page under the gaps stays clickable */
  }
  .note {
    pointer-events: auto;
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 10px 12px;
    border: 1px solid var(--border);
    border-left-width: 3px;
    border-radius: 10px;
    background: var(--surface);
    color: var(--text);
    font-size: 13px;
    line-height: 1.45;
    box-shadow: 0 6px 22px rgba(0, 0, 0, 0.18);
    animation: slide 0.22s ease-out;
  }
  .note > :global(svg) { flex: none; margin-top: 2px; }
  .note.error { border-color: var(--bad-glow); border-left-color: var(--bad); }
  .note.error > :global(svg) { color: var(--bad); }
  .note.warn { border-color: var(--busy-glow); border-left-color: var(--busy); }
  .note.warn > :global(svg) { color: var(--busy); }
  .note.info { border-left-color: var(--on); }
  .note.info > :global(svg) { color: var(--on); }
  .body { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }
  .text { overflow-wrap: anywhere; }
  .action { padding: 5px 12px; font-size: 12.5px; }
  .close { flex: none; padding: 3px; border: none; background: none; color: inherit; opacity: 0.6; }
  .close:hover { opacity: 1; }
  @keyframes slide {
    from { opacity: 0; transform: translateX(12px); }
    to { opacity: 1; transform: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .note { animation: none; }
  }
</style>
