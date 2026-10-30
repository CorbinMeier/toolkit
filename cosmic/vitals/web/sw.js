// vitals service worker: caches the app shell only, never the data (/vitals, /vitals/stream).
// Network first, so a new vitals build shows at once; the cache answers when the laptop is unreachable.
const CACHE = 'vitals-shell-v1';
const SHELL = ['/', '/app.js', '/app.css', '/uplot.js', '/uplot.css', '/icon.svg'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {})))).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/vitals')) return;
  if (!SHELL.includes(url.pathname)) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(url.pathname, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(url.pathname).then((hit) => hit || Response.error()))
  );
});
