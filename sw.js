// 松村式Stocksurfing - Service Worker (v3.3)
// 方針: ネットワーク優先（online時は常に最新を配信、offline時のみキャッシュにフォールバック）。
// これにより GitHub への更新が、奥様のPWAにも次回オンライン起動時に確実に反映される。
// 旧版(v3-0)は全アセットをキャッシュ優先で返していたため、index.html の更新が永久に届かなかった。

const CACHE_NAME = 'stocksurfing-v3-3';
const ASSETS = ['./', './index.html', './manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  if (e.request.method !== 'GET') return;
  // ネットワーク優先: 取得できたら最新を返しつつキャッシュも更新。失敗時のみキャッシュ。
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        // 正常レスポンスのみキャッシュ更新（オフライン用の保険）
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(e.request, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(e.request).then((c) => c || caches.match('./index.html')))
  );
});
