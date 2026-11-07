// ============================================================================
// Social Radar 钉钉自定义机器人回执模块（对齐 dsh-kanban IM 卡片与加签规范）
// ----------------------------------------------------------------------------
// 严格遵循钉钉开放平台「自定义机器人 Markdown 子集规范」：
// 1) 正文首行 `### ` 必须顶格（严禁被 `[keyword]` 挤到行中导致标题语法失效）；
// 2) 钉钉不支持反引号代码块与行内反引号，自动转换为中文直角引号「...」；
// 3) 全角冒号置于 `**字段名**：` 外侧，保证移动端/桌面端加粗稳定生效；
// 4) 段落与条目之间统一使用双换行 `\n\n`，避免单换行被合并粘连；
// 5) 自定义安全关键词（默认 `superAgent`）同时注入卡片 `title` 与底部引用行 `> ...`。
// ============================================================================

import { createHmac } from "node:crypto";

export const DEFAULT_SITE_URL = "https://vc-radar-cil.pages.dev";
export const DEFAULT_KEYWORD = "superAgent";

function cleanStr(value, maxLen = 500) {
  if (typeof value !== "string") return "";
  return value.trim().slice(0, maxLen);
}

function redactSecrets(text) {
  return String(text ?? "")
    .replace(/access_token=[A-Za-z0-9._-]+/gi, "access_token=***")
    .replace(/sk-[A-Za-z0-9]{12,}/g, "sk-***")
    .replace(/Bearer\s+[A-Za-z0-9._-]+/gi, "Bearer ***");
}

export function sanitizeDingtalkInline(value, maxLen = 0, { multiline = false } = {}) {
  let text = redactSecrets(value);
  text = text.replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, (_, body) =>
    String(body || "")
      .trim()
      .split(/\r?\n/)
      .map((line) => `> ${line.replace(/`/g, "")}`)
      .join("\n\n")
  );
  text = text.replace(/`([^`\r\n]+)`/g, "「$1」");
  text = text.replace(/`/g, "");
  text = text
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*#{1,6}\s+/, ""))
    .join(multiline ? "\n\n" : " ")
    .replace(/\n{3,}/g, "\n\n");
  return maxLen > 0 ? cleanStr(text, maxLen) : text.trim();
}

export function injectKeyword(text, keyword = DEFAULT_KEYWORD) {
  const base = String(text || "").trim();
  const kw = cleanStr(keyword, 64);
  if (!kw || base.includes(kw)) {
    return base;
  }
  return `[${kw}] ${base}`;
}

/**
 * 钉钉自定义机器人 Webhook URL 加签（可选）：
 * 以 `secret` 作为 HMAC-SHA256 key，对 `timestamp + "\n" + secret` 签名并 Base64 + encodeURIComponent。
 */
export function signDingtalkWebhook(secret, timestampMs = Date.now()) {
  const ts = String(Math.trunc(Number(timestampMs) || Date.now()));
  const stringToSign = `${ts}\n${secret}`;
  const rawSign = createHmac("sha256", String(secret)).update(stringToSign).digest("base64");
  return { timestamp: ts, sign: encodeURIComponent(rawSign) };
}

export function formatBeijingSlot(nowMs = Date.now(), trigger = "cron") {
  const ts = Number.isFinite(Number(nowMs)) ? Number(nowMs) : Date.now();
  const bj = new Date(ts + 8 * 3600 * 1000);
  const yyyy = bj.getUTCFullYear();
  const mm = String(bj.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(bj.getUTCDate()).padStart(2, "0");
  const hh = String(bj.getUTCHours()).padStart(2, "0");
  const mi = String(bj.getUTCMinutes()).padStart(2, "0");
  const period = trigger === "manual" ? "手动触发巡检" : bj.getUTCHours() < 14 ? "早间巡检" : "晚间巡检";
  return `${yyyy}-${mm}-${dd} ${hh}:${mi}（${period}）`;
}

export function formatPatrolReceipt(
  report = {},
  {
    keyword = DEFAULT_KEYWORD,
    siteBaseUrl = DEFAULT_SITE_URL,
    secret = "",
    timestamp = Date.now(),
  } = {}
) {
  const base = cleanStr(siteBaseUrl || DEFAULT_SITE_URL, 200).replace(/\/+$/, "") || DEFAULT_SITE_URL;
  const kw = sanitizeDingtalkInline(keyword, 64);
  const footer = `> ${kw ? `${kw} · ` : ""}Social Radar 定时巡检回执`;
  const slotText = sanitizeDingtalkInline(
    report.slotLabel || formatBeijingSlot(timestamp, report.trigger || "cron"),
    80
  );
  const signature = secret ? signDingtalkWebhook(secret, timestamp) : null;

  if (report.fatalError) {
    const headerTitle = "🔴 Social Radar 巡检告警";
    const blocks = [
      `### ${headerTitle}`,
      `**巡检批次**：${slotText}`,
      `**异常摘要**：${sanitizeDingtalkInline(report.fatalError, 260, { multiline: true })}`,
      footer,
    ];
    return {
      msgtype: "actionCard",
      actionCard: {
        title: injectKeyword(headerTitle, kw),
        text: blocks.join("\n\n"),
        singleTitle: "打开 Social Radar",
        singleURL: `${base}/`,
      },
      ...(signature ? { _dingtalkQuery: signature } : {}),
    };
  }

  const admittedNew = Array.isArray(report.admittedNew) ? report.admittedNew : [];
  const discovered = Number(report.discovered || 0);
  const processed = Number(report.processed || 0);
  const rejectedNew = Number(report.rejectedNew || 0);
  const totalAdmitted = Number(report.totalAdmitted || 0);
  const counts = report.sourceCounts || {};

  if (admittedNew.length > 0) {
    const headerTitle = `🟢 Social Radar 巡检｜新增 ${admittedNew.length} 款社交产品`;
    const blocks = [
      `### ${headerTitle}`,
      `**巡检批次**：${slotText}`,
      `**扫描概况**：共扫 **${discovered}** 款候选 ｜ 新评估 **${processed}** 款 ｜ **新收录 ${admittedNew.length} 款**${
        rejectedNew > 0 ? ` ｜ 筛除 ${rejectedNew} 款` : ""
      } ｜ 库内累计 **${totalAdmitted} 款**`,
    ];

    admittedNew.slice(0, 4).forEach((item, idx) => {
      const channelLabel = item.channel === "vc" ? "早期融资" : "冲榜新品";
      const metaParts = [channelLabel];
      if (item.channel === "vc" && item.funding) metaParts.push(item.funding);
      else if (item.platform) metaParts.push(item.platform);
      if (Array.isArray(item.mech_tags) && item.mech_tags[0]) metaParts.push(item.mech_tags[0]);
      const name = sanitizeDingtalkInline(item.name || item.id || "新产品", 48);
      const meta = sanitizeDingtalkInline(metaParts.join(" · "), 80);
      const hook = sanitizeDingtalkInline(item.hook || "已生成 6 维度大白话拆解与交叉比对", 140);
      blocks.push(`**${idx + 1}. ${name}**（${meta}）\n\n${hook}`);
    });

    if (admittedNew.length > 4) {
      blocks.push(`另有 **${admittedNew.length - 4}** 款新产品已同步写入线上产品库。`);
    }
    blocks.push(footer);

    const btns = [
      ...admittedNew.slice(0, 3).map((item) => ({
        title: `查看 ${sanitizeDingtalkInline(item.name || item.id, 18)} 拆解`,
        actionURL: `${base}/#p/${encodeURIComponent(String(item.id || ""))}`,
      })),
      {
        title: "打开 Social Radar 全部产品",
        actionURL: `${base}/`,
      },
    ];

    return {
      msgtype: "actionCard",
      actionCard: {
        title: injectKeyword(headerTitle, kw),
        text: blocks.join("\n\n"),
        btnOrientation: "0",
        btns,
      },
      ...(signature ? { _dingtalkQuery: signature } : {}),
    };
  }

  const headerTitle = "📡 Social Radar 巡检｜暂无新增产品";
  const evalNote =
    processed > 0
      ? `新评估 **${processed}** 款${rejectedNew > 0 ? `（筛除 ${rejectedNew} 款非目标应用）` : ""}`
      : "候选均已在库，0 重复消耗";
  const srcParts = [];
  if (typeof counts.chart === "number") srcParts.push(`多国榜单 ${counts.chart} 款`);
  if (typeof counts.community === "number") srcParts.push(`社区首发 ${counts.community} 款`);
  if (typeof counts.vc === "number") srcParts.push(`早期融资 ${counts.vc} 款`);
  const srcStatus = srcParts.length
    ? `${srcParts.join(" · ")}，各路检索均正常`
    : "多国 App Store 榜单、社区首发与早期融资检索均正常";

  const blocks = [
    `### ${headerTitle}`,
    `**巡检批次**：${slotText}`,
    `**扫描概况**：共扫 **${discovered}** 款候选 ｜ ${evalNote} ｜ 库内累计 **${totalAdmitted} 款**`,
    `**链路状态**：${sanitizeDingtalkInline(srcStatus, 120)}`,
    footer,
  ];

  return {
    msgtype: "actionCard",
    actionCard: {
      title: injectKeyword(headerTitle, kw),
      text: blocks.join("\n\n"),
      singleTitle: "打开 Social Radar",
      singleURL: `${base}/`,
    },
    ...(signature ? { _dingtalkQuery: signature } : {}),
  };
}

export async function sendDingtalkReceipt(
  report = {},
  env = {},
  { fetchImpl = globalThis.fetch, timestamp = Date.now() } = {}
) {
  const rawWebhook = cleanStr(env?.DINGTALK_WEBHOOK_URL, 1024);
  if (!rawWebhook) {
    return { ok: false, skipped: true, reason: "missing_dingtalk_webhook_url" };
  }
  const keyword = cleanStr(env?.DINGTALK_KEYWORD, 64) || DEFAULT_KEYWORD;
  const secret = cleanStr(env?.DINGTALK_SIGN_SECRET, 256);
  const siteBaseUrl = cleanStr(env?.SITE_BASE_URL, 256) || DEFAULT_SITE_URL;

  const formatted = formatPatrolReceipt(report, {
    keyword,
    siteBaseUrl,
    secret,
    timestamp,
  });
  const bodyObj = { ...formatted };
  let finalUrl = rawWebhook;
  if (bodyObj._dingtalkQuery) {
    const { timestamp: ts, sign } = bodyObj._dingtalkQuery;
    delete bodyObj._dingtalkQuery;
    const sep = finalUrl.includes("?") ? "&" : "?";
    finalUrl = `${finalUrl}${sep}timestamp=${encodeURIComponent(ts)}&sign=${sign}`;
  }

  const res = await fetchImpl(finalUrl, {
    method: "POST",
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(bodyObj),
  });

  const rawText = await res.text();
  let parsed = null;
  try {
    parsed = JSON.parse(rawText);
  } catch {
    parsed = { raw: rawText.slice(0, 200) };
  }

  const platformOk =
    Boolean(res.ok) &&
    !(parsed && typeof parsed === "object" && typeof parsed.errcode === "number" && parsed.errcode !== 0);

  return {
    ok: platformOk,
    httpStatus: res.status,
    errcode: typeof parsed?.errcode === "number" ? parsed.errcode : null,
    errmsg: typeof parsed?.errmsg === "string" ? redactSecrets(parsed.errmsg).slice(0, 120) : "",
  };
}
