/// <reference types="svelte" />
/// <reference types="vite/client" />

declare const __APP_VERSION__: string;

interface Window {
  /** Set by public/splash.js when the launch screen's first frame is drawn. */
  __splashShownAt?: number;
}
