<script lang="ts">
  // Trace: what each request cost and how requests relate. Three views of the
  // gateway's in-memory trace (docker/server/reqtrace.py):
  //   1. a turn's steps on a time line (waiting, the model reading, the model
  //      writing, the client running a tool);
  //   2. the prompt of each step against the context window, split into what
  //      came from the prefix cache and what had to be computed;
  //   3. the prefix cache: SGLang's pool counters and the tree of shared
  //      prefixes, rebuilt from the requests (SGLang doesn't expose its own).
  // No text a client sent is shown unless content capture is switched on.
  import Icon from "../components/Icon.svelte";
  import Notice from "../components/Notice.svelte";
  import NodeName from "../components/NodeName.svelte";
  import { api, errorText, type TraceNode, type TraceRecord } from "../lib/api";
  import { app } from "../lib/store.svelte";
  import { median, msPerToken, phases, refreshTrace, refreshTree, resetTrace, trace, turns } from "../lib/trace.svelte";

  // From the moment the backend starts, not only once the model is loaded:
  // its gateway records (and takes the content switch) as soon as it is up,
  // a few seconds in. The page used to wait for "running", so capture couldn't
  // be switched on during the minutes a model takes to load.
  const phase = $derived(app.snapshot?.server.state ?? "stopped");
  const serving = $derived(["starting", "running", "external"].includes(phase));
  /** The gateway has answered: the switches work. */
  const ready = $derived(serving && trace.loaded && !trace.error);
  let paused = $state(false);
  let actionError = $state("");

  $effect(() => {
    if (!serving || paused) return;
    refreshTrace();
    refreshTree();
    const a = setInterval(refreshTrace, 2000);
    const b = setInterval(refreshTree, 4000);
    return () => {
      clearInterval(a);
      clearInterval(b);
    };
  });

  const all = $derived(turns(trace.records));
  let picked = $state<string | null>(null);
  // Follows the newest turn until one is picked by hand.
  const turn = $derived(all.find((t) => t.key === picked) ?? all[0] ?? null);
  const rate = $derived(msPerToken(trace.records));

  /** A client is shown by the nickname given on this machine, else the name it paired with. */
  const nicknames = $derived(app.snapshot?.cluster?.nicknames ?? {});
  const called = (t: { device: string; deviceId: string }) => nicknames[t.deviceId] ?? t.device;

  // ---- formatting -----------------------------------------------------------
  const ms = (v: number | null | undefined) =>
    v == null ? "–" : v >= 10_000 ? `${(v / 1000).toFixed(0)} s` : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`;
  const tok = (v: number | null | undefined) =>
    v == null ? "–" : v >= 10_000 ? `${(v / 1000).toFixed(1)}K` : Math.round(v).toLocaleString();
  const pct = (part: number, whole: number) => (whole > 0 ? `${Math.round((100 * part) / whole)}%` : "–");
  const clock = (unix: number) => new Date(unix * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const KIND: Record<string, string> = {
    action: "Choose action", reply: "Write reply", edit: "Write edit", create: "Write file", plan: "Plan",
    rethink: "Rethink", stock: "Take stock", arguments: "Tool arguments", route: "Ask or edit?", other: "Request",
  };
  const EVENT: Record<string, string> = {
    cut: "tool output cut", dropped: "old turns dropped", repeat: "repeated action", rethink: "rethink", stock: "took stock",
    stuck: "stuck", overflow: "refused as too long", retry: "reply redone", nudge: "nudged", plan: "plan",
  };
  /** A step's name: the tool the action chose once known, else the kind of call. */
  const stepName = (r: TraceRecord) =>
    r.title ?? (r.kind === "action" && r.tool ? `${r.tool}` : (KIND[r.kind ?? "other"] ?? r.kind ?? "Request"));
  /** The prefix cache held far less than an earlier request had already sent. */
  const missed = (r: TraceRecord) =>
    (r.expected_cached ?? 0) >= 512 && (r.cached_tokens ?? 0) < 0.5 * (r.expected_cached ?? 0);

  // ---- tiles ----------------------------------------------------------------
  const done = $derived(trace.records.filter((r) => r.state === "done"));
  const totals = $derived({
    prompt: done.reduce((n, r) => n + (r.prompt_tokens ?? 0), 0),
    cached: done.reduce((n, r) => n + (r.cached_tokens ?? 0), 0),
    step: median(done.map((r) => r.total_ms ?? NaN)),
    first: median(done.filter((r) => r.ttft_ms != null).map((r) => r.ttft_ms!)),
    failed: trace.records.filter((r) => r.state === "failed").length,
  });

  // ---- time line of the picked turn -----------------------------------------
  const line = $derived.by(() => {
    if (!turn) return { rows: [], span: 1 };
    const t0 = turn.records[0].at * 1000;
    const rows = turn.records.map((r) => {
      const p = phases(r, rate);
      return { r, p, start: r.at * 1000 - t0, tool: r.tool_ms ?? 0 };
    });
    const span = Math.max(1, ...rows.map((x) => x.start + (x.r.total_ms ?? 0) + x.tool));
    return { rows, span };
  });
  const sums = $derived.by(() => {
    const s = { queue: 0, read: 0, write: 0, tool: 0, estimated: false };
    for (const { p, tool } of line.rows) {
      s.queue += p.queue;
      s.read += p.read;
      s.write += p.write;
      s.tool += tool;
      s.estimated ||= p.estimated;
    }
    return s;
  });

  /** Round tick marks for the time axis: at most eight. */
  const ticks = $derived.by(() => {
    const steps = [100, 200, 500, 1000, 2000, 5000, 10_000, 20_000, 30_000, 60_000, 120_000, 300_000, 600_000];
    const step = steps.find((v) => line.span / v <= 8) ?? 1_200_000;
    const out: number[] = [];
    for (let t = 0; t <= line.span; t += step) out.push(t);
    return out;
  });
  /** Where the turn's time went, as shares of its whole length. */
  const shares = $derived.by(() => {
    const whole = Math.max(1, turn?.durationMs ?? 1);
    const between = Math.max(0, whole - sums.queue - sums.read - sums.write - sums.tool);
    return [
      { key: "read", label: "Reading the prompt", hint: "the model computing the part of the prompt that wasn't cached", v: sums.read },
      { key: "write", label: "Writing", hint: "the model generating its action or reply", v: sums.write },
      { key: "tool", label: "Client tool", hint: "the client running the tool the model chose", v: sums.tool },
      { key: "queue", label: "Waiting for a slot", hint: "held by the gateway's limits before the model saw it", v: sums.queue },
      { key: "gap", label: "Between requests", hint: "network, and the client between one request and the next", v: between },
    ].map((x) => ({ ...x, share: x.v / whole }));
  });

  // ---- context of the picked turn -------------------------------------------
  const ctx = $derived.by(() => {
    const recs = (turn?.records ?? []).filter((r) => r.prompt_tokens != null);
    const window = Math.max(trace.context, ...recs.map((r) => r.window ?? 0), 1);
    const peak = Math.max(1, ...recs.map((r) => r.prompt_tokens ?? 0));
    // The window sets the scale once the prompt gets near it; far below it,
    // the columns would be slivers, so the scale follows the prompt instead.
    const near = peak > 0.4 * window;
    return { recs, window, top: near ? Math.max(window, peak) : peak * 1.15, near };
  });

  // ---- prefix tree ----------------------------------------------------------
  const ROW = 16;
  const tree = $derived.by(() => {
    const kids = new Map<string, TraceNode[]>();
    for (const n of trace.nodes) (kids.get(n.parent) ?? kids.set(n.parent, []).get(n.parent)!).push(n);
    // The busiest continuation stays on its parent's row; the others branch below.
    for (const list of kids.values()) list.sort((a, b) => b.requests - a.requests || b.last - a.last);
    const placed: { n: TraceNode; row: number; from: number }[] = [];
    let next = 0;
    const todo: { n: TraceNode; row: number | null; from: number }[] = (kids.get("") ?? []).map((n) => ({ n, row: null, from: -1 })).reverse();
    while (todo.length) {
      const { n, row, from } = todo.pop()!;
      const mine = row ?? next++;
      placed.push({ n, row: mine, from });
      const list = kids.get(n.id) ?? [];
      // Pushed in reverse, so the first child (same row) is laid out first.
      for (let i = list.length - 1; i >= 0; i--) todo.push({ n: list[i], row: i === 0 ? mine : null, from: mine });
    }
    const max = Math.max(1, ...trace.nodes.map((n) => n.end));
    return { placed, rows: Math.max(next, 1), max };
  });
  const mine = $derived(new Set(turn?.records.map((r) => r.seq) ?? []));
  /** Recency as four ordered steps: how many requests ago the segment was last used. */
  const age = (n: TraceNode) => {
    const ago = trace.latest - n.last;
    return ago <= 5 ? 3 : ago <= 25 ? 2 : ago <= 100 ? 1 : 0;
  };

  // ---- tooltip (hover and keyboard focus) -----------------------------------
  type Tip = { x: number; y: number; title: string; rows: [string, string][] };
  let tip = $state<Tip | null>(null);
  function show(e: MouseEvent | FocusEvent, title: string, rows: [string, string][]) {
    const box = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const x = "clientX" in e && e.clientX ? e.clientX : box.left + box.width / 2;
    const y = "clientY" in e && e.clientY ? e.clientY : box.bottom;
    tip = { x: Math.min(x + 12, window.innerWidth - 280), y: Math.min(y + 14, window.innerHeight - 30 - 20 * rows.length), title, rows };
  }
  const hide = () => (tip = null);
  const stepTip = (x: (typeof line.rows)[number]): [string, string][] => {
    const { r, p } = x;
    const rows: [string, string][] = [["Total", ms(r.total_ms)]];
    if (p.queue >= 1) rows.push(["Waiting for a slot", ms(p.queue)]);
    if (p.split) {
      rows.push([`Reading the prompt${p.estimated ? " (est.)" : ""}`, ms(p.read)]);
      rows.push([`Writing${p.estimated ? " (est.)" : ""}`, ms(p.write)]);
    }
    if (r.tool_ms != null) rows.push(["Client ran the tool", ms(r.tool_ms)]);
    rows.push(["Prompt", `${tok(r.prompt_tokens)} tokens`]);
    rows.push(["From cache", `${tok(r.cached_tokens)} (${pct(r.cached_tokens ?? 0, r.prompt_tokens ?? 0)})`]);
    rows.push(["Written", `${tok(r.completion_tokens)} tokens`]);
    if (r.error) rows.push(["Error", r.error]);
    return rows;
  };

  // ---- actions --------------------------------------------------------------
  async function setContent(on: boolean) {
    actionError = "";
    try {
      await api.setTraceContent(on);
      await refreshTrace();
    } catch (e) {
      actionError = errorText(e);
    }
  }
  async function clear() {
    actionError = "";
    try {
      await api.clearTrace();
      resetTrace();
      picked = null;
      await refreshTrace();
    } catch (e) {
      actionError = errorText(e);
    }
  }
  let open = $state<number | null>(null); // the step whose captured text is shown
</script>

<div class="page trace">
  <header>
    <div>
      <h1>Trace</h1>
      <p class="muted">
        What each request cost, how the context fills, and what the prefix cache reuses. Kept in memory only, for the
        latest 2,000 requests.
      </p>
    </div>
    <div class="controls">
      <label class="switch" title="Keeps the latest message, the reply and tool titles of each request in the server's memory, to show here. Off: only timings, token counts and hashes are kept, and what was captured is dropped.">
        <input type="checkbox" checked={trace.content} disabled={!ready} onchange={(e) => setContent(e.currentTarget.checked)} />
        Capture content
      </label>
      <button class="ghost small" disabled={!ready} onclick={() => (paused = !paused)}>{paused ? "Resume" : "Pause"}</button>
      <button class="ghost small" disabled={!ready} onclick={clear}><Icon name="trash" size={13} /> Clear</button>
    </div>
  </header>

  {#if actionError}<Notice onclose={() => (actionError = "")}>{actionError}</Notice>{/if}
  {#if trace.content}
    <p class="capturing"><Icon name="alert" size={13} /> Content capture is on: the latest message and the reply of every request are held in the server's memory and shown below. Switch it off to drop them.</p>
  {/if}

  {#if !serving}
    <div class="card empty muted">The trace is recorded while the server runs. Start it on the Server page.</div>
  {:else if trace.error && !trace.records.length && phase === "starting"}
    <div class="card empty muted">The server is starting. The trace (and the Capture content switch) is available as soon as its gateway answers, a few seconds in, well before the model has loaded.</div>
  {:else if trace.error && !trace.records.length}
    <!-- A backend started before this page existed has no trace endpoint. -->
    <div class="card empty muted">This backend isn't recording a trace: it was started with an older version of the app. Stop the server and start it again.</div>
  {:else if !trace.records.length}
    <div class="card empty muted">{trace.loaded ? (phase === "starting" ? "Recording. No requests yet: the model is still loading." : "No requests yet. Send one from a paired client.") : "Loading…"}</div>
  {:else}
    <div class="tiles">
      <div class="card tile"><span class="eyebrow">Requests</span><strong>{trace.records.length.toLocaleString()}</strong><span class="muted">{all.length} turns{totals.failed ? ` · ${totals.failed} failed` : ""}</span></div>
      <div class="card tile"><span class="eyebrow">Prompt from cache</span><strong>{pct(totals.cached, totals.prompt)}</strong><span class="muted">{tok(totals.cached)} of {tok(totals.prompt)} tokens</span></div>
      <div class="card tile"><span class="eyebrow">Median request</span><strong>{ms(totals.step)}</strong><span class="muted">first token {ms(totals.first)} (streamed)</span></div>
      <div class="card tile"><span class="eyebrow">Writing speed</span><strong>{rate ? `${(1000 / rate).toFixed(0)} tok/s` : "–"}</strong><span class="muted">from streamed replies</span></div>
    </div>

    <div class="split">
      <div class="card turns" role="listbox" aria-label="Turns">
        <div class="eyebrow">Turns, newest first</div>
        {#each all as t (t.key)}
          <button class="turn" class:on={turn?.key === t.key} role="option" aria-selected={turn?.key === t.key} onclick={() => (picked = t.key)}>
            <span class="when">{clock(t.at)}</span>
            <span class="what">
              <strong>{t.label}</strong> <span class="muted">· {called(t)}{t.records[0].session ? ` · ${t.records[0].session.slice(0, 4)}` : ""}</span>
              <span class="muted line2">{t.records.length} request{t.records.length === 1 ? "" : "s"} · {ms(t.durationMs)} · {pct(t.cached, t.prompt)} cached</span>
            </span>
            <span class="flags">
              {#if t.running}<span class="badge neutral">running</span>{/if}
              {#if t.events.stuck}<span class="badge insufficient"><Icon name="alert" size={11} /> stuck</span>{/if}
              {#if t.failed}<span class="badge insufficient"><Icon name="alert" size={11} /> {t.failed} failed</span>{/if}
              {#if t.events.cut || t.events.dropped}<span class="badge tight"><Icon name="alert" size={11} /> context cut</span>{/if}
            </span>
          </button>
        {/each}
      </div>

      <div class="detail">
        {#if turn}
          <div class="card">
            <div class="head">
              <div class="eyebrow">Where the time went · {turn.label}</div>
              <span class="client muted">Client: <NodeName id={turn.deviceId} name={turn.device} /></span>
              <div class="legend">
                <span><i class="key read"></i>Reading the prompt</span>
                <span><i class="key write"></i>Writing</span>
                <span><i class="key tool"></i>Client tool</span>
                <span><i class="key queue"></i>Waiting</span>
              </div>
            </div>
            <!-- A span per request on the turn's clock, under the turn's own span:
                 each starts where the one before ended, so they step down and right. -->
            <div class="flame">
              <div class="faxis">
                {#each ticks as t}<span style:left="{(100 * t) / line.span}%">{t === 0 ? "0" : ms(t)}</span>{/each}
              </div>
              <div class="frows">
                {#each ticks as t}<i class="grid" style:left="{(100 * t) / line.span}%"></i>{/each}
                <div class="frow"><div class="span root" style:left="0" style:width="100%"><b>{turn.label} · {called(turn)}</b><em>{ms(turn.durationMs)}</em></div></div>
                {#each line.rows as x (x.r.seq)}
                  {@const r = x.r}
                  {@const from = (100 * x.start) / line.span}
                  {@const wide = (100 * ((r.total_ms ?? 0) + x.tool)) / line.span}
                  <div class="frow">
                    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                    <div class="span" role="img" tabindex="0" aria-label="{stepName(r)}: {ms(r.total_ms)}" style:left="{from}%" style:width="{wide}%"
                      onmousemove={(e) => show(e, stepName(r), stepTip(x))} onmouseleave={hide}
                      onfocus={(e) => show(e, stepName(r), stepTip(x))} onblur={hide}>
                      {#if x.p.queue >= 1}<i class="seg queue" style:flex-grow={x.p.queue}></i>{/if}
                      {#if x.p.read >= 1}<i class="seg read" style:flex-grow={x.p.read}></i>{/if}
                      {#if x.p.write >= 1}<i class="seg write" style:flex-grow={x.p.write}></i>{/if}
                      {#if x.tool >= 1}<i class="seg tool" style:flex-grow={x.tool}></i>{/if}
                      {#if r.state === "failed"}<i class="seg failed"></i>{/if}
                    </div>
                    <!-- The name sits beside its span: after it, or before it near the right edge. -->
                    {#if from + wide < 70}
                      <span class="fname" style:left="calc({from + wide}% + 6px)">{stepName(r)} <em>{ms(r.total_ms)}</em></span>
                    {:else}
                      <span class="fname before" style:right="calc({100 - from}% + 6px)">{stepName(r)} <em>{ms(r.total_ms)}</em></span>
                    {/if}
                  </div>
                {/each}
              </div>
            </div>
            <table class="shares">
              <thead><tr><th>Phase</th><th class="n">Time</th><th class="n">% of the turn</th><th></th></tr></thead>
              <tbody>
                {#each shares as sh (sh.key)}
                  <tr title={sh.hint}>
                    <td><i class="key {sh.key}"></i>{sh.label}</td>
                    <td class="n">{ms(sh.v)}</td>
                    <td class="n">{(100 * sh.share).toFixed(1)}%</td>
                    <td class="bar"><div><i style:width="{Math.min(100, 100 * sh.share)}%"></i></div></td>
                  </tr>
                {/each}
              </tbody>
            </table>
            {#if sums.estimated}
              <p class="muted foot">Calls that aren't streamed have no first-token time: their split is estimated from the tokens written, at the speed of the streamed replies.</p>
            {:else if line.rows.some((x) => !x.p.split)}
              <p class="muted foot">Reading and writing can't be told apart yet: no streamed reply in the trace to measure the writing speed from.</p>
            {/if}
          </div>

          <div class="card">
            <div class="head">
              <div class="eyebrow">Context per request · window {tok(ctx.window)} tokens</div>
              <div class="legend">
                <span><i class="key cached"></i>From the prefix cache (free)</span>
                <span><i class="key read"></i>Computed (the model read it)</span>
              </div>
            </div>
            <div class="cols" style:--limit="{(100 * ctx.window) / ctx.top}%">
              {#if ctx.near}<span class="limit"><span>window</span></span>{/if}
              {#each ctx.recs as r (r.seq)}
                {@const cached = r.cached_tokens ?? 0}
                {@const fresh = Math.max(0, (r.prompt_tokens ?? 0) - cached)}
                {@const rows = [["Prompt", `${tok(r.prompt_tokens)} tokens (${pct(r.prompt_tokens ?? 0, ctx.window)} of the window)`], ["From cache", tok(cached)], ["Computed", tok(fresh)], ...(r.expected_cached != null ? [["Sent before", tok(r.expected_cached)]] : []), ...Object.entries(r.events ?? {}).map(([k, v]) => [EVENT[k] ?? k, `×${v}`])] as [string, string][]}
                <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
                <div class="col" role="img" tabindex="0" aria-label="Request {r.step ?? r.seq}: {tok(r.prompt_tokens)} prompt tokens, {tok(cached)} from cache"
                  onmousemove={(e) => show(e, `${r.step ?? ""} ${stepName(r)}`, rows)} onmouseleave={hide}
                  onfocus={(e) => show(e, `${r.step ?? ""} ${stepName(r)}`, rows)} onblur={hide}>
                  {#if r.events?.cut || r.events?.dropped || missed(r)}<span class="mark" title="context changed here">▾</span>{/if}
                  <i class="seg read" style:height="{(100 * fresh) / ctx.top}%"></i>
                  <i class="seg cached" style:height="{(100 * cached) / ctx.top}%"></i>
                </div>
              {/each}
            </div>
            <p class="muted foot">
              Peak {tok(turn.peakPrompt)} tokens, {pct(turn.peakPrompt, ctx.window)} of the window{ctx.near ? "" : " (the window is off the top of this scale)"}. ▾ marks a request where the client cut
              context, or where the cache held less than half of what had been sent before.
            </p>
          </div>

          <div class="card">
            <div class="eyebrow">Requests of this turn</div>
            <div class="tablewrap">
              <table>
                <thead>
                  <tr><th>#</th><th>Call</th><th class="n">Waited</th><th class="n">First token</th><th class="n">Total</th><th class="n">Tool</th><th class="n">Prompt</th><th class="n">Cached</th><th class="n">Written</th><th>Notes</th></tr>
                </thead>
                <tbody>
                  {#each turn.records as r (r.seq)}
                    <tr class:failed={r.state === "failed"}>
                      <td class="n muted">{r.step ?? r.seq}</td>
                      <td>
                        {stepName(r)}
                        {#if r.kind && r.kind !== "action" && r.title}<span class="muted"> · {KIND[r.kind] ?? r.kind}</span>{/if}
                        {#if r.input_tail != null || r.output}
                          <button class="ghost small peek" onclick={() => (open = open === r.seq ? null : r.seq)}>{open === r.seq ? "hide" : "show"} text</button>
                        {/if}
                      </td>
                      <td class="n">{ms(r.queue_ms)}</td>
                      <td class="n">{ms(r.ttft_ms)}</td>
                      <td class="n">{r.state === "queued" || r.state === "running" ? "…" : ms(r.total_ms)}</td>
                      <td class="n">{ms(r.tool_ms)}</td>
                      <td class="n">{tok(r.prompt_tokens)}</td>
                      <td class="n">{tok(r.cached_tokens)} <span class="muted">{r.prompt_tokens ? pct(r.cached_tokens ?? 0, r.prompt_tokens) : ""}</span></td>
                      <td class="n">{tok(r.completion_tokens)}</td>
                      <td><div class="notes">
                        {#if r.state === "failed"}<span class="badge insufficient"><Icon name="alert" size={11} /> {r.status ?? ""} {r.error ?? "failed"}</span>{/if}
                        {#if missed(r)}<span class="badge tight" title="An earlier request had already sent {tok(r.expected_cached)} tokens of this prompt; the cache served {tok(r.cached_tokens)}."><Icon name="alert" size={11} /> cache miss</span>{/if}
                        {#each Object.entries(r.events ?? {}) as [k, v]}<span class="badge neutral">{EVENT[k] ?? k}{v > 1 ? ` ×${v}` : ""}</span>{/each}
                        {#if r.searched}<span class="badge neutral">web search</span>{/if}
                      </div></td>
                    </tr>
                    {#if open === r.seq}
                      <tr class="text"><td></td><td colspan="9">
                        {#if r.input_tail}<div class="eyebrow">Latest message (end)</div><pre class="selectable">{r.input_tail}</pre>{/if}
                        {#if r.output}<div class="eyebrow">Reply</div><pre class="selectable">{r.output}</pre>{/if}
                      </td></tr>
                    {/if}
                  {/each}
                </tbody>
              </table>
            </div>
          </div>
        {/if}
      </div>
    </div>

    <div class="card">
      <div class="head">
        <div class="eyebrow">Prefix cache (RadixAttention)</div>
        <div class="legend">
          <span>Last used:</span>
          <span><i class="key age a3"></i>≤ 5 requests ago</span>
          <span><i class="key age a2"></i>≤ 25</span>
          <span><i class="key age a1"></i>≤ 100</span>
          <span><i class="key age a0"></i>older</span>
        </div>
      </div>
      {#if trace.pool.max_total_num_tokens}
        {@const max = trace.pool.max_total_num_tokens}
        {@const busy = trace.pool.kv_used_tokens ?? 0}
        {@const kept = trace.pool.kv_evictable_tokens ?? 0}
        <div class="pool">
          <div class="meter" role="img" aria-label="{tok(kept)} tokens cached and {tok(busy)} in use, of {tok(max)}">
            <i class="read" style:width="{Math.min(100, (100 * busy) / max)}%"></i><i class="cached" style:width="{Math.min(100, (100 * kept) / max)}%"></i>
          </div>
          <span><strong>{tok(kept)}</strong> of {tok(max)} tokens cached for reuse ({pct(kept, max)})</span>
          <span class="muted">{tok(busy)} in use by running requests · {tok(trace.pool.evicted_tokens_total)} evicted so far</span>
          {#if trace.pool.state_available != null}<span class="muted">recurrent state: {tok(trace.pool.state_used)} in use, {tok(trace.pool.state_evictable)} cached, {tok(trace.pool.state_available)} free</span>{/if}
        </div>
      {/if}
      <div class="treebox">
        <div class="tree" style:height="{tree.rows * ROW + 6}px">
          {#each tree.placed as { n, row, from } (n.id)}
            {#if from >= 0 && from !== row}
              <i class="branch" style:left="{(100 * n.start) / tree.max}%" style:top="{from * ROW + 8}px" style:height="{(row - from) * ROW}px"></i>
            {/if}
            <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
            <div class="node a{age(n)}" class:mine={n.ends.some((s) => mine.has(s))} role="img" tabindex="0"
              aria-label="Shared prefix, tokens {tok(n.start)} to {tok(n.end)}, used by {n.requests} requests"
              style:left="{(100 * n.start) / tree.max}%" style:width="max(3px, calc({(100 * (n.end - n.start)) / tree.max}% - 2px))" style:top="{row * ROW + 3}px"
              onmousemove={(e) => show(e, `Tokens ${tok(n.start)} – ${tok(n.end)}`, [["Size", `${tok(n.end - n.start)} tokens`], ["Requests through it", String(n.requests)], ["Requests ending here", String(n.ended)], ["Last used", `${trace.latest - n.last} requests ago, ${clock(n.last_at)}`]])}
              onmouseleave={hide}
              onfocus={(e) => show(e, `Tokens ${tok(n.start)} – ${tok(n.end)}`, [["Size", `${tok(n.end - n.start)} tokens`], ["Requests through it", String(n.requests)], ["Requests ending here", String(n.ended)]])}
              onblur={hide}></div>
          {/each}
        </div>
        <div class="axis"><span>0</span><span>{tok(tree.max / 2)}</span><span>{tok(tree.max)} tokens into the prompt</span></div>
      </div>
      <p class="muted foot">
        Each bar is a run of the prompt that requests share; a branch is where two requests part ways. Rebuilt from the requests
        this server received (SGLang doesn't expose its own tree), so it shows what could be reused; "Cached" in the table is
        what SGLang did reuse. Outlined: the picked turn.
      </p>
    </div>
  {/if}
</div>

{#if tip}
  <div class="tip" style:left="{tip.x}px" style:top="{tip.y}px" role="tooltip">
    <div class="tt">{tip.title}</div>
    {#each tip.rows as [label, value]}<div class="tr"><span class="muted">{label}</span><strong>{value}</strong></div>{/each}
  </div>
{/if}

<style>
  /* Chart colours: three validated categorical slots (reading, writing, client
     tool) and one blue ramp for recency; light and dark are separate steps. */
  .trace {
    /* One colour per meaning on the whole page. Blue is the prompt: strong where
       the model had to read it (time in one chart, tokens in the other), pale
       where it came from the cache. Orange is writing, green the client. */
    --c-read: #3987e5; --c-cached: #184f95; --c-write: #d95926; --c-tool: #199e70; --c-queue: var(--slate);
    --a0: #184f95; --a1: #256abf; --a2: #3987e5; --a3: #86b6ef;
    display: flex; flex-direction: column; gap: 14px;
  }
  :global(:root[data-theme="light"]) .trace {
    --c-read: #2a78d6; --c-cached: #86b6ef; --c-write: #eb6834; --c-tool: #1baf7a;
    --a0: #86b6ef; --a1: #5598e7; --a2: #2a78d6; --a3: #184f95;
  }
  header { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }
  h1 { margin: 0 0 4px; font-size: 22px; }
  header p { margin: 0; max-width: 62ch; font-size: 13px; }
  .controls { display: flex; gap: 8px; align-items: center; }
  .switch { display: inline-flex; gap: 6px; align-items: center; font-size: 13px; color: var(--muted); cursor: pointer; }
  .capturing { margin: 0; padding: 8px 12px; border-radius: 10px; background: var(--busy-soft); color: var(--warn-text); font-size: 12.5px; display: flex; gap: 8px; align-items: center; }
  .empty { padding: 28px; text-align: center; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
  .tile { display: flex; flex-direction: column; gap: 4px; }
  .tile strong { font-size: 24px; font-weight: 650; }
  .tile .muted { font-size: 12px; }
  .split { display: grid; grid-template-columns: minmax(220px, 280px) minmax(0, 1fr); gap: 14px; align-items: start; }
  /* Narrow window: the turn list goes on top as a short scrolling strip, and
     the cards take the full width (sized by the page, not the screen). */
  .trace { container-type: inline-size; }
  @container (max-width: 860px) {
    .split { grid-template-columns: 1fr; }
    .tiles { grid-template-columns: 1fr 1fr; }
    .turns { max-height: 190px; }
    .shares .bar { display: none; }
    .fname { font-size: 11px; }
    header { flex-direction: column; }
  }
  @container (max-width: 520px) {
    .tiles { grid-template-columns: 1fr 1fr; }
    .tile strong { font-size: 19px; }
    .legend { gap: 8px; font-size: 11px; }
    .flame { --row: 20px; }
  }
  .detail { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
  .turns { display: flex; flex-direction: column; gap: 2px; max-height: 720px; overflow-y: auto; padding: 12px 8px; }
  .turns .eyebrow { padding: 0 6px 6px; }
  .turn { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; text-align: left; border: 0; background: transparent; padding: 7px 6px; border-radius: 8px; font-size: 12.5px; }
  .turn.on { background: var(--surface-2); box-shadow: inset 2px 0 0 var(--text); }
  .when { color: var(--faint); font-variant-numeric: tabular-nums; font-size: 11.5px; padding-top: 1px; }
  .line2 { display: block; font-size: 11.5px; }
  .flags { grid-column: 2; display: flex; gap: 4px; flex-wrap: wrap; }
  .flags :global(.badge), .notes :global(.badge) { display: inline-flex; gap: 3px; align-items: center; font-size: 10.5px; }
  .head { display: flex; justify-content: space-between; gap: 10px 18px; flex-wrap: wrap; align-items: baseline; margin-bottom: 10px; }
  .legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .key { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
  .queue { background: var(--c-queue); }
  .read { background: var(--c-read); }
  .write { background: var(--c-write); }
  .tool { background: var(--c-tool); }
  .cached { background: var(--c-cached); }
  .key.gap { background: transparent; box-shadow: inset 0 0 0 1px var(--faint); }
  .key.age.a0, .node.a0 { background: var(--a0); }
  .key.age.a1, .node.a1 { background: var(--a1); }
  .key.age.a2, .node.a2 { background: var(--a2); }
  .key.age.a3, .node.a3 { background: var(--a3); }
  .foot { margin: 10px 0 0; font-size: 12px; }
  .client { font-size: 12px; margin-right: auto; }

  /* Time line, flame-graph style: a time axis, the turn's span, then one span
     per request placed on the turn's clock, coloured by what the time was. */
  .flame { --row: 22px; position: relative; font-size: 12px; }
  .faxis { position: relative; height: 18px; border-bottom: 1px solid var(--faint); color: var(--muted); font-variant-numeric: tabular-nums; font-size: 11px; }
  .faxis span { position: absolute; bottom: 3px; transform: translateX(-50%); white-space: nowrap; }
  .faxis span:first-child { transform: none; }
  .faxis span::after { content: ""; position: absolute; left: 50%; bottom: -4px; height: 4px; border-left: 1px solid var(--faint); }
  .faxis span:first-child::after { left: 0; }
  .frows { position: relative; max-height: 440px; overflow-y: auto; overflow-x: hidden; padding: 4px 0; }
  .grid { position: absolute; top: 0; bottom: 0; border-left: 1px solid var(--border); opacity: 0.6; pointer-events: none; }
  .frow { position: relative; height: var(--row); }
  .span { position: absolute; top: 2px; height: calc(var(--row) - 4px); min-width: 3px; display: flex; gap: 1px; border-radius: 3px; overflow: hidden; }
  .span:hover, .span:focus-visible { outline: 2px solid var(--text); outline-offset: 1px; }
  .span.root { background: var(--surface-3); align-items: center; justify-content: space-between; padding: 0 8px; gap: 8px; color: var(--text); }
  .span.root b { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .span.root em, .fname em { font-style: normal; color: var(--muted); font-variant-numeric: tabular-nums; }
  .seg { display: block; min-width: 1px; flex-basis: 0; }
  .seg.failed { flex: 0 0 4px; background: var(--bad); }
  .fname { position: absolute; top: 0; line-height: var(--row); white-space: nowrap; color: var(--text); pointer-events: none; max-width: 60%; overflow: hidden; text-overflow: ellipsis; }
  .fname.before { text-align: right; }
  /* The legend as a table, with each phase's share of the turn. */
  .shares { width: 100%; margin-top: 12px; border-collapse: collapse; font-size: 12.5px; }
  .shares td, .shares th { padding: 4px 8px; }
  .shares .bar { width: 28%; min-width: 70px; }
  .shares .bar div { height: 8px; border: 1px solid var(--faint); border-radius: 2px; }
  .shares .bar i { display: block; height: 100%; background: var(--faint); }

  /* Context: stacked columns against the window. */
  .cols { position: relative; display: flex; align-items: flex-end; gap: 2px; height: 150px; padding-top: 14px; border-bottom: 1px solid var(--border); }
  .col { position: relative; flex: 1 1 0; max-width: 24px; min-width: 3px; height: 100%; display: flex; flex-direction: column; justify-content: flex-end; gap: 2px; }
  .col:hover, .col:focus-visible { outline: 1px solid var(--faint); outline-offset: 1px; }
  .col .seg { width: 100%; min-height: 0; flex: 0 0 auto; }
  .col .seg:first-of-type { border-radius: 3px 3px 0 0; }
  .col .mark { position: absolute; top: -13px; left: 50%; transform: translateX(-50%); font-size: 10px; color: var(--text); line-height: 1; }
  .limit { position: absolute; left: 0; right: 0; bottom: var(--limit); border-top: 1px solid var(--faint); pointer-events: none; }
  .limit span { position: absolute; right: 0; top: -14px; font-size: 10.5px; color: var(--faint); }

  .tablewrap { overflow: auto; margin-top: 8px; max-height: 460px; }
  thead th { position: sticky; top: 0; background: var(--surface); }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th { text-align: left; font-weight: 500; color: var(--faint); font-size: 11.5px; padding: 4px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 5px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  .n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  tr.failed td { background: var(--bad-soft); }
  .notes { display: flex; gap: 4px; flex-wrap: wrap; }
  .peek { padding: 0 6px; font-size: 11px; }
  tr.text pre { margin: 4px 0 10px; padding: 8px 10px; background: var(--log-bg); color: var(--log-text); border-radius: 8px; font: 11.5px/1.5 var(--mono); white-space: pre-wrap; max-height: 260px; overflow: auto; }

  .pool { display: flex; gap: 8px 16px; align-items: center; flex-wrap: wrap; font-size: 12.5px; margin-bottom: 12px; }
  .meter { flex: 0 1 220px; min-width: 120px; height: 8px; border-radius: 4px; background: var(--surface-3); overflow: hidden; }
  .meter { display: flex; gap: 2px; }
  .meter i { display: block; height: 100%; }
  .treebox { max-height: 340px; overflow-y: auto; padding-right: 4px; }
  .tree { position: relative; min-height: 22px; }
  .node { position: absolute; height: 10px; border-radius: 0 4px 4px 0; }
  .node:hover, .node:focus-visible { filter: brightness(1.2); outline: 1px solid var(--text); }
  .node.mine { box-shadow: 0 0 0 2px var(--surface), 0 0 0 3px var(--text); }
  .branch { position: absolute; width: 1px; background: var(--border); }
  .axis { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--faint); border-top: 1px solid var(--border); padding-top: 3px; margin-top: 2px; font-variant-numeric: tabular-nums; }

  .tip { position: fixed; z-index: 60; pointer-events: none; min-width: 180px; max-width: 280px; padding: 8px 10px; border-radius: 8px; background: var(--surface-3); border: 1px solid var(--border); box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35); font-size: 12px; }
  .tt { font-weight: 600; margin-bottom: 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tr { display: flex; justify-content: space-between; gap: 14px; }
  .tr strong { font-variant-numeric: tabular-nums; font-weight: 600; text-align: right; }
</style>
