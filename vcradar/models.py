"""跨模块共享的数据类型（接口契约）。只放数据结构，不放逻辑。

改这里要先改 README.md；V16b 已授权 O/P/Q/R，T 增补产品词。
时间一律用带时区的 datetime（UTC 或 Asia/Shanghai）；「北京日期」用 date。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------- 信源
@dataclass(frozen=True)
class SourceDef:
    """source 表一行 = 一个抓取地址（PRD 第 3 节表格、第 8 节 source）。"""
    id: str                    # 稳定 id，例 "tc_venture"
    name: str                  # 信源名（分组字段，推送和状态页按它数），例 "TechCrunch"
    column: str                # 栏目名，例 "venture"
    url: str
    kind: str                  # rss | api | page
    stale_days: Optional[int]  # 过期阈值（美国工作日）；None = 不判过期（YC 目录）
    order: int                 # 抓取顺序；Term Sheet 网页版 = 0（最先抓）
    robots_exception: bool = False  # 只有 Term Sheet 网页版 True


class ErrorType(str, Enum):
    """PRD 9.1 出错类型。值是内部代码，文案见 render.ERROR_TEXT。"""
    RATE_LIMITED = "rate_limited"        # 被限流 / 拒绝访问（429、403）
    UNREACHABLE = "unreachable"          # 打不开（404、5xx、其他非 2xx、连接失败）
    TIMEOUT = "timeout"                  # 30 秒没响应
    EMPTY = "empty"                      # 200 但 0 条
    STALE = "stale"                      # 过期（媒体/接口）
    INTERCEPTED = "intercepted"          # 被拦截
    YC_UNREADABLE = "yc_unreadable"      # YC 目录读不到
    YC_DROP = "yc_drop"                  # YC 目录数量骤减
    NEWSLETTER_NO_DEALS = "newsletter_no_deals"  # 通讯读不到交易段
    NEWSLETTER_STALE = "newsletter_stale"        # 通讯过期
    TS_STOPPED = "ts_stopped"            # Term Sheet 网页版已停抓


class FetchResult(str, Enum):
    """fetch_run.结果"""
    SUCCESS = "成功"
    FAIL = "失败"
    STALE = "过期"
    NOT_RUN = "没轮到"
    STOPPED = "已停抓"


@dataclass
class HttpResponse:
    url: str
    status: Optional[int]          # None = 没拿到响应（超时/连接失败）
    content_type: str = ""
    body: str = ""
    elapsed_s: float = 0.0
    error: Optional[str] = None    # "timeout" | "conn:<msg>" | None
    final_url: str = ""


@dataclass
class RawItem:
    """raw_item：一个信源条目。StrictlyVC / Term Sheet 一期里每笔交易算一个条目。"""
    source_id: str
    external_id: str               # 外部 id（RSS guid/链接；YC objectID；通讯 = 期网址 + "#" + 公司名）
    url: str                       # 来源链接（必须有）
    title: str
    published_at: Optional[datetime]   # 机器可读发布时间（aware）；YC 目录条目为 None（取首次抓到时间）
    excerpt: str = ""              # 摘录（入库，留 180 天）
    body: str = ""                 # 抽取用正文（≤ 8000 字符，内存里用，只把摘录入库）
    original_date_et: Optional[str] = None   # 「来源原文日期（美东）」页面显示的原文日期，例 "2026-10-29"
    structured: dict[str, Any] = field(default_factory=dict)
    # structured 约定键（有才填）：company_name, website, location(YC all_locations 原文),
    # yc_batch, round_text, amount_text, leads(list), follows(list), hq_text,
    # original_link(StrictlyVC 附的原始报道链接), issue_url, kind("single"/"daily"/"weekly"/"monthly" AlleyWatch),
    # launched_at(YC 目录 unix 秒), tags(list), one_liner


@dataclass
class FetchRunRecord:
    """fetch_run 一行。"""
    source_id: str
    started_at: datetime
    ended_at: datetime
    http_code: Optional[int]
    count: int
    result: FetchResult
    error_type: Optional[ErrorType] = None
    error_summary: str = ""
    note: str = ""                 # 说明（Term Sheet 封禁那两条写 HTTP 码或「拦截页」；人工停抓写「人工停抓」）
    latest_at: Optional[datetime] = None
    is_backfill: bool = False
    missed_range: str = ""         # 漏抓时间段（北京时间），有漏抓才填
    pages: int = 1


@dataclass
class SourceState:
    """抓取前从库里读出的这个地址的状态（fetcher 只读）。"""
    source_id: str
    crawl_status: str = "正常"      # 正常 | 已停抓
    last_ok: bool = True           # 上次运行是否成功（False 时本次成功要补抓，最多 3 页）
    known_external_ids: set[str] = field(default_factory=set)  # 已入库条目 id（补抓判断「追上」用）
    known_issue_urls: set[str] = field(default_factory=set)    # 通讯：已抓过的期网址
    yc_last_batch: Optional[str] = None
    yc_last_batch_total: Optional[int] = None
    last_latest_at: Optional[datetime] = None   # 上次成功时最新一条的时间（算漏抓时间段用）


@dataclass
class FetchOutcome:
    """一个地址一次运行的结果。"""
    source: SourceDef
    runs: list[FetchRunRecord]     # 通常 1 条；Term Sheet 封禁重试 2 条
    items: list[RawItem]
    ok: bool                       # 这个地址今天算成功（含重试成功）
    error_type: Optional[ErrorType] = None
    stopped_now: bool = False      # 本次运行刚置停抓（Term Sheet 封禁）
    latest_label: str = ""         # 推送底部 / 状态页用的「最新一条」写法，例 "当期 193 家"、"最新一期 10-29"
    latest_beijing_date: Optional[date] = None
    missed_range: str = ""         # 有漏抓时的北京时间段
    yc_batch: Optional[str] = None
    yc_batch_total: Optional[int] = None


# ---------------------------------------------------------------- 抽取
@dataclass
class LabelDim:
    verdict: Optional[str] = None  # "是" | "否" | "拿不准" | None(没跑)
    quote: str = ""


@dataclass
class Extracted:
    """一条条目的抽取结果（规则 + 模型合并后）。值找不到写 None，渲染时写「未披露」/「未知」。"""
    company_name: Optional[str] = None
    website: Optional[str] = None
    round_text: Optional[str] = None     # 原文轮次写法，例 "Series A"
    amount_text: Optional[str] = None    # 原文金额写法（逐字），例 "$17M"、"$27.5 million"
    leads: list[str] = field(default_factory=list)
    follows: list[str] = field(default_factory=list)
    hq_text: Optional[str] = None        # 原文总部写法，例 "San Francisco"
    hq_quote: str = ""                   # 总部依据原句
    is_fund_raise: bool = False          # 基金募资
    is_debt_only: bool = False
    summary: str = ""                    # 中文简介（7.1）
    one_liner: str = ""                  # 一句话原文
    ai: LabelDim = field(default_factory=LabelDim)
    social: LabelDim = field(default_factory=LabelDim)
    quotes: dict[str, str] = field(default_factory=dict)   # 字段名 -> 原文句子
    complete_by_rules: bool = False      # 公司名、城市、轮次、金额 4 项规则都抽到（7.1「抽全」）
    model_used: Optional[str] = None
    model_calls: int = 0
    model_error: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)    # prompt_tokens / completion_tokens 累计
    product_terms: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 推送
@dataclass
class CardSource:
    name: str                 # 来源名，例 "TechCrunch"
    beijing_date: date
    url: str


@dataclass
class Card:
    company_id: str
    name: str
    marks: list[str]          # 按「新一轮、大额、补抓、假期补发、金额有分歧」顺序
    round_label: str          # "天使轮" | "种子轮" | "A 轮"
    amount_label: str         # "1700 万美元" | "金额未披露" | 各来源的中文金额
    lead_label: str           # "Lux Capital、Index Ventures 共同领投" | "Y Combinator" | "领投未披露"
    tag_label: str            # "AI相关" | "AI相关 + 社交/社群相关" | "都不是" | "标签待核" | "AI相关 + 标签待核"
    group: str                # "AI+社交" | "社交" | "AI" | "都不是" | "标签待核"
    summary: str
    sources: list[CardSource]
    amount_usd: Optional[float] = None   # 排序用
    event_date: Optional[date] = None
    country: str = "国家待确认"
    amount_status: str = ""   # 披露 | 金额待核 | 币种待确认 | 暂未换算 | 未披露 | 金额待确认


@dataclass
class PendingLine:
    """轮次待核名单行；国家待确认的公司照常出卡片，不另列名单。"""
    company_id: str
    name: str
    detail: str
    reason: str


@dataclass
class SourceStatusLine:
    name: str                  # 信源名
    ok: bool
    stopped: bool
    count_label: str           # "31 条" | "当期 193 家" | "最新一期 10-29"
    error_type: Optional[ErrorType] = None
    latest_beijing_date: Optional[date] = None
    missed_range: str = ""


@dataclass
class PushSection:
    """独立排序和编号的推送补充段，不计入正常推送统计。"""
    key: str
    title: str
    hint: str
    cards: list[Card] = field(default_factory=list)
    overflow_text: str = ""
    preserve_order: bool = False
    overflow_count: int = 0


@dataclass
class DigestData:
    """render.render_push 的全部输入。"""
    push_date: date                       # 北京日期
    window_start: Optional[datetime]      # 本期汇总窗口（上次 push=true 的 generated_at，不含）
    window_end: datetime
    status: str                           # 基线/没跑完/全部失败/部分失败/有新增/没有新增
    resend: bool = False
    baseline_count: int = 0
    cards: list[Card] = field(default_factory=list)
    round_pending: list[PendingLine] = field(default_factory=list)
    filtered: dict[str, int] = field(default_factory=dict)  # 显示原因 -> 家数
    yc_new_batch: Optional[tuple[str, int]] = None          # (批次名, 公司数)
    sources: list[SourceStatusLine] = field(default_factory=list)
    ts_stopped_notice: Optional[str] = None                 # "封禁" | "异议"，只在置位后第一个推送日
    new_evidence_count: int = 0
    new_evidence_since_label: str = ""                      # "昨天" | "上次推送后"
    last_push_summary: Optional[tuple[date, int]] = None    # 上次有新增：(日期, 家数)
    base_url: str = ""
    weekly: bool = True
    extra_sections: list[PushSection] = field(default_factory=list)
    review_site_passed: bool = False
