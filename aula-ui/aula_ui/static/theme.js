"use strict";
(() => {
  for (const type of ["gesturestart", "gesturechange"]) {
    document.addEventListener(type, event => event.preventDefault(), {passive: false});
  }
  let preference = "system";
  try {
    const saved = localStorage.getItem("aula-theme");
    if (["system", "light", "dark"].includes(saved)) preference = saved;
  } catch (error) {
    console.warn("Could not read appearance preference", error);
  }
  const system = matchMedia("(prefers-color-scheme: light)");
  function apply() {
    const theme = preference === "system" ? (system.matches ? "light" : "dark") : preference;
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "light" ? "#edf1f4" : "#0e1113");
  }
  window.AulaTheme = {
    get: () => preference,
    set(value) {
      if (!["system", "light", "dark"].includes(value)) throw new Error("Invalid appearance");
      preference = value;
      apply();
      try { localStorage.setItem("aula-theme", value); return true; }
      catch (error) { console.warn("Could not save appearance preference", error); return false; }
    },
  };
  system.addEventListener("change", apply);
  window.addEventListener("storage", event => {
    if (event.key !== "aula-theme") return;
    preference = ["light", "dark"].includes(event.newValue) ? event.newValue : "system";
    apply();
  });
  apply();
})();
