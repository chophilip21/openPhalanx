<script lang="ts">
  // A message banner (error, warning or info) that the user can always close.
  import type { Snippet } from "svelte";
  import Icon from "./Icon.svelte";

  let {
    kind = "error",
    onclose,
    children,
  }: { kind?: "error" | "warn" | "info"; onclose: () => void; children: Snippet } = $props();
</script>

<div class="error-banner notice {kind}" role={kind === "error" ? "alert" : "status"}>
  <Icon name={kind === "info" ? "info" : "alert"} size={16} />
  <span class="selectable text">{@render children()}</span>
  <button class="ghost close" onclick={onclose} aria-label="Dismiss" title="Dismiss"><Icon name="x" size={14} /></button>
</div>

<style>
  .notice { align-items: center; }
  .text { flex: 1; min-width: 0; overflow-wrap: anywhere; }
  .close { flex: none; padding: 3px; border: none; background: none; color: inherit; opacity: 0.7; }
  .close:hover { opacity: 1; }
  .warn { border-color: var(--busy-glow); background: var(--busy-soft); color: var(--warn-text); }
  .info { border-color: var(--border); background: var(--surface-2); color: var(--text); }
</style>
