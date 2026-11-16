/* StudyMate service worker — makes the installed app usable with no network.
 *
 * Strategy
 *   navigation      network first, cached shell as the fallback
 *   /static/*       cache first (URLs are fingerprinted, so they never go stale)
 *   /api/ GET       network first, cache fallback — this is what keeps documents
 *                   readable offline: /full, /render/N.png, /words/N, /text
 *   everything else untouched (uploads, tutor streams, deletes must hit the server)
 */
const VERSION = 'sm-v2';
const SHELL = `${VERSION}-shell`;
const RUNTIME = `${VERSION}-runtime`;
const SHELL_FILES = ['/', '/manifest.webmanifest',
  '/static/icons/icon-192.png', '/static/icons/icon-512.png'];
const MAX_RUNTIME = 500;                     // keep the cache from growing forever

// The HTML references app.js/app.css with a ?v=<fingerprint> query. Those URLs are
// immutable, so they must be cached *exactly* as the page asks for them — caching
// '/static/app.js' alone would leave an offline boot with no JavaScript.
async function precacheAssets(cache) {
  try {
    const res = await fetch('/', { cache: 'reload' });
    await cache.put('/', res.clone());
    const html = await res.text();
    const urls = [...html.matchAll(/(?:src|href)="(\/static\/[^"]+)"/g)].map((m) => m[1]);
    await Promise.allSettled(urls.map((u) => cache.add(new Request(u, { cache: 'reload' }))));
  } catch (err) { /* offline during install — the runtime cache will fill in later */ }
}

self.addEventListener('install', (e) => {
  e.waitUntil((async () => {
    const cache = await caches.open(SHELL);
    await Promise.allSettled(SHELL_FILES.map((u) => cache.add(new Request(u, { cache: 'reload' }))));
    await precacheAssets(cache);
    self.skipWaiting();
  })());
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)));
    await self.clients.claim();
  })());
});

async function trim(cacheName) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  if (keys.length > MAX_RUNTIME) {
    await Promise.all(keys.slice(0, keys.length - MAX_RUNTIME).map((k) => cache.delete(k)));
  }
}

async function networkFirst(request, cacheName, fallback) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(request);
    if (res && res.ok) {
      cache.put(request, res.clone());
      trim(cacheName);
    }
    return res;
  } catch (err) {
    const hit = await cache.match(request);
    if (hit) return hit;
    if (fallback) {
      const shell = await caches.match(fallback);
      if (shell) return shell;
    }
    throw err;
  }
}

async function cacheFirst(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request);
  if (hit) return hit;                                  // fingerprinted URL: immutable
  try {
    const res = await fetch(request);
    if (res && res.ok) { cache.put(request, res.clone()); trim(cacheName); }
    return res;
  } catch (err) {
    // offline and this exact version isn't cached — an older build is better than a blank page
    const anyVersion = await caches.match(request, { ignoreSearch: true });
    if (anyVersion) return anyVersion;
    throw err;
  }
}

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // api calls that make a document readable offline
  const cacheable = /^\/api\/(docs(\/[^/]+)?(\/full|\/render\/\d+\.png|\/words\/\d+|\/page\/\d+)?|workspace(\/|$).*|version)$/;
  if (url.pathname.startsWith('/api/')) {
    if (cacheable.test(url.pathname)) {
      event.respondWith(networkFirst(request, RUNTIME));
    }
    return;                                   // everything else: straight to the network
  }

  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request, RUNTIME, '/'));
    return;
  }

  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest') {
    event.respondWith(cacheFirst(request, SHELL));
  }
});
