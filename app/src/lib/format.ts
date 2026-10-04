const GIB = 1024 ** 3;

export const gib = (bytes: number | null | undefined, digits = 1) =>
  bytes == null ? "–" : `${(bytes / GIB).toFixed(digits)} GiB`;

export const gb = (bytes: number) => `${(bytes / 1e9).toFixed(1)} GB`;

export const tokens = (n: number) => (n >= 1024 ? `${Math.round(n / 1024)}k` : `${n}`);

export const rate = (bytesPerSec: number) =>
  bytesPerSec >= 1e6 ? `${(bytesPerSec / 1e6).toFixed(1)} MB/s` : `${(bytesPerSec / 1e3).toFixed(0)} kB/s`;

export const pct = (ratio: number | null | undefined) => (ratio == null ? "–" : `${Math.round(ratio * 100)}%`);

export function ago(unixSeconds: number | null | undefined): string {
  if (!unixSeconds) return "never";
  const s = Math.max(0, Date.now() / 1000 - unixSeconds);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

export function countdown(unixSeconds: number | null | undefined, now: number): string {
  if (!unixSeconds) return "";
  const s = Math.max(0, Math.round(unixSeconds - now / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
