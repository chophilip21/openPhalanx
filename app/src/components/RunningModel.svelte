<script lang="ts">
  // The model the backend is serving. It's fixed until the server stops,
  // which the tooltip explains.
  import Icon from "./Icon.svelte";
  import type { RunningModel } from "../lib/api";
  import { tokens } from "../lib/format";

  let { model, loading = false }: { model: RunningModel; loading?: boolean } = $props();

  const facts = $derived(
    [model.quant, model.context_len ? `${tokens(model.context_len)} context` : null].filter(Boolean).join(" · "),
  );
</script>

<button type="button" class="running" aria-label="Model {loading ? 'loading' : 'running'}: {model.label}. Stop the server to switch models.">
  <span class="lock"><Icon name="lock" size={14} /></span>
  <span class="text">
    <span class="eyebrow">{loading ? "Loading" : "Running"}</span>
    <span class="name">{model.label}</span>
    {#if facts}<span class="facts">{facts}</span>{/if}
    {#if model.split}<span class="split"><Icon name="users" size={12} /> Split · {model.split}</span>{/if}
  </span>
  <span class="info"><Icon name="info" size={14} /></span>
  <span class="tip-box">
    <strong>Locked while the server runs.</strong> The model and context window are loaded into the GPU when the
    server starts. To switch models, stop the server (the power button on the Server page), choose another model,
    then start it again.
    {#if model.split}<br /><br />Split across the cluster: every server listed holds part of the model, so serving
      pauses if one drops out.{/if}
  </span>
</button>

<style>
  .running {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 8px 12px 8px 10px;
    border-radius: 12px;
    border: 1px solid color-mix(in srgb, var(--on) 45%, transparent);
    background: var(--on-soft);
    color: var(--text);
    cursor: help;
    text-align: left;
    font-weight: 400;
    max-width: 100%;
  }
  .lock { color: var(--on); display: inline-flex; }
  .text { display: inline-flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; min-width: 0; }
  .eyebrow { color: var(--on); }
  .name { font-weight: 650; font-size: 15px; overflow-wrap: anywhere; }
  .facts { color: var(--muted); font-size: 13px; }
  .split { flex-basis: 100%; display: inline-flex; gap: 6px; align-items: center; color: var(--violet); font-size: 12px; font-weight: 600; }
  .info { color: var(--muted); display: inline-flex; }
  .running:hover .info, .running:focus .info { color: var(--text); }
  .tip-box {
    display: none;
    position: absolute;
    top: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    width: min(360px, 80vw);
    padding: 12px 14px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    color: var(--text);
    font-size: 13px;
    line-height: 1.5;
    z-index: 20;
  }
  .running:hover .tip-box, .running:focus .tip-box { display: block; }
</style>
