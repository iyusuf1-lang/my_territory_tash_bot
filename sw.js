// ═══════════════════════════════════════════════
// Territory Tashkent — Service Worker v1.0
// ═══════════════════════════════════════════════

const CACHE_NAME = "territory-v1.0";
const TILE_CACHE = "territory-tiles-v1";
const API_CACHE  = "territory-api-v1";

// Asosiy fayllar (doim cache qilinadi)
const STATIC_FILES = [
  "/",
  "/index.html",
  "/trek.html",
  "/onboarding.html",
  "/offline.html",
  "/manifest.json",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

// ── Install ───────────────────────────────────
self.addEventListener("install", (event) => {
  console.log("[SW] Installing...");
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      console.log("[SW] Caching static files");
      // Har bir faylni alohida cache qilish (xatolik bo'lsa o'tkazib yuborish)
      return Promise.allSettled(
        STATIC_FILES.map((url) =>
          cache.add(url).catch(() => console.warn("[SW] Skip:", url))
        )
      );
    }).then(() => self.skipWaiting())
  );
});

// ── Activate ──────────────────────────────────
self.addEventListener("activate", (event) => {
  console.log("[SW] Activating...");
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== TILE_CACHE && k !== API_CACHE)
          .map((k) => {
            console.log("[SW] Deleting old cache:", k);
            return caches.delete(k);
          })
      )
    ).then(() => self.clients.claim())
  );
});

// ── Fetch Strategy ────────────────────────────
self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // 1. Xarita tile-lari (OpenStreetMap) — Cache First
  if (url.hostname.includes("tile.openstreetmap.org") ||
      url.hostname.includes("tiles.stadiamaps.com")) {
    event.respondWith(cacheTiles(event.request));
    return;
  }

  // 2. API so'rovlar — Network First (5s timeout)
  if (url.pathname.startsWith("/api/")) {
    event.respondWith(networkFirst(event.request));
    return;
  }

  // 3. Statik fayllar — Cache First, Network Fallback
  event.respondWith(cacheFirst(event.request));
});

// ── Cache Strategies ──────────────────────────

// Cache First: Avval cache, keyin network
async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    // Offline fallback
    const offline = await caches.match("/offline.html");
    return offline || new Response("Offline", { status: 503 });
  }
}

// Network First: Avval network, keyin cache
async function networkFirst(request) {
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) =>
        setTimeout(() => reject(new Error("timeout")), 5000)
      ),
    ]);

    if (response.ok) {
      const cache = await caches.open(API_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    return (
      cached ||
      new Response(JSON.stringify({ ok: false, error: "Offline" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      })
    );
  }
}

// Tile cache: 7 kun saqlash
async function cacheTiles(request) {
  const cache = await caches.open(TILE_CACHE);
  const cached = await cache.match(request);
  if (cached) return cached;

  try {
    const response = await fetch(request);
    if (response.ok) {
      // Tile uchun max-age headerini o'rnatish
      const headers = new Headers(response.headers);
      headers.set("Cache-Control", "max-age=604800"); // 7 kun
      const cachedResponse = new Response(await response.blob(), {
        status: response.status,
        headers,
      });
      cache.put(request, cachedResponse.clone());
      return cachedResponse;
    }
    return response;
  } catch {
    return new Response("", { status: 503 });
  }
}

// ── Background Sync ───────────────────────────
self.addEventListener("sync", (event) => {
  if (event.tag === "sync-treks") {
    event.waitUntil(syncPendingTreks());
  }
});

async function syncPendingTreks() {
  // IndexedDB dan kutayotgan treklarni yuborish
  // (trek.html da saqlangan bo'ladi)
  console.log("[SW] Syncing pending treks...");
  const clients = await self.clients.matchAll();
  clients.forEach((client) => client.postMessage({ type: "SYNC_COMPLETE" }));
}

// ── Push Notifications ────────────────────────
self.addEventListener("push", (event) => {
  const data = event.data ? event.data.json() : {};
  const options = {
    body: data.body || "Yangi voqea!",
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-72.png",
    vibrate: [200, 100, 200],
    data: { url: data.url || "/" },
    actions: [
      { action: "open", title: "Ko'rish" },
      { action: "close", title: "Yopish" },
    ],
  };
  event.waitUntil(
    self.registration.showNotification(
      data.title || "Territory Tashkent",
      options
    )
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  if (event.action === "open" || !event.action) {
    const url = event.notification.data?.url || "/";
    event.waitUntil(clients.openWindow(url));
  }
});

// ── Message Handler ───────────────────────────
self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
  if (event.data?.type === "CACHE_URLS") {
    const urls = event.data.urls || [];
    caches.open(CACHE_NAME).then((cache) => cache.addAll(urls));
  }
});

console.log("[SW] Service Worker loaded ✅");
