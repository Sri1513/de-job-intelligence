/* Job Scout service worker — static-asset shell only.
   Static files (icons, css, js, images) are served cache-first so the
   installed app launches fast. Everything else (pages, /api/*) always
   goes to the network: dashboard data must never be stale. */
const CACHE = "jobscout-static-v1";
const STATIC_PREFIX = "/static/";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) =>
      cache.addAll([
        "/static/icon-192.png",
        "/static/icon-512.png",
        "/static/manifest.webmanifest",
      ])
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // third-party: untouched
  if (!url.pathname.startsWith(STATIC_PREFIX)) return; // pages + API: network only
  event.respondWith(
    caches.match(event.request).then(
      (hit) =>
        hit ||
        fetch(event.request).then((resp) => {
          const copy = resp.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          return resp;
        })
    )
  );
});
