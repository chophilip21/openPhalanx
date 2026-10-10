<script lang="ts">
  // The context window slider, shared by the Models page and the Server page's
  // model card. Both read and write the same setting (settings.context_len).
  // It moves locally while dragged and saves when released; locked while the
  // server starts or runs, since the running model keeps the context it
  // started with.
  //
  // It never goes past what the selected model fits in the VRAM free now (or
  // pooled across a split cluster): the limit comes from the app, which also
  // refuses a longer context, and is re-read every few seconds since other
  // programs change the free memory.
  import { onMount } from "svelte";
  import Icon from "./Icon.svelte";
  import Notice from "./Notice.svelte";
  import { api, errorText, type ContextLimit } from "../lib/api";
  import { gib, tokens } from "../lib/format";

  let {
    value,
    locked = false,
    compact = false,
    forced = false,
    onchange,
  }: { value: number; locked?: boolean; compact?: boolean; forced?: boolean; onchange: (ctx: number) => void | Promise<void> } = $props();

  const STEPS = [4096, 8192, 12288, 16384, 24576, 32768, 49152, 65536, 98304, 131072, 196608, 262144];
  const LAST = STEPS.length - 1;
  // Index of the last step at or under `ctx` (-1 if none).
  const lastStepUnder = (ctx: number) => STEPS.reduce((at, s, i) => (s <= ctx ? i : at), -1);
  // A context's place on the track, in steps (74k sits between 64k and 96k).
  const posOf = (ctx: number) => {
    const i = lastStepUnder(ctx);
    if (i < 0) return 0;
    if (i >= LAST) return LAST;
    return i + (ctx - STEPS[i]) / (STEPS[i + 1] - STEPS[i]);
  };

  let limit = $state<ContextLimit | null>(null);
  const load = () => api.contextLimit().then((l) => (limit = l)).catch(() => {});
  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
  // A new value or model: read the limit again at once.
  $effect(() => {
    void value;
    load();
  });

  const max = $derived(limit?.max_fit ?? null);
  // The model's own maximum is the cap (not the VRAM): a plain limit, not a danger.
  const byModel = $derived(!!limit?.by_model);
  const maxPos = $derived(max == null ? LAST : posOf(max));
  const over = $derived(max != null && value > max);
  const noFit = $derived(!!limit?.model && limit.max_fit == null && limit.available_bytes != null);
  const where = $derived((limit?.servers ?? 1) > 1 ? "pooled across the cluster" : "free on this GPU");

  // While dragged: where the thumb is and the context it stands for. It snaps
  // to the steps, except past the limit, where it stops at the exact limit.
  let dragging = $state<{ pos: number; ctx: number } | null>(null);
  let blocked = $state(false);
  // The "stops there" note outlives the click that hit the limit: it stays
  // until the slider moves back under the max, or the user closes it.
  let hitLimit = $state(false);
  const shown = $derived(dragging?.ctx ?? value);
  const pos = $derived(dragging?.pos ?? posOf(value));

  function move(el: HTMLInputElement) {
    const i = Math.round(Number(el.value));
    blocked = max != null && STEPS[i] > max;
    if (blocked) hitLimit = true;
    else if (max == null || STEPS[i] < max) hitLimit = false;
    dragging = blocked ? { pos: maxPos, ctx: max! } : { pos: i, ctx: STEPS[i] };
    // The thumb follows the snapped position, not where the pointer let go.
    el.value = String(dragging.pos);
  }
  // The long-context mode (YaRN) is the user's choice, off by default.
  let modeError = $state("");
  async function setLongContext(on: boolean) {
    modeError = "";
    try {
      // Turning it off: first bring the context back into the native window.
      if (!on && limit?.native_max != null && value > limit.native_max) await onchange(limit.native_max);
      await api.setLongContext(on);
    } catch (e) {
      modeError = errorText(e);
    }
    await load();
  }
  async function commit(el: HTMLInputElement) {
    const ctx = dragging?.ctx;
    blocked = false;
    if (ctx == null) return;
    try {
      if (ctx !== value) await onchange(ctx);
    } finally {
      dragging = null;
      el.value = String(posOf(value));
    }
  }
</script>

<div class="ctx" class:compact class:locked>
  <div class="value">
    {tokens(shown)} <span class="muted">tokens</span>
    {#if locked}
      <span class="note" title="The running model keeps the context it started with. Stop the server to change it.">
        <Icon name="lock" size={12} stroke={2.4} /> locked while the server runs
      </span>
    {:else if max != null}
      <span class="note" title={byModel
        ? `${limit?.model} supports up to ${tokens(max)} of context; that's the model's own maximum`
        : `The longest context ${limit?.model} fits in ${gib(limit?.available_bytes ?? 0)} of VRAM ${where}`}>
        {byModel ? "model's max" : "your max"}: <strong>{tokens(max)}</strong>
      </span>
    {/if}
  </div>
  <div class="track">
    <input
      class="slider"
      class:over={!locked && over && dragging == null}
      class:model-cap={byModel}
      style="--fill: {(100 * pos) / LAST}; --pos-limit: {locked || maxPos >= LAST ? '100%' : `calc(8px + (100% - 16px) * ${maxPos / LAST})`}"
      type="range"
      min="0"
      max={LAST}
      step="any"
      value={pos}
      disabled={locked || noFit}
      oninput={(e) => move(e.currentTarget)}
      onchange={(e) => commit(e.currentTarget)}
      aria-label="Context window"
    />
  </div>
  <div class="ticks">
    {#each STEPS as s, i}
      <span class:on={Math.abs(pos - i) < 0.01} class:off={!locked && max != null && s > max} class:model-cap={byModel} style="left:{(100 * i) / LAST}%">
        {i % (compact ? 3 : 2) === 0 || i === LAST ? tokens(s) : ""}
      </span>
    {/each}
  </div>
  {#if limit?.long_context_max != null && limit.native_max != null}
    <label class="mode" class:disabled={locked}>
      <input type="checkbox" checked={limit.long_context} disabled={locked}
        onchange={(e) => setLongContext(e.currentTarget.checked)} />
      <span>
        Allow up to {tokens(limit.long_context_max)} with the long-context mode (YaRN, experimental)
        <span class="muted">
          · {limit.model} was trained for {tokens(limit.native_max)}. Past that it finds details in the prompt
          about half as reliably in our tests; at {tokens(limit.native_max)} and below nothing changes.
        </span>
      </span>
    </label>
    {#if modeError}<div class="note-box"><Notice onclose={() => (modeError = "")}>{modeError}</Notice></div>{/if}
  {/if}
  {#if byModel && !locked && max != null && shown >= max}
    <!-- An explanation, not a prompt: shown while the slider sits at the model's
         own maximum, gone once it moves below. -->
    <p class="cap">{limit?.model} supports up to {tokens(max)} of context (the model's own maximum), so the slider stops there.</p>
  {:else if !byModel && (blocked || hitLimit) && !locked && max != null}
    <div class="note-box">
      <Notice kind="warn" onclose={() => (hitLimit = false)}>
        Past {tokens(max)} {limit?.model} would run out of VRAM ({gib(limit?.available_bytes ?? 0)} {where}), so the slider stops there.
      </Notice>
    </div>
  {:else if !locked && over}
    <p class="warn">
      {#if byModel}
        {tokens(value)} is more than {limit?.model} supports (its maximum is {tokens(max ?? 0)}); the server can't start like this.
      {:else}
        {tokens(value)} is more than {limit?.model} fits right now (max {tokens(max ?? 0)} with {gib(limit?.available_bytes ?? 0)} {where}); the server can't start like this.
      {/if}
      <button class="ghost small" onclick={() => onchange(max!)}>Use {tokens(max ?? 0)}</button>
    </p>
  {:else if !locked && noFit && forced}
    <!-- "Force load" is ticked: the user chose to try anyway. -->
    <p class="warn forced">{limit?.model} doesn't fit {gib(limit?.available_bytes ?? 0)} {where} by our estimate, even at a 2k context. Force load will try it anyway.</p>
  {:else if !locked && noFit}
    <p class="warn">{limit?.model} doesn't fit even a 2k context in {gib(limit?.available_bytes ?? 0)} {where}. Pick a smaller model or free some VRAM.</p>
  {/if}
</div>

<style>
  .value { font-size: 26px; font-weight: 650; font-variant-numeric: tabular-nums; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap; }
  .value .muted { font-size: 14px; font-weight: 500; }
  .compact .value { font-size: 18px; }
  .compact .value .muted { font-size: 12.5px; }
  /* Same size as "tokens" next to it. */
  .note { display: inline-flex; align-items: center; gap: 4px; margin-left: 6px; font-size: 14px; font-weight: 500; color: var(--muted); }
  .compact .note { font-size: 12.5px; }
  .note strong { color: var(--text); }
  .track { position: relative; }
  /* One drawn track: green up to the value, grey up to the VRAM limit, a red
     tint past it. Stops are placed on the thumb's travel (8px in from each
     end), so they line up with the thumb and the tick labels. */
  .slider {
    --pos-fill: calc(8px + (100% - 16px) * var(--fill) / 100);
    -webkit-appearance: none;
    appearance: none;
    display: block;
    width: 100%;
    height: 16px;
    margin: 10px 0 4px;
    background: linear-gradient(
        to right,
        var(--on) 0 var(--pos-fill),
        var(--surface-3) var(--pos-fill) var(--pos-limit),
        color-mix(in srgb, var(--bad) 30%, var(--surface-3)) var(--pos-limit) 100%
      )
      center / 100% 6px no-repeat;
    border-radius: 3px;
    /* Not the global input box: its border and padding would frame the track
       and shift the thumb off the drawn stops. */
    border: 0;
    padding: 0;
    cursor: pointer;
  }
  .slider:focus { border: 0; }
  .slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--on);
    border: 2px solid var(--surface);
    box-shadow: 0 0 0 1px var(--on);
  }
  .slider::-moz-range-thumb {
    width: 12px;
    height: 12px;
    border-radius: 50%;
    background: var(--on);
    border: 2px solid var(--surface);
  }
  /* The saved value is past the limit: the fill and thumb turn red. */
  .slider.over { background-image: linear-gradient(to right, var(--bad) 0 var(--pos-fill), var(--surface-3) var(--pos-fill) 100%); }
  .slider.over::-webkit-slider-thumb { background: var(--bad); box-shadow: 0 0 0 1px var(--bad); }
  .slider:focus-visible { outline: 2px solid var(--on); outline-offset: 4px; }
  .slider:disabled { cursor: not-allowed; opacity: 0.55; }
  .ticks { position: relative; height: 16px; margin: 0 8px; font-size: 11px; color: var(--faint); }
  .ticks span { position: absolute; transform: translateX(-50%); white-space: nowrap; font-variant-numeric: tabular-nums; }
  .ticks span.on { color: var(--text); font-weight: 600; }
  .ticks span.off { color: var(--bad-text); opacity: 0.6; }
  .ticks span.off.model-cap { color: var(--faint); opacity: 0.45; }
  /* Past the model's own maximum: unreachable, but no danger colour. */
  .slider.model-cap:not(.over) {
    background-image: linear-gradient(
      to right,
      var(--on) 0 var(--pos-fill),
      var(--surface-3) var(--pos-fill) var(--pos-limit),
      color-mix(in srgb, var(--faint) 30%, var(--surface-3)) var(--pos-limit) 100%
    );
  }
  .locked .ticks span.on { color: var(--muted); }
  .note-box { margin-top: 8px; font-size: 12.5px; }
  .mode { display: flex; gap: 8px; align-items: flex-start; margin-top: 10px; font-size: 12.5px; cursor: pointer; }
  .mode.disabled { cursor: not-allowed; opacity: 0.6; }
  .mode input { margin-top: 2px; flex: none; }
  .cap { margin: 6px 0 0; font-size: 12.5px; color: var(--muted); }
  .warn.forced { color: var(--warn-text); }
  .warn { margin: 6px 0 0; font-size: 12.5px; color: var(--bad-text); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
</style>
