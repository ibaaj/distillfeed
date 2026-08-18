const CACHE = 'distillfeed-v241-final';
const SHELL = [
  '/static/layout-init.js?v=0.24.1-final',
  '/static/app.css?v=0.24.1-final',
  '/static/review-state.js?v=0.24.1-final',
  '/static/review.js?v=0.24.1-final',
  '/static/app.js?v=0.24.1-final',
  '/static/manifest.webmanifest',
  '/static/distillfeed-icon.svg?v=0.24.1-final',
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))));
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  const cacheable = event.request.destination === 'document' || url.pathname.startsWith('/static/');
  if (!cacheable) return;
  event.respondWith(fetch(event.request, { cache: 'no-cache' }).then(response => {
    if (response.ok) {
      const copy = response.clone(); caches.open(CACHE).then(cache => cache.put(event.request, copy));
    }
    return response;
  }).catch(() => caches.match(event.request).then(response => {
    if (response) return response;
    return event.request.destination === 'document' ? caches.match('/') : undefined;
  })));
});
