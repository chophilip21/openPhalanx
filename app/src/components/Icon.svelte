<script lang="ts">
  // Minimal stroke icons (Lucide-style paths, 24x24 viewBox).
  const PATHS: Record<string, string> = {
    power: "M12 2v10 M18.4 6.6a9 9 0 1 1-12.77.04",
    home: "M3 10.5 12 3l9 7.5V21a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z",
    box: "M21 8 12 3 3 8v8l9 5 9-5z M3 8l9 5 9-5 M12 13v8",
    laptop: "M4 5h16v10H4z M2 19h20",
    terminal: "M4 17l6-5-6-5 M12 19h8",
    copy: "M9 9h11v11H9z M5 15H4V4h11v1",
    refresh: "M21 12a9 9 0 1 1-2.64-6.36L21 8 M21 3v5h-5",
    check: "M5 12l5 5L20 7",
    alert: "M12 9v4 M12 17h.01 M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    x: "M18 6 6 18 M6 6l12 12",
    download: "M12 3v12 M7 10l5 5 5-5 M5 21h14",
    trash: "M3 6h18 M8 6V4h8v2 M6 6l1 15h10l1-15",
    external: "M14 4h6v6 M20 4l-9 9 M18 14v6H4V6h6",
    folder: "M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z",
    shield: "M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z",
    cpu: "M6 6h12v12H6z M9 9h6v6H9z M9 2v4 M15 2v4 M9 18v4 M15 18v4 M2 9h4 M2 15h4 M18 9h4 M18 15h4",
    plus: "M12 5v14 M5 12h14",
    search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z M21 21l-5-5",
  };
  let { name, size = 18, stroke = 2 }: { name: string; size?: number; stroke?: number } = $props();
</script>

<svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width={stroke} stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
  {#each (PATHS[name] ?? "").split(" M") as d, i}
    <path d={i === 0 ? d : "M" + d} />
  {/each}
</svg>
