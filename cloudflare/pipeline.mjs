// ============================================================================
// Social Radar 云端巡检与多源标准化拆解流水线（Cloudflare Worker 原生实现）
// ----------------------------------------------------------------------------
// 零本机依赖、零 GitHub Actions 依赖：
// 1. 多国 App Store 双榜新品 + Hacker News / Product Hunt 社区首发 + 早期社交融资搜索；
// 2. 基于 R2 `state/catalog.json` 去重（已入库或已评估筛除的候选不再重复消耗 Search/LLM）；
// 3. 对新冒出的候选产品执行统一标准流程：官网探测 + Apple 真实好差评 + DeepSeek 热帖搜索
//    + 22 岁产品爱好者口吻 6 维度大白话拆解 + 多方交叉比对 + R2 SHA-256 图片转存；
// 4. 更新 R2 `state/data.json`，Pages `/data.json` 秒级热生效。
// ============================================================================

import { createHash } from "node:crypto";

export const R2_DATA_KEY = "state/data.json";
export const R2_CATALOG_KEY = "state/catalog.json";
export const R2_LAST_RUN_KEY = "state/last-run.json";

const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";

const DEEPSEEK_SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search";
const DEEPSEEK_CHAT_ENDPOINT = "https://api.deepseek.com/chat/completions";
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
const MAX_DATA_JSON_BYTES = 480_000;

const CHART_REGIONS = ["us", "jp", "kr", "tw", "hk", "gb"];
const CHART_GENRES = [
  ["6005", "社交"],
  ["6008", "摄影与录像"],
  ["6012", "生活"],
];
const REGION_LABELS = {
  us: "北美",
  jp: "日本",
  kr: "韩国",
  tw: "台湾",
  hk: "香港",
  gb: "英国",
};

const ALLOWED_MECH_TAGS = [
  "熟人/密友",
  "陌生人/破冰",
  "兴趣/品味社区",
  "多人共创",
  "校园/破冰",
  "AI辅助连接",
];

const SECTION_TITLES = [
  "产品形态与切入点",
  "目标人群与场景",
  "核心社交机制与关系链",
  "冷启动与增长飞轮",
  "真实口碑、野生玩法与踩过的坑",
  "商业化与可参考讨论点",
];

const CORPORATE_JARGON_REPLACEMENTS = [
  ["赋能", "帮"],
  ["抓手", "切入点"],
  ["闭环", "跑通"],
  ["心智", "印象"],
  ["场域", "地方"],
  ["沉淀", "攒下"],
  ["矩阵", "一组"],
  ["组合拳", "一套打法"],
  ["全链路", "全流程"],
  ["颗粒度", "细节"],
  ["底层逻辑", "核心原因"],
  ["生态位", "位置"],
  ["降维打击", "直接碾压"],
  ["双轮驱动", "两头一起推"],
  ["价值锚点", "核心卖点"],
  ["信源", "来源"],
  ["核对", "比对"],
  ["待核", "待确认"],
  ["自动", "直接"],
  ["脚本", "程序"],
  ["总部位于", "团队在"],
  ["总部在", "团队在"],
  ["总部设在", "团队在"],
];

const BIGTECH_SELLERS_RE =
  /\b(?:bytedance|tiktok|poligon|spring\s+\(by|meta\s+platforms|instagram|snap\s+inc|google\s+llc|tencent|kakao|naver|line\s+corp|yahoo\s+japan|alibaba|baidu|kuaishou|netease|xiaohongshu|pinterest|reddit\s+inc|discord\s+inc|microsoft|apple\s+inc)\b/i;

const UTILITY_NOISE_RE =
  /\b(?:vpn|proxy|adblock|qr\s*code|barcode|scanner|pdf|calculator|flashlight|cleaner|battery|antivirus|authenticator|password|weather|alarm\s+clock|compass|translator|remote\s+control|printer|dashcam|cctv|ip\s+camera|baby\s+monitor|parking|banking|tax\s+|invoice|spreadsheet|watermark\s+remover|background\s+eraser|photo\s+enhancer|upscaler|teleprompter|ringtone|wallpaper|font\s+keyboard|parcel|courier|short\s+drama|reelshort|dramabox|shortmax|kalos\s+tv|netshort|goodshort|moboreels)\b|扫描全能|清理大师|手电筒|万年历|天气预报|计算器|投屏|遥控器|路由器|行车记录仪|监控摄像|短剧|看剧|免费小说|壁纸大全|铃声|输入法|网盘|压缩|解压|证件照|去水印|老照片修复|マイナンバー|確定申告|乗換案内|ポイントカード|チラシ|クーポン|家計簿|歩数計|防犯|タクシー配車|배달|택시|은행|보험|계산기|날씨|백신|스캐너|단편\s*드라마|^(?:探探|tantan|陌陌|momo|tinder|bumble|badoo|grindr|soul|微信|wechat|微博|weibo|qq|line|kakaotalk)(?:\b|[-－—:：\s])/i;

const B2B_ENTERPRISE_RE =
  /\b(?:b2b|enterprise|cybersecurity|soc\s*2|compliance|payroll|erp|crm|devops|kubernetes|cloud\s+infrastructure|semiconductor|defense|autonomous\s+vehicle|surgical|clinical\s+trial|ehr|medical\s+billing|supply\s+chain|procurement|warehouse|freight|tax\s+automation|legal\s+tech|law\s+firm|wealth\s+management|hedge\s+fund|actuarial|asic|fpga)\b/i;

const SOCIAL_SIGNAL_RE =
  /\b(?:social|community|communities|friends?|besties|hangout|club|clubs|dating|matchmaking|meet\s+people|group\s+chat|voice\s+chat|walkie|photo\s+sharing|disposable\s+camera|widget|campus|college|co-create|multiplayer|creator|fandom|book\s+club|film\s+club|taste|recommendations?|recs|scrapbook|journal\s+with|shared\s+album|party|events?|invite|messaging|chat\s+with|connect\s+with)\b|社交|社区|社群|好友|密友|朋友|交友|脱单|破冰|搭子|群聊|语音房|同好|共创|小组件|合拍|打卡|友だち|友達|親友|コミュニティ|つながる|共有|トーク|マッチング|交換日記|친구|커뮤니티|소셜|공유|모임|채팅|다이어리/i;

const SEO_SCRAPER_HOSTS = [
  "apkpure.com",
  "apkcombo.com",
  "uptodown.com",
  "softonic.com",
  "appbrain.com",
  "appadvice.com",
  "apkmonk.com",
  "malavida.com",
  "steprimo.com",
  "sensortower.com",
  "sensortower-china.com",
  "appstorestatistics.com",
  "openaso.ai",
  "apptopia.com",
  "gamemasha.com",
  "xianz.ai",
  "qimai.cn",
  "chandashi.com",
  "data.ai",
  "similarweb.com",
  "foxdata.com",
  "foxdata.cn",
  "mwm.ai",
  "applion.jp",
  "appgoblin.info",
  "appmagic.rocks",
  "apprankspy.com",
  "app-ranking.net",
  "apkcube.com",
  "chrome-stats.com",
  "androidrank.org",
  "apprecs.com",
  "appstor.io",
  "hn.algolia.com",
  "ldplayer.net",
  "memuplay.com",
  "1ting.com",
  "storelift.net",
  "unstar.app",
  "fluxbuilder.com",
  "bumetric.com",
  "clubhousedb.com",
  "biyografya.com",
];

const BAD_IMG_URL_RE =
  /(?:avatar|logo|icon|favicon|emoji|badge|sprite|spinner|pixel|tracker|1x1|qrcode|banner-ad|\.svg(?:\?|$))/i;
const APPSTORE_LINK_RE = /https?:\/\/apps\.apple\.com\/(?:[a-z]{2}\/)?app\/(?:[^/"'\s>]+\/)?id(\d{8,12})/i;
const PLAYSTORE_LINK_RE = /https?:\/\/play\.google\.com\/store\/apps\/details\?id=[A-Za-z0-9._]+/i;
const WAITLIST_TEXT_RE =
  /\b(?:join\s+(?:the\s+)?waitlist|request\s+early\s+access|coming\s+soon|invite[\s-]only\s+beta|get\s+early\s+access)\b|预约内测|加入候补|即将上线|内测申请/i;

function buildVcSearchQueries(nowMs = Date.now()) {
  const curYear = new Date(nowMs + 8 * 3600 * 1000).getUTCFullYear();
  const yrClause = `${curYear - 1} OR ${curYear}`;
  return [
    `"social app" startup raises seed OR "pre-seed" OR "Series A" ${yrClause} TechCrunch`,
    `"consumer social" startup seed round a16z OR Benchmark OR Founders Fund OR Lightspeed ${yrClause}`,
    `社交 App 社区 融资 种子轮 OR 天使轮 ${yrClause} 36氪`,
    `SNS アプリ コミュニティ シードラウンド 出資 OR 資金調達 ${yrClause} PR TIMES`,
  ];
}

const VC_DEAL_EXTRACT_PROMPT = `你是早期消费级社交产品研究员。请从下面多篇公开科技媒体融资报道摘录中，提取所有属于「人与人连接型消费级社交 / 社区 / 好友互动 App 或 Web 产品」的早期融资项目（种子前轮 / 天使轮 / 种子轮 / A 轮）。
严格剔除：B2B 企业软件、社媒营销自动化后台、纯单机 AI 陪聊虚拟人、金融交易、医疗与硬件。
所有说明字段必须使用简体中文（严禁夹杂日韩假名谚文或英文长句）。
返回严格合法 JSON 对象：
{
  "deals": [
    {
      "product_name": "产品中文名或简短品牌名（若原名为日韩文或英文短语请译为中文）",
      "company_name": "出品公司或团队中文名",
      "web_url": "文中提及的产品官网 URL，不确定则填空字符串",
      "funding_text": "纯中文轮次与金额，如 '种子轮 · 820 万美元'、'种子前轮 · 2000 万美元' 或 '种子轮 · 金额未公开'",
      "investors_text": "纯中文领投/参投机构，如 'a16z 领投' 或 'ZVC、DGDV 参投'",
      "summary": "纯中文一句话说明产品核心社交玩法",
      "source_url": "对应报道原文 URL（必须直接取自输入条目）",
      "source_title": "将对应报道标题译为通顺的简体中文标题"
    }
  ]
}
最多返回 10 个互不重复的代表性早期社交产品。`;

export const DOSSIER_SYSTEM_PROMPT = `你是一个常年泡在即刻、小红书和海外应用商店冲浪的 22 岁互联网产品经理朋友。
你的任务是根据提供的【多方原始证据包】（商店多区元数据、真实高赞与差评、官网正文、搜索抓到的热门评测帖/创始人专访、早期融资记录），完成两件事：
1. 判断它是不是值得讨论的「人与人连接型社交/社区产品」；
2. 如果准入，先核实融资信息与【多方交叉比对】，再用纯正中文母语大白话写出 6 个角度的深度拆解，并把外部参考链接标题全部译为简体中文。

### 一、准入边界（严格执行）
- **准入（admitted = true）**：核心价值是促进真实人类之间互动、分享、关系建立或共同创作的消费级产品——包括密友日常相机/小组件、陌生人语音/问答/兴趣破冰、书影音/美食/推荐语社区、多人共创画布/房间、校园熟人破冰、或帮真人牵线搭桥的社交应用。
- **剔除（admitted = false）**：
  1. 纯单机工具（修图滤镜、浏览器、输入法、网盘、VPN、运营商营业厅、日历待办、纯视频剪辑器）；
  2. 只跟 AI 虚拟人单聊谈恋爱/练口语、没有任何真人社交关系链的纯 AI 陪聊工具；
  3. 面向企业或创作者的 B2B 软件、社媒营销排期/矩阵起号/带货后台、客服工具、开发者基础设施、医疗/金融/硬件公司。
  若剔除，直接返回 \`{"admitted": false, "reject_reason": "简短中文原因"}\`，其余字段留空即可。

### 二、多方交叉比对与融资核实（核心要求）
- **融资回填核实**：仔细检查外部专访、新闻稿与报道中是否明确提到了**当前这款社交产品或其同一开发商主体**的融资轮次、加速器孵化或出资机构（严禁把同名其他 B2B/广告/电商公司的融资张冠李戴到当前 App 上！）：
  - 若有明确实锤，用纯中文填入 \`verified_funding\`（如 \`"种子轮 · 金额未公开"\`、\`"种子前轮 · 2000 万美元"\`、\`"a16z 加速器孵化"\`）和 \`verified_investors\`（如 \`"ZVC、DG 大和创投参投"\`）；
  - 若证据包完全未提及融资，\`verified_funding\` 与 \`verified_investors\` 必须填空字符串 \`""\`，严禁脑补。
- **交叉比对**：把【官方说法】、【外部专访/热帖】和【真实用户好差评】放在一起比对：
  - 多方印证的事实写进 \`cross_check.verified_points\`（1～3 条）；
  - 口碑反差或仅单方口径的数字如实写进 \`cross_check.divergences\`（1～2 条），绝不报喜不报忧。

### 三、说话方式与全中文硬性规定（零外文残留）
1. **100% 全中文表达**：除极简产品字母品牌词本身（如 Setlog、PicPet、Wabi）外，**严禁在任何字段残留日文假名、韩文谚文、英文句子/短语或其他外文**！原文里的功能名、引语、行业词必须全部意译成自然中文（如 \`waitlist\` 写「候补名单」、\`early access\` 写「抢先体验」、\`streak\` 写「连续打卡」、\`photo dump\` 写「日常随手拍」、\`Pre-Seed\` 写「种子前轮」、\`推し度\` 写「真爱粉程度」、\`イントロクイズ\` 写「前奏猜歌」、\`no ads\` 写「不投广告」）。
2. **像跟懂产品的同龄朋友聊微信一样说人话**：句子短一点、画面感强一点，一上来先让人脑子里浮现「点开屏幕看到啥、手指头怎么点」。
3. **严禁互联网黑话与公文翻译腔**：绝对不准出现「赋能、抓手、闭环、心智、场域、沉淀、矩阵、组合拳、全链路、颗粒度、底层逻辑、生态位、降维打击、该应用程序旨在、致力于为用户提供」；也不要写「信源、核对、脚本、自动、待核、总部位于」，美元金额统一写成「750 万美元」这种中文习惯写法（不要写 \`$7.5M\`）。

### 四、输出 JSON 结构（严格合法 JSON 对象）
{
  "admitted": true,
  "reject_reason": "",
  "origin_type": "indie 或 bigtech（字节/Meta/腾讯/Snap/Kakao/LINE/Naver 等大厂背景填 bigtech，独立创业团队填 indie）",
  "localized_name": "产品中文展示名（若原名为日韩文或含英文描述副标题，必须译为自然中文名，严禁带日韩假名谚文或英文长短语）",
  "localized_company": "出品团队中文展示名（如 'New Chat 团队'、'日本独立开发者武田翔太'、'字节跳动 TikTok 团队'）",
  "verified_funding": "实锤融资阶段与金额（纯中文，无实锤则填 ''）",
  "verified_investors": "实锤投资机构或孵化器（纯中文，无实锤则填 ''）",
  "badge_text": "卡片右上角 8～14 字精炼纯中文亮眼标签，如 '日本总榜 #1 · 750万日活' 或 '种子轮融资 · 密友日常'",
  "platform_label": "纯中文上架状态，如 '苹果端已上架'、'苹果 / 安卓已上架'、'网页版可直接玩'、'内测候补中'",
  "mech_tags": ["从固定枚举里选 1～2 个：熟人/密友、陌生人/破冰、兴趣/品味社区、多人共创、校园/破冰、AI辅助连接"],
  "hook": "卡片一句话纯中文大白话钩子（28～52 字）：直接讲清最核心的交互动作或反常识机制，让人一眼看懂怎么玩",
  "cross_check": {
    "verified_points": [
      "纯中文多方印证事实 1",
      "纯中文多方印证事实 2"
    ],
    "divergences": [
      "纯中文口碑反差或单方口径提示"
    ]
  },
  "deep_sections": [
    {"title": "产品形态与切入点", "body": "90～180 字纯中文大白话：点开长啥样、第一步干嘛、切了传统社交软件哪个让人心累的痛点"},
    {"title": "目标人群与场景", "body": "80～150 字纯中文大白话：哪拨人在什么具体时刻打开它，为什么微信/照片墙/短视频满足不了这个微妙需求"},
    {"title": "核心社交机制与关系链", "body": "90～170 字纯中文大白话：熟人还是生人、怎么破冰、发帖或互动门槛怎么压低、靠什么机制防止冷场"},
    {"title": "冷启动与增长飞轮", "body": "80～160 字纯中文大白话：第一批种子人群怎么攒起来的、有什么天然拉人钩子、外部帖子披露了什么下载/日活/冲榜表现"},
    {"title": "真实口碑、野生玩法与踩过的坑", "body": "90～170 字纯中文大白话：结合真实用户好评差评与热门评测帖——大家最上头的野生玩法是什么，骂得最凶的硬伤或翻车点又是什么"},
    {"title": "商业化与可参考讨论点", "body": "80～150 字纯中文大白话：目前怎么赚钱或还在免费攒人，做社交产品最值得拿出来讨论、抄作业或避坑的一两个点"}
  ],
  "translated_refs": [
    {
      "url": "原样对应输入证据中的外部报道或热帖 URL",
      "source_zh": "纯中文媒体/平台名称（如 'ZVC 创始人专访'、'日本时事通信社'、'海外创投报道'，严禁填英文域名）",
      "title_zh": "将原外文标题完整译为通顺的简体中文标题（严禁残留任何外文）"
    }
  ]
}`;

// ---------------- 基础工具函数 ----------------

export function beijingStamp(nowMs = Date.now()) {
  const bj = new Date(nowMs + 8 * 3600 * 1000);
  const iso = bj.toISOString();
  return {
    today: iso.slice(0, 10),
    publishedAt: `${iso.slice(0, 10)} ${iso.slice(11, 19)}`,
    isoBj: `${iso.slice(0, 19)}+08:00`,
  };
}

export function slugifyProduct(name, fallback = "app") {
  const slug = String(name || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (slug) return slug.slice(0, 48);
  const fbClean = String(fallback || "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
  if (/^ios\d{6,}$/.test(fbClean)) {
    return `app-${fbClean.slice(3)}`;
  }
  const hash = createHash("sha256")
    .update(String(name || fallback || "item"))
    .digest("hex")
    .slice(0, 8);
  return `p-${hash}`;
}

export function cleanAppTitle(rawTitle) {
  const title = String(rawTitle || "").trim();
  for (const sep of [" — ", " – ", " - ", " : ", "：", "｜", " | "]) {
    if (title.includes(sep)) {
      const head = title.split(sep, 1)[0].trim();
      if (head.length >= 2 && head.length <= 32) return head;
    }
  }
  if (title.includes(":")) {
    const head = title.split(":", 1)[0].trim();
    if (head.length >= 2 && head.length <= 28 && !/^\d+$/.test(head)) return head;
  }
  return title.slice(0, 40);
}

export function companyToken(name) {
  return String(name || "")
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

export function nameMatches(name, text) {
  const tok = companyToken(name);
  if (!tok) return false;
  const escaped = [...tok].map((ch) => ch.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("[^\\p{L}\\p{N}]*");
  const pre = /^[a-z0-9]/i.test(tok) ? "(?<![a-z0-9])" : "";
  const post = /[a-z0-9]$/i.test(tok) ? "(?![a-z0-9])" : "";
  const re = new RegExp(`${pre}${escaped}${post}`, "iu");
  return re.test(String(text || "").normalize("NFKC"));
}

export function registrableDomain(rawUrl) {
  if (typeof rawUrl !== "string" || !rawUrl.trim()) return "";
  try {
    const host = new URL(rawUrl).hostname.toLowerCase().replace(/^www\./, "");
    const parts = host.split(".").filter(Boolean);
    if (parts.length < 2) return host;
    const twoPartTlds = new Set(["co.jp", "ne.jp", "or.jp", "co.kr", "co.uk", "com.tw", "com.hk", "com.cn"]);
    const tail2 = parts.slice(-2).join(".");
    if (parts.length >= 3 && twoPartTlds.has(tail2)) {
      return parts.slice(-3).join(".");
    }
    return tail2;
  } catch {
    return "";
  }
}

function formatUsdVernacular(text) {
  return String(text || "")
    .replace(/\$\s*(\d+(?:\.\d+)?)\s*[Bb](?:illion)?\b/g, (_, n) => `${Math.round(Number(n) * 100) / 100} 亿美元`)
    .replace(/\$\s*(\d+(?:\.\d+)?)\s*[Mm](?:illion)?\b/g, (_, n) => {
      const wan = Math.round(Number(n) * 100);
      return wan >= 10000 ? `${Math.round((wan / 10000) * 100) / 100} 亿美元` : `${wan} 万美元`;
    })
    .replace(/\$\s*(\d+(?:\.\d+)?)\s*[Kk]\b/g, (_, n) => `${Math.round((Number(n) / 10) * 10) / 10} 万美元`);
}

export function sanitizeVernacular(rawText) {
  let cleaned = formatUsdVernacular(String(rawText || "").trim());
  for (const [bad, good] of CORPORATE_JARGON_REPLACEMENTS) {
    cleaned = cleaned.split(bad).join(good);
  }
  cleaned = cleaned
    .replace(/\bpre-seed\b/gi, "种子前轮")
    .replace(/\bseries\s+a\b/gi, "A 轮")
    .replace(/\bwaitlist\b/gi, "候补名单")
    .replace(/\bearly\s+access\b/gi, "抢先体验")
    .replace(/\bphoto\s+dump\b/gi, "日常随手拍")
    .replace(/\bstreak\b/gi, "连续打卡")
    .replace(/\bbug\s+fixes\b/gi, "常规问题修复")
    .replace(/Hacker\s+News\s+Show\s+HN/gi, "极客社区首发")
    .replace(/Show\s+HN\s*社区首发/gi, "极客社区首发")
    .replace(/Product\s+Hunt\s*每日新品/gi, "海外新品社区榜单")
    .replace(/Product\s+Hunt\s*首发/gi, "新品社区首发")
    .replace(/[\u3040-\u30ff\uac00-\ud7af]+/g, "")
    .replace(/美国(?:早期|公司|天使轮)/g, "北美地区")
    .replace(/非美国/g, "海外其他地区")
    .replace(/(?:DEEPSEEK|API_KEY|sk-[a-z0-9]{16,}|\.env|Authorization|Bearer\s+|localhost)/gi, "")
    .replace(/\/(?:Users|tmp|root|srv)\/[^\s，。；！？、）)"']+/g, "")
    .replace(/[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+/g, "")
    .replace(/(?<![0-9.])(?:\d{1,3}\.){3}\d{1,3}(?![0-9.])/g, "");
  return cleaned.replace(/[ \t]{2,}/g, " ").trim();
}

export function normalizeDossierPayload(raw) {
  if (!raw || typeof raw !== "object" || !raw.admitted) {
    return {
      admitted: false,
      reject_reason: sanitizeVernacular(raw?.reject_reason || "非目标社交产品"),
    };
  }
  const origin = String(raw.origin_type || "").trim().toLowerCase() === "bigtech" ? "bigtech" : "indie";
  const tags = (Array.isArray(raw.mech_tags) ? raw.mech_tags : [])
    .filter((t) => typeof t === "string" && ALLOWED_MECH_TAGS.includes(t))
    .slice(0, 2);
  if (!tags.length) tags.push("兴趣/品味社区");

  const rawSections = Array.isArray(raw.deep_sections) ? raw.deep_sections : [];
  const byTitle = {};
  rawSections.forEach((item, idx) => {
    if (item && typeof item === "object") {
      const t = String(item.title || "").trim();
      const b = sanitizeVernacular(item.body || item.text || "");
      if (b) {
        byTitle[t] = b;
        byTitle[`#${idx}`] = b;
      }
    }
  });
  const sections = [];
  SECTION_TITLES.forEach((fixedTitle, idx) => {
    let body = byTitle[fixedTitle] || byTitle[`#${idx}`] || "";
    if (fixedTitle === "产品形态与切入点" && !body) {
      body = byTitle["产品形态与切口"] || "";
    }
    if (body) sections.push({ title: fixedTitle, body });
  });

  const ccRaw = raw.cross_check && typeof raw.cross_check === "object" ? raw.cross_check : {};
  const verified = (Array.isArray(ccRaw.verified_points) ? ccRaw.verified_points : [])
    .map((x) => sanitizeVernacular(x))
    .filter(Boolean)
    .slice(0, 3);
  const divergences = (Array.isArray(ccRaw.divergences) ? ccRaw.divergences : [])
    .map((x) => sanitizeVernacular(x))
    .filter(Boolean)
    .slice(0, 2);

  const translatedRefs = (Array.isArray(raw.translated_refs) ? raw.translated_refs : [])
    .filter((r) => r && typeof r === "object" && typeof r.url === "string" && r.url.trim())
    .map((r) => ({
      url: r.url.trim(),
      source_zh: sanitizeVernacular(r.source_zh || "").slice(0, 32),
      title_zh: sanitizeVernacular(r.title_zh || "").slice(0, 110),
    }))
    .filter((r) => r.title_zh || r.source_zh);

  return {
    admitted: sections.length >= 4,
    reject_reason: sections.length >= 4 ? "" : "拆解信息不足",
    origin_type: origin,
    localized_name: sanitizeVernacular(raw.localized_name || "").slice(0, 36),
    localized_company: sanitizeVernacular(raw.localized_company || "").slice(0, 48),
    verified_funding: sanitizeVernacular(raw.verified_funding || "").slice(0, 56),
    verified_investors: sanitizeVernacular(raw.verified_investors || "").slice(0, 72),
    badge_text: sanitizeVernacular(raw.badge_text || "").slice(0, 22),
    platform_label: sanitizeVernacular(raw.platform_label || "").slice(0, 24),
    mech_tags: tags,
    hook: sanitizeVernacular(raw.hook || "").slice(0, 88),
    cross_check: { verified_points: verified, divergences },
    deep_sections: sections,
    translated_refs: translatedRefs,
  };
}

export function sniffImage(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < 16) return null;
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) {
    return { ext: "jpg", contentType: "image/jpeg" };
  }
  if (
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x4e &&
    bytes[3] === 0x47 &&
    bytes[4] === 0x0d &&
    bytes[5] === 0x0a &&
    bytes[6] === 0x1a &&
    bytes[7] === 0x0a
  ) {
    return { ext: "png", contentType: "image/png" };
  }
  const head4 = String.fromCharCode(bytes[0], bytes[1], bytes[2], bytes[3]);
  const mid4 = String.fromCharCode(bytes[8], bytes[9], bytes[10], bytes[11]);
  if (head4 === "RIFF" && mid4 === "WEBP") {
    return { ext: "webp", contentType: "image/webp" };
  }
  const head6 = String.fromCharCode(...bytes.slice(0, 6));
  if (head6 === "GIF87a" || head6 === "GIF89a") {
    return { ext: "gif", contentType: "image/gif" };
  }
  const ftyp = String.fromCharCode(...bytes.slice(4, 12));
  if (ftyp === "ftypavif" || ftyp === "ftypavis") {
    return { ext: "avif", contentType: "image/avif" };
  }
  return null;
}

function decodeHtmlEntities(str) {
  return String(str || "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/gi, "'")
    .replace(/&nbsp;/g, " ");
}

function stripHtmlToText(html) {
  return decodeHtmlEntities(
    String(html || "")
      .replace(/<(?:script|style|noscript|svg|nav|footer|header)[^>]*>[\s\S]*?<\/(?:script|style|noscript|svg|nav|footer|header)>/gi, " ")
      .replace(/<[^>]+>/g, " ")
  )
    .replace(/\s+/g, " ")
    .trim();
}

function extractPageAssets(htmlBody, baseUrl) {
  const snippet = String(htmlBody || "").slice(0, 300_000);
  let ogImage = "";
  let touchIcon = "";
  const images = [];

  const metaRe = /<meta\s+[^>]*>/gi;
  for (const match of snippet.matchAll(metaRe)) {
    const tag = match[0];
    const prop = (tag.match(/(?:property|name)\s*=\s*["']([^"']+)["']/i)?.[1] || "").toLowerCase();
    const content = tag.match(/content\s*=\s*["']([^"']+)["']/i)?.[1] || "";
    if ((prop === "og:image" || prop === "twitter:image") && content && !ogImage) {
      try {
        ogImage = new URL(decodeHtmlEntities(content), baseUrl).toString();
      } catch {}
    }
  }

  const linkRe = /<link\s+[^>]*>/gi;
  for (const match of snippet.matchAll(linkRe)) {
    const tag = match[0];
    const rel = (tag.match(/rel\s*=\s*["']([^"']+)["']/i)?.[1] || "").toLowerCase();
    const href = tag.match(/href\s*=\s*["']([^"']+)["']/i)?.[1] || "";
    if (rel.includes("apple-touch-icon") && href && !touchIcon) {
      try {
        touchIcon = new URL(decodeHtmlEntities(href), baseUrl).toString();
      } catch {}
    }
  }

  const imgRe = /<img\s+[^>]*>/gi;
  for (const match of snippet.matchAll(imgRe)) {
    const tag = match[0];
    const rawSrc =
      tag.match(/data-(?:src|original|lazy-src)\s*=\s*["']([^"']+)["']/i)?.[1] ||
      tag.match(/src\s*=\s*["']([^"']+)["']/i)?.[1] ||
      "";
    if (!rawSrc || rawSrc.startsWith("data:")) continue;
    const widthVal = Number((tag.match(/width\s*=\s*["']?(\d+)/i)?.[1] || "0"));
    const heightVal = Number((tag.match(/height\s*=\s*["']?(\d+)/i)?.[1] || "0"));
    if ((widthVal > 0 && widthVal < 140) || (heightVal > 0 && heightVal < 140)) continue;
    try {
      const full = new URL(decodeHtmlEntities(rawSrc), baseUrl).toString();
      if (/^https?:\/\//i.test(full) && !BAD_IMG_URL_RE.test(full) && !images.includes(full)) {
        images.push(full);
        if (images.length >= 6) break;
      }
    } catch {}
  }

  const appMatch = APPSTORE_LINK_RE.exec(snippet);
  const playMatch = PLAYSTORE_LINK_RE.exec(snippet);
  return {
    ogImage,
    touchIcon,
    images,
    appStoreId: appMatch ? appMatch[1] : null,
    appStoreUrl: appMatch ? appMatch[0] : "",
    playStoreUrl: playMatch ? playMatch[0] : "",
  };
}

export function isObviousUtilityNoise(name, seller, description, genres = []) {
  const blob = `${name}\n${seller}\n${String(description || "").slice(0, 800)}`;
  if (UTILITY_NOISE_RE.test(blob)) return true;
  if (B2B_ENTERPRISE_RE.test(blob) && !SOCIAL_SIGNAL_RE.test(name)) return true;
  const isSocialGenre = genres.some((g) => {
    const s = String(g || "").toLowerCase();
    return s.includes("social") || s.includes("ソーシャル") || s.includes("소셜") || s.includes("社交");
  });
  if (!isSocialGenre && !SOCIAL_SIGNAL_RE.test(blob)) return true;
  return false;
}

function sourceDisplayName(rawUrl) {
  try {
    const host = new URL(rawUrl).hostname.toLowerCase().replace(/^www\./, "");
    const known = {
      "techcrunch.com": "海外创投报道",
      "mp.weixin.qq.com": "微信公众号评测",
      "36kr.com": "36氪",
      "sspai.com": "少数派",
      "zvc.vc": "ZVC 专访",
      "prtimes.jp": "日本企业新闻通稿",
      "jiji.com": "日本时事通信社",
      "watch.impress.co.jp": "日本科技媒体",
      "thebridge.jp": "日本创投媒体",
      "note.com": "日本创作者专栏",
      "speedrun.a16z.com": "a16z 加速器名录",
      "a16z.com": "a16z 创投动态",
      "ycombinator.com": "YC 孵化名录",
      "news.ycombinator.com": "极客社区讨论",
      "reddit.com": "海外社区讨论",
      "producthunt.com": "海外新品社区",
      "substack.com": "独立专栏评测",
      "medium.com": "专栏深度文章",
      "businessinsider.com": "海外商业报道",
      "forbes.com": "福布斯报道",
      "fortune.com": "财富商业报道",
      "bloomberg.com": "彭博商业报道",
      "alleywatch.com": "纽约创投报道",
      "siliconangle.com": "硅谷创投报道",
    };
    for (const [domain, label] of Object.entries(known)) {
      if (host === domain || host.endsWith(`.${domain}`)) return label;
    }
    return "外部报道";
  } catch {
    return "外部报道";
  }
}

function hasUntranslatedForeignTitle(text) {
  const s = String(text || "").trim();
  if (!s) return true;
  if (/[\u3040-\u30ff\uac00-\ud7af\u0590-\u05ff\u0600-\u06ff\u0400-\u04ff]/.test(s)) return true;
  const hasCjk = /[\u4e00-\u9fff]/.test(s);
  const asciiWords = (s.match(/[A-Za-z]{2,}/g) || []).length;
  return !hasCjk && asciiWords >= 2;
}

function refUrlLookupKey(rawUrl) {
  return String(rawUrl || "").trim().replace(/\/+$/, "");
}

function isRelevantPost(cand, row) {
  const url = String(row?.url || "").trim();
  if (!/^https?:\/\//i.test(url)) return false;
  try {
    const host = new URL(url).hostname.toLowerCase().replace(/^www\./, "");
    if (SEO_SCRAPER_HOSTS.some((d) => host === d || host.endsWith(`.${d}`))) return false;
    if (host === "apps.apple.com" || host === "play.google.com") return false;
  } catch {
    return false;
  }
  const blob = [row.title, row.snippet, row.content, row.url].map((x) => String(x || "")).join(" ");
  if (!nameMatches(cand.name, blob) && !(cand.company_name && nameMatches(cand.company_name, blob))) {
    return false;
  }
  const tok = companyToken(cand.name);
  if (tok.length <= 5 && /^[a-z0-9]+$/.test(tok)) {
    const ctxRe =
      /\b(?:app|ios|iphone|android|social|friends|community|startup|raises?|seed|founder|launch|beta|club)\b|应用|软件|小程序|社交|社区|好友|朋友|日活|下载|融资|领投|评测|测评|实测|玩法|上线|留存|冲榜|总榜|アプリ|앱/i;
    let ok = ctxRe.test(blob);
    if (!ok && cand.company_name && cand.company_name.toLowerCase() !== cand.name.toLowerCase()) {
      ok = nameMatches(cand.company_name, blob);
    }
    if (!ok) return false;
  }
  return true;
}

// ---------------- 受控网络与 AI 客户端 ----------------

function createBudgetedFetch(fetchImpl, maxSubrequests = 400) {
  let used = 0;
  async function request(url, init = {}, timeoutMs = 12_000) {
    if (used >= maxSubrequests) {
      return null;
    }
    used += 1;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      return await fetchImpl(url, { ...init, signal: ctrl.signal });
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }
  return {
    request,
    used: () => used,
    remaining: () => Math.max(0, maxSubrequests - used),
  };
}

async function fetchJson(net, url, timeoutMs = 12_000) {
  const res = await net.request(
    url,
    { method: "GET", headers: { "User-Agent": UA, Accept: "application/json" } },
    timeoutMs
  );
  if (!res || res.status !== 200) return null;
  try {
    return await res.json();
  } catch {
    return null;
  }
}

async function fetchText(net, url, timeoutMs = 10_000, maxChars = 350_000) {
  const res = await net.request(
    url,
    {
      method: "GET",
      headers: { "User-Agent": UA, Accept: "text/html,application/xhtml+xml,application/xml,application/json;q=0.9,*/*;q=0.8" },
    },
    timeoutMs
  );
  if (!res) return { status: null, body: "", finalUrl: url };
  const ctype = (res.headers.get("content-type") || "").toLowerCase();
  if (ctype.includes("image/") || ctype.includes("video/") || ctype.includes("application/pdf")) {
    return { status: res.status, body: "", finalUrl: res.url || url };
  }
  try {
    const text = await res.text();
    return { status: res.status, body: text.slice(0, maxChars), finalUrl: res.url || url };
  } catch {
    return { status: res.status, body: "", finalUrl: res.url || url };
  }
}

export function parseCompactCount(raw) {
  if (typeof raw === "number" && Number.isFinite(raw)) return Math.trunc(raw);
  const cleaned = String(raw ?? "")
    .replace(/[,，\s]+/g, "")
    .trim();
  if (!cleaned) return null;
  const m = cleaned.match(/^(\d+(?:\.\d+)?)([kKmM万億亿])?$/);
  if (!m) return null;
  const num = Number(m[1]);
  if (!Number.isFinite(num)) return null;
  const unit = (m[2] || "").toLowerCase();
  const mult =
    unit === "k" ? 1_000 : unit === "m" ? 1_000_000 : unit === "万" ? 10_000 : unit === "亿" || unit === "億" ? 100_000_000 : 1;
  return Math.round(num * mult);
}

function resolveArtworkTemplate(template, width = 512, height = 512) {
  const raw = String(template || "").trim();
  if (!raw.startsWith("http")) return "";
  return raw.replace(/\{w\}x\{h\}(?:\{c\}|bb)\.\{f\}/i, `${width}x${height}bb.jpg`);
}

function extractSerializedServerData(htmlBody) {
  const m = String(htmlBody || "").match(/<script[^>]*\bid=["']serialized-server-data["'][^>]*>([\s\S]*?)<\/script>/i);
  if (!m || !m[1]) return null;
  try {
    return JSON.parse(m[1]);
  } catch {
    return null;
  }
}

function collectStorefrontLockups(node, out = [], seen = new Set(), depth = 0) {
  if (!node || typeof node !== "object" || depth > 20) return out;
  if (Array.isArray(node)) {
    for (const item of node) collectStorefrontLockups(item, out, seen, depth + 1);
    return out;
  }
  const adamId = String(node.adamId || "").trim();
  const title = String(node.title || "").trim();
  if (
    /^\d{8,12}$/.test(adamId) &&
    title &&
    !seen.has(adamId) &&
    (node.ordinal != null || node.icon?.template || node.developerName || node.subtitle)
  ) {
    seen.add(adamId);
    out.push(node);
  }
  for (const val of Object.values(node)) {
    if (val && typeof val === "object") collectStorefrontLockups(val, out, seen, depth + 1);
  }
  return out;
}

async function fetchStorefrontChart(net, region, genreId, genreLabel) {
  const url = `https://apps.apple.com/${region}/iphone/charts/${genreId}?chart=top-free`;
  const page = await fetchText(net, url, 14_000, 1_300_000);
  if (page.status !== 200 || !page.body) return [];
  const ssd = extractSerializedServerData(page.body);
  if (!ssd) return [];
  const lockups = collectStorefrontLockups(ssd);
  const rows = [];
  lockups.forEach((lockup, idx) => {
    const appId = String(lockup.adamId || "").trim();
    // Apple 商店数字 ID 单调递增：>= 6480000000 对应 2024.05 之后创建的新应用，直接过滤十年老国民应用
    if (!/^\d+$/.test(appId) || Number(appId) < 6_480_000_000) return;
    const rankNum = Number(lockup.ordinal);
    const rank = Number.isFinite(rankNum) && rankNum > 0 ? rankNum : idx + 1;
    const icon512 = resolveArtworkTemplate(lockup.icon?.template, 512, 512);
    const ratingAvg = typeof lockup.rating === "number" ? Math.round(lockup.rating * 100) / 100 : undefined;
    const ratingCount = parseCompactCount(lockup.ratingCount);
    rows.push({
      appId,
      region: region.toUpperCase(),
      genreId,
      genre: genreLabel,
      rank,
      rssMeta: {
        trackName: String(lockup.title || "").trim(),
        sellerName: String(lockup.developerName || lockup.artistName || "").trim(),
        releaseDate: "",
        fromStorefront: true,
        description: String(lockup.subtitle || "").trim(),
        genres: [genreLabel, genreId === "6005" ? "Social Networking" : ""].filter(Boolean),
        artworkUrl512: icon512,
        trackViewUrl: `https://apps.apple.com/${region}/app/id${appId}`,
        averageUserRating: ratingAvg,
        userRatingCount: ratingCount ?? undefined,
      },
    });
  });
  return rows;
}

export function extractAppStorePageBundle(htmlBody, country = "us") {
  const html = String(htmlBody || "");
  let description = "";
  let datePublished = "";
  let ratingAvg = null;
  let ratingCount = null;
  let iconUrl = "";

  for (const m of html.matchAll(/<script[^>]*type=["']application\/ld\+json["'][^>]*>([\s\S]*?)<\/script>/gi)) {
    try {
      const ld = JSON.parse(m[1]);
      if (ld?.["@type"] === "SoftwareApplication") {
        if (typeof ld.description === "string" && ld.description.length > description.length) {
          description = decodeHtmlEntities(ld.description).trim();
        }
        if (typeof ld.datePublished === "string" && /^\d{4}-\d{2}-\d{2}/.test(ld.datePublished)) {
          datePublished = ld.datePublished.slice(0, 10);
        }
        const rv = Number(ld.aggregateRating?.ratingValue);
        const rc = parseCompactCount(ld.aggregateRating?.reviewCount ?? ld.aggregateRating?.ratingCount);
        if (Number.isFinite(rv) && rv > 0) ratingAvg = Math.round(rv * 100) / 100;
        if (rc != null) ratingCount = rc;
        if (typeof ld.image === "string" && ld.image.startsWith("http")) iconUrl = ld.image;
      }
    } catch {}
  }

  const screenshots = [];
  const seenShotBases = new Set();
  const posReviews = [];
  const negReviews = [];
  const seenRevTitles = new Set();

  const ssd = extractSerializedServerData(html);
  if (ssd) {
    const walk = (node, depth = 0) => {
      if (!node || typeof node !== "object" || depth > 20) return;
      if (Array.isArray(node)) {
        for (const item of node) walk(item, depth + 1);
        return;
      }
      if (typeof node.description === "string" && node.description.length > description.length) {
        description = node.description.trim();
      }
      if (
        typeof node.template === "string" &&
        typeof node.width === "number" &&
        typeof node.height === "number" &&
        node.width >= 500 &&
        node.height > node.width * 1.35 &&
        !/AppIcon/i.test(node.template) &&
        screenshots.length < 5
      ) {
        const baseKey = node.template.split("/{w}")[0];
        if (!seenShotBases.has(baseKey)) {
          seenShotBases.add(baseKey);
          const h = Math.round((node.height / node.width) * 600);
          const resolved = resolveArtworkTemplate(node.template, 600, h);
          if (resolved) screenshots.push(resolved);
        }
      }
      if (typeof node.contents === "string" && typeof node.rating === "number" && node.title) {
        const title = String(node.title).trim();
        const content = String(node.contents).trim();
        if (content && !seenRevTitles.has(title)) {
          seenRevTitles.add(title);
          const row = {
            country: String(country || "us").toUpperCase(),
            stars: Math.trunc(node.rating),
            title: title.slice(0, 80),
            content: content.slice(0, 240),
          };
          if (row.stars >= 4 && posReviews.length < 4) posReviews.push(row);
          else if (row.stars <= 2 && negReviews.length < 4) negReviews.push(row);
        }
      }
      for (const val of Object.values(node)) {
        if (val && typeof val === "object") walk(val, depth + 1);
      }
    };
    walk(ssd);
  }

  if (!screenshots.length) {
    for (const m of html.matchAll(/https:\/\/is[0-9]-ssl\.mzstatic\.com\/image\/thumb\/[^\s"',>){}]+\.(?:jpg|png|webp)/gi)) {
      const u = m[0];
      if (/Icon|Placeholder\.mill\/\d+x\d+bb|1200x630wa/i.test(u)) continue;
      if (!screenshots.includes(u)) {
        screenshots.push(u);
        if (screenshots.length >= 5) break;
      }
    }
  }

  return {
    description,
    datePublished,
    ratingAvg,
    ratingCount,
    iconUrl,
    screenshots,
    reviews: [...posReviews, ...negReviews].slice(0, 8),
  };
}

async function deepseekSearch(net, apiKey, query, { count = 6, needContent = true } = {}) {
  const trimmed = String(query || "").trim().slice(0, 126);
  if (!apiKey || !trimmed) return [];
  const res = await net.request(
    DEEPSEEK_SEARCH_ENDPOINT,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ query: trimmed, count, need_content: Boolean(needContent) }),
    },
    15_000
  );
  if (!res || res.status !== 200) return [];
  try {
    const payload = await res.json();
    return Array.isArray(payload?.results) ? payload.results : [];
  } catch {
    return [];
  }
}

async function deepseekJson(net, apiKey, systemPrompt, userPrompt, { model = "deepseek-flash", timeoutMs = 45_000 } = {}) {
  if (!apiKey) return null;
  for (const delayMs of [0, 900]) {
    if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
    const res = await net.request(
      DEEPSEEK_CHAT_ENDPOINT,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${apiKey}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model,
          messages: [
            { role: "system", content: systemPrompt },
            { role: "user", content: userPrompt },
          ],
          thinking: { type: "disabled" },
          response_format: { type: "json_object" },
          temperature: 0,
          max_tokens: 4500,
          stream: false,
        }),
      },
      timeoutMs
    );
    if (!res) continue;
    if (res.status === 200) {
      try {
        const env = await res.json();
        const content = env?.choices?.[0]?.message?.content;
        if (typeof content === "string") {
          const parsed = JSON.parse(content);
          if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
        }
      } catch {
        return null;
      }
    }
    if (![429, 500, 502, 503, 504, 522].includes(res.status)) break;
  }
  return null;
}

async function mirrorImageToR2(net, mediaBucket, rawUrl, { minBytes = 10_000, referer = "" } = {}) {
  if (!mediaBucket || typeof rawUrl !== "string" || !/^https?:\/\//i.test(rawUrl)) return "";
  const headers = {
    "User-Agent": UA,
    Accept: "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
  };
  if (referer) headers.Referer = referer;
  const res = await net.request(rawUrl, { method: "GET", headers }, 12_000);
  if (!res || res.status !== 200) return "";
  let buf;
  try {
    const ab = await res.arrayBuffer();
    if (ab.byteLength < minBytes || ab.byteLength > MAX_IMAGE_BYTES) return "";
    buf = new Uint8Array(ab);
  } catch {
    return "";
  }
  const sniffed = sniffImage(buf);
  if (!sniffed) return "";
  const sha = createHash("sha256").update(buf).digest("hex");
  const key = `${sha}.${sniffed.ext}`;
  try {
    const existing = typeof mediaBucket.head === "function" ? await mediaBucket.head(key) : null;
    if (!existing) {
      await mediaBucket.put(key, buf, {
        httpMetadata: {
          contentType: sniffed.contentType,
          cacheControl: "public, max-age=31536000, immutable",
        },
      });
    }
    return `/media/${key}`;
  } catch {
    return "";
  }
}

// ---------------- 三路云端候选发现 ----------------

async function discoverAppStoreCharts(net, { cutoffDays = 540, nowMs = Date.now(), regions = CHART_REGIONS } = {}) {
  const cutoffIso = new Date(nowMs - cutoffDays * 86400 * 1000).toISOString().slice(0, 10);
  const tasks = [];
  for (const region of regions) {
    for (const [genreId, genreLabel] of CHART_GENRES) {
      tasks.push({ region, genreId, genreLabel });
    }
  }

  const appearances = new Map();
  const rssEntries = new Map();
  let itunesBlocked = false;
  for (let i = 0; i < tasks.length; i += 6) {
    const batch = tasks.slice(i, i + 6);
    let batchItunesOk = 0;
    const settled = await Promise.all(
      batch.map(async ({ region, genreId, genreLabel }) => {
        if (!itunesBlocked) {
          const url = `https://itunes.apple.com/${region}/rss/topfreeapplications/limit=60/genre=${genreId}/json`;
          const payload = await fetchJson(net, url, 12_000);
          if (payload && typeof payload === "object") {
            batchItunesOk += 1;
            let entries = payload?.feed?.entry || [];
            if (entries && !Array.isArray(entries)) entries = [entries];
            return entries.map((entry, idx) => {
              const appId = String(entry?.id?.attributes?.["im:id"] || "").trim();
              const images = Array.isArray(entry?.["im:image"]) ? entry["im:image"] : [];
              const rawIcon = String(images.at(-1)?.label || "").trim();
              const icon512 = rawIcon ? rawIcon.replace(/\/\d+x\d+bb\./, "/512x512bb.") : "";
              const catTerm = String(entry?.category?.attributes?.term || entry?.category?.attributes?.label || genreLabel).trim();
              return {
                appId,
                region: region.toUpperCase(),
                genreId,
                genre: genreLabel,
                rank: idx + 1,
                rssMeta: {
                  trackName: String(entry?.["im:name"]?.label || entry?.title?.label || "").trim(),
                  sellerName: String(entry?.["im:artist"]?.label || "").trim(),
                  releaseDate: String(entry?.["im:releaseDate"]?.label || "").trim(),
                  description: String(entry?.summary?.label || "").trim(),
                  genres: [catTerm, genreLabel].filter(Boolean),
                  artworkUrl512: icon512,
                  trackViewUrl: String(entry?.id?.label || "").split("?")[0],
                },
              };
            });
          }
        }
        // 当 Cloudflare Worker 出口被 legacy itunes.apple.com 拦截时，自动切换至 apps.apple.com 现代榜单页 JSON
        return fetchStorefrontChart(net, region, genreId, genreLabel);
      })
    );
    if (!itunesBlocked && batch.length > 0 && batchItunesOk === 0) {
      itunesBlocked = true;
    }
    for (const rows of settled) {
      for (const row of rows) {
        if (!/^\d+$/.test(row.appId)) continue;
        if (!appearances.has(row.appId)) appearances.set(row.appId, []);
        appearances.get(row.appId).push({
          appId: row.appId,
          region: row.region,
          genreId: row.genreId,
          genre: row.genre,
          rank: row.rank,
        });
        const existingRss = rssEntries.get(row.appId);
        if (!existingRss || (row.rssMeta.trackName && !existingRss.meta.trackName)) {
          rssEntries.set(row.appId, { country: row.region.toLowerCase(), meta: row.rssMeta });
        }
      }
    }
  }

  // 先基于 RSS/Storefront 自带元数据筛出近期且非工具噪音的候选（即使后续批量 lookup 被限流也不丢任何榜单新品）
  const prefilteredIdsByCountry = new Map();
  for (const [appId, ranks] of appearances.entries()) {
    const best = [...ranks].sort((a, b) => (a.genreId === "6005" ? 0 : 1) - (b.genreId === "6005" ? 0 : 1) || a.rank - b.rank)[0];
    const country = best.region.toLowerCase();
    const rss = rssEntries.get(appId)?.meta || {};
    const relRaw = String(rss.releaseDate || "").slice(0, 10);
    if (relRaw && (!/^\d{4}-\d{2}-\d{2}$/.test(relRaw) || relRaw < cutoffIso)) {
      continue;
    }
    if (rss.trackName && isObviousUtilityNoise(rss.trackName, rss.sellerName || "", rss.description || "", rss.genres || [])) {
      continue;
    }
    if (!prefilteredIdsByCountry.has(country)) prefilteredIdsByCountry.set(country, []);
    prefilteredIdsByCountry.get(country).push(appId);
  }

  const lookupMeta = new Map();
  if (!itunesBlocked) {
    for (const [country, ids] of prefilteredIdsByCountry.entries()) {
      for (let idx = 0; idx < ids.length; idx += 50) {
        const slice = ids.slice(idx, idx + 50);
        const url = `https://itunes.apple.com/lookup?id=${slice.join(",")}&country=${country}`;
        const payload = await fetchJson(net, url, 12_000);
        const results = Array.isArray(payload?.results) ? payload.results : [];
        for (const item of results) {
          const tid = String(item?.trackId || "").trim();
          if (tid) lookupMeta.set(tid, { country, meta: item });
        }
      }
    }
  }
  for (const [appId, entry] of rssEntries.entries()) {
    if (!lookupMeta.has(appId) && typeof entry?.meta?.averageUserRating === "number") {
      lookupMeta.set(appId, entry);
    }
  }

  const combinedIds = new Set([...rssEntries.keys(), ...lookupMeta.keys()]);
  const candidates = [];
  for (const appId of combinedIds) {
    const lkEntry = lookupMeta.get(appId);
    const rssEntry = rssEntries.get(appId);
    const country = lkEntry?.country || rssEntry?.country || "us";
    const meta = { ...(rssEntry?.meta || {}), ...(lkEntry?.meta || {}) };
    const relRaw = String(meta.releaseDate || "").slice(0, 10);
    const hasValidRel = /^\d{4}-\d{2}-\d{2}$/.test(relRaw);
    if (hasValidRel) {
      if (relRaw < cutoffIso) continue;
    } else if (!(meta.fromStorefront && Number(appId) >= 6_480_000_000)) {
      continue;
    }

    const rawName = String(meta.trackName || "").trim();
    const seller = String(meta.sellerName || meta.artistName || "").trim();
    const desc = String(meta.description || "").trim();
    const genres = Array.isArray(meta.genres) ? meta.genres : [];
    if (!rawName || isObviousUtilityNoise(rawName, seller, desc, genres)) continue;

    const ranks = [...(appearances.get(appId) || [])].sort(
      (a, b) => a.rank - b.rank || (a.genreId === "6005" ? 0 : 1) - (b.genreId === "6005" ? 0 : 1)
    );
    const bestRank = ranks[0]?.rank ?? 99;
    const seenReg = new Set();
    const dedupRegions = [];
    for (const r of ranks) {
      if (!seenReg.has(r.region)) {
        seenReg.add(r.region);
        dedupRegions.push(r);
      }
    }
    const regionSummary = dedupRegions
      .slice(0, 3)
      .map((r) => `${REGION_LABELS[r.region.toLowerCase()] || r.region}${r.genre}榜 #${r.rank}`)
      .join(" · ");

    const shortName = cleanAppTitle(rawName);
    let slug = slugifyProduct(shortName, `ios-${appId}`);
    if (slug === `p-ios${appId}` || slug.length < 2 || ["app", "ios", "social", "chat", "live"].includes(slug)) {
      slug = `app-${appId}`;
    }

    const isBigtech = BIGTECH_SELLERS_RE.test(seller);
    const ratingAvg = typeof meta.averageUserRating === "number" ? Math.round(meta.averageUserRating * 100) / 100 : null;
    const ratingCount = typeof meta.userRatingCount === "number" ? Math.trunc(meta.userRatingCount) : null;
    const shots = (Array.isArray(meta.screenshotUrls) ? meta.screenshotUrls : [])
      .filter((u) => typeof u === "string" && u.startsWith("http"))
      .slice(0, 5);
    const icon = String(meta.artworkUrl512 || meta.artworkUrl100 || "").trim();
    const storeUrl = String(meta.trackViewUrl || `https://apps.apple.com/${country}/app/id${appId}`).split("?")[0];
    const sellerUrl = String(meta.sellerUrl || "").trim();

    const genreBonus = ranks.some((r) => r.genreId === "6005") ? 18 : 6;
    const multiRegionBonus = Math.min(dedupRegions.length * 8, 28);
    const rankScore = Math.max(0, 100 - bestRank);
    const ratingBonus = Math.min(15, Math.pow(ratingCount || 0, 0.35));
    const score = rankScore + genreBonus + multiRegionBonus + ratingBonus;
    const releaseLabel = hasValidRel ? `iOS 已上架（${relRaw.slice(0, 7)} 首发）` : "iOS 已上架（近期冲榜新品）";

    candidates.push({
      product_id: slug,
      name: shortName,
      channel: "chart",
      live_status: "ios",
      origin_type: isBigtech ? "bigtech" : "indie",
      company_name: seller,
      app_store_id: appId,
      app_store_url: storeUrl,
      play_store_url: "",
      web_url: sellerUrl.startsWith("http") ? sellerUrl : "",
      icon_url: icon,
      raw_screenshots: shots,
      description: desc.slice(0, 2400),
      release_notes: String(meta.releaseNotes || "").slice(0, 800),
      funding_text: isBigtech ? `${seller} 旗下新包` : "未公开披露融资",
      investors_text: isBigtech ? seller : "自然冲榜新品",
      release_status_text: releaseLabel,
      source_note: regionSummary || "App Store 免费榜新品",
      chart_best_rank: bestRank,
      chart_regions: dedupRegions.slice(0, 4),
      rating_avg: ratingAvg,
      rating_count: ratingCount,
      first_release_date: hasValidRel ? relRaw : null,
      primary_country: country,
      seed_sources: [
        {
          source_kind: "appstore_meta",
          url: storeUrl,
          source_name: `App Store (${country.toUpperCase()})`,
          title: rawName,
          excerpt: `${regionSummary}\n开发者：${seller}\n首发日期：${hasValidRel ? relRaw : "近期冲榜新品"}\n简介：${desc.slice(0, 1000)}`,
        },
      ],
      priority_score: score,
    });
  }

  candidates.sort((a, b) => b.priority_score - a.priority_score);
  return { candidates, lookupMeta };
}

async function discoverCommunities(net) {
  const found = [];
  const hnUrl =
    "https://hn.algolia.com/api/v1/search_by_date?tags=show_hn&query=social%20OR%20friends%20OR%20community%20OR%20club&hitsPerPage=20";
  const hnPayload = await fetchJson(net, hnUrl, 10_000);
  const hits = Array.isArray(hnPayload?.hits) ? hnPayload.hits : [];
  for (const hit of hits) {
    const title = String(hit?.title || "").trim();
    const extUrl = String(hit?.url || "").trim();
    const storyText = String(hit?.story_text || "");
    if (!extUrl.startsWith("http") || B2B_ENTERPRISE_RE.test(title)) continue;
    if (!SOCIAL_SIGNAL_RE.test(`${title} ${storyText}`)) continue;
    const cleaned = title.replace(/^Show\s+HN\s*:\s*/i, "");
    const prodName = cleanAppTitle(cleaned);
    if (!prodName || prodName.length > 36) continue;
    const points = Number(hit?.points || 0);
    const hnItemUrl = `https://news.ycombinator.com/item?id=${hit?.objectID}`;
    found.push({
      product_id: slugifyProduct(prodName, String(hit?.objectID || "hn")),
      name: prodName,
      channel: "chart",
      live_status: "web",
      origin_type: "indie",
      company_name: prodName,
      app_store_id: null,
      app_store_url: "",
      play_store_url: "",
      web_url: extUrl,
      icon_url: "",
      raw_screenshots: [],
      description: `${cleaned}\n${storyText.slice(0, 800)}`.trim(),
      release_notes: "",
      funding_text: "早期独立产品",
      investors_text: "极客社区首发",
      release_status_text: "网页版已上线",
      source_note: `极客社区首发（${points} 赞）`,
      chart_best_rank: null,
      chart_regions: [],
      rating_avg: null,
      rating_count: null,
      first_release_date: null,
      primary_country: "us",
      seed_sources: [
        {
          source_kind: "community_post",
          url: hnItemUrl,
          source_name: "极客社区讨论",
          title,
          excerpt: `${cleaned}\n${storyText.slice(0, 900)}`,
        },
      ],
      priority_score: 35 + Math.min(points, 50),
    });
  }

  const phRes = await fetchText(net, "https://www.producthunt.com/feed", 10_000);
  if (phRes.status === 200 && phRes.body) {
    const entries = [...phRes.body.matchAll(/<entry>([\s\S]*?)<\/entry>/gi)].slice(0, 25);
    for (const m of entries) {
      const entryXml = m[1];
      const title = decodeHtmlEntities(entryXml.match(/<title[^>]*>([\s\S]*?)<\/title>/i)?.[1] || "").trim();
      const rawContent = entryXml.match(/<content[^>]*>([\s\S]*?)<\/content>/i)?.[1] || "";
      const summary = stripHtmlToText(decodeHtmlEntities(rawContent));
      const href = entryXml.match(/<link[^>]+href=["']([^"']+)["']/i)?.[1] || "";
      const blob = `${title} ${summary}`;
      if (!href.startsWith("http") || B2B_ENTERPRISE_RE.test(blob) || UTILITY_NOISE_RE.test(blob)) continue;
      if (!SOCIAL_SIGNAL_RE.test(blob)) continue;
      const prodName = cleanAppTitle(title);
      if (!prodName) continue;
      found.push({
        product_id: slugifyProduct(prodName, "ph"),
        name: prodName,
        channel: "chart",
        live_status: "web",
        origin_type: "indie",
        company_name: prodName,
        app_store_id: null,
        app_store_url: "",
        play_store_url: "",
        web_url: href,
        icon_url: "",
        raw_screenshots: [],
        description: summary.slice(0, 1000),
        release_notes: "",
        funding_text: "早期独立产品",
        investors_text: "新品社区首发",
        release_status_text: "产品已上线",
        source_note: "海外新品社区榜单",
        chart_best_rank: null,
        chart_regions: [],
        rating_avg: null,
        rating_count: null,
        first_release_date: null,
        primary_country: "us",
        seed_sources: [
          {
            source_kind: "community_post",
            url: href,
            source_name: "海外新品社区",
            title,
            excerpt: summary.slice(0, 800),
          },
        ],
        priority_score: 42,
      });
    }
  }
  return found;
}

async function discoverVcSocialDeals(net, apiKey, model = "deepseek-flash", nowMs = Date.now()) {
  if (!apiKey) return [];
  const snippets = [];
  const validUrls = new Map();
  for (const q of buildVcSearchQueries(nowMs)) {
    const results = await deepseekSearch(net, apiKey, q, { count: 6, needContent: true });
    for (const row of results) {
      const u = String(row?.url || "").trim();
      if (!/^https?:\/\//i.test(u) || validUrls.has(u)) continue;
      const title = String(row?.title || "").trim();
      const body = String(row?.content || row?.snippet || "").trim();
      if (B2B_ENTERPRISE_RE.test(title) || !body) continue;
      validUrls.set(u, { title: title.slice(0, 140), excerpt: body.slice(0, 900) });
      snippets.push(`[${snippets.length + 1}] 标题：${title.slice(0, 140)}\nURL：${u}\n摘录：${body.slice(0, 650)}`);
      if (snippets.length >= 18) break;
    }
    if (snippets.length >= 18) break;
  }
  if (!snippets.length) return [];

  const ans = await deepseekJson(net, apiKey, VC_DEAL_EXTRACT_PROMPT, snippets.join("\n\n"), {
    model,
    timeoutMs: 45_000,
  });
  const deals = Array.isArray(ans?.deals) ? ans.deals : [];
  const out = [];
  const seenSlugs = new Set();
  for (const item of deals) {
    if (!item || typeof item !== "object") continue;
    const pname = cleanAppTitle(String(item.product_name || "").trim());
    const srcUrl = String(item.source_url || "").trim();
    if (!pname || pname.length < 2 || !validUrls.has(srcUrl)) continue;
    const slug = slugifyProduct(pname, "vc");
    if (seenSlugs.has(slug)) continue;
    seenSlugs.add(slug);
    let web = String(item.web_url || "").trim();
    if (!/^https?:\/\//i.test(web)) web = "";
    const host = sourceDisplayName(srcUrl);
    const meta = validUrls.get(srcUrl);
    out.push({
      product_id: slug,
      name: pname,
      channel: "vc",
      live_status: web ? "web" : "waitlist",
      origin_type: "indie",
      company_name: String(item.company_name || pname).trim().slice(0, 48),
      app_store_id: null,
      app_store_url: "",
      play_store_url: "",
      web_url: web,
      icon_url: "",
      raw_screenshots: [],
      description: String(item.summary || "").trim().slice(0, 600),
      release_notes: "",
      funding_text: sanitizeVernacular(item.funding_text || "早期轮次").slice(0, 48),
      investors_text: sanitizeVernacular(item.investors_text || "早期机构投资").slice(0, 60),
      release_status_text: "待探测上线状态",
      source_note: `${host} 融资报道`,
      chart_best_rank: null,
      chart_regions: [],
      rating_avg: null,
      rating_count: null,
      first_release_date: null,
      primary_country: "us",
      seed_sources: [
        {
          source_kind: "vc_article",
          url: srcUrl,
          source_name: host,
          title: meta.title,
          excerpt: meta.excerpt,
        },
      ],
      priority_score: 108,
    });
  }
  return out;
}

export function mergeCandidates(vcList = [], chartList = [], commList = []) {
  const merged = new Map();
  const byDomain = new Map();
  const byAppId = new Map();
  const sharedHosts = new Set(["", "apple.com", "google.com", "producthunt.com", "ycombinator.com"]);

  for (const item of [...vcList, ...chartList, ...commList]) {
    const dom = registrableDomain(item.web_url);
    const cleanDom = sharedHosts.has(dom) ? "" : dom;
    let existingKey = null;
    if (item.app_store_id && byAppId.has(item.app_store_id)) {
      existingKey = byAppId.get(item.app_store_id);
    } else if (cleanDom && byDomain.has(cleanDom)) {
      existingKey = byDomain.get(cleanDom);
    } else if (merged.has(item.product_id)) {
      existingKey = item.product_id;
    }

    if (existingKey) {
      const target = merged.get(existingKey);
      if (!target.app_store_id && item.app_store_id) {
        target.app_store_id = item.app_store_id;
        target.app_store_url = item.app_store_url;
        target.live_status = "ios";
      }
      if (!target.icon_url && item.icon_url) target.icon_url = item.icon_url;
      if (!target.raw_screenshots?.length && item.raw_screenshots?.length) {
        target.raw_screenshots = [...item.raw_screenshots];
      }
      if (!target.web_url && item.web_url) target.web_url = item.web_url;
      if (
        item.chart_best_rank != null &&
        (target.chart_best_rank == null || item.chart_best_rank < target.chart_best_rank)
      ) {
        target.chart_best_rank = item.chart_best_rank;
        target.chart_regions = item.chart_regions;
      }
      if (item.rating_avg && !target.rating_avg) {
        target.rating_avg = item.rating_avg;
        target.rating_count = item.rating_count;
      }
      target.seed_sources.push(...(item.seed_sources || []));
      target.priority_score = Math.max(target.priority_score, item.priority_score) + 12;
      continue;
    }

    const pid = item.product_id;
    merged.set(pid, item);
    if (item.app_store_id) byAppId.set(item.app_store_id, pid);
    if (cleanDom) byDomain.set(cleanDom, pid);
  }
  return [...merged.values()].sort((a, b) => b.priority_score - a.priority_score);
}

// ---------------- 单品多源富化与 R2 镜像 ----------------

async function fetchAppStoreReviews(net, appId, countries = ["us", "jp"]) {
  const reviews = [];
  const seenTitles = new Set();
  const uniq = [...new Set(countries.filter(Boolean).map((c) => c.toLowerCase()))];
  for (const country of uniq) {
    const url = `https://itunes.apple.com/${country}/rss/customerreviews/id=${appId}/sortBy=mostHelpful/json`;
    const payload = await fetchJson(net, url, 10_000);
    let entries = payload?.feed?.entry || [];
    if (entries && !Array.isArray(entries)) entries = [entries];
    const pos = [];
    const neg = [];
    for (const entry of entries) {
      const ratingStr = String(entry?.["im:rating"]?.label || "").trim();
      const title = String(entry?.title?.label || "").trim();
      const content = String(entry?.content?.label || "").trim();
      if (!/^\d+$/.test(ratingStr) || !content || seenTitles.has(title)) continue;
      seenTitles.add(title);
      const stars = Number(ratingStr);
      const row = { country: country.toUpperCase(), stars, title: title.slice(0, 80), content: content.slice(0, 240) };
      if (stars >= 4 && pos.length < 4) pos.push(row);
      else if (stars <= 2 && neg.length < 4) neg.push(row);
    }
    reviews.push(...pos, ...neg);
    if (reviews.length >= 8) break;
  }
  return reviews.slice(0, 10);
}

async function enrichCandidate(cand, { net, apiKey, model = "deepseek-flash", mediaBucket, isoBj }) {
  const articleImages = [];
  let homepageExcerpt = "";

  // 1. 探测官网
  if (cand.web_url) {
    const hp = await fetchText(net, cand.web_url, 10_000);
    if (hp.status && hp.status >= 200 && hp.status < 300 && hp.body) {
      homepageExcerpt = stripHtmlToText(hp.body).slice(0, 1600);
      const assets = extractPageAssets(hp.body, hp.finalUrl || cand.web_url);
      if (!cand.app_store_id && assets.appStoreId) {
        cand.app_store_id = assets.appStoreId;
        cand.app_store_url = assets.appStoreUrl;
      }
      if (!cand.play_store_url && assets.playStoreUrl) cand.play_store_url = assets.playStoreUrl;
      if (!cand.icon_url && assets.touchIcon) cand.icon_url = assets.touchIcon;
      if (assets.ogImage) articleImages.push({ url: assets.ogImage, label: "官网画面", referer: hp.finalUrl || cand.web_url });
      for (const imgU of assets.images.slice(0, 2)) {
        articleImages.push({ url: imgU, label: "官网画面", referer: hp.finalUrl || cand.web_url });
      }
      if (!cand.app_store_id && !cand.play_store_url && WAITLIST_TEXT_RE.test(hp.body.slice(0, 25_000))) {
        cand.live_status = "waitlist";
      }
    }
  }

  // 2. 早期融资产品回查 App Store
  let storeMeta = null;
  if (!cand.app_store_id && cand.channel === "vc") {
    const searchUrl = `https://itunes.apple.com/search?term=${encodeURIComponent(cand.name.slice(0, 32))}&entity=software&limit=5&country=us`;
    const searchJson = await fetchJson(net, searchUrl, 10_000);
    const rows = Array.isArray(searchJson?.results) ? searchJson.results : [];
    const candDom = registrableDomain(cand.web_url);
    const candTok = companyToken(cand.name);
    for (const row of rows) {
      const sellerDom = registrableDomain(row.sellerUrl || "");
      const titleTok = companyToken(cleanAppTitle(row.trackName || ""));
      const sellerTok = companyToken(row.sellerName || "");
      const socialInDesc = /\b(?:friends?|social|community|share|chat|club)\b|社交|好友|社区/i.test(String(row.description || ""));
      if ((candDom && sellerDom && candDom === sellerDom) || (candTok.length >= 4 && titleTok === candTok && (sellerTok.includes(candTok) || socialInDesc))) {
        cand.app_store_id = String(row.trackId || "");
        storeMeta = row;
        break;
      }
    }
  }

  if (cand.app_store_id && !storeMeta && !cand.raw_screenshots?.length) {
    for (const c of [...new Set([cand.primary_country || "us", "us", "jp"])]) {
      const lk = await fetchJson(net, `https://itunes.apple.com/lookup?id=${cand.app_store_id}&country=${c}`, 10_000);
      if (Array.isArray(lk?.results) && lk.results[0]) {
        storeMeta = lk.results[0];
        break;
      }
    }
  }

  if (storeMeta) {
    cand.live_status = "ios";
    cand.app_store_url = String(storeMeta.trackViewUrl || cand.app_store_url || "").split("?")[0];
    if (!cand.icon_url) cand.icon_url = String(storeMeta.artworkUrl512 || storeMeta.artworkUrl100 || "");
    if (!cand.raw_screenshots?.length) {
      cand.raw_screenshots = (Array.isArray(storeMeta.screenshotUrls) ? storeMeta.screenshotUrls : [])
        .filter((u) => typeof u === "string" && u.startsWith("http"))
        .slice(0, 5);
    }
    if (!cand.description) cand.description = String(storeMeta.description || "").slice(0, 2400);
    if (!cand.release_notes) cand.release_notes = String(storeMeta.releaseNotes || "").slice(0, 800);
    if (!cand.first_release_date) cand.first_release_date = String(storeMeta.releaseDate || "").slice(0, 10) || null;
    if (cand.rating_avg == null && typeof storeMeta.averageUserRating === "number") {
      cand.rating_avg = Math.round(storeMeta.averageUserRating * 100) / 100;
      cand.rating_count = Math.trunc(storeMeta.userRatingCount || 0);
    }
    if (!cand.web_url && String(storeMeta.sellerUrl || "").startsWith("http")) {
      cand.web_url = String(storeMeta.sellerUrl);
    }
    const relMonth = String(cand.first_release_date || "").slice(0, 7);
    cand.release_status_text = relMonth ? `iOS 已上架（${relMonth} 首发）` : "iOS 已上架";
  } else if (cand.live_status === "ios" && cand.first_release_date) {
    cand.release_status_text = `iOS 已上架（${cand.first_release_date.slice(0, 7)} 首发）`;
  } else if (cand.play_store_url) {
    cand.live_status = "android";
    cand.release_status_text = "Android 已上架";
  } else if (cand.live_status === "waitlist") {
    cand.release_status_text = "内测候补中（官网登记）";
  } else if (cand.web_url) {
    cand.live_status = "web";
    cand.release_status_text = "Web 可直接访问";
  }

  // 若 App Store Lookup 未返回完整简介/截图，则从 apps.apple.com SSR 详情页一次性提取完整简介、高清竖屏截图与内嵌真实评论
  let storePageReviews = [];
  if (cand.app_store_id && (!cand.raw_screenshots?.length || String(cand.description || "").length < 120)) {
    const pageUrl = cand.app_store_url || `https://apps.apple.com/${cand.primary_country || "us"}/app/id${cand.app_store_id}`;
    const storePage = await fetchText(net, pageUrl, 12_000, 1_100_000);
    if (storePage.status === 200 && storePage.body) {
      const bundle = extractAppStorePageBundle(storePage.body, cand.primary_country || "us");
      if (bundle.description && bundle.description.length > String(cand.description || "").length) {
        cand.description = bundle.description.slice(0, 2400);
      }
      if (!cand.raw_screenshots?.length && bundle.screenshots.length) {
        cand.raw_screenshots = bundle.screenshots;
      }
      if (!cand.icon_url && bundle.iconUrl) {
        cand.icon_url = bundle.iconUrl;
      }
      if (!cand.first_release_date && bundle.datePublished) {
        cand.first_release_date = bundle.datePublished;
        cand.release_status_text = `iOS 已上架（${bundle.datePublished.slice(0, 7)} 首发）`;
      }
      if (cand.rating_avg == null && bundle.ratingAvg != null) {
        cand.rating_avg = bundle.ratingAvg;
        if (bundle.ratingCount != null) cand.rating_count = bundle.ratingCount;
      }
      if (bundle.reviews.length) {
        storePageReviews = bundle.reviews;
      }
    }
  }

  // 3. 拉取 Apple 官方真实用户好评与差评（优先复用详情页已内嵌的真实评论，不足时再拉 RSS）
  let reviews = [...storePageReviews];
  if (cand.app_store_id && reviews.length < 4) {
    const rssReviews = await fetchAppStoreReviews(net, cand.app_store_id, [cand.primary_country || "us", "us", "jp"]);
    const seenTitles = new Set(reviews.map((r) => r.title));
    for (const r of rssReviews) {
      if (!seenTitles.has(r.title)) {
        seenTitles.add(r.title);
        reviews.push(r);
      }
    }
    reviews = reviews.slice(0, 10);
  }

  // 4. 调用 DeepSeek Search 搜索热门评测帖、专访与多语种早期融资记录
  let anchor = "";
  if (cand.company_name && cand.company_name.toLowerCase() !== cand.name.toLowerCase()) {
    anchor = cand.company_name.split(",")[0].split("Inc")[0].trim().slice(0, 24);
  } else if (cand.web_url) {
    anchor = registrableDomain(cand.web_url).slice(0, 24);
  }
  const queries = [
    `"${cand.name}" ${anchor ? `${anchor} ` : ""}app interview OR DAU OR downloads OR funding`.slice(0, 120),
    ...(cand.origin_type !== "bigtech"
      ? [`"${cand.name}" ${anchor ? `${anchor} ` : ""}funding OR "seed round" OR 融资 OR 出資 OR 資金調達 OR 투자`.slice(0, 120)]
      : []),
    `"${cand.name}" social app review OR features OR how it works`.slice(0, 120),
    `"${cand.name}" App 评测 OR 玩法 OR 社交 OR 日活`.slice(0, 120),
  ];

  const seenUrls = new Set((cand.seed_sources || []).map((s) => s.url).filter(Boolean));
  const hotPosts = [];
  for (const q of queries) {
    const rows = await deepseekSearch(net, apiKey, q, { count: 6, needContent: true });
    for (const row of rows) {
      if (!isRelevantPost(cand, row)) continue;
      const u = String(row.url || "").trim();
      if (!u || seenUrls.has(u)) continue;
      seenUrls.add(u);
      const bodyText = String(row.content || row.snippet || row.summary || "").trim();
      hotPosts.push({
        url: u,
        title: String(row.title || "").trim().slice(0, 120),
        source_name: sourceDisplayName(u),
        excerpt: bodyText.slice(0, 2200),
      });
      if (hotPosts.length >= 6) break;
    }
    if (hotPosts.length >= 6) break;
  }

  // 抓取前 2 篇热帖网页提取真实配图与补充正文
  for (const post of hotPosts.slice(0, 2)) {
    const pg = await fetchText(net, post.url, 8_000);
    if (pg.status && pg.status >= 200 && pg.status < 300 && pg.body) {
      if (post.excerpt.length < 450) {
        const cleaned = stripHtmlToText(pg.body);
        if (cleaned) post.excerpt = cleaned.slice(0, 2200);
      }
      const assets = extractPageAssets(pg.body, pg.finalUrl || post.url);
      for (const imgU of [assets.ogImage, ...assets.images].filter(Boolean).slice(0, 3)) {
        articleImages.push({ url: imgU, label: `${post.source_name}配图`, referer: pg.finalUrl || post.url });
      }
    }
  }

  // 5. 组装证据包调用 DeepSeek 输出准入判定 + 融资核实 + 交叉比对 + 全中文 6 维度大白话拆解
  const evidenceBlocks = [
    [
      "【产品基础档案】",
      `- 名称：${cand.name}`,
      `- 出品公司/开发者：${cand.company_name}`,
      `- 发现来源：${cand.channel}（${cand.source_note}）`,
      `- 当前上架状态：${cand.release_status_text}（live_status=${cand.live_status}）`,
      `- 融资与投资方：${cand.funding_text}｜${cand.investors_text}`,
      `- 多区榜单排名：${cand.chart_regions?.length ? JSON.stringify(cand.chart_regions) : "未上免费榜前百"}`,
      `- 商店评分：${cand.rating_avg ? `${cand.rating_avg} 分（${cand.rating_count} 人评价）` : "暂无公开评分"}`,
      `- 商店简介：${String(cand.description || "").slice(0, 1400)}`,
      `- 最近更新日志：${String(cand.release_notes || "").slice(0, 400)}`,
    ].join("\n"),
  ];
  if (homepageExcerpt) evidenceBlocks.push(`【产品官网正文摘录】\n${homepageExcerpt.slice(0, 1200)}`);
  if (reviews.length) {
    const revTxt = reviews.slice(0, 8).map((r) => `- [${r.country} ${r.stars}星] ${r.title}：${r.content}`).join("\n");
    evidenceBlocks.push(`【App Store 真实用户好评与差评样本】\n${revTxt}`);
  }
  if (hotPosts.length) {
    const postsTxt = hotPosts
      .slice(0, 6)
      .map((p, idx) => `- 帖子 ${idx + 1}：《${p.title}》（来源：${p.source_name}｜URL：${p.url}）\n  摘要：${p.excerpt.slice(0, 850)}`)
      .join("\n\n");
    evidenceBlocks.push(`【搜索服务抓到的外部热门帖子、专访与评测】\n${postsTxt}`);
  }
  for (const seed of cand.seed_sources || []) {
    if (seed.source_kind === "vc_article" || seed.source_kind === "community_post") {
      evidenceBlocks.push(
        `【早期报道/首发帖：《${seed.title || ""}》（${seed.source_name || ""}｜URL：${seed.url || ""}）】\n${String(seed.excerpt || "").slice(0, 850)}`
      );
    }
  }

  const llmParsed = await deepseekJson(net, apiKey, DOSSIER_SYSTEM_PROMPT, evidenceBlocks.join("\n\n"), {
    model,
    timeoutMs: 48_000,
  });
  if (!llmParsed) return null;

  const normalized = normalizeDossierPayload(llmParsed);
  if (!normalized.admitted) {
    return {
      admitted: false,
      product_id: cand.product_id,
      name: cand.name,
      app_store_id: cand.app_store_id || null,
      domain: registrableDomain(cand.web_url),
      reject_reason: normalized.reject_reason,
      evaluated_at: isoBj,
    };
  }

  // 6. R2 媒体转存（图标 + 热帖实拍图 + 商店截图）
  let mirroredIcon = "";
  const mirroredShots = [];
  const seenPaths = new Set();

  if (cand.icon_url) {
    mirroredIcon = await mirrorImageToR2(net, mediaBucket, cand.icon_url, { minBytes: 1000, referer: cand.web_url });
    if (mirroredIcon) seenPaths.add(mirroredIcon);
  }
  for (const item of articleImages) {
    if (mirroredShots.filter((s) => s.label !== "App Store 截图").length >= 3) break;
    const path = await mirrorImageToR2(net, mediaBucket, item.url, { minBytes: 16_000, referer: item.referer });
    if (path && !seenPaths.has(path)) {
      seenPaths.add(path);
      mirroredShots.push({ url: path, label: sanitizeVernacular(item.label) || "产品画面" });
    }
  }
  for (const shotUrl of (cand.raw_screenshots || []).slice(0, 5)) {
    if (mirroredShots.length >= 6) break;
    const path = await mirrorImageToR2(net, mediaBucket, shotUrl, { minBytes: 10_000, referer: cand.app_store_url });
    if (path && !seenPaths.has(path)) {
      seenPaths.add(path);
      mirroredShots.push({ url: path, label: "App Store 截图" });
    }
  }

  const isBigtech = normalized.origin_type === "bigtech";
  const displayProdName = normalized.localized_name || sanitizeVernacular(cand.name) || "海外社交新品";
  const displayCompany = normalized.localized_company || sanitizeVernacular(cand.company_name || displayProdName) || displayProdName;

  // 7. 融资回填与双通道标记（冲榜新品被核实有早期融资时同步回填，并打上 has_vc 标记进入「早期融资」视图）
  const unverifiedFundingSet = new Set(["", "未公开披露融资", "早期独立产品", "未公开披露"]);
  let finalFunding = sanitizeVernacular(cand.funding_text);
  let finalInvestors = sanitizeVernacular(cand.investors_text);
  if (!isBigtech && normalized.verified_funding) {
    if (cand.channel !== "vc" || unverifiedFundingSet.has(finalFunding) || finalFunding === "早期轮次") {
      finalFunding = normalized.verified_funding;
    }
    if (
      normalized.verified_investors &&
      (cand.channel !== "vc" ||
        unverifiedFundingSet.has(finalInvestors) ||
        finalInvestors === "自然冲榜新品" ||
        finalInvestors === "极客社区首发" ||
        finalInvestors === "新品社区首发" ||
        finalInvestors === "早期机构投资")
    ) {
      finalInvestors = normalized.verified_investors;
    } else if (!normalized.verified_investors && finalInvestors === "自然冲榜新品") {
      finalInvestors = "早期创投支持";
    }
  }
  const hasVc =
    !isBigtech &&
    (cand.channel === "vc" ||
      (Boolean(finalFunding) && !unverifiedFundingSet.has(finalFunding) && !finalFunding.endsWith("旗下新包")));

  // 8. 外部参考链接全中文整理（优先采用模型翻译标题，缺失或仍含外文时生成纯中文兜底标题）
  const trByUrl = new Map();
  for (const tr of normalized.translated_refs || []) {
    if (tr.url) trByUrl.set(refUrlLookupKey(tr.url), tr);
  }
  const buildZhRefLink = (url, rawTitle, fallbackSource) => {
    const tr = trByUrl.get(refUrlLookupKey(url));
    const safeDefaultSource = sourceDisplayName(url);
    const candidateSource = sanitizeVernacular(tr?.source_zh || fallbackSource || safeDefaultSource);
    const finalSource = candidateSource && !hasUntranslatedForeignTitle(candidateSource) ? candidateSource : safeDefaultSource;
    const candidateTitle = sanitizeVernacular(tr?.title_zh || rawTitle || "");
    const finalTitle =
      candidateTitle && !hasUntranslatedForeignTitle(candidateTitle)
        ? candidateTitle
        : `${displayProdName} 相关${finalSource.endsWith("报道") || finalSource.endsWith("评测") || finalSource.endsWith("专访") || finalSource.endsWith("讨论") || finalSource.endsWith("通稿") ? finalSource : `${finalSource}报道`}`;
    return { title: finalTitle, source: finalSource, url };
  };

  const refLinks = [];
  const seenRefUrls = new Set();
  for (const p of hotPosts) {
    if (p.url && !seenRefUrls.has(p.url)) {
      seenRefUrls.add(p.url);
      refLinks.push(buildZhRefLink(p.url, p.title, p.source_name));
    }
  }
  for (const seed of cand.seed_sources || []) {
    if (seed.url && /^https?:\/\//i.test(seed.url) && seed.source_kind !== "appstore_meta" && !seenRefUrls.has(seed.url)) {
      seenRefUrls.add(seed.url);
      refLinks.push(buildZhRefLink(seed.url, seed.title, seed.source_name));
    }
  }

  let badgeKind = cand.channel === "vc" ? "vc" : isBigtech ? "warn" : "hot";
  if (cand.live_status === "waitlist") badgeKind = "warn";
  const priorityScore =
    Number(cand.priority_score || 50) +
    (hotPosts.length >= 2 ? 15 : 0) +
    (mirroredShots.length > 0 ? 8 : 0) +
    (cand.channel !== "vc" && hasVc ? 10 : 0);

  return {
    admitted: true,
    priority_score: priorityScore,
    updated_at: isoBj,
    app_store_id: cand.app_store_id || null,
    domain: registrableDomain(cand.web_url),
    product: {
      id: cand.product_id,
      name: displayProdName,
      channel: cand.channel === "vc" ? "vc" : "chart",
      has_vc: hasVc,
      live_status: ["ios", "android", "web", "waitlist"].includes(cand.live_status) ? cand.live_status : "ios",
      origin: isBigtech ? "bigtech" : "indie",
      company: displayCompany,
      company_id: cand.company_id || null,
      app_store_url: cand.app_store_url || "",
      play_store_url: cand.play_store_url || "",
      web_url: cand.web_url || "",
      icon_url: mirroredIcon,
      badge: normalized.badge_text || sanitizeVernacular(cand.source_note).slice(0, 20),
      badge_kind: badgeKind,
      platform: normalized.platform_label || sanitizeVernacular(cand.release_status_text),
      hook: normalized.hook,
      mech_tags: normalized.mech_tags,
      deep: normalized.deep_sections,
      cross_check: normalized.cross_check,
      screenshots: mirroredShots.slice(0, 6),
      ref_links: refLinks.slice(0, 6),
      funding: finalFunding,
      investors: finalInvestors,
      release_status: sanitizeVernacular(cand.release_status_text),
      source_note: sanitizeVernacular(cand.source_note),
      rating_avg: cand.rating_avg ?? null,
      rating_count: cand.rating_count ?? null,
    },
  };
}

// ---------------- R2 状态读写与完整巡检入口 ----------------

async function readR2Json(mediaBucket, key) {
  if (!mediaBucket || typeof mediaBucket.get !== "function") return null;
  try {
    const obj = await mediaBucket.get(key);
    if (!obj) return null;
    const text = await obj.text();
    return JSON.parse(text);
  } catch {
    return null;
  }
}

async function writeR2Json(mediaBucket, key, value) {
  const body = JSON.stringify(value, null, 1);
  await mediaBucket.put(key, body, {
    httpMetadata: { contentType: "application/json; charset=utf-8" },
  });
  return body.length;
}

function extractAppIdFromUrl(url) {
  const m = APPSTORE_LINK_RE.exec(String(url || ""));
  return m ? m[1] : null;
}

export async function loadOrBootstrapCatalog(mediaBucket, isoBj) {
  const baseData = (await readR2Json(mediaBucket, R2_DATA_KEY)) || {
    meta: { published_at: "", simulated: false, today: "", recent_cutoff: "" },
    listing: [],
    week: { date: null, cards: [], round_pending: [], filtered: [], event_ids: [], yc: [] },
    companies: [],
    events: [],
    products: [],
    digests: [],
    wording: {},
  };

  let catalog = await readR2Json(mediaBucket, R2_CATALOG_KEY);
  if (!catalog || typeof catalog !== "object" || !catalog.products) {
    catalog = {
      version: 1,
      updatedAt: isoBj,
      products: {},
      rejected: {},
    };
  }
  if (!catalog.rejected || typeof catalog.rejected !== "object") catalog.rejected = {};

  // 确保线上 `state/data.json` 里已有的全部社交产品都同步进 catalog 索引
  const existingList = Array.isArray(baseData.products) ? baseData.products : [];
  existingList.forEach((prod, idx) => {
    if (!prod || !prod.id || catalog.products[prod.id]) return;
    catalog.products[prod.id] = {
      priority_score: Math.max(40, 120 - idx * 2),
      updated_at: isoBj,
      app_store_id: extractAppIdFromUrl(prod.app_store_url),
      domain: registrableDomain(prod.web_url),
      product: prod,
    };
  });

  return { baseData, catalog };
}

function isAlreadySeen(cand, catalog) {
  if (catalog.products[cand.product_id] || catalog.rejected[cand.product_id]) return true;
  const candAppId = cand.app_store_id || extractAppIdFromUrl(cand.app_store_url);
  const candDom = registrableDomain(cand.web_url);
  const sharedHosts = new Set(["", "apple.com", "google.com", "producthunt.com", "ycombinator.com"]);
  const cleanDom = sharedHosts.has(candDom) ? "" : candDom;

  for (const entry of Object.values(catalog.products)) {
    if (candAppId && entry.app_store_id && candAppId === entry.app_store_id) return true;
    if (cleanDom && entry.domain && cleanDom === entry.domain) return true;
    if (companyToken(entry.product?.name) === companyToken(cand.name) && entry.product?.channel === cand.channel) {
      return true;
    }
  }
  for (const rej of Object.values(catalog.rejected)) {
    if (candAppId && rej.app_store_id && candAppId === rej.app_store_id) return true;
    if (cleanDom && rej.domain && cleanDom === rej.domain) return true;
  }
  return false;
}

export async function runSocialRadarPatrol(
  env,
  {
    fetchImpl = globalThis.fetch,
    nowMs = Date.now(),
    trigger = "cron",
    regions = CHART_REGIONS,
  } = {}
) {
  const mediaBucket = env?.MEDIA;
  if (!mediaBucket) {
    throw new Error("未绑定 R2 存储桶 MEDIA（vc-radar-media）");
  }
  const apiKey = String(env?.DEEPSEEK_API_KEY || "").trim();
  if (!apiKey) {
    throw new Error("未配置 DEEPSEEK_API_KEY 密钥");
  }

  const maxEnrich = Math.max(1, Math.min(8, Number(env?.MAX_ENRICH_PER_RUN || 4)));
  const maxProducts = Math.max(12, Math.min(48, Number(env?.SOCIAL_PRODUCT_LIMIT || 36)));
  const maxSubrequests = Math.max(45, Math.min(800, Number(env?.MAX_SUBREQUESTS || 400)));
  const model = String(env?.DEEPSEEK_MODEL || "deepseek-flash").trim() || "deepseek-flash";
  const net = createBudgetedFetch(fetchImpl, maxSubrequests);
  const { today, publishedAt, isoBj } = beijingStamp(nowMs);

  const { baseData, catalog } = await loadOrBootstrapCatalog(mediaBucket, isoBj);

  // 1. 三路信源并行/顺序发现
  const [{ candidates: chartCands, lookupMeta }, commCands, vcCands] = await Promise.all([
    discoverAppStoreCharts(net, { nowMs, regions }),
    discoverCommunities(net),
    discoverVcSocialDeals(net, apiKey, model, nowMs),
  ]);

  // 顺手免费刷新已在库 iOS 产品的最新评分人数，并把新发现的早期融资报道回填到已在库的自然冲榜产品上
  let metadataRefreshed = 0;
  for (const entry of Object.values(catalog.products)) {
    if (entry.app_store_id && lookupMeta.has(entry.app_store_id)) {
      const { meta } = lookupMeta.get(entry.app_store_id);
      if (typeof meta.averageUserRating === "number") {
        entry.product.rating_avg = Math.round(meta.averageUserRating * 100) / 100;
        entry.product.rating_count = Math.trunc(meta.userRatingCount || 0);
        metadataRefreshed += 1;
      }
    }
  }
  const sharedDealHosts = new Set(["", "apple.com", "google.com", "producthunt.com", "ycombinator.com"]);
  const unverifiedFundingVals = new Set(["", "未公开披露融资", "早期独立产品", "未公开披露"]);
  for (const deal of vcCands) {
    const dealDom = registrableDomain(deal.web_url);
    const cleanDealDom = sharedDealHosts.has(dealDom) ? "" : dealDom;
    const dealTok = companyToken(deal.name);
    for (const entry of Object.values(catalog.products)) {
      const p = entry?.product;
      if (!p || p.origin === "bigtech" || p.has_vc || p.channel === "vc") continue;
      if (!unverifiedFundingVals.has(String(p.funding || "").trim())) continue;
      const domMatched = cleanDealDom && entry.domain && cleanDealDom === entry.domain;
      const nameMatched =
        dealTok.length >= 4 &&
        companyToken(p.name) === dealTok &&
        (domMatched || (deal.company_name && nameMatches(deal.company_name, `${p.company} ${p.name}`)));
      if (domMatched || nameMatched) {
        p.funding = sanitizeVernacular(deal.funding_text) || "早期轮次";
        p.investors = sanitizeVernacular(deal.investors_text) || "早期创投支持";
        p.has_vc = true;
        metadataRefreshed += 1;
        break;
      }
    }
  }

  const mergedAll = mergeCandidates(vcCands, chartCands, commCands);
  const unseenAll = mergedAll.filter((c) => !isAlreadySeen(c, catalog));

  // 均衡挑选新候选（兼顾早期融资与多国冲榜）
  const unseenVc = unseenAll.filter((c) => c.channel === "vc").slice(0, Math.ceil(maxEnrich / 2));
  const unseenChart = unseenAll.filter((c) => c.channel !== "vc").slice(0, maxEnrich - unseenVc.length);
  const pickedIds = new Set([...unseenVc, ...unseenChart].map((c) => c.product_id));
  const remainder = unseenAll.filter((c) => !pickedIds.has(c.product_id)).slice(0, maxEnrich - pickedIds.size);
  const todo = [...unseenVc, ...unseenChart, ...remainder].slice(0, maxEnrich);

  const admittedNew = [];
  let rejectedNew = 0;
  let failedCount = 0;
  let processed = 0;

  for (const cand of todo) {
    if (net.remaining() < 12) break;
    processed += 1;
    try {
      const res = await enrichCandidate(cand, { net, apiKey, model, mediaBucket, isoBj });
      if (!res) {
        failedCount += 1;
        continue;
      }
      if (res.admitted) {
        catalog.products[res.product.id] = {
          priority_score: res.priority_score,
          updated_at: res.updated_at,
          app_store_id: res.app_store_id,
          domain: res.domain,
          product: res.product,
        };
        admittedNew.push(res.product);
      } else {
        catalog.rejected[res.product_id] = {
          name: res.name,
          app_store_id: res.app_store_id,
          domain: res.domain,
          reason: res.reject_reason,
          evaluated_at: res.evaluated_at,
        };
        rejectedNew += 1;
      }
    } catch {
      failedCount += 1;
    }
  }

  // 排序并生成最新线上 `data.json`（新收录或评分更新或首次落盘时写入 R2）
  const sortedEntries = Object.values(catalog.products).sort(
    (a, b) =>
      String(b.updated_at || "").localeCompare(String(a.updated_at || "")) * 0 +
      Number(b.priority_score || 0) - Number(a.priority_score || 0)
  );
  // 让最新本轮收录的产品排在前面容易一眼看到，其余按 priority_score 排
  const newIdSet = new Set(admittedNew.map((p) => p.id));
  sortedEntries.sort((a, b) => {
    const aNew = newIdSet.has(a.product?.id) ? 1 : 0;
    const bNew = newIdSet.has(b.product?.id) ? 1 : 0;
    if (aNew !== bNew) return bNew - aNew;
    return Number(b.priority_score || 0) - Number(a.priority_score || 0);
  });

  let finalProducts = sortedEntries.slice(0, maxProducts).map((e) => e.product);
  const nextData = {
    ...baseData,
    meta: {
      ...(baseData.meta || {}),
      published_at: publishedAt,
      today,
    },
    products: finalProducts,
  };

  while (
    finalProducts.length > 8 &&
    new TextEncoder().encode(JSON.stringify(nextData)).length > MAX_DATA_JSON_BYTES
  ) {
    finalProducts = finalProducts.slice(0, -1);
    nextData.products = finalProducts;
  }

  catalog.updatedAt = isoBj;
  await writeR2Json(mediaBucket, R2_CATALOG_KEY, catalog);
  await writeR2Json(mediaBucket, R2_DATA_KEY, nextData);

  const report = {
    ok: true,
    trigger,
    runAt: isoBj,
    discovered: mergedAll.length,
    unseenCount: unseenAll.length,
    processed,
    admittedNew,
    rejectedNew,
    failedCount,
    metadataRefreshed,
    totalAdmitted: Object.keys(catalog.products).length,
    subrequestsUsed: net.used(),
    sourceCounts: {
      chart: chartCands.length,
      community: commCands.length,
      vc: vcCands.length,
    },
  };

  return report;
}
