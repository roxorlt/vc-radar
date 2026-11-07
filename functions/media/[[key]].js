/**
 * Cloudflare Pages Function: GET /media/<key>
 * Streams content-addressed product icons, store screenshots, and article images
 * from the bound R2 bucket (`env.MEDIA` -> `vc-radar-media`) with immutable edge caching.
 */
const SAFE_KEY = /^[a-f0-9]{64}\.(?:jpg|jpeg|png|webp|gif|avif)$/i;

export async function onRequestGet(context) {
  const raw = context.params.key;
  const key = Array.isArray(raw) ? raw.join("/") : String(raw || "");
  if (!SAFE_KEY.test(key)) {
    return new Response("Not Found", { status: 404 });
  }
  if (!context.env || !context.env.MEDIA) {
    return new Response("Media bucket unavailable", { status: 503 });
  }

  const cache = caches.default;
  const cacheKey = new Request(context.request.url, { method: "GET" });
  const cached = await cache.match(cacheKey);
  if (cached) {
    return cached;
  }

  const obj = await context.env.MEDIA.get(key);
  if (!obj) {
    return new Response("Not Found", { status: 404 });
  }

  const headers = new Headers();
  obj.writeHttpMetadata(headers);
  if (!headers.get("Content-Type")) {
    const ext = key.split(".").pop().toLowerCase();
    const mime = {
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      png: "image/png",
      webp: "image/webp",
      gif: "image/gif",
      avif: "image/avif",
    }[ext] || "application/octet-stream";
    headers.set("Content-Type", mime);
  }
  headers.set("Cache-Control", "public, max-age=31536000, immutable");
  headers.set("ETag", obj.httpEtag);
  headers.set("X-Content-Type-Options", "nosniff");

  const response = new Response(obj.body, { status: 200, headers });
  context.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}
