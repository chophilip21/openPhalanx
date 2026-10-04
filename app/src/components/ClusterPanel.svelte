<script lang="ts">
  // Cluster management: requests waiting for approval, this machine's role,
  // and the Openphalanx servers on this network that this machine can reach.
  // Only running, reachable servers are listed (see core cluster.rs).
  import { ask } from "@tauri-apps/plugin-dialog";
  import Icon from "./Icon.svelte";
  import { api, errorText, type ClusterState, type ClusterStrategy } from "../lib/api";

  let { cluster }: { cluster: ClusterState } = $props();

  let busy = $state("");
  let error = $state("");
  let notice = $state("");
  let renaming = $state(false);
  let newName = $state("");

  const short = (fp: string) => fp.split(":").slice(0, 8).join(":") + "…";
  const role = $derived(cluster.role);
  const memberCount = $derived(cluster.members.length);

  /** Every server in this machine's cluster, this machine first. */
  const inCluster = $derived.by(() => {
    const me = { id: cluster.id, name: `${cluster.name} (this machine)`, url: (cluster.url ?? "").replace("https://", ""), online: true };
    if (role.role === "host") {
      return [
        { ...me, label: "Host", badge: "host" },
        ...cluster.members.map((m) => ({
          id: m.id,
          name: m.name,
          url: m.url.replace("https://", ""),
          online: m.online,
          label: m.online ? "Member" : "Member · offline",
          badge: m.online ? "ok" : "neutral",
          removable: true,
        })),
      ];
    }
    if (role.role === "member") {
      const link = cluster.host_link;
      return [
        { id: role.host.id, name: role.host.name, url: role.host.url.replace("https://", ""), online: link?.connected ?? false,
          label: link?.connected ? "Host" : "Host · unreachable", badge: link?.connected ? "host" : "neutral" },
        { ...me, label: "Member", badge: "ok" },
      ];
    }
    return [];
  });

  async function act(key: string, f: () => Promise<unknown>, done = "") {
    busy = key;
    error = "";
    notice = "";
    try {
      await f();
      notice = done;
    } catch (e) {
      error = errorText(e);
    } finally {
      busy = "";
    }
  }

  async function removeMember(id: string, name: string) {
    const yes = await ask(
      `Remove ${name} from the cluster? It becomes standalone and shows up again under "Not in this cluster", where you can add it back.`,
      { title: "Remove from cluster", kind: "warning" },
    );
    if (yes) await act(`remove:${id}`, () => api.clusterRemove(id), `Removed ${name}. Add it back any time.`);
  }

  async function approveHost(fromId: string) {
    await act(`host:${fromId}`, async () => {
      const problems = await api.clusterApproveHost(fromId);
      if (problems.length) throw new Error(`This machine is now the host, but some servers couldn't be invited: ${problems.join("; ")}`);
    }, "This machine is now the host; the others join as they confirm.");
  }

  async function leave() {
    if (role.role !== "member") return;
    const yes = await ask(`Leave ${role.host.name}'s cluster? This machine becomes standalone.`, { title: "Leave cluster", kind: "warning" });
    if (yes) await act("leave", () => api.clusterLeave(), "Left the cluster.");
  }

  async function dissolve() {
    const yes = await ask(`End this cluster? All ${memberCount} member${memberCount === 1 ? "" : "s"} become standalone.`, {
      title: "Dissolve cluster",
      kind: "warning",
    });
    if (yes) await act("dissolve", () => api.clusterDissolve(), "Cluster dissolved.");
  }

  const STRATEGIES: { id: ClusterStrategy; label: string; summary: string }[] = [
    { id: "split", label: "Split model", summary: "One model across all servers: load models bigger than any single machine." },
    { id: "replicas", label: "Replicas", summary: "A full copy on every server: more users at full speed, keeps serving if one drops." },
  ];
  const current = $derived(STRATEGIES.find((s) => s.id === cluster.strategy) ?? STRATEGIES[0]);

  async function setStrategy(s: ClusterStrategy) {
    if (s === cluster.strategy) return;
    await act("strategy", () => api.clusterSetStrategy(s));
  }

  async function rename() {
    const name = newName.trim();
    if (!name) return;
    await act("rename", () => api.clusterRename(name));
    renaming = false;
  }
</script>

<div class="card cluster">
  {#each cluster.invites as inv (inv.host.id)}
    <div class="request">
      <Icon name="server" size={16} />
      <div class="req-text">
        <strong>{inv.host.name}</strong> invites this machine into its cluster.
        <span class="muted">Check that its certificate reads <span class="mono">{short(inv.host.fingerprint)}</span> on that machine.</span>
      </div>
      <button class="primary" disabled={!!busy} onclick={() => act(`inv:${inv.host.id}`, () => api.clusterApproveInvite(inv.host.id), `Joined ${inv.host.name}'s cluster.`)}>Approve</button>
      <button class="ghost" disabled={!!busy} onclick={() => api.clusterDecline(inv.host.id)}>Decline</button>
    </div>
  {/each}
  {#each cluster.host_requests as req (req.from.id)}
    <div class="request">
      <Icon name="shield" size={16} />
      <div class="req-text">
        <strong>{req.from.name}</strong> asks this machine to become the host of a {req.members.length}-server cluster.
        <span class="muted">Clients would then pair here. Certificate <span class="mono">{short(req.from.fingerprint)}</span>.</span>
      </div>
      <button class="primary" disabled={!!busy} onclick={() => approveHost(req.from.id)}>Become host</button>
      <button class="ghost" disabled={!!busy} onclick={() => api.clusterDecline(req.from.id)}>Decline</button>
    </div>
  {/each}

  <div class="me">
    <div class="me-text">
      {#if renaming}
        <form class="rename" onsubmit={(e) => { e.preventDefault(); rename(); }}>
          <input bind:value={newName} maxlength="64" placeholder="Server name" />
          <button type="submit" disabled={!newName.trim() || !!busy}>Save</button>
          <button type="button" class="ghost" onclick={() => (renaming = false)}>Cancel</button>
        </form>
      {:else}
        <span class="name">{cluster.name}</span>
        <button class="ghost tiny-btn" title="Rename this server" onclick={() => { newName = cluster.name; renaming = true; }}>Rename</button>
      {/if}
      <span class="muted small">
        {#if role.role === "host"}
          Host of a {memberCount + 1}-server cluster · clients pair here
        {:else if role.role === "member"}
          Member of <strong>{role.host.name}</strong>'s cluster · clients pair with the host
          {#if cluster.host_link && !cluster.host_link.connected}<span class="warn"> · {cluster.host_link.error ?? "connecting…"}</span>{/if}
        {:else}
          Standalone · add servers below to form a cluster
        {/if}
        · certificate <span class="mono">{short(cluster.fingerprint)}</span>
      </span>
    </div>
    {#if role.role === "member"}
      <button class="ghost" disabled={!!busy} onclick={leave}>Leave cluster</button>
    {:else if role.role === "host"}
      <button class="ghost" disabled={!!busy} onclick={dissolve}>Dissolve</button>
    {/if}
  </div>

  <div class="strategy {cluster.strategy}">
    <span class="eyebrow strategy-label">Strategy</span>
    <div class="seg" role="radiogroup" aria-label="Cluster strategy">
      {#each STRATEGIES as st}
        <button
          role="radio"
          aria-checked={cluster.strategy === st.id}
          class:active={cluster.strategy === st.id}
          disabled={role.role === "member" || !!busy}
          onclick={() => setStrategy(st.id)}
        >{st.label}</button>
      {/each}
    </div>
    <button type="button" class="tip" aria-label="About cluster strategies">
      <Icon name="info" size={15} />
      <span class="tip-box">
        <strong>Split model</strong> (default): one model, its layers spread across the servers. Load models
        bigger than any one machine (e.g. a 32B coder on a 24 GB + 16 GB pair). About single-GPU speed;
        needs every server up and a fast wired network.
        <br /><br />
        <strong>Replicas</strong>: every server runs its own full copy, and requests are spread across them.
        More users and parallel agents at full speed, and service continues if a server drops out; the
        model must fit each machine.
        <br /><br />
        <span class="muted">The host decides. Used once members serve models (the next step).</span>
      </span>
    </button>
    <span class="strategy-summary">{current.summary}</span>
    {#if role.role === "member"}<span class="muted small">set by {role.host.name}</span>{/if}
  </div>

  {#if role.role !== "standalone"}
    <div class="servers">
      <div class="servers-head">
        <span class="eyebrow in">In this cluster</span>
        <span class="muted small">{inCluster.length} server{inCluster.length === 1 ? "" : "s"}</span>
      </div>
      {#each inCluster as m (m.id)}
        <div class="server joined" class:offline={!m.online}>
          <span class="node-icon joined-icon"><Icon name="check" size={14} stroke={2.6} /></span>
          <div class="server-text">
            <span class="name">{m.name}</span>
            <span class="muted mono tiny">{m.url}</span>
          </div>
          <span class="badge {m.badge}">{m.label}</span>
          {#if "removable" in m}
            <button class="remove" disabled={!!busy} title="Take {m.name} out of the cluster; you can add it back"
              onclick={() => removeMember(m.id, m.name)}>
              <Icon name="x" size={13} /> Remove
            </button>
          {:else if role.role === "member" && m.id === cluster.id}
            <button class="remove" disabled={!!busy} title="Make this machine standalone" onclick={leave}>
              <Icon name="x" size={13} /> Leave
            </button>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <div class="servers">
    <div class="servers-head">
      <span class="eyebrow">{role.role === "standalone" ? "Servers on this network" : "Not in this cluster"}</span>
      <span class="muted small">running Openphalanx and reachable from here</span>
    </div>
    {#if cluster.discovery_error}
      <p class="warn small">{cluster.discovery_error}</p>
    {:else if cluster.candidates.length === 0}
      <p class="muted small">No other servers found. Start Openphalanx (or openphalanx-server) on another machine on this network.</p>
    {/if}
    {#each cluster.candidates as c (c.id)}
      <div class="server available" class:busy={c.busy}>
        <span class="node-icon"><Icon name="server" size={15} /></span>
        <div class="server-text">
          <span class="name">{c.name}</span>
          <span class="muted mono tiny">{c.url.replace("https://", "")} · {short(c.fingerprint)}</span>
        </div>
        {#if c.busy}
          <span class="badge neutral">In another cluster</span>
        {:else if role.role !== "member"}
          <button disabled={!!busy} onclick={() => act(`add:${c.id}`, () => api.clusterInvite(c.id), `Invited ${c.name}. It joins once someone approves there.`)}>
            <Icon name="plus" size={13} /> Add to cluster
          </button>
          <button class="ghost" disabled={!!busy} title="Ask it to host this machine{memberCount ? ' and its members' : ''}"
            onclick={() => act(`host:${c.id}`, () => api.clusterMakeHost(c.id), `Asked ${c.name} to host. Approve it on that machine.`)}>
            Make host
          </button>
        {/if}
      </div>
    {/each}
  </div>

  {#if notice}<p class="ok small">{notice}</p>{/if}
  {#if error}<p class="err small">{error}</p>{/if}
  {#if cluster.error}<p class="err small">{cluster.error}</p>{/if}
</div>

<style>
  .cluster { display: flex; flex-direction: column; gap: 14px; }
  .request { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 10px; border: 1px solid var(--busy); background: var(--busy-soft); flex-wrap: wrap; }
  .req-text { flex: 1; min-width: 240px; font-size: 13px; display: flex; flex-direction: column; gap: 2px; }
  .me { display: flex; align-items: center; gap: 12px; justify-content: space-between; flex-wrap: wrap; }
  .me-text { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .name { font-weight: 600; }
  .rename { display: flex; gap: 6px; }
  .rename input { padding: 4px 8px; }
  .tiny-btn { padding: 2px 6px; font-size: 12px; }
  .servers { display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--border); padding-top: 12px; }
  .servers-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  .server { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 10px; flex-wrap: wrap; }
  /* In the cluster: solid, tinted, check mark. */
  .server.joined { background: var(--on-soft); border: 1px solid var(--on); }
  .server.joined.offline { opacity: 0.6; }
  .joined-icon { background: var(--on) !important; color: var(--on-ink) !important; }
  /* Not in it: dashed outline, so it reads as "available". */
  .server.available { background: transparent; border: 1px dashed var(--border); }
  .server.available.busy { opacity: 0.55; }
  .eyebrow.in { color: var(--on); }
  .badge.host { color: var(--link); background: var(--surface); }
  .server-text { display: flex; flex-direction: column; flex: 1; min-width: 180px; }
  .server button { display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px; font-size: 12.5px; }
  .node-icon { width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; background: var(--surface); color: var(--muted); }
  .small { font-size: 12.5px; margin: 0; }
  .tiny { font-size: 11.5px; }
  .warn { color: var(--busy); }
  .ok { color: var(--on); }
  .err { color: var(--bad); }
  /* The strategy decides what the cluster can do, so it is colour-coded:
     violet for Split model, blue for Replicas. */
  .strategy {
    --accent: var(--violet);
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 12px 14px;
    border-radius: 12px;
    border: 1px solid color-mix(in srgb, var(--accent) 55%, transparent);
    border-left: 4px solid var(--accent);
    background: color-mix(in srgb, var(--accent) 10%, transparent);
  }
  .strategy.replicas { --accent: var(--link); }
  .strategy-label { color: var(--accent); }
  .strategy-summary { color: var(--accent); font-size: 13px; font-weight: 600; flex: 1; min-width: 220px; }
  .seg { display: inline-flex; background: var(--surface); border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 10px; padding: 3px; }
  .seg button { border: none; background: transparent; padding: 6px 14px; border-radius: 7px; color: var(--muted); font-size: 13px; font-weight: 600; }
  .seg button:hover:not(:disabled):not(.active) { color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .seg button.active { background: var(--accent); color: #fff; box-shadow: 0 2px 10px color-mix(in srgb, var(--accent) 45%, transparent); }
  .seg button:disabled { cursor: default; }
  .seg button.active:disabled { opacity: 1; }
  .tip { position: relative; display: inline-flex; color: var(--muted); cursor: help; background: none; border: none; padding: 2px; text-align: left; font-weight: 400; }
  .tip { color: var(--accent); }
  .tip:hover, .tip:focus { color: var(--text); }
  .tip-box {
    display: none;
    position: absolute;
    top: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    width: min(380px, 80vw);
    padding: 12px 14px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    color: var(--text);
    font-size: 12.5px;
    line-height: 1.5;
    z-index: 20;
  }
  .tip:hover .tip-box, .tip:focus .tip-box { display: block; }
  .remove {
    color: var(--bad);
    border-color: color-mix(in srgb, var(--bad) 45%, transparent);
    background: color-mix(in srgb, var(--bad) 8%, transparent);
  }
  .remove:hover:not(:disabled) { background: color-mix(in srgb, var(--bad) 16%, transparent); }
</style>
