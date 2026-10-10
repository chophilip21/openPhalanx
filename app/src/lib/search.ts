// Fuzzy text search for lists (the Models page).

/** Lowercase letters and digits only, so "qwen 2.5", "Qwen2.5" and "qwen25" compare equal. */
const compact = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, "");

/** Typos allowed for a search word of this length. */
const allowance = (len: number) => (len < 4 ? 0 : len < 8 ? 1 : 2);

/** Fewest edits (insert, delete, replace, swap of neighbours) that turn `word` into some part of `text`. */
function distance(word: string, text: string): number {
  const m = word.length;
  // One column per text position; row i is the first i letters of the word.
  let before: number[] = [];
  let prev = Array.from({ length: m + 1 }, (_, i) => i);
  let best = prev[m];
  for (let j = 1; j <= text.length; j++) {
    const cur = [0];
    for (let i = 1; i <= m; i++) {
      let d = Math.min(prev[i - 1] + (word[i - 1] === text[j - 1] ? 0 : 1), prev[i] + 1, cur[i - 1] + 1);
      if (i > 1 && j > 1 && word[i - 1] === text[j - 2] && word[i - 2] === text[j - 1]) d = Math.min(d, before[i - 2] + 1);
      cur.push(d);
    }
    best = Math.min(best, cur[m]);
    [before, prev] = [prev, cur];
  }
  return best;
}

/**
 * The items matching every word of `query`, in their original order. Words
 * match anywhere in an item's text, ignoring case and punctuation. Typos are
 * only forgiven when nothing matches as typed, so a correct query isn't
 * padded with near misses.
 */
export function fuzzyFilter<T>(items: T[], query: string, text: (item: T) => string): T[] {
  const words = query.split(/\s+/).map(compact).filter(Boolean);
  if (!words.length) return items;
  const hay = items.map((item) => compact(text(item)));
  const exact = items.filter((_, n) => words.every((w) => hay[n].includes(w)));
  if (exact.length) return exact;
  return items.filter((_, n) => words.every((w) => hay[n].includes(w) || distance(w, hay[n]) <= allowance(w.length)));
}
