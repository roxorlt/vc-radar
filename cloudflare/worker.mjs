// ============================================================================
// Social Radar 定时巡检与钉钉回执 Cloudflare Worker 入口
// ----------------------------------------------------------------------------
// - Cron Trigger：`30 0,12 * * *`（北京时间每天 08:30 与 20:30 自动触发）
// - GET  /healthz      只读健康检查与最近一次巡检回执脱敏元数据
// - POST /v1/cron/run  受 Bearer 令牌保护的手动触发入口（常量时间比对，未配令牌 fail-closed 503）
// ============================================================================

import { createHash, timingSafeEqual } from "node:crypto";
import { formatBeijingSlot, sendDingtalkReceipt } from "./dingtalk.mjs";
import { R2_DATA_KEY, R2_LAST_RUN_KEY, beijingStamp, runSocialRadarPatrol } from "./pipeline.mjs";

const JSON_HEADERS = Object.freeze({
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
});

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body, null, 2), { status, headers: JSON_HEADERS });
}

function redactError(err) {
  const msg = String(err?.message || err || "未知巡检错误");
  return msg
    .replace(/access_token=[A-Za-z0-9._-]+/gi, "access_token=***")
    .replace(/sk-[A-Za-z0-9]{12,}/g, "sk-***")
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "Bearer ***")
    .slice(0, 240);
}

function verifyBearerToken(authHeader, expectedToken) {
  const expected = typeof expectedToken === "string" ? expectedToken.trim() : "";
  if (!expected) {
    return { ok: false, status: 503, code: "UNAVAILABLE", message: "未配置触发令牌" };
  }
  const raw = typeof authHeader === "string" ? authHeader.trim() : "";
  if (!/^Bearer\s+\S+$/i.test(raw)) {
    return { ok: false, status: 403, code: "FORBIDDEN", message: "缺少有效的 Bearer 令牌" };
  }
  const provided = raw.replace(/^Bearer\s+/i, "").trim();
  const a = createHash("sha256").update(provided).digest();
  const b = createHash("sha256").update(expected).digest();
  if (!timingSafeEqual(a, b)) {
    return { ok: false, status: 403, code: "FORBIDDEN", message: "触发令牌校验未通过" };
  }
  return { ok: true };
}

async function saveLastRun(mediaBucket, summary) {
  if (!mediaBucket || typeof mediaBucket.put !== "function") return;
  try {
    await mediaBucket.put(R2_LAST_RUN_KEY, JSON.stringify(summary, null, 2), {
      httpMetadata: { contentType: "application/json; charset=utf-8" },
    });
  } catch {}
}

async function readR2ObjectJson(mediaBucket, key) {
  if (!mediaBucket || typeof mediaBucket.get !== "function") return null;
  try {
    const obj = await mediaBucket.get(key);
    if (!obj) return null;
    return JSON.parse(await obj.text());
  } catch {
    return null;
  }
}

async function readLastRun(mediaBucket) {
  return readR2ObjectJson(mediaBucket, R2_LAST_RUN_KEY);
}

export async function replayLastPatrolReceipt(
  env,
  { channel = "prod", nowMs = Date.now(), fetchImpl = globalThis.fetch } = {}
) {
  const targetChannel = channel === "test" ? "test" : "prod";
  const lastRun = await readLastRun(env?.MEDIA);
  if (!lastRun) {
    return { ok: false, status: 404, error: "NO_LAST_RUN", message: "尚无历史巡检回执记录" };
  }
  const dataJson = await readR2ObjectJson(env?.MEDIA, R2_DATA_KEY);
  const products = Array.isArray(dataJson?.products) ? dataJson.products : [];
  const byId = new Map(products.map((p) => [p.id, p]));
  const admittedIds = Array.isArray(lastRun.admittedNewIds) ? lastRun.admittedNewIds : [];
  const admittedNames = Array.isArray(lastRun.admittedNewNames) ? lastRun.admittedNewNames : [];
  const admittedNew = admittedIds.map(
    (id, idx) =>
      byId.get(id) || {
        id,
        name: admittedNames[idx] || id,
        channel: "chart",
        hook: "已收录至线上社交产品雷达库",
      }
  );
  const report = {
    ...lastRun,
    admittedNew,
  };
  const dingtalk = await sendDingtalkReceipt(report, env, {
    fetchImpl,
    timestamp: nowMs,
    channel: targetChannel,
  });
  return {
    ok: Boolean(dingtalk.ok),
    status: dingtalk.ok ? 200 : 502,
    replayed: true,
    channel: targetChannel,
    slotLabel: lastRun.slotLabel || "",
    admittedNewCount: admittedNew.length,
    admittedNewNames: admittedNew.map((p) => p.name),
    dingtalk,
  };
}

export async function executePatrolAndNotify(
  env,
  {
    trigger = "cron",
    nowMs = Date.now(),
    notify = true,
    channel = "prod",
    regions,
    fetchImpl = globalThis.fetch,
  } = {}
) {
  const targetChannel = channel === "test" ? "test" : "prod";
  const slotLabel = formatBeijingSlot(nowMs, trigger);
  const { isoBj } = beijingStamp(nowMs);
  let report;
  try {
    report = await runSocialRadarPatrol(env, {
      fetchImpl,
      nowMs,
      trigger,
      ...(Array.isArray(regions) && regions.length ? { regions } : {}),
    });
    report.slotLabel = slotLabel;
  } catch (err) {
    const fatalError = redactError(err);
    let dingtalk = { ok: false, skipped: true, channel: targetChannel };
    if (notify) {
      try {
        dingtalk = await sendDingtalkReceipt(
          { trigger, slotLabel, fatalError },
          env,
          { fetchImpl, timestamp: nowMs, channel: targetChannel }
        );
      } catch (pushErr) {
        dingtalk = { ok: false, channel: targetChannel, error: redactError(pushErr) };
      }
    }
    const summary = {
      ok: false,
      trigger,
      runAt: isoBj,
      slotLabel,
      fatalError,
      dingtalk: {
        ok: Boolean(dingtalk.ok),
        channel: dingtalk.channel || targetChannel,
        skipped: Boolean(dingtalk.skipped),
        errcode: dingtalk.errcode ?? null,
      },
    };
    await saveLastRun(env?.MEDIA, summary);
    console.log(JSON.stringify({ event: "patrol_failed", ...summary }));
    return summary;
  }

  let dingtalk = { ok: false, skipped: true, channel: targetChannel };
  if (notify) {
    try {
      dingtalk = await sendDingtalkReceipt(report, env, {
        fetchImpl,
        timestamp: nowMs,
        channel: targetChannel,
      });
    } catch (pushErr) {
      dingtalk = { ok: false, channel: targetChannel, error: redactError(pushErr) };
    }
  }

  const summary = {
    ok: true,
    trigger,
    runAt: isoBj,
    slotLabel,
    discovered: report.discovered,
    unseenCount: report.unseenCount,
    processed: report.processed,
    admittedNewCount: report.admittedNew.length,
    admittedNewIds: report.admittedNew.map((p) => p.id),
    admittedNewNames: report.admittedNew.map((p) => p.name),
    rejectedNew: report.rejectedNew,
    failedCount: report.failedCount,
    metadataRefreshed: report.metadataRefreshed,
    totalAdmitted: report.totalAdmitted,
    subrequestsUsed: report.subrequestsUsed,
    sourceCounts: report.sourceCounts,
    dingtalk: {
      ok: Boolean(dingtalk.ok),
      channel: dingtalk.channel || targetChannel,
      skipped: Boolean(dingtalk.skipped),
      httpStatus: dingtalk.httpStatus ?? null,
      errcode: dingtalk.errcode ?? null,
    },
  };

  await saveLastRun(env?.MEDIA, summary);
  console.log(JSON.stringify({ event: "patrol_completed", ...summary }));
  return summary;
}

export function createWorkerHandler({ fetchImpl = globalThis.fetch, now = () => Date.now() } = {}) {
  return {
    async scheduled(controller, env, _ctx) {
      const scheduledMs = Number.isFinite(Number(controller?.scheduledTime))
        ? Number(controller.scheduledTime)
        : now();
      await executePatrolAndNotify(env, {
        trigger: "cron",
        nowMs: scheduledMs,
        notify: true,
        fetchImpl,
      });
    },

    async fetch(request, env, _ctx) {
      let url;
      try {
        url = new URL(request.url);
      } catch {
        return jsonResponse({ ok: false, error: "BAD_REQUEST" }, 400);
      }

      if ((url.pathname === "/" || url.pathname === "/healthz") && request.method === "GET") {
        const lastRun = await readLastRun(env?.MEDIA);
        return jsonResponse({
          ok: true,
          service: "vc-radar-cron",
          scheduleUtc: "30 0,12 * * *",
          scheduleBeijing: "每日 08:30、20:30",
          lastRun,
        });
      }

      if (url.pathname === "/v1/cron/run") {
        if (request.method !== "POST") {
          return jsonResponse({ ok: false, error: "METHOD_NOT_ALLOWED" }, 405);
        }
        const auth = verifyBearerToken(request.headers.get("authorization"), env?.CRON_TRIGGER_TOKEN);
        if (!auth.ok) {
          return jsonResponse({ ok: false, error: auth.code, message: auth.message }, auth.status);
        }
        let body = {};
        try {
          const text = await request.text();
          if (text.trim()) body = JSON.parse(text);
        } catch {
          return jsonResponse({ ok: false, error: "BAD_JSON" }, 400);
        }
        const notify = body?.notify !== false;
        const channel = body?.channel === "test" ? "test" : "prod";
        if (body?.replayLast === true) {
          const replayRes = await replayLastPatrolReceipt(env, {
            channel,
            nowMs: now(),
            fetchImpl,
          });
          return jsonResponse(replayRes, replayRes.status || (replayRes.ok ? 200 : 500));
        }
        const regions = Array.isArray(body?.regions) ? body.regions : undefined;
        const summary = await executePatrolAndNotify(env, {
          trigger: "manual",
          nowMs: now(),
          notify,
          channel,
          regions,
          fetchImpl,
        });
        return jsonResponse(summary, summary.ok ? 200 : 500);
      }

      return jsonResponse({ ok: false, error: "NOT_FOUND" }, 404);
    },
  };
}

const defaultWorker = createWorkerHandler();
export default defaultWorker;
