const CACHE_NAME = 'pocket-log-analyzer-v1';
const ASSETS = [
  '/flight/',
  '/flight/static/style.css',
  '/flight/static/manifest.json',
  '/flight/static/vendor/leaflet.css',
  '/flight/static/vendor/leaflet.js',
  '/flight/static/vendor/chart.js'
];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(ASSETS))
  );
});

self.addEventListener('fetch', (e) => {
  e.respondWith(
    fetch(e.request).catch(() => caches.match(e.request))
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))
    ))
  );
});
