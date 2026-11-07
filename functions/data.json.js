/**
 * Cloudflare Pages Function: GET /data.json
 * Serves the latest Social Radar catalog (`state/data.json`) directly from the shared
 * R2 bucket (`env.MEDIA` -> `vc-radar-media`) so scheduled Worker runs update the live
 * site immediately without rebuilding or redeploying Pages.
 * Falls back cleanly to the bundled static `data.json` asset if R2 state is not seeded yet.
 */
const R2_DATA_KEY = "state/data.json";

export async function onRequestGet(context) {
  const media = context.env && context.env.MEDIA;
  if (media && typeof media.get === "function") {
    try {
      const obj = await media.get(R2_DATA_KEY);
      if (obj) {
        const headers = new Headers();
        headers.set("Content-Type", "application/json; charset=utf-8");
        headers.set("Cache-Control", "public, max-age=60, stale-while-revalidate=300");
        if (obj.httpEtag) {
          headers.set("ETag", obj.httpEtag);
        }
        headers.set("X-Content-Type-Options", "nosniff");
        headers.set("X-Radar-Data-Source", "r2");
        return new Response(context.request.method === "HEAD" ? null : obj.body, { status: 200, headers });
      }
    } catch (_) {
      // Fall through to static asset if R2 read encounters a transient error.
    }
  }

  if (context.env && context.env.ASSETS && typeof context.env.ASSETS.fetch === "function") {
    return context.env.ASSETS.fetch(context.request);
  }
  return new Response("Not Found", { status: 404 });
}

export const onRequestHead = onRequestGet;
