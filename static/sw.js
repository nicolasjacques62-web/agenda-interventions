/**
 * HPS3D — Service Worker
 * Stratégie : cache-first pour les assets, network-first pour les pages.
 * Permet la consultation hors-ligne des pages visitées.
 */

const CACHE_NAME = 'hps3d-v1';

// Pages et ressources à mettre en cache dès l'installation
const PRECACHE_URLS = ['/offline'];

// ── Installation ──────────────────────────────────────────────
self.addEventListener('install', event => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(PRECACHE_URLS).catch(() => {}))
  );
});

// ── Activation : purge les anciens caches ─────────────────────
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Interception des requêtes ─────────────────────────────────
self.addEventListener('fetch', event => {
  const req = event.request;
  const url = new URL(req.url);

  // On ne gère que les GET
  if (req.method !== 'GET') return;

  // Ressources CDN externes (Bootstrap, icônes) → cache-first
  if (url.hostname !== self.location.hostname) {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(resp => {
          if (resp && resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(req, clone));
          }
          return resp;
        }).catch(() => cached || new Response('', { status: 503 }));
      })
    );
    return;
  }

  // Fichiers statiques du projet → cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then(cached => {
        if (cached) return cached;
        return fetch(req).then(resp => {
          if (resp && resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(req, clone));
          }
          return resp;
        }).catch(() => new Response('', { status: 503 }));
      })
    );
    return;
  }

  // Pages de l'application → network-first avec fallback cache
  event.respondWith(
    fetch(req)
      .then(resp => {
        // Mise en cache des pages HTML visitées
        if (resp.ok && (resp.headers.get('content-type') || '').includes('text/html')) {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(req, clone));
        }
        return resp;
      })
      .catch(() =>
        caches.match(req).then(cached =>
          cached || caches.match('/offline')
        )
      )
  );
});
