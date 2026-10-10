// Launch screen, loaded straight from index.html so it runs before the app's
// bundle. A file rather than an inline script: the app's CSP allows scripts
// from 'self' only.
(function () {
  // Same choice as lib/theme.svelte.ts, so the splash has the right colors.
  var theme = null;
  try { theme = localStorage.getItem("openphalanx.theme"); } catch (e) {}
  if (theme !== "light" && theme !== "dark") {
    theme = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.dataset.theme = theme;

  // The animation and the two-second minimum both count from the first frame
  // the webview draws, not from when the page was parsed: a window that takes
  // a while to paint would otherwise show the end of it, or nothing.
  requestAnimationFrame(function () {
    window.__splashShownAt = performance.now();
    var el = document.getElementById("splash");
    if (el) el.classList.add("go");
  });
})();
