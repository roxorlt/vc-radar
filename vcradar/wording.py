"""V16e 用户可见用语；内部库值只在显示边界转换。"""
from __future__ import annotations

import re

from .fx import format_amount_text
from .models import ErrorType


PRODUCT_NAME = "早期创投监控"
PUSH_TITLE = "{day} 周{weekday} · 早期融资 · 本周"
MARK_ORDER = ("新一轮", "大额", "补抓", "金额待核")
MARK_LABELS = {"补抓": "补录", "金额待核": "金额说法不一"}
STATUS_LABELS = {"轮次待核": "轮次待确认", "总部待核": "已纳入"}
FETCH_LABELS = {"没轮到": "这次没来得及抓", "已停抓": "已停止抓取"}
STOP_REASONS = {"封禁": "对方封禁", "异议": "对方提出异议", "人工停抓": "人工停止抓取"}
NO_NEW = "本周没有发现新的天使轮、种子轮、A 轮融资。"
BASELINE_HINT = "首次运行收录 {count} 家以前的公司，不算新增"
BASELINE_TOP = "首次运行：收录了 {count} 家以前的公司，不算新增"
INCOMPLETE_HINT = "本周一到 {day} 日的内容没抓全"
MISSED_HINT = "可能有遗漏：{name}，{span}的内容没抓全"
FAILED_DAYS = "本周有 {count} 天没抓到"
STOPPED_HINT = "Term Sheet 已停止抓取：{reason}"
LAST_NEW = "上一次有新公司是 {day} 那期，{count} 家"
EVIDENCE_HINT = "{period} {count} 家资料有出入"
YC_HINT = "YC 新一批 {count} 家"
OVERFLOW = "还有一些没有列出"
OVERFLOW_LINK = "还有一些没有列出，[在往期页看]({url})"
BACKFILL_TITLE = "之前漏掉的公司（一次性补充）"
BACKFILL_HINT = "另补充此前漏掉的 {count} 家"
PAGE_LINKS = "[看本周全部]({week})"
FETCH_WARNING_END = "。本周汇总可能不全，不代表没有新公司。"
PUSH_FAILURE_REASONS = {
    ErrorType.RATE_LIMITED: "对方限制了访问", ErrorType.UNREACHABLE: "页面打不开",
    ErrorType.TIMEOUT: "网站没响应", ErrorType.EMPTY: "页面能打开，但一条都读不出来",
    ErrorType.STALE: "最新一条还是 {date} 的", ErrorType.INTERCEPTED: "被网站拦下来了",
    ErrorType.YC_UNREADABLE: "读不到公司目录", ErrorType.YC_DROP: "公司数量比上次少了一半以上",
    ErrorType.NEWSLETTER_NO_DEALS: "页面能打开，但读不到融资清单",
    ErrorType.NEWSLETTER_STALE: "最新一期还是 {date} 的",
}
DIGEST_LABELS = {"基线": "首次运行", "没跑完": "今天没抓完", "全部失败": "今天所有来源都没抓到",
                 "部分失败": "部分来源网站没抓到", "有新增": "发现新公司", "没有新增": "没有发现新公司"}
RUN_ERRORS = {
    "全部失败": "所有来源都没抓到：今天每个网站都出了问题。" + FETCH_WARNING_END.removeprefix("。"),
    "没跑完": "部分来源没抓到：今天没能按时抓完。" + FETCH_WARNING_END.removeprefix("。"),
}


def warning_subject(name: str) -> str:
    """为英文或数字结尾的报警来源名补一个空格。"""
    name = name.rstrip()
    return name + (" " if re.search(r"[A-Za-z0-9]$", name) else "")


def normalize_warning(text: str) -> str:
    """统一新旧报警行的来源、日期和句间空格。"""
    text = re.sub(r"([A-Za-z0-9\u4e00-\u9fff])[ \t]*没抓到：",
                  lambda m: warning_subject(m[1]) + "没抓到：", text)
    text = re.sub(r"(\d)[ \t]*(的(?:内容没补上|。))", r"\1 \2", text)
    text = re.sub(r"(之前|之后)[ \t]+的内容没补上", r"\1的内容没补上", text)
    return re.sub(r"(?<=不代表没有新公司。)[ \t]+", "", text)


class _WarningTemplate(str):
    def format(self, *args, **kwargs):
        """兼容原模板调用，并按来源名结尾统一报警空格。"""
        return normalize_warning(super().format(*args, **kwargs))


ERROR_TEXT = {key: _WarningTemplate("{X} 没抓到：" + value + FETCH_WARNING_END) for key, value in PUSH_FAILURE_REASONS.items()}
ERROR_TEXT[ErrorType.TS_STOPPED] = "Term Sheet 没抓到：{reason}" + FETCH_WARNING_END
SOURCE_ERRORS = {
    "rate_limited": "对方限制了访问", "unreachable": "页面打不开", "timeout": "30 秒没响应",
    "empty": "页面能打开，但一条都读不出来", "stale": "最新一条太久没更新",
    "intercepted": "被网站拦下来了", "yc_unreadable": "读不到公司目录",
    "yc_drop": "公司目录数量比上次少一半以上", "newsletter_no_deals": "页面能打开，但读不到融资清单",
    "newsletter_stale": "最新一期太久没更新",
}

# 静态页面和服务端共用，JSON 带此字典供页面和离线渲染使用。
SITE_TEXT = {
    "product": PRODUCT_NAME, "week": "本周", "archive": "往期", "back": "‹ 返回往期",
    "product_label": "产品", "publication_unknown": "发布时间未知", "excluded": "不在收录范围",
    "no_new": NO_NEW,
    "round_pending": "轮次待确认", "tag_pending": "标签待确认", "country_pending": "国家待确认",
    "city_pending": "城市待确认", "baseline": "首次运行时已有，未推送", "supplement": "此前漏掉",
    "round": "轮次", "amount": "金额", "tag": "标签", "status": "状态", "all": "全部",
    "admitted": "已纳入", "angel": "天使轮", "seed": "种子轮", "series_a": "A 轮",
    "ai": "AI相关", "social": "社交/社群相关", "neither": "都不是",
    "new_round": "新一轮", "large": "大额", "backfilled": "补录", "disputed": "金额说法不一",
    "missing": "来源里没写", "undisclosed": "未披露", "amount_missing": "金额未披露",
    "lead_missing": "领投未披露", "lead": "领投", "co_lead": "共同领投", "follow": "跟投",
    "unknown": "未知", "detail": "详情", "source_label": "来源：", "filter_period": "推送期",
    "recent": "近 30 天", "issue": "某一期", "select_date": "指定某一期", "push": "推送",
    "no_matches": "没有符合条件的公司", "beijing": "北京时间",
    "page_no_date": "页面没写日期", "read_on": "读取",
    "original_date": "原文日期（美国东部时间）", "original_round": "原文轮次",
    "country_basis": "国家和依据", "date": "日期", "first_seen": "首次发现", "push_date": "推送日期",
    "key_fields": "关键字段", "current_round": "本轮", "description": "简介", "all_sources": "全部来源", "history": "融资历史",
    "conflicts": "各来源说法不一", "evidence": "资料有出入：",
    "deep": "深度解读", "deep_pending": "解读生成中", "deep_absent": "暂时没有深度解读",
    "deep_failed": "深度解读生成失败", "point": "要点", "quote": "原句",
    "no_company": "没有找到这家公司", "failed": "失败", "absent": "暂时没有", "skipped": "不做",
    "loading_detail": "正在读取公司详情…",
    "detail_failed": "暂时无法读取公司详情，请稍后重试", "retry": "重试",
    "load_failed": "暂时无法读取，请稍后刷新", "company_detail": "公司详情", "yc": "YC 新一批名单",
    "loading": "正在读取…", "no_script": "请开启浏览器的 JavaScript 功能查看页面。",
    "nav": "页面", "round_missing": "轮次未披露",
    "supplement_unsent": "补充未推送", "view_all": "查看全部",
    "verification": "入库查证", "verified": "入库时已查证",
    "not_verified": "尚未查，下次运行再查", "verification_omitted": "没有做入库查证",
    "verify_count": "已查过 {count} 次，仍无法确认", "verify_unconfirmed": "仍无法确认",
    "verify_sources": "入库查证用到的来源",
    "verify_identity_missing": "仍无法确认：来源里没有能区分同名公司的信息",
    "amount_corrected": "金额已按来源原文更正", "amount_not_written": "来源里没有写明金额",
    "homepage": "官网",
}

BANNED_OLD_WORDS = frozenset((
    "总部待核", "总部非美国", "总部和依据", "总部依据", "轮次待核", "标签待核", "金额待核",
    "作为基线", "基线", "部分未能补全", "抓取未跑完", "信源全部失败", "抓取失败", "有漏抓", "漏抓",
    "补抓", "有新证据", "新证据", "质量待核", "没轮到", "美东", "已停抓", "停抓", "信源", "回看",
    "见回看页", "YC 新批次", "批次超过 12 个月", "轮次不在范围", "字段分歧", "美国早期融资",
    "美国早期创投监控", "没有新增的美国", "上次有新增", "不代表没有新增", "待核",
    r"\$\d[\d.,]*\s*[KMB]\b",
    "脚本", "自动", "核对", "弱证据", "官网未知",
    "总部位于", "总部在", "总部设在",
))


def display_text(text: str) -> str:
    """转换固定文案和内部标签，不用于来源原句或公司正文。"""
    if text == "总部":
        return "所在国家"
    for old, new in (("总部和依据", "国家和依据"), ("总部依据", "国家依据"),
                     ("总部所在地", "所在地"),
                     ("总部待核", "国家待确认"), ("轮次待核", "轮次待确认"),
                     ("标签待核", "标签待确认"), ("金额待核", "金额说法不一"),
                     ("补抓", "补录"), ("有新证据", "资料有出入"), ("新证据", "资料有出入"),
                     ("质量待核", "内容待复查"), ("这次没轮到", "这次没来得及抓"),
                     ("没轮到", "这次没来得及抓"), ("已停抓", "已停止抓取"), ("停抓", "停止抓取"),
                     ("字段分歧", "各来源说法不一"), ("批次超过 12 个月", "YC 批次超过一年"),
                     ("轮次不在范围", "B 轮及以后"), ("轮次超范围", "B 轮及以后"),
                     ("信源状态页", "来源网站状态"), ("信源状态", "来源网站状态"),
                     ("信源", "来源网站"), ("回看页", "往期"), ("回看", "往期"),
                     ("待核", "待确认")):
        text = text.replace(old, new)
    return text


def filter_reason(reason: str, *, round_text: str = "", extractions=()) -> str:
    """按轮次与抽取事实给出过滤原因；历史国家原因隐藏。"""
    if reason == "总部非美国":
        return ""
    if reason in {"轮次不在范围", "轮次超范围"}:
        if any(e.get("is_fund_raise") for e in extractions):
            return "基金募资"
        if any(e.get("is_debt_only") for e in extractions):
            return "只有债务融资"
        if re.search(r"\bfund(?:raising|\s+(?:i{1,3}|iv|v|\d+))?\b|基金", round_text, re.I):
            return "基金募资"
        if re.search(r"debt|债务", round_text, re.I):
            return "只有债务融资"
        return "B 轮及以后"
    return display_text(reason)


def legacy_to_display(text: str) -> str:
    """兼容旧 digest 文本，显示时转换，保留原数据库和金额快照。"""
    text = re.sub(r"另有\s*\d+\s*家总部待核\s*(?:·\s*)?", "", text)
    text = re.sub(r"(?m)[ \t]*·[ \t]*(?=。?$)", "", text)
    text = re.sub(r"已收录\s*(\d+)\s*家作为基线", lambda m: BASELINE_HINT.format(count=m[1]), text)
    text = re.sub(r"首次运行：" + re.escape("首次运行收录 ") + r"(\d+) 家以前的公司，不算新增",
                  lambda m: BASELINE_TOP.format(count=m[1]), text)
    text = re.sub(r"本周一至\s*(.+?)\s*日部分未能补全", lambda m: INCOMPLETE_HINT.format(day=m[1]), text)
    for state, old in (("全部失败", "今天信源全部失败"), ("没跑完", "今天抓取未跑完")):
        text = re.sub(re.escape(old) + r"：[^\n]*?恢复后补抓的进下一期。", RUN_ERRORS[state], text)
        text = text.replace(old, "今天所有来源都没抓到" if state == "全部失败" else "今天没抓完")
    text = re.sub(r"有漏抓：([^，,\n·]+)[，,]\s*([^\n·]+?)(?=\s*·|\n|$)",
                  lambda m: MISSED_HINT.format(name=m[1].strip(), span=m[2].strip().rstrip("。")), text)
    text = re.sub(r"上次有新增：\s*(\d{2}-\d{2})\s*那期，\s*(\d+)\s*家",
                  lambda m: LAST_NEW.format(day=m[1], count=m[2]), text)
    text = text.replace("美国早期创投监控", PRODUCT_NAME).replace("美国早期融资", "早期融资")
    text = re.sub(r"本周没有新增的(?:美国)?天使轮、种子轮、A 轮公司。", NO_NEW, text)
    text = text.replace("不代表没有新增", "不代表没有新公司").replace("抓取失败", "没抓到")
    text = text.replace("来源原文日期（美东）", SITE_TEXT["original_date"])
    text = text.replace("YC 新批次", "YC 新一批").replace("见回看页", "名单见网页")
    text = re.sub(r"已接入信源\s*(\d+)\s*个", r"已接入 \1 个来源网站", text)
    text = re.sub(r"(?<=：)封禁|(?<=：)异议", lambda m: STOP_REASONS[m[0]], text)
    text = display_text(text)
    text = re.sub(r"。(?=另有 |已过滤 )", " · ", text)
    text = re.sub(r"(?m)^((?:本周|上次推送后)共 [^\n]+?)。$", r"\1", text)
    return format_amount_text(text) if text.strip() else ""
