// App-wide reactive state fed by the Rust monitor's "snapshot" events.
import { api, events, type DownloadView, type Snapshot } from "./api";

export const app = $state<{ snapshot: Snapshot | null; downloads: Record<string, DownloadView>; logs: string[] }>({
  snapshot: null,
  downloads: {},
  logs: [],
});

const MAX_LOG_LINES = 3000;

export async function connect() {
  app.snapshot = await api.snapshot();
  for (const d of app.snapshot.downloads) app.downloads[d.key] = d;
  await events.snapshot((s) => {
    app.snapshot = s;
  });
  await events.download((d) => {
    app.downloads[d.key] = d;
  });
  await events.log((lines) => {
    app.logs.push(...lines);
    if (app.logs.length > MAX_LOG_LINES) app.logs.splice(0, app.logs.length - MAX_LOG_LINES);
  });
}
