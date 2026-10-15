<script lang="ts">
  import Icon from "./Icon.svelte";
  import type { Check } from "../lib/api";

  // "pending": still being checked (a spinner instead of a verdict).
  type Row = Omit<Check, "status"> & { status: Check["status"] | "pending" };
  let { checks }: { checks: Row[] } = $props();
  const icon = { pass: "check", warn: "alert", fail: "x" } as const;
</script>

<ul>
  {#each checks as c (c.id)}
    <li class={c.status}>
      <span class="ic">
        {#if c.status === "pending"}<span class="spin"></span>{:else}<Icon name={icon[c.status]} size={14} stroke={2.6} />{/if}
      </span>
      <span class="label">{c.label}</span>
      <!-- Passed checks take one line (full text on hover), so a failure further
           down isn't pushed out of the window; problems keep their whole text. -->
      <span class="detail selectable" title={c.status === "pass" ? c.detail : undefined}>{c.detail}</span>
    </li>
  {/each}
</ul>

<style>
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  li { display: grid; grid-template-columns: 22px 160px minmax(0, 1fr); align-items: start; gap: 8px; padding: 6px 0; font-size: 13px; }
  .ic { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; margin-top: -1px; }
  .pass .ic { color: var(--on); background: var(--on-soft); }
  .warn .ic { color: var(--busy); background: var(--busy-soft); }
  .fail .ic { color: var(--bad); background: var(--bad-soft); }
  .pending .ic { color: var(--muted); }
  .spin { width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--border); border-top-color: var(--muted); animation: spin 0.8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .label { color: var(--text); font-weight: 500; }
  .detail { color: var(--muted); }
  .pass .detail { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .fail .detail { color: var(--bad-text); }
</style>
