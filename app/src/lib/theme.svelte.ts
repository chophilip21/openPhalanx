// Light/dark theme: follows the system until the user picks one. The choice
// is kept in settings.json (`settings.theme`, so it survives updates like
// every other setting); localStorage holds a copy only to paint the first
// frame before the backend answers.
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

/** The saved choice from settings.json, applied when the snapshot arrives. */
export function adoptTheme(saved: Theme | null) {
  theme.chosen = saved !== null;
  try {
    if (saved) localStorage.setItem(KEY, saved);
    else localStorage.removeItem(KEY);
  } catch {
    // Storage unavailable: settings.json still has it.
  }
  apply(saved ?? systemTheme());
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

/** Settings → Appearance: a fixed theme, or follow the system ("system"). */
export function setThemeMode(mode: Theme | "system") {
  theme.chosen = mode !== "system";
  try {
    if (mode === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, mode);
  } catch {
    // Storage unavailable: the choice lasts for this session.
  }
  apply(mode === "system" ? systemTheme() : mode);
}
