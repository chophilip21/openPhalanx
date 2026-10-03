<script lang="ts">
  import Icon from "./Icon.svelte";
  import type { Check } from "../lib/api";

  let { checks }: { checks: Check[] } = $props();
  const icon = { pass: "check", warn: "alert", fail: "x" } as const;
</script>

<ul>
  {#each checks as c (c.id)}
    <li class={c.status}>
      <span class="ic"><Icon name={icon[c.status]} size={14} stroke={2.6} /></span>
      <span class="label">{c.label}</span>
      <span class="detail selectable">{c.detail}</span>
    </li>
  {/each}
</ul>

<style>
  ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  li { display: grid; grid-template-columns: 22px 170px 1fr; align-items: start; gap: 8px; padding: 6px 0; font-size: 13px; }
  .ic { display: grid; place-items: center; width: 20px; height: 20px; border-radius: 50%; margin-top: -1px; }
  .pass .ic { color: var(--on); background: rgba(52, 211, 153, 0.12); }
  .warn .ic { color: var(--busy); background: rgba(251, 191, 36, 0.12); }
  .fail .ic { color: var(--bad); background: rgba(248, 113, 113, 0.12); }
  .label { color: var(--text); font-weight: 500; }
  .detail { color: var(--muted); }
  .fail .detail { color: #fecaca; }
</style>
