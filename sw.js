// 松村式Stocksurfing - Service Worker (v3.0)
// 最小限のキャッシュ戦略: networkファーストで、オフライン時のみキャッシュを返す

const CACHE_NAME = 'stocksurfing-v3-0';
const ASSETS = ['./', './index.html', './manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  // data.jsonは常にネットワーク優先で最新を取得
  if (e.request.url.includes('data.json')) {
    e.respondWith(
      fetch(e.request).catch(() => caches.match(e.request))
    );
    return;
  }
  // その他はキャッシュ→ネットワーク
  e.respondWith(
    caches.match(e.request).then((res) => res || fetch(e.request))
  );
});
