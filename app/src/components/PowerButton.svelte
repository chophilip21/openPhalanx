<script lang="ts">
  import Icon from "./Icon.svelte";
  import type { ServerState } from "../lib/api";

  let { state, disabled = false, onclick }: { state: ServerState; disabled?: boolean; onclick: () => void } =
    $props();
  const tone = $derived(
    state === "running" ? "on" : state === "starting" || state === "stopping" ? "busy" : state === "error" ? "bad" : "off",
  );
</script>

<div class="wrap {tone}">
  <div class="ring r1"></div>
  <div class="ring r2"></div>
  <button class="power" {disabled} {onclick} aria-label={state === "running" ? "Stop server" : "Start server"}>
    <Icon name="power" size={56} stroke={2.2} />
  </button>
</div>

<style>
  .wrap {
    position: relative;
    width: 210px;
    height: 210px;
    display: grid;
    place-items: center;
    --c: var(--faint);
    --g: transparent;
  }
  .wrap.on { --c: var(--on); --g: var(--on-glow); }
  .wrap.busy { --c: var(--busy); --g: var(--busy-glow); }
  .wrap.bad { --c: var(--bad); --g: var(--bad-glow); }
  .ring {
    position: absolute;
    inset: 0;
    border-radius: 50%;
    border: 1px solid var(--c);
    opacity: 0.18;
  }
  .r2 { inset: 22px; opacity: 0.3; }
  .busy .ring { animation: pulse 1.8s ease-in-out infinite; }
  .busy .r2 { animation-delay: 0.3s; }
  .on .ring { box-shadow: 0 0 40px var(--g); }
  .power {
    position: relative;
    width: 128px;
    height: 128px;
    border-radius: 50%;
    border: 2px solid var(--c);
    color: var(--c);
    background: radial-gradient(circle at 50% 35%, var(--surface-3), var(--surface));
    box-shadow: 0 0 0 8px rgba(255, 255, 255, 0.02), 0 0 48px var(--g);
    display: grid;
    place-items: center;
    transition: transform 0.15s, box-shadow 0.3s, color 0.3s, border-color 0.3s;
  }
  .power:hover:not(:disabled) { transform: scale(1.04); background: radial-gradient(circle at 50% 35%, var(--surface-3), var(--surface-2)); }
  .power:active:not(:disabled) { transform: scale(0.98); }
  .off .power:hover:not(:disabled) { color: var(--on); border-color: var(--on); }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 0.15; }
    50% { transform: scale(1.05); opacity: 0.45; }
  }
</style>
