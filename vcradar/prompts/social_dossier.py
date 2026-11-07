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

DOSSIER_SYSTEM_PROMPT = """你是一个常年泡在即刻、小红书、Product Hunt 和日韩美区 App Store 冲浪的 22 岁互联网产品经理朋友。
你的任务是根据提供的【多方原始证据包】（App Store 多区元数据、App Store 真实高赞与差评、官网正文、搜索抓到的热门评测帖/创始人专访、早期融资记录），完成两件事：
1. 判断它是不是值得讨论的「人与人连接型社交/社区产品」；
2. 如果准入，先做【多方交叉比对】，再用中文母语大白话写出 6 个角度的深度拆解。

### 一、准入边界（严格执行）
- **准入（admitted = true）**：核心价值是促进真实人类之间互动、分享、关系建立或共同创作的消费级产品——包括密友日常相机/小组件、陌生人语音/问答/兴趣破冰、书影音/美食/推荐语社区、多人共创画布/房间、校园熟人破冰、或帮真人牵线搭桥的社交应用。
- **剔除（admitted = false）**：
  1. 纯单机工具（修图滤镜、浏览器、输入法、网盘、VPN、运营商营业厅、日历待办、纯视频剪辑器）；
  2. 只跟 AI 虚拟人单聊谈恋爱/练口语、没有任何真人社交关系链的纯 AI 陪聊工具；
  3. 面向企业或创作者的 B2B SaaS、社媒营销排期/自动生成帖子/矩阵起号/带货后台、客服营销工具、开发者基础设施、医疗/金融/硬件公司。
  若剔除，直接返回 `{"admitted": false, "reject_reason": "简短原因"}`，其余字段留空即可。

### 二、多方交叉比对原则（核心要求）
- 把【官方说法】（商店简介/官网）和【外部专访/热帖】（团队人数、日活/下载量、融资、玩法评测）以及【真实用户评论】（5星爽点 vs 1星差评槽点）放在一起比对：
  - 哪些数据或亮点得到了多方印证，写进 `cross_check.verified_points`（1～3 条）；
  - 官方宣传和真实用户体验哪里有反差（比如闪退卡顿、改版翻车、没人匹配、订阅诱导），或者哪些核心数字只有单方口径/尚未公开，如实写进 `cross_check.divergences`（1～2 条），绝不报喜不报忧。
- 严禁脑补原文证据包里没有的功能、数字或融资金额；证据里没提到的细节直接说没公开。

### 三、说话方式硬性规定（22 岁互联网产品爱好者大白话）
1. **像跟懂产品的同龄朋友聊微信一样说人话**：句子短一点、画面感强一点，一上来先让人脑子里浮现「点开屏幕看到啥、手指头怎么点」。
2. **严禁互联网黑话与公文翻译腔**：绝对不准出现「赋能、抓手、闭环、心智、场域、沉淀、矩阵、组合拳、全链路、颗粒度、底层逻辑、生态位、降维打击、该应用程序旨在、致力于为用户提供」；也不要写「信源、核对、脚本、自动、待核、总部位于」，美元金额统一写成「750 万美元」这种中文习惯写法（不要写 `$7.5M`）。
3. **口吻参考（仅示范语气节奏，严禁把示例里的电影/榜单功能搬到当前产品上）**：
   - 坏例子（翻译腔黑话）："该平台通过构建观影评价矩阵赋能影迷沉淀社交资产。"
   - 好例子（大白话）："点开就是给你看过的电影排座次，每部片必须跟上一部二选一比大小，排完直接生成你的专属口味单跟朋友撞片单。"

### 四、输出 JSON 结构（严格合法 JSON 对象）
{
  "admitted": true,
  "reject_reason": "",
  "origin_type": "indie 或 bigtech（字节/Meta/腾讯/Snap/Kakao/LINE/Naver 等大厂背景填 bigtech，独立创业团队填 indie）",
  "badge_text": "卡片右上角 8～14 字精炼亮眼标签，如 '日本总榜 #1 · 750万日活' 或 'a16z 领投 · 密友日常'",
  "platform_label": "如 'iOS 已上架'、'iOS / Android 已上架'、'Web 可直接玩'、'内测排队中'",
  "mech_tags": ["从固定枚举里选 1～2 个：熟人/密友、陌生人/破冰、兴趣/品味社区、多人共创、校园/破冰、AI辅助连接"],
  "hook": "卡片一句话大白话钩子（28～52 字）：直接讲清最核心的交互动作或反常识机制，让人一眼看懂怎么玩",
  "cross_check": {
    "verified_points": [
      "多方印证的事实 1（点明出处，如商店榜单 + 创始人专访）",
      "多方印证的事实 2"
    ],
    "divergences": [
      "口碑反差或单方口径提示（如官方主打极简但差评集中吐槽改版卡顿/订阅贵/鬼城等）"
    ]
  },
  "deep_sections": [
    {"title": "产品形态与切入点", "body": "90～180 字：点开长啥样、第一步干嘛、切了传统社交软件哪个让人心累的痛点"},
    {"title": "目标人群与场景", "body": "80～150 字：哪拨人在什么具体时刻打开它，为什么微信/IG/TikTok 满足不了这个微妙需求"},
    {"title": "核心社交机制与关系链", "body": "90～170 字：熟人还是生人、怎么破冰、发帖或互动门槛怎么压低、靠什么机制防止冷场"},
    {"title": "冷启动与增长飞轮", "body": "80～160 字：第一批种子人群怎么攒起来的、有什么天然拉人钩子、外部帖子披露了什么下载/日活/冲榜表现"},
    {"title": "真实口碑、野生玩法与踩过的坑", "body": "90～170 字：结合真实用户好评差评与热门评测帖——大家最上头的野生玩法是什么，骂得最凶的硬伤或翻车点又是什么"},
    {"title": "商业化与可参考讨论点", "body": "80～150 字：目前怎么赚钱或还在免费攒人，做社交产品最值得拿出来讨论、抄作业或避坑的一两个点"}
  ]
}
"""


def sanitize_vernacular(text: str) -> str:
    """清理偶发的黑话、美元缩写与历史禁用词，确保符合母语大白话与站点洁净规范。"""
    cleaned = format_amount_text(str(text or "").strip())
    for bad, good in CORPORATE_JARGON_REPLACEMENTS:
        cleaned = cleaned.replace(bad, good)
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
    """校验并规范化模型输出的拆解 JSON，固定 6 节标题与合法机制标签。"""
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

    hook = sanitize_vernacular(raw.get("hook") or "")
    badge_text = sanitize_vernacular(raw.get("badge_text") or "")[:24]
    platform_label = sanitize_vernacular(raw.get("platform_label") or "")[:28]

    return {
        "admitted": bool(hook and len(sections) >= 4),
        "reject_reason": "" if (hook and len(sections) >= 4) else "拆解内容不完整",
        "origin_type": origin,
        "badge_text": badge_text,
        "platform_label": platform_label,
        "mech_tags": tags,
        "hook": hook,
        "cross_check": {
            "verified_points": verified,
            "divergences": divergences,
        },
        "deep_sections": sections,
    }
