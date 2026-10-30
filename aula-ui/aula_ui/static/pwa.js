"use strict";
(() => {
  const base = document.querySelector("base")?.href || location.href;
  // Version the server rendered this page with; later compared with the deployed one.
  let running = document.querySelector('meta[name="aula-version"]')?.content || "";
  let offered = "";
  let registration = null;
  window.AulaVersion = {get current() { return running; }};

  const showUpdate = latest => {
    if (!latest || latest === running || latest === offered) return;
    offered = latest;
    let toast = document.querySelector("#update-toast");
    if (!toast) {
      toast = document.createElement("aside");
      toast.id = "update-toast";
      toast.className = "update-toast";
      toast.setAttribute("role", "status");
      const text = document.createElement("span");
      text.className = "update-text";
      const reload = document.createElement("button");
      reload.type = "button";
      reload.className = "update-reload";
      reload.textContent = "Reload";
      reload.addEventListener("click", async () => {
        reload.disabled = true;
        reload.textContent = "Updating…";
        // Install the new service worker first so the reload gets fresh shell files, not cached ones.
        try {
          if (registration) {
            const switched = new Promise(resolve =>
              navigator.serviceWorker.addEventListener("controllerchange", resolve, {once: true}));
            await registration.update();
            if (registration.installing || registration.waiting) {
              await Promise.race([switched, new Promise(resolve => setTimeout(resolve, 4000))]);
            }
          }
        } catch (error) { /* reload anyway */ }
        location.reload();
      });
      const dismiss = document.createElement("button");
      dismiss.type = "button";
      dismiss.className = "update-dismiss";
      dismiss.setAttribute("aria-label", "Dismiss update notice");
      dismiss.textContent = "×";
      dismiss.addEventListener("click", () => { toast.hidden = true; });
      toast.append(text, reload, dismiss);
      document.body.append(toast);
    }
    toast.querySelector(".update-text").textContent = `A new version (${latest}) is available.`;
    toast.hidden = false;
  };

  let checking = false;
  const check = async () => {
    if (checking || document.visibilityState === "hidden" || !navigator.onLine) return;
    checking = true;
    try {
      const response = await fetch(new URL("api/version", base).href, {cache: "no-store"});
      if (response.ok) {
        const latest = (await response.json()).version || "";
        if (!running) running = latest;
        else showUpdate(latest);
      }
    } catch (error) { /* offline or restarting; try later */ }
    checking = false;
    registration?.update().catch(() => {});
  };
  window.AulaVersion.check = check;

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register(new URL("sw.js", base).href).then(reg => {
        registration = reg;
        reg.addEventListener("updatefound", () => {
          const worker = reg.installing;
          worker?.addEventListener("statechange", () => { if (worker.state === "activated") check(); });
        });
      }).catch(error => console.warn("Service worker unavailable", error));
    });
  }
  check();
  setInterval(check, 5 * 60 * 1000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") check(); });
  window.addEventListener("online", check);
  const standalone = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  const banner = document.querySelector("#install-banner");
  if (!banner || standalone()) return;
  const text = banner.querySelector(".install-text");
  const button = banner.querySelector(".install-button");
  const dismiss = banner.querySelector(".install-dismiss");
  let deferred = null;
  let dismissed = false;
  try { dismissed = localStorage.getItem("aula-install-dismissed") === "1"; } catch (error) { /* ignore */ }
  if (dismissed) return;
  window.addEventListener("beforeinstallprompt", event => {
    event.preventDefault();
    deferred = event;
    text.textContent = "Install Aula Feed for quick access from your home screen.";
    button.hidden = false;
    banner.hidden = false;
  });
  window.addEventListener("appinstalled", () => { banner.hidden = true; });
  button.addEventListener("click", async () => {
    if (!deferred) return;
    deferred.prompt();
    await deferred.userChoice.catch(() => {});
    deferred = null;
    banner.hidden = true;
  });
  dismiss.addEventListener("click", () => {
    banner.hidden = true;
    try { localStorage.setItem("aula-install-dismissed", "1"); } catch (error) { /* ignore */ }
  });
  const ua = navigator.userAgent;
  const ios = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  if (ios) {
    text.textContent = "Install: tap Share, then “Add to Home Screen”.";
    button.hidden = true;
    banner.hidden = false;
  }
})();
