"""Cloudflare 定时巡检 Worker、Pages R2 热更新函数与钉钉自定义机器人回执端到端离线测试。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import zlib
import struct

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_png_bytes(width: int = 320, height: int = 240, rgb: tuple[int, int, int] = (40, 120, 220)) -> bytes:
    """生成大于 16KB 的合法未压缩块 PNG 字节流，供 R2 魔数校验与 SHA-256 去重测试使用。"""
    raw_rows = bytearray()
    row_pixels = bytes(rgb) * width
    for y in range(height):
        raw_rows.append(0)
        # 混入行号扰动以防压缩率过高低于 16KB 阈值
        varied = bytes(((b + y * 13 + idx * 7) & 0xFF) for idx, b in enumerate(row_pixels))
        raw_rows.extend(varied)
    compressed = zlib.compress(bytes(raw_rows), level=0)

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")


def _run_node_mjs(script_body: str, env_extra: dict[str, str] | None = None) -> dict:
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script_body],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
        env={**__import__("os").environ, **(env_extra or {})},
    )
    assert proc.returncode == 0, f"Node 测试脚本失败:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{") and ln.strip().endswith("}")]
    assert lines, f"未找到 JSON 输出:\n{proc.stdout}"
    return json.loads(lines[-1])


def test_dingtalk_receipt_formatting_signing_and_errcode():
    """验证钉钉回执严格对齐 kanban 规范：首行 ### 顶格、禁反引号、全角冒号置外、双换行分段、superAgent 注入与 errcode 校验。"""
    script = r"""
import { formatPatrolReceipt, sendDingtalkReceipt, signDingtalkWebhook } from "./cloudflare/dingtalk.mjs";
import { slugifyProduct } from "./cloudflare/pipeline.mjs";

const cjkSlugA = slugifyProduct("伙伙", "vc");
const cjkSlugB = slugifyProduct("搭搭", "vc");
if (cjkSlugA === cjkSlugB || cjkSlugA === "p-vc") {
  throw new Error(`中文产品 slug 未按名称生成唯一哈希: ${cjkSlugA} vs ${cjkSlugB}`);
}

const ts = Date.UTC(2026, 10, 7, 0, 30, 0); // 北京时间 2026-11-07 08:30 早间巡检

// 1. 有新增产品卡片
const newCard = formatPatrolReceipt({
  trigger: "cron",
  discovered: 46,
  processed: 2,
  rejectedNew: 1,
  totalAdmitted: 22,
  admittedNew: [
    {
      id: "picswap",
      name: "PicSwap",
      channel: "chart",
      platform: "iOS 已上架",
      mech_tags: ["熟人/密友"],
      hook: "点开用 `2秒` 实况互换朋友眼前的画面，不修图零压力",
    },
    {
      id: "clubroom",
      name: "ClubRoom",
      channel: "vc",
      funding: "种子轮 · 750 万美元",
      mech_tags: ["多人共创"],
      hook: "和朋友一起搭线上小客厅",
    },
  ],
}, { keyword: "superAgent", siteBaseUrl: "https://vc-radar-cil.pages.dev", timestamp: ts });

// 2. 无新增产品卡片
const emptyCard = formatPatrolReceipt({
  trigger: "cron",
  discovered: 44,
  processed: 0,
  rejectedNew: 0,
  totalAdmitted: 22,
  admittedNew: [],
  sourceCounts: { chart: 32, community: 5, vc: 7 },
}, { keyword: "superAgent", siteBaseUrl: "https://vc-radar-cil.pages.dev", timestamp: ts + 12 * 3600 * 1000 });

// 3. 异常告警卡片（测试密钥脱敏）
const errCard = formatPatrolReceipt({
  trigger: "cron",
  fatalError: "请求失败 Bearer sk-secret1234567890abcdef access_token=tok_998877",
}, { keyword: "superAgent", siteBaseUrl: "https://vc-radar-cil.pages.dev", timestamp: ts });

// 4. 发送与 errcode 业务状态码校验（无加签 + 有加签）
const calls = [];
const okRes = await sendDingtalkReceipt(
  { discovered: 10, processed: 0, totalAdmitted: 21, admittedNew: [] },
  { DINGTALK_WEBHOOK_URL: "https://oapi.dingtalk.com/robot/send?access_token=dummy_tok", DINGTALK_KEYWORD: "superAgent" },
  {
    timestamp: ts,
    fetchImpl: async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ errcode: 0, errmsg: "ok" }), { status: 200 });
    },
  }
);

const badBizRes = await sendDingtalkReceipt(
  { discovered: 10, processed: 0, totalAdmitted: 21, admittedNew: [] },
  {
    DINGTALK_WEBHOOK_URL: "https://oapi.dingtalk.com/robot/send?access_token=dummy_tok",
    DINGTALK_SIGN_SECRET: "SECtestsecret123",
  },
  {
    timestamp: ts,
    fetchImpl: async (url, init) => {
      calls.push({ url, body: JSON.parse(init.body) });
      return new Response(JSON.stringify({ errcode: 310000, errmsg: "keywords not in content" }), { status: 200 });
    },
  }
);

const sig = signDingtalkWebhook("SECtestsecret123", ts);
console.log(JSON.stringify({ newCard, emptyCard, errCard, okRes, badBizRes, calls, sig }));
"""
    out = _run_node_mjs(script)

    new_card = out["newCard"]
    assert new_card["msgtype"] == "actionCard"
    assert new_card["actionCard"]["title"].startswith("[superAgent] 🟢 Social Radar 巡检｜新增 2 款社交产品")
    text = new_card["actionCard"]["text"]
    assert text.startswith("### 🟢 Social Radar 巡检｜新增 2 款社交产品\n\n")
    assert "`" not in text
    assert "「2秒」" in text
    assert "**巡检批次**：2026-11-07 08:30（早间巡检）" in text
    assert text.endswith("> superAgent · Social Radar 定时巡检回执")
    btns = new_card["actionCard"]["btns"]
    assert btns[0] == {"title": "查看 PicSwap 拆解", "actionURL": "https://vc-radar-cil.pages.dev/#p/picswap"}
    assert btns[-1] == {"title": "打开 Social Radar 全部产品", "actionURL": "https://vc-radar-cil.pages.dev/"}

    empty_card = out["emptyCard"]
    assert empty_card["actionCard"]["title"] == "[superAgent] 📡 Social Radar 巡检｜暂无新增产品"
    assert "2026-11-07 20:30（晚间巡检）" in empty_card["actionCard"]["text"]
    assert "候选均已在库，0 重复消耗" in empty_card["actionCard"]["text"]
    assert empty_card["actionCard"]["singleURL"] == "https://vc-radar-cil.pages.dev/"

    err_text = out["errCard"]["actionCard"]["text"]
    assert "sk-secret" not in err_text and "tok_998877" not in err_text
    assert "Bearer ***" in err_text and "access_token=***" in err_text

    assert out["okRes"]["ok"] is True
    assert "sign=" not in out["calls"][0]["url"]
    assert out["badBizRes"]["ok"] is False
    assert out["badBizRes"]["errcode"] == 310000
    assert f"timestamp={out['sig']['timestamp']}&sign={out['sig']['sign']}" in out["calls"][1]["url"]


def test_worker_cron_enrichment_dedup_and_pages_r2_data_function():
    """验证完整云端闭环：首轮发现新社交产品并转存 R2 图片 + Pages /data.json 秒读更新 + 次轮自动去重零重复消耗 + 鉴权防护。"""
    png_hex = _make_png_bytes().hex()
    script = f"""
import {{ createWorkerHandler }} from "./cloudflare/worker.mjs";
import {{ onRequestGet as dataJsonGet }} from "./functions/data.json.js";
import {{ onRequestGet as mediaGet }} from "./functions/media/[[key]].js";

const pngBytes = Buffer.from("{png_hex}", "hex");

// 内存模拟 Cloudflare R2 存储桶
function createMemoryR2(initial = {{}}) {{
  const store = new Map(Object.entries(initial));
  return {{
    store,
    async head(key) {{
      return store.has(key) ? {{ key, httpEtag: '"etag-' + key + '"' }} : null;
    }},
    async get(key) {{
      if (!store.has(key)) return null;
      const val = store.get(key);
      const bytes = val instanceof Uint8Array ? val : new TextEncoder().encode(String(val));
      return {{
        body: bytes,
        httpEtag: '"etag-' + key + '"',
        writeHttpMetadata(h) {{ h.set("Content-Type", "image/png"); }},
        async text() {{ return new TextDecoder().decode(bytes); }},
      }};
    }},
    async put(key, val) {{
      const bytes = typeof val === "string" ? new TextEncoder().encode(val) : new Uint8Array(val);
      store.set(key, bytes);
    }},
  }};
}}

const initialData = {{
  meta: {{ published_at: "2026-11-06 20:30:00", simulated: false, today: "2026-11-06", recent_cutoff: "2026-10-08" }},
  listing: [],
  week: {{ date: null, cards: [], round_pending: [], filtered: [], event_ids: [], yc: [] }},
  companies: [],
  events: [],
  products: [
    {{
      id: "setlog",
      name: "setlog",
      channel: "chart",
      live_status: "ios",
      origin: "indie",
      company: "Setlog Studio",
      app_store_url: "https://apps.apple.com/jp/app/id6500000001",
      web_url: "https://setlog.example.com",
      icon_url: "/media/" + "a".repeat(64) + ".png",
      badge: "日本社交榜 #1",
      badge_kind: "hot",
      platform: "iOS 已上架",
      hook: "录 2 秒瞬间跟密友交换日常",
      mech_tags: ["熟人/密友"],
      deep: [{{ title: "产品形态与切入点", body: "点开直接拍 2 秒视频。" }}],
      cross_check: {{ verified_points: ["日本榜首"], divergences: [] }},
      screenshots: [],
      ref_links: [],
      funding: "未公开披露融资",
      investors: "自然冲榜新品",
      release_status: "iOS 已上架",
      source_note: "日本社交榜 #1",
      rating_avg: 4.7,
      rating_count: 120,
    }},
  ],
  digests: [],
  wording: {{ product: "Social Radar" }},
}};

const r2 = createMemoryR2({{
  "state/data.json": JSON.stringify(initialData),
}});

let llmDossierCalls = 0;
const dingtalkPushes = [];

async function mockFetch(rawUrl, init = {{}}) {{
  const url = String(rawUrl);
  if (url.startsWith("https://oapi.dingtalk.com/robot/send")) {{
    dingtalkPushes.push(JSON.parse(init.body));
    return new Response(JSON.stringify({{ errcode: 0, errmsg: "ok" }}), {{ status: 200 }});
  }}
  if (url.includes("itunes.apple.com/us/rss/topfreeapplications") && url.includes("genre=6005")) {{
    return new Response(JSON.stringify({{
      feed: {{
        entry: [
          {{ id: {{ attributes: {{ "im:id": "6500000001" }} }} }}, // 已在库的 setlog
          {{ id: {{ attributes: {{ "im:id": "6600000002" }} }} }}, // 新社交应用 PicSwap
          {{ id: {{ attributes: {{ "im:id": "6600000099" }} }} }}, // 明显工具噪音 VPN Cleaner
        ],
      }},
    }}), {{ status: 200, headers: {{ "content-type": "application/json" }} }});
  }}
  if (url.includes("itunes.apple.com/") && url.includes("/rss/topfreeapplications")) {{
    return new Response(JSON.stringify({{ feed: {{ entry: [] }} }}), {{ status: 200 }});
  }}
  if (url.includes("itunes.apple.com/lookup")) {{
    return new Response(JSON.stringify({{
      results: [
        {{
          trackId: 6500000001,
          trackName: "setlog",
          sellerName: "Setlog Studio",
          releaseDate: "2026-08-01T00:00:00Z",
          genres: ["Social Networking"],
          description: "Share moments with close friends",
          averageUserRating: 4.91,
          userRatingCount: 520,
        }},
        {{
          trackId: 6600000002,
          trackName: "PicSwap — Besties Widget",
          sellerName: "PicSwap Labs",
          sellerUrl: "https://picswap.app",
          trackViewUrl: "https://apps.apple.com/us/app/picswap/id6600000002?uo=4",
          artworkUrl512: "https://cdn.picswap.app/icon.png",
          screenshotUrls: ["https://cdn.picswap.app/shot1.png"],
          releaseDate: "2026-09-15T00:00:00Z",
          genres: ["Social Networking"],
          description: "Live lockscreen photo swap with your best friends.",
          averageUserRating: 4.85,
          userRatingCount: 340,
        }},
        {{
          trackId: 6600000099,
          trackName: "Super VPN Proxy Cleaner",
          sellerName: "Utility Corp",
          releaseDate: "2026-10-01T00:00:00Z",
          genres: ["Utilities"],
          description: "Fast VPN proxy scanner and battery cleaner",
        }},
      ],
    }}), {{ status: 200 }});
  }}
  if (url.includes("customerreviews")) {{
    return new Response(JSON.stringify({{
      feed: {{
        entry: [
          {{ "im:rating": {{ label: "5" }}, title: {{ label: "超好玩" }}, content: {{ label: "每天跟室友互发表情包锁屏" }} }},
          {{ "im:rating": {{ label: "2" }}, title: {{ label: "安卓还没出" }}, content: {{ label: "异地男友是安卓机暂时没法加好友" }} }},
        ],
      }},
    }}), {{ status: 200 }});
  }}
  if (url.includes("hn.algolia.com") || url.includes("producthunt.com/feed")) {{
    return new Response(url.includes("algolia") ? JSON.stringify({{ hits: [] }}) : "<feed></feed>", {{ status: 200 }});
  }}
  if (url === "https://picswap.app" || url === "https://blog.example.com/picswap-review") {{
    return new Response(
      '<html><head><meta property="og:image" content="https://cdn.picswap.app/article-photo.png"></head><body><p>PicSwap 团队仅 3 人，靠大学宿舍锁屏互拍冷启动破 20 万日活。</p></body></html>',
      {{ status: 200, headers: {{ "content-type": "text/html" }} }}
    );
  }}
  if (url.startsWith("https://cdn.picswap.app/")) {{
    return new Response(pngBytes, {{ status: 200, headers: {{ "content-type": "image/png" }} }});
  }}
  if (url === "https://api.deepseek.com/v1/tools/search") {{
    const body = JSON.parse(init.body);
    if (body.query.includes("PicSwap")) {{
      return new Response(JSON.stringify({{
        results: [
          {{
            url: "https://blog.example.com/picswap-review",
            title: "PicSwap 深度实测：3 人团队做出 20 万日活锁屏相机",
            content: "PicSwap 主打双人同时按下快门才能解锁对方刚才拍下的桌面小组件画面，拿了 $3.5M 种子轮。",
          }},
        ],
      }}), {{ status: 200 }});
    }}
    return new Response(JSON.stringify({{ results: [] }}), {{ status: 200 }});
  }}
  if (url === "https://api.deepseek.com/chat/completions") {{
    const reqBody = JSON.parse(init.body);
    const sys = reqBody.messages[0].content;
    if (sys.includes("早期消费级社交产品研究员")) {{
      return new Response(JSON.stringify({{ choices: [{{ message: {{ content: JSON.stringify({{ deals: [] }}) }} }}] }}), {{ status: 200 }});
    }}
    llmDossierCalls += 1;
    const reply = {{
      admitted: true,
      origin_type: "indie",
      badge_text: "北美社交榜 #2 · 20万日活",
      platform_label: "iOS 已上架",
      mech_tags: ["熟人/密友"],
      hook: "发一张照片直接钉上死党锁屏，赋能密友日常零压力互拍且拿过 $3.5M 种子轮",
      cross_check: {{
        verified_points: ["北美社交榜冲进前二，评测帖披露日活突破 20 万"],
        divergences: ["评论区集中反馈暂缺 Android 版本，跨系统情侣无法互绑"],
      }},
      deep_sections: [
        {{ title: "产品形态与切入点", body: "点开就是取景框，拍完直接霸占好友手机桌面小组件。" }},
        {{ title: "目标人群与场景", body: "大学室友和异地死党上课摸鱼互损。" }},
        {{ title: "核心社交机制与关系链", body: "最多绑 4 个密友，没有公开点赞数。" }},
        {{ title: "冷启动与增长飞轮", body: "一个人装没法玩，天然一拉一装机。" }},
        {{ title: "真实口碑、野生玩法与踩过的坑", body: "野生玩法是拿来做起床打卡；槽点是还没有安卓端。" }},
        {{ title: "商业化与可参考讨论点", body: "目前完全免费攒关系链，小组件强触达值得参考。" }},
      ],
    }};
    return new Response(JSON.stringify({{ choices: [{{ message: {{ content: JSON.stringify(reply) }} }}] }}), {{ status: 200 }});
  }}
  return new Response("Not Found", {{ status: 404 }});
}}

const env = {{
  MEDIA: r2,
  DEEPSEEK_API_KEY: "sk-test-offline-key",
  DINGTALK_WEBHOOK_URL: "https://oapi.dingtalk.com/robot/send?access_token=test_tok",
  DINGTALK_KEYWORD: "superAgent",
  SITE_BASE_URL: "https://vc-radar-cil.pages.dev",
  CRON_TRIGGER_TOKEN: "trigger-secret-token-123",
}};

let clock = Date.UTC(2026, 10, 7, 0, 30, 0);
const worker = createWorkerHandler({{ fetchImpl: mockFetch, now: () => clock }});

// 第 1 轮定时触发：应只富化 PicSwap（setlog 已在库跳过，VPN Cleaner 被工具噪音规则拦截）
await worker.scheduled({{ scheduledTime: clock }}, env, {{}});
const callsAfterRun1 = llmDossierCalls;

// 验证 Pages `/data.json` 函数实时返回最新 R2 数据
const pagesResp = await dataJsonGet({{
  request: new Request("https://vc-radar-cil.pages.dev/data.json"),
  env: {{ MEDIA: r2 }},
}});
const liveData = await pagesResp.json();

// 验证 `/media/[[key]].js` 绝不泄露 `state/data.json`，但正常提供新镜像的 PNG
const blockedStateResp = await mediaGet({{
  request: new Request("https://vc-radar-cil.pages.dev/media/state/data.json"),
  params: {{ key: ["state", "data.json"] }},
  env: {{ MEDIA: r2 }},
  waitUntil() {{}},
}});

// 第 2 轮定时触发（12 小时后）：候选均已在 catalog 中，应 0 次调用拆解模型，并推送「暂无新增产品」回执
clock += 12 * 3600 * 1000;
await worker.scheduled({{ scheduledTime: clock }}, env, {{}});
const callsAfterRun2 = llmDossierCalls;

// 验证 HTTP 接口鉴权（缺省 503、错令 403、正确 200）与 /healthz
const healthResp = await worker.fetch(new Request("https://vc-radar-cron.workers.dev/healthz"), env, {{}});
const healthJson = await healthResp.json();

const noTokenEnvResp = await worker.fetch(
  new Request("https://vc-radar-cron.workers.dev/v1/cron/run", {{ method: "POST" }}),
  {{ ...env, CRON_TRIGGER_TOKEN: "" }},
  {{}}
);
const badTokenResp = await worker.fetch(
  new Request("https://vc-radar-cron.workers.dev/v1/cron/run", {{
    method: "POST",
    headers: {{ Authorization: "Bearer wrong-token" }},
  }}),
  env,
  {{}}
);

console.log(JSON.stringify({{
  callsAfterRun1,
  callsAfterRun2,
  radarSourceHeader: pagesResp.headers.get("X-Radar-Data-Source"),
  liveProductIds: liveData.products.map(p => p.id),
  picswap: liveData.products.find(p => p.id === "picswap"),
  setlogUpdatedRating: liveData.products.find(p => p.id === "setlog")?.rating_avg,
  blockedStateStatus: blockedStateResp.status,
  dingtalkTitles: dingtalkPushes.map(d => d.actionCard?.title),
  healthOk: healthJson.ok,
  lastRunAdmittedCount: healthJson.lastRun?.admittedNewCount,
  noTokenStatus: noTokenEnvResp.status,
  badTokenStatus: badTokenResp.status,
}}));
"""
    out = _run_node_mjs(script)

    assert out["callsAfterRun1"] == 1
    assert out["callsAfterRun2"] == 1  # 第二轮 0 次重复调用 LLM
    assert out["radarSourceHeader"] == "r2"
    assert out["liveProductIds"] == ["picswap", "setlog"]

    picswap = out["picswap"]
    assert picswap["icon_url"].startswith("/media/") and picswap["icon_url"].endswith(".png")
    # 黑话「赋能」已被替换为「帮」，美元缩写 `$3.5M` 已被替换为中文习惯「350 万美元」
    assert "赋能" not in picswap["hook"] and "帮" in picswap["hook"]
    assert "$3.5M" not in picswap["hook"] and "350 万美元" in picswap["hook"]
    assert picswap["deep"][0]["title"] == "产品形态与切入点"
    assert len(picswap["deep"]) == 6
    assert out["setlogUpdatedRating"] == 4.91

    assert out["blockedStateStatus"] == 404
    assert out["dingtalkTitles"] == [
        "[superAgent] 🟢 Social Radar 巡检｜新增 1 款社交产品",
        "[superAgent] 📡 Social Radar 巡检｜暂无新增产品",
    ]
    assert out["healthOk"] is True
    assert out["lastRunAdmittedCount"] == 0
    assert out["noTokenStatus"] == 503
    assert out["badTokenStatus"] == 403
