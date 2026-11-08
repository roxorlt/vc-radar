"""社交/社群产品准入筛选、多方交叉比对与 22 岁产品爱好者大白话拆解提示词。"""
from __future__ import annotations

import re

from ..fx import format_amount_text
from ..wording import BANNED_OLD_WORDS, display_text

ALLOWED_MECH_TAGS: tuple[str, ...] = (
    "熟人/密友",
    "陌生人/破冰",
    "兴趣/品味社区",
    "多人共创",
    "校园/破冰",
    "AI辅助连接",
)

SECTION_TITLES: tuple[str, ...] = (
    "产品形态与切入点",
    "目标人群与场景",
    "核心社交机制与关系链",
    "冷启动与增长飞轮",
    "真实口碑、野生玩法与踩过的坑",
    "商业化与可参考讨论点",
)

CORPORATE_JARGON_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("赋能", "帮"),
    ("抓手", "切入点"),
    ("闭环", "跑通"),
    ("心智", "印象"),
    ("场域", "地方"),
    ("沉淀", "攒下"),
    ("矩阵", "一组"),
    ("组合拳", "一套打法"),
    ("全链路", "全流程"),
    ("颗粒度", "细节"),
    ("底层逻辑", "核心原因"),
    ("生态位", "位置"),
    ("降维打击", "直接碾压"),
    ("双轮驱动", "两头一起推"),
    ("价值锚点", "核心卖点"),
    ("信源", "来源"),
    ("核对", "比对"),
    ("待核", "待确认"),
    ("自动", "直接"),
    ("脚本", "程序"),
    ("总部位于", "团队在"),
    ("总部在", "团队在"),
    ("总部设在", "团队在"),
)

DOSSIER_SYSTEM_PROMPT = """你是一个常年泡在即刻、小红书、海外新品榜和日韩美区苹果商店冲浪的 22 岁互联网产品经理朋友。
你的任务是根据提供的【多方原始证据包】（苹果商店多区元数据、真实高赞与差评、官网正文、搜索抓到的热门评测帖/创始人专访、早期融资记录），完成两件事：
1. 判断它是不是值得讨论的「人与人连接型社交/社区产品」；
2. 如果准入，先做【多方交叉比对与融资核实】，再用 100% 纯正简体中文母语大白话写出 6 个角度的深度拆解，并把外部参考报道标题全部翻译成中文。

### 一、准入边界（严格执行）
- **准入（admitted = true）**：核心价值是促进真实人类之间互动、分享、关系建立或共同创作的消费级产品——包括密友日常相机/小组件、陌生人语音/问答/兴趣破冰、书影音/美食/推荐语社区、多人共创画布/房间、校园熟人破冰、或帮真人牵线搭桥的社交应用。
- **剔除（admitted = false）**：
  1. 纯单机工具（修图滤镜、浏览器、输入法、网盘、VPN、运营商营业厅、日历待办、纯视频剪辑器）；
  2. 只跟 AI 虚拟人单聊谈恋爱/练口语、没有任何真人社交关系链的纯 AI 陪聊工具；
  3. 面向企业或创作者的 B2B SaaS、社媒营销排期/矩阵起号/带货后台、客服营销工具、开发者基础设施、医疗/金融/硬件公司。
  若剔除，直接返回 `{"admitted": false, "reject_reason": "简短中文原因"}`，其余字段留空即可。

### 二、多方交叉比对与融资核实（核心要求）
- **融资回填核实**：仔细检查外部专访、新闻稿与报道中是否明确提到了**当前这款社交产品或其同一开发商主体**的融资轮次、加速器孵化或出资机构（严禁把同名其他 B2B/广告/电商公司的融资张冠李戴到当前 App 上！）：
  - 若有明确实锤，用纯中文填入 `verified_funding`（如 `"种子轮 · 金额未公开"`、`"种子前轮 · 2000 万美元"`、`"加速器孵化 · 早期轮次"`）和 `verified_investors`（如 `"ZVC、DG 大和创投参投"`）；
  - 若证据包完全未提及融资，`verified_funding` 与 `verified_investors` 必须填空字符串 `""`，严禁脑补。
- **交叉比对**：把【官方说法】、【外部专访/热帖】和【真实用户评论】放在一起比对：
  - 哪些数据或亮点得到了多方印证，写进 `cross_check.verified_points`（1～3 条）；
  - 官方宣传和真实用户体验哪里有反差，或者哪些核心数字只有单方口径/尚未公开，如实写进 `cross_check.divergences`（1～2 条），绝不报喜不报忧。

### 三、说话方式与中文硬性规定（App 名称保留原样，引用文章标题与正文译为中文）
1. **App 名称保留原样，正文与引用文章标题全部译为简体中文**：产品本身的 App 名字（如 Setlog、PicPet、Wabi、ビデオ通話 ソラ）保持原样不要强行意译；但拆解正文与外部参考文章标题（`translated_refs`）必须译成自然通顺的简体中文（标题里提到该 App 时同样保留原名）！原文里的功能名、引语、行业词请意译成自然中文（如 `waitlist` 写「候补名单」、`early access` 写「抢先体验」、`streak` 写「连续打卡」、`photo dump` 写「日常随手拍」、`Pre-Seed` 写「种子前轮」、`推し度` 写「真爱粉程度」、`イントロクイズ` 写「前奏猜歌」、`no ads` 写「不投广告」）。
2. **像跟懂产品的同龄朋友聊微信一样说人话**：句子短一点、画面感强一点，一上来先让人脑子里浮现「点开屏幕看到啥、手指头怎么点」。
3. **严禁互联网黑话与公文翻译腔**：绝对不准出现「赋能、抓手、闭环、心智、场域、沉淀、矩阵、组合拳、全链路、颗粒度、底层逻辑、生态位、降维打击、该应用程序旨在、致力于为用户提供」；也不要写「信源、核对、脚本、自动、待核、总部位于」，美元金额统一写成「750 万美元」这种中文习惯写法（不要写 `$7.5M`）。

### 四、输出 JSON 结构（严格合法 JSON 对象）
{
  "admitted": true,
  "reject_reason": "",
  "origin_type": "indie 或 bigtech（字节/Meta/腾讯/Snap/Kakao/LINE/Naver 等大厂背景填 bigtech，独立创业团队填 indie）",
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
      "title_zh": "将原外文文章标题完整译为通顺的简体中文标题（标题里提及的 App 名称保留原名不要翻译）"
    }
  ]
}
"""


def sanitize_vernacular(text: str) -> str:
    """清理偶发的黑话、外文术语残留、日韩假名谚文、美元缩写与历史禁用词，确保符合母语大白话规范。"""
    raw_str = str(text or "").strip()
    if not raw_str:
        return ""
    cleaned = format_amount_text(raw_str)
    for bad, good in CORPORATE_JARGON_REPLACEMENTS:
        cleaned = cleaned.replace(bad, good)
    cleaned = re.sub(r"\bpre-seed\b", "种子前轮", cleaned, flags=re.I)
    cleaned = re.sub(r"\bseries\s+a\b", "A 轮", cleaned, flags=re.I)
    cleaned = re.sub(r"\bwaitlist\b", "候补名单", cleaned, flags=re.I)
    cleaned = re.sub(r"\bearly\s+access\b", "抢先体验", cleaned, flags=re.I)
    cleaned = re.sub(r"\bphoto\s+dump\b", "日常随手拍", cleaned, flags=re.I)
    cleaned = re.sub(r"\bstreak\b", "连续打卡", cleaned, flags=re.I)
    cleaned = re.sub(r"\bbug\s+fixes\b", "常规问题修复", cleaned, flags=re.I)
    cleaned = re.sub(r"Hacker\s+News\s+Show\s+HN", "极客社区首发", cleaned, flags=re.I)
    cleaned = re.sub(r"Show\s+HN\s*社区首发", "极客社区首发", cleaned, flags=re.I)
    cleaned = re.sub(r"Product\s+Hunt\s*每日新品", "海外新品社区榜单", cleaned, flags=re.I)
    cleaned = re.sub(r"Product\s+Hunt\s*首发", "新品社区首发", cleaned, flags=re.I)
    cleaned = re.sub(r"[\u3040-\u30ff\uac00-\ud7af]+", "", cleaned)
    cleaned = display_text(cleaned)
    cleaned = re.sub(r"美国(?:早期|公司|天使轮)", "北美地区", cleaned)
    cleaned = cleaned.replace("非美国", "海外其他地区")
    # 兜底清理仍可能触发 BANNED_OLD_WORDS 的词条
    for word in BANNED_OLD_WORDS:
        if word.startswith(r"\$"):
            continue
        if word in cleaned:
            cleaned = cleaned.replace(word, "")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def normalize_dossier_payload(raw: dict) -> dict:
    """校验并规范化模型输出的拆解 JSON，固定 6 节标题、融资回填字段与翻译后的外链列表。"""
    if not isinstance(raw, dict) or not raw.get("admitted"):
        return {
            "admitted": False,
            "reject_reason": sanitize_vernacular(raw.get("reject_reason", "非目标社交产品") if isinstance(raw, dict) else "解析失败"),
        }

    origin = "bigtech" if str(raw.get("origin_type", "")).strip().lower() == "bigtech" else "indie"
    tags = [t for t in (raw.get("mech_tags") or []) if isinstance(t, str) and t in ALLOWED_MECH_TAGS][:2]
    if not tags:
        tags = ["兴趣/品味社区"]

    raw_sections = raw.get("deep_sections") if isinstance(raw.get("deep_sections"), list) else []
    by_title = {}
    for idx, item in enumerate(raw_sections):
        if isinstance(item, dict):
            title = str(item.get("title") or "").strip()
            body = sanitize_vernacular(item.get("body") or item.get("text") or "")
            if body:
                by_title[title] = body
                by_title[f"#{idx}"] = body

    sections = []
    for idx, fixed_title in enumerate(SECTION_TITLES):
        body = by_title.get(fixed_title) or by_title.get(f"#{idx}") or ""
        if fixed_title == "产品形态与切入点" and not body:
            body = by_title.get("产品形态与切口", "")
        if body:
            sections.append({"title": fixed_title, "body": body})

    cross_raw = raw.get("cross_check") if isinstance(raw.get("cross_check"), dict) else {}
    verified = [
        sanitize_vernacular(x)
        for x in (cross_raw.get("verified_points") or [])
        if isinstance(x, str) and x.strip()
    ][:3]
    divergences = [
        sanitize_vernacular(x)
        for x in (cross_raw.get("divergences") or [])
        if isinstance(x, str) and x.strip()
    ][:2]

    translated_refs = []
    for item in (raw.get("translated_refs") or []) if isinstance(raw.get("translated_refs"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].strip():
            src_zh = sanitize_vernacular(item.get("source_zh") or "")[:32]
            tit_zh = sanitize_vernacular(item.get("title_zh") or "")[:110]
            if src_zh or tit_zh:
                translated_refs.append({"url": item["url"].strip(), "source_zh": src_zh, "title_zh": tit_zh})

    hook = sanitize_vernacular(raw.get("hook") or "")
    badge_text = sanitize_vernacular(raw.get("badge_text") or "")[:24]
    platform_label = sanitize_vernacular(raw.get("platform_label") or "")[:28]

    return {
        "admitted": bool(hook and len(sections) >= 4),
        "reject_reason": "" if (hook and len(sections) >= 4) else "拆解内容不完整",
        "origin_type": origin,
        "localized_name": sanitize_vernacular(raw.get("localized_name") or "")[:36],
        "localized_company": sanitize_vernacular(raw.get("localized_company") or "")[:48],
        "verified_funding": sanitize_vernacular(raw.get("verified_funding") or "")[:56],
        "verified_investors": sanitize_vernacular(raw.get("verified_investors") or "")[:72],
        "badge_text": badge_text,
        "platform_label": platform_label,
        "mech_tags": tags,
        "hook": hook,
        "cross_check": {
            "verified_points": verified,
            "divergences": divergences,
        },
        "deep_sections": sections,
        "translated_refs": translated_refs,
    }
