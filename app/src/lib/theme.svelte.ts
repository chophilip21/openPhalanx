// Light/dark theme: follows the system until the user picks one, then
// remembers the choice (per machine, in localStorage).
export type Theme = "light" | "dark";

const KEY = "openphalanx.theme";

function systemTheme(): Theme {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function saved(): Theme | null {
  try {
    const v = localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

export const theme = $state<{ current: Theme; chosen: boolean }>({ current: "dark", chosen: false });

function apply(t: Theme) {
  theme.current = t;
  document.documentElement.dataset.theme = t;
}

export function initTheme() {
  const s = saved();
  theme.chosen = s !== null;
  apply(s ?? systemTheme());
  window.matchMedia?.("(prefers-color-scheme: light)").addEventListener("change", () => {
    if (!theme.chosen) apply(systemTheme());
  });
}

export function toggleTheme() {
  const next: Theme = theme.current === "dark" ? "light" : "dark";
  theme.chosen = true;
  try {
    localStorage.setItem(KEY, next);
  } catch {
    // Storage unavailable: the choice lasts for this session.
  }
  apply(next);
}
