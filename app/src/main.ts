import { mount } from "svelte";
import "./app.css";
import App from "./App.svelte";
import { initTheme } from "./lib/theme.svelte";

// Before mounting, so the first frame already has the right theme.
initTheme();

export default mount(App, { target: document.getElementById("app")! });
