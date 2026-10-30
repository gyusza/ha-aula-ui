"use strict";
// Bump VERSION with each release so clients drop the old offline cache.
const VERSION = "v1.9.51";
const CACHE = `aula-shell-${VERSION}`;
const SHELL = ["style.css", "app.js", "account.js", "notifications.js", "ai-settings.js", "siri.js", "sync-settings.js", "pwa.js", "theme.js",
  "icon.svg", "offline.html", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png"];
const scopeUrl = new URL(self.registration.scope);
const scopePath = scopeUrl.pathname;

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE)
    .then(cache => Promise.all(SHELL.map(path => cache.add(new Request(path, {cache: "reload"})).catch(() => {}))))
    .then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(key => key.startsWith("aula-shell-") && key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== location.origin) return;
  const rel = url.pathname.startsWith(scopePath) ? url.pathname.slice(scopePath.length) : url.pathname;
  if (rel.startsWith("api/") || rel === "calendar.ics" || rel.startsWith("calendar.ics")) return;
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(async () => (await caches.match(new URL("offline.html", scopeUrl))) || Response.error()));
    return;
  }
  if (!SHELL.includes(rel)) return;
  // Network first: the page HTML is always fetched fresh, so a cache-first app.js could pair a new
  // index.html with an old script after an add-on update. The cache is only the offline fallback.
  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const response = await fetch(request);
      if (response.ok) event.waitUntil(cache.put(request, response.clone()).catch(() => {}));
      return response;
    } catch (error) {
      const hit = await cache.match(request, {ignoreSearch: true});
      if (hit) return hit;
      throw error;
    }
  })());
});
self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (error) { data = {message: event.data?.text() || ""}; }
  const tag = typeof data.tag === "string" ? data.tag : "aula";
  event.waitUntil(self.registration.showNotification(data.title || "Aula", {
    body: data.message || "",
    tag,
    renotify: tag !== "aula",
    icon: new URL("icons/icon-192.png", scopeUrl).href,
    badge: new URL("icons/icon-192.png", scopeUrl).href,
    data: {url: data.url || "./"},
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "./", scopeUrl).href;
  event.waitUntil(self.clients.matchAll({type: "window", includeUncontrolled: true}).then(async clients => {
    const same = clients.find(client => new URL(client.url).origin === scopeUrl.origin);
    if (same) {
      try { await same.navigate(target); } catch (error) { /* fall through to focus */ }
      return same.focus();
    }
    return self.clients.openWindow(target);
  }));
});
