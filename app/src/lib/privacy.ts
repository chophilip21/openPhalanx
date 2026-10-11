// Privacy: hide the first digits of IP addresses wherever the app shows one
// (Settings → "Hide IP addresses"; `settings.hide_ips`).
//
// It works on what is on screen, not on the data: a MutationObserver rewrites
// text and tooltips under the app's root as they appear or change. Server
// names default to their address, and addresses also arrive inside messages
// built in Rust, log lines, chart labels and tooltips, so masking each place
// would miss some. What is sent or copied (copy buttons read the data) stays
// real; text selected by hand is masked, which is the point while sharing a
// screen.

// IPv4 (192.168.1.74), optionally with a port, and the dashed form some
// hostnames use (100-125-91-43). IPv6 is left alone: its colon-separated hex
// looks like the certificate fingerprints shown next to it.
const IPV4 = /(?<![\w.-])(\d{1,3})([.-])(\d{1,3})\2(\d{1,3})\2(\d{1,3})(?![\w-]|\.\d)/g;
const HIDDEN_DIGITS = 5;

/** `192.168.1.74` → `xxx.xx8.1.74`: the first five digits become `x`. */
export function maskIps(text: string): string {
  if (!text || !/\d[.-]\d/.test(text)) return text;
  return text.replace(IPV4, (match, a: string, sep: string, b: string, c: string, d: string) => {
    const parts = [a, b, c, d].map(Number);
    if (parts.some((p) => p > 255)) return match; // a version or a date, not an address
    let left = HIDDEN_DIGITS;
    return match.replace(/\d/g, (digit) => (left-- > 0 ? "x" : digit));
  });
}

type Seen = { real: string; shown: string };
const texts = new WeakMap<Text, Seen>();
const titles = new WeakMap<Element, Seen>();
const SKIP = new Set(["SCRIPT", "STYLE", "TEXTAREA", "INPUT"]);

function maskText(node: Text) {
  const parent = node.parentElement;
  if (!parent || SKIP.has(parent.tagName) || parent.closest("[data-real]")) return;
  const seen = texts.get(node);
  if (seen && node.data === seen.shown) return; // our own change
  const real = node.data;
  const shown = maskIps(real);
  if (shown === real) {
    texts.delete(node);
    return;
  }
  texts.set(node, { real, shown });
  node.data = shown;
}

function maskTitle(el: Element) {
  const title = el.getAttribute("title");
  if (title === null) return;
  const seen = titles.get(el);
  if (seen && title === seen.shown) return;
  const shown = maskIps(title);
  if (shown === title) {
    titles.delete(el);
    return;
  }
  titles.set(el, { real: title, shown });
  el.setAttribute("title", shown);
}

function maskTree(root: Node) {
  if (root.nodeType === Node.TEXT_NODE) return maskText(root as Text);
  if (root.nodeType !== Node.ELEMENT_NODE) return;
  const el = root as Element;
  if (SKIP.has(el.tagName)) return;
  maskTitle(el);
  const walk = document.createTreeWalker(el, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) maskText(n as Text);
    else maskTitle(n as Element);
  }
}

function unmaskTree(root: Element) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
  for (let n: Node | null = root; n; n = walk.nextNode()) {
    if (n.nodeType === Node.TEXT_NODE) {
      const t = n as Text;
      const seen = texts.get(t);
      if (seen && t.data === seen.shown) t.data = seen.real;
      texts.delete(t);
    } else {
      const el = n as Element;
      const seen = titles.get(el);
      if (seen && el.getAttribute("title") === seen.shown) el.setAttribute("title", seen.real);
      titles.delete(el);
    }
  }
}

let observer: MutationObserver | null = null;

/** Turns masking on or off for everything under `root`. */
export function setIpMasking(on: boolean, root: Element = document.body) {
  if (on && !observer) {
    observer = new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === "characterData") maskText(r.target as Text);
        else if (r.type === "attributes") maskTitle(r.target as Element);
        else r.addedNodes.forEach(maskTree);
      }
    });
    observer.observe(root, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["title"] });
    maskTree(root);
  } else if (!on && observer) {
    observer.disconnect();
    observer = null;
    unmaskTree(root);
  }
}
