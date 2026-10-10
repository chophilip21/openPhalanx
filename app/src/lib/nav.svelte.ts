// Navigation shared between pages, plus one-off hints shown after a jump
// (e.g. "press start" after choosing a model).
export const nav = $state<{ page: string; startHint: string | null }>({ page: "home", startHint: null });

/** The Server page's "Force load" tick. Kept here so it survives a visit to
 *  another page. `restore`: the context window from before it was ticked. */
export const forceLoad = $state<{ on: boolean; restore: number | null }>({ on: false, restore: null });

export function goto(page: string) {
  nav.page = page;
}

/** After a model is chosen: go to the Server page and point at the power button. */
export function suggestStart(modelLabel: string) {
  nav.startHint = modelLabel;
  nav.page = "home";
}
