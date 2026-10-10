<script lang="ts">
  // Another machine's name as this machine shows it: the nickname the user
  // gave it here, else its address. The nickname can be set, changed or
  // removed in place; it is saved on this machine and used everywhere the
  // machine is named (cluster, charts, logs, trace).
  import { api, errorText } from "../lib/api";
  import { app } from "../lib/store.svelte";

  let { id, name, announced = "" }: { id: string; name: string; announced?: string } = $props();

  const nickname = $derived(app.snapshot?.cluster?.nicknames?.[id] ?? null);
  let editing = $state(false);
  let draft = $state("");
  let error = $state("");
  let saving = $state(false);

  async function save(value: string | null) {
    saving = true;
    error = "";
    try {
      await api.clusterSetNickname(id, value?.trim() || null);
      // Shown at once; the next snapshot (every 2 s) carries it too.
      const names = app.snapshot?.cluster?.nicknames;
      if (names) {
        if (value?.trim()) names[id] = value.trim();
        else delete names[id];
      }
      editing = false;
    } catch (e) {
      error = errorText(e);
    } finally {
      saving = false;
    }
  }
</script>

{#if editing}
  <form class="edit" onsubmit={(e) => { e.preventDefault(); save(draft); }}>
    <!-- svelte-ignore a11y_autofocus -->
    <input bind:value={draft} maxlength="64" placeholder="Nickname" aria-label="Nickname for {name}" autofocus />
    <button type="submit" disabled={saving || !draft.trim()}>Save</button>
    {#if nickname}<button type="button" class="ghost" disabled={saving} title="Show its address again" onclick={() => save(null)}>Remove nickname</button>{/if}
    <button type="button" class="ghost" onclick={() => (editing = false)}>Cancel</button>
    {#if error}<span class="err">{error}</span>{/if}
  </form>
{:else}
  <span class="node" title={announced ? `It calls itself “${announced}”` : undefined}>
    <span class="name">{nickname ?? name}</span>
    <button class="ghost tiny" title={nickname ? "Change or remove the nickname" : "Give it a nickname (saved on this machine)"}
      aria-label="{nickname ? 'Edit' : 'Add'} nickname for {nickname ?? name}" onclick={() => { draft = nickname ?? ""; editing = true; }}>
      {nickname ? "Edit" : "Nickname"}
    </button>
  </span>
{/if}

<style>
  .node { display: inline-flex; align-items: baseline; gap: 6px; min-width: 0; }
  .name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tiny { padding: 0 5px; font-size: 11px; line-height: 1.6; opacity: 0.75; }
  .tiny:hover { opacity: 1; }
  .edit { display: inline-flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .edit input { width: 150px; padding: 3px 7px; font-size: 12.5px; }
  .edit button { padding: 3px 8px; font-size: 12px; }
  .err { color: var(--bad-text); font-size: 12px; }
</style>
