"""扫变更：入库、轮次归类、国家标注、过滤原因、去重合并、基线和推送窗口。PRD 第 6 节。"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .cal import bj_date, is_cn_workday, is_push_day
from . import fx, outbox, placenames
from .config import BJ
from .db import sync_sources, first_issue_generated_at
from .fetcher import has_missed, missed_interval
from .labels import decide_dim, keyword_hit, overall
from .models import Card, CardSource, Extracted, FetchOutcome, LabelDim, PendingLine, RawItem
from .sources import registry
from .sources.yc_directory import batch_in_window

MARK_ORDER = ("新一轮", "大额", "补抓", "金额待核")
FILTER_ORDER = ("批次超过 12 个月", "轮次不在范围")
COUNTRY_PENDING = "国家待确认"
_UNKNOWN = {"", "unknown", "undisclosed", "not disclosed", "未披露", "未知", "没写", "n/a", "none"}
_MULTIPART_SUFFIXES = {
    "co.uk", "org.uk", "me.uk", "ac.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp",
    "co.kr", "co.in", "com.br", "com.cn", "com.hk", "com.sg", "com.tw", "co.il", "co.za",
    "com.mx", "com.tr", "com.ar", "com.my", "com.ph", "co.id", "co.th", "com.vn",
    # 常见托管平台的租户有独立站点身份，不能把整个托管平台合成一家公司。
    "github.io", "pages.dev", "vercel.app", "netlify.app", "appspot.com",
}
_LOG = logging.getLogger(__name__)
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_locations():
    try:
        names = json.loads((_CONFIG_DIR / "country-names.json").read_text(encoding="utf-8"))
        cities = json.loads((_CONFIG_DIR / "city-country.json").read_text(encoding="utf-8"))
        countries, states = names["countries"], names["us_states"]
        if len(countries) != 249 or not states or not cities:
            raise ValueError("国家、州或城市表不完整")
        for code, entry in countries.items():
            if not re.fullmatch(r"[A-Z]{2}", code) or not entry["name"] or not entry["aliases"]:
                raise ValueError("国家表字段无效")
            if not all(isinstance(alias, str) and alias for alias in entry["aliases"]):
                raise ValueError("国家别名无效")
        allowed = {entry["name"] for entry in countries.values()} | {"有歧义"}
        for city, entry in cities.items():
            if isinstance(entry, str):
                valid = entry in allowed
            else:
                valid = (isinstance(entry, dict) and entry.get("default") in allowed
                         and isinstance(entry.get("when"), list))
                if valid:
                    for rule in entry["when"]:
                        valid = (valid and rule.get("country") in allowed and
                                 (rule.get("terms") or rule.get("pattern") or rule.get("us_city")))
                        if rule.get("pattern"):
                            re.compile(rule["pattern"])
            if not city or not valid:
                raise ValueError("城市表国家无效")
        if any(not re.fullmatch(r"[A-Z]{2}", code) or not aliases
               or not all(isinstance(alias, str) and alias for alias in aliases) for code, aliases in states.items()):
            raise ValueError("州表无效")
        if any(state not in states for state in names["us_city_states"].values()):
            raise ValueError("城市州表无效")
        known_cities = {city.casefold() for city in cities}
        if any(alias not in known_cities or canonical not in known_cities
               for alias, canonical in names["city_aliases"].items()):
            raise ValueError("城市别名无效")
        return names, cities
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise RuntimeError("国家或城市配置缺失或损坏，无法启动") from exc


_LOCATION_CONFIG, _CITIES = _load_locations()
_COUNTRY_ALIASES = {entry["name"]: tuple(entry["aliases"]) for entry in _LOCATION_CONFIG["countries"].values()}
_COUNTRY_BY_ALIAS = {unicodedata.normalize("NFKC", alias).casefold(): country for country, aliases in _COUNTRY_ALIASES.items()
                     for alias in (country, *aliases)}
_CONDITIONAL_CITIES = {city.casefold(): entry for city, entry in _CITIES.items() if isinstance(entry, dict)}
_COUNTRY_BY_CITY = {unicodedata.normalize("NFKC", city).casefold(): entry if isinstance(entry, str) else entry["default"]
                    for city, entry in _CITIES.items()}
_US_CODES = tuple(_LOCATION_CONFIG["us_states"])
_US_STATES = tuple(aliases[0] for aliases in _LOCATION_CONFIG["us_states"].values())
_STATE_BY_ALIAS = {alias.casefold(): code for code, aliases in _LOCATION_CONFIG["us_states"].items() for alias in aliases}
_US_CITIES = tuple(city for city, country in _CITIES.items() if country == "美国")
_CITY_STATES = _LOCATION_CONFIG["us_city_states"]
_CITY_ALIASES = _LOCATION_CONFIG["city_aliases"]


def _pattern(terms) -> re.Pattern:
    alternatives = []
    for term in sorted(terms, key=len, reverse=True):
        term = unicodedata.normalize("NFKC", term)
        escaped = re.escape(term)
        if term == "US":
            escaped = "(?-i:US)"
        # 中文地点可以紧接「在」等汉字，英文需要词边界。
        alternatives.append(escaped if re.search(r"[\u4e00-\u9fff]", term)
                            else r"(?<!\w)" + escaped + r"(?!\w)")
    return re.compile("|".join(alternatives), re.I)


_COUNTRY_PATTERN = _pattern(alias for aliases in _COUNTRY_ALIASES.values() for alias in aliases)
_CHINESE_COUNTRY_PATTERN = _pattern(_COUNTRY_ALIASES)
_US_CITY_PATTERN = _pattern(_US_CITIES)
_US_STATE_PATTERN = _pattern(_STATE_BY_ALIAS)
_US_CODE_PATTERN = re.compile(r"(?<!\w)(?:" + "|".join(_US_CODES) + r")(?!\w)")
_US_COUNTRY_PATTERN = _pattern(("美国", *_COUNTRY_ALIASES["美国"]))
_FOREIGN_COUNTRY_PATTERN = _pattern(alias for country, aliases in _COUNTRY_ALIASES.items()
                                    if country != "美国" for alias in (country, *aliases))
_CITY_PATTERN = _pattern(city for city, entry in _CITIES.items()
                         if not isinstance(entry, dict) or entry.get("kind") != "region")
_CONDITIONAL_PATTERN = _pattern(_CONDITIONAL_CITIES)
_FOREIGN_PATTERN = _pattern(city for city, country in _COUNTRY_BY_CITY.items() if country not in {"美国", "有歧义"})
_LOCATION_TERM = "|".join(re.escape(term) for term in sorted(
    {*_COUNTRY_BY_ALIAS, *_CITIES, *_STATE_BY_ALIAS}, key=len, reverse=True)) + r"|(?-i:" + "|".join(_US_CODES) + ")"
_LOCATION_PATTERN = re.compile(r"(?<!\w)(?:" + _LOCATION_TERM + r")(?:\s*,\s*(?:" + _LOCATION_TERM + r")){0,3}(?!\w)", re.I)


@contextmanager
def country_log(write):
    """将未知城市同时记入本次运行的 JSON 日志，退出后移除处理器。"""
    seen = set()
    class Handler(logging.Handler):
        def emit(self, record):
            if hasattr(record, "city"):
                identity = record.city, record.company_name
                if identity not in seen:
                    seen.add(identity)
                    write({"event": "country_city_unknown", "city": record.city, "company_name": record.company_name})
    handler = Handler()
    _LOG.addHandler(handler)
    try:
        yield
    finally:
        _LOG.removeHandler(handler)


def _present(value: Optional[str]) -> bool:
    return bool(value and value.strip().casefold() not in _UNKNOWN)


def _is_x(item: RawItem) -> bool:
    sid = item.source_id.casefold()
    return sid == "x" or sid.startswith(("x_", "twitter")) or registrable_domain(item.url) in {"x.com", "twitter.com"}


def _is_yc_directory(item: RawItem) -> bool:
    sid = item.source_id.casefold()
    return (sid.startswith("yc") and any(part in sid for part in ("director", "compan", "_dir"))) or (
        registrable_domain(item.url) == "ycombinator.com" and urlsplit(item.url).path.startswith("/companies")
    )


def _is_yc_launch(item: RawItem) -> bool:
    return ("yc" in item.source_id.casefold() and "launch" in item.source_id.casefold()) or (
        registrable_domain(item.url) == "ycombinator.com" and urlsplit(item.url).path.startswith("/launches")
    )


def baseline_cutoff(run_date: date) -> date:
    """首期基线的日期分界：运行当周的北京周一。"""
    return run_date - timedelta(days=run_date.weekday())


def is_baseline_item(item: RawItem, run_date: date, first_run: bool) -> bool:
    """首次运行时，缺发布时间或发布早于本周一的条目只作基线，忽略 launched_at。"""
    if not first_run:
        return False
    return item.published_at is None or bj_date(item.published_at) < baseline_cutoff(run_date)


def baseline_company_count(conn: sqlite3.Connection) -> int:
    """有基线未推送事件、且所有事件都未曾进过名单的去重公司数。"""
    return conn.execute(
        "SELECT COUNT(*) FROM company c WHERE EXISTS ("
        "SELECT 1 FROM funding_event e WHERE e.company_id=c.id AND e.baseline_unpushed=1) "
        "AND NOT EXISTS (SELECT 1 FROM funding_event e WHERE e.company_id=c.id AND e.push_date IS NOT NULL)"
    ).fetchone()[0]


def normalize_round(round_text: Optional[str], *, is_fund: bool = False, is_debt_only: bool = False) -> str:
    """6.2 → "天使轮" | "种子轮" | "A 轮" | "过滤" | "待核"。"""
    if is_fund or is_debt_only:
        return "过滤"
    if not _present(round_text):
        return "待核"
    value = unicodedata.normalize("NFKC", round_text).casefold()
    value = re.sub(r"[‐‑–—]", "-", value)
    if re.search(r"\b(?:fund|growth)\b|基金募资|只有债务|仅债务|[B-Z] 轮", value, re.I):
        return "过滤"
    if re.search(r"\bseries\s*[- ]?[b-z]\b|\b[b-z](?:[-+ ]\d+)?\s*(?:round|轮)", value):
        return "过滤"
    if re.search(r"\bangel\b|天使", value):
        return "天使轮"
    if re.search(r"\b(?:pre[- ]?seed|seed|pre[- ]?(?:series[- ]?)?a)\b|种子|YC\s*标准投资", value, re.I):
        return "种子轮"
    if "yc" in value and re.search(r"standard\s+(?:investment|deal)", value):
        return "种子轮"
    if re.search(r"\bseries\s*[- ]?a\b|\ba(?:\s*[-+]\s*\d*)?\s*(?:extension|round|轮)\b", value):
        return "A 轮"
    if re.fullmatch(r"a(?:\s*[-+]\s*\d*)?", value.strip()):
        return "A 轮"
    if re.search(r"\b(?:debt|fundraising|fundraise)\b", value):
        return "过滤"
    return "待核"


def judge_country(item: RawItem, ext: Extracted) -> tuple[str, str]:
    """返回中文国家名或「国家待确认」，附所在地原句或待确认原因。"""
    company_name = ext.company_name or item.structured.get("company_name") or item.title
    if _is_x(item):
        return COUNTRY_PENDING, "X 帖子不算国家依据"
    if _is_yc_directory(item):
        location = item.structured.get("location") or item.structured.get("all_locations") or ""
        if isinstance(location, list):
            location = location[0] if location else ""
        first = str(location).split(";", 1)[0].strip()
        if not first:
            return COUNTRY_PENDING, "YC 公司目录里地点为空"
        verdict = _classify_location(first, company_name=company_name)
        if verdict:
            return verdict, first
        return COUNTRY_PENDING, "YC 公司目录里地点未明确国家：" + first
    # 只从明确总部/所在地句式抽地点，避免把投资方所在地、注册地当总部。
    text = "\n".join((item.title, item.excerpt, item.body))
    evidence = ext.hq_quote or ""
    for source in (evidence, text):
        explicit = _headquarters_location(source)
        if explicit:
            # 明确但无法定位的总部也优先于团队、投资方或模型补出的地点。
            verdict = _classify_location(explicit, company_name=company_name)
            return verdict or COUNTRY_PENDING, _location_basis(source, explicit)
    location = ext.hq_text or item.structured.get("hq_text")
    # 字段虽附原句，原句只讲注册地或投资方时仍不能作为公司所在地。
    unrelated = re.search(r"\b(?:registered|incorporated|investors?|fund|venture capital)\b|注册地|投资方", evidence, re.I)
    if unrelated and not re.search(r"[-–‐‑—]based\s+(?:startup|company)\b", evidence, re.I):
        location = None
    if _present(location):
        verdict = _classify_location(str(location), company_name=company_name,
                                     context=evidence or _location_basis(text, str(location)))
        if verdict and (not evidence or str(location).casefold() in evidence.casefold()):
            return verdict, evidence or str(location)
    for match in _LOCATION_PATTERN.finditer(text):
        before = text[max(0, match.start() - 90):match.start()]
        after = text[match.end():match.end() + 90]
        if re.match(r"(?:[-–‐‑— ]based\b|\s+(?:startup|start-up|company)\b)", after, re.I):
            if (re.search(r"(?:led|backed|financed)\s+by\s*(?:(?:a|an|the)\s+)?$|\bfrom\s*(?:(?:a|an|the)\s+)?$", before, re.I)
                    or re.match(r"[-–‐‑— ]based\s+(?:investors?|funds?|venture\s+capital|VC\s+firms?)\b", after, re.I)):
                continue
            basis = _location_basis(text, match[0])
            return _classify_location(match[0], company_name=company_name, context=basis) or COUNTRY_PENDING, basis
        if re.search(r"(?:located|based)\s+in\s*$", before, re.I):
            if re.search(r"\b(?:investors?|fund|team|office)\b", before, re.I):
                continue
            return _classify_location(match[0], company_name=company_name) or COUNTRY_PENDING, _location_basis(text, match[0])
    return COUNTRY_PENDING, "来源没写明国家"


def _location_basis(text: str, location: str) -> str:
    return next((sentence.strip() for sentence in re.split(r"(?<=[。!?])|(?<=\.)\s+|\n+", text)
                 if location.casefold() in sentence.casefold()), location)


def _headquarters_location(text: str) -> Optional[str]:
    matches = re.finditer(
        r"(?:headquartered\s+(?:in|at)|headquarters\s+(?:(?:is|are)\s+)?(?:in|at)|"
        r"(?:company|startup|firm)\s+(?:is\s+)?based\s+in|总部(?:位于|设在|在))\s*[:：]?\s*([^;。\n]+)",
        text, re.I,
    )
    for match in matches:
        before = re.split(r"[;。\n]|\.\s+", text[:match.start()])[-1]
        if re.search(r"\b(?:investors?|fund|VC\s+firm)\b|投资方", before, re.I):
            continue
        # 后半句可能说团队或办公室在别国，不能改变总部判定。
        return re.split(r",?\s+(?:but|while|with|and\s+(?:a|the|its))\b|，|\.\s", match[1], maxsplit=1, flags=re.I)[0].strip()
    return None


def _conditional_country(city, context):
    entry = _CONDITIONAL_CITIES.get(city)
    if entry is None:
        return _COUNTRY_BY_CITY[city]
    for rule in entry["when"]:
        terms = any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", context,
                              0 if len(term) == 2 and term.isupper() else re.I)
                    for term in rule.get("terms", []))
        if (terms or (rule.get("pattern") and re.search(rule["pattern"], context, re.I))
                or (rule.get("us_city") and _US_CITY_PATTERN.search(context))):
            return rule["country"]
    return entry["default"]


def _classify_location(location: str, *, company_name: str = "", context: str | None = None) -> Optional[str]:
    text = unicodedata.normalize("NFKC", location).split(";", 1)[0].strip()
    if not text or re.search(r"\bnorth america\b|北美", text, re.I):
        return None
    # 国家名优先；New Mexico 等州名中的子词不能误当国家。
    states = [m for m in _US_STATE_PATTERN.finditer(text) if m[0].casefold() not in _CONDITIONAL_CITIES]
    countries = {_COUNTRY_BY_ALIAS[match[0].casefold()]
                 for pattern in (_COUNTRY_PATTERN, _CHINESE_COUNTRY_PATTERN) for match in pattern.finditer(text)
                 if match[0].casefold() not in _CONDITIONAL_CITIES
                 if not any(state.start() <= match.start() and match.end() <= state.end()
                            and state[0].casefold() != match[0].casefold() for state in states)}
    for entry in _CONDITIONAL_CITIES.values():
        for rule in entry["when"]:
            if rule.get("explicit") and rule.get("pattern") and re.search(rule["pattern"], text, re.I):
                countries.add(rule["country"])
    if "美国" in countries and _US_COUNTRY_PATTERN.search(text):
        countries -= {_COUNTRY_BY_ALIAS[state[0].casefold()] for state in states
                      if state[0].casefold() in _COUNTRY_BY_ALIAS}
    if countries:
        return next(iter(countries)) if len(countries) == 1 else None
    if _US_CODE_PATTERN.search(text):
        return "美国"
    cities = list(dict.fromkeys(match[0].casefold() for pattern in (_CITY_PATTERN, _CONDITIONAL_PATTERN)
                                for match in pattern.finditer(text)))
    countries = {_conditional_country(city, context or text) for city in cities}
    if states:
        # New York 也是城市；跨国并列城市不能仅因同名州而判美国。
        if len(countries) > 1 and all(state[0].casefold() in cities for state in states):
            return None
        return "美国"
    if countries:
        return next(iter(countries)) if len(countries) == 1 and "有歧义" not in countries else None
    _LOG.warning("城市未收录：%s；公司：%s", location, company_name,
                 extra={"city": location, "company_name": company_name})
    return None


def filter_reasons(item: RawItem, ext: Extracted, country: str, round_norm: str, latest_yc_batches: list[str]) -> list[str]:
    """满足的全部原因，按批次超过 12 个月、轮次不在范围排序；国家不参与过滤。"""
    reasons = []
    batch = item.structured.get("yc_batch")
    if batch and latest_yc_batches and not batch_in_window(batch, latest_yc_batches):
        reasons.append("批次超过 12 个月")
    if round_norm == "过滤" or ext.is_fund_raise or ext.is_debt_only:
        reasons.append("轮次不在范围")
    return reasons


def display_filter_reasons(reasons: list[str]) -> list[str]:
    """隐藏历史国家过滤原因，剩余原因按当前优先级显示，不改原记录。"""
    return sorted((reason for reason in reasons if reason != "总部非美国"),
                  key=lambda reason: FILTER_ORDER.index(reason) if reason in FILTER_ORDER else len(FILTER_ORDER))


def country_from_hq(hq: Optional[str], basis: str, *, company_name: str = "") -> str:
    """用现有所在地与证据回填旧库国家，无法判定时保留待确认。"""
    if re.search(r"冲突|来源.*(?:没写|未写)|地点为空|X 帖子|不算.*依据|同名不同域名", basis):
        return COUNTRY_PENDING
    item = RawItem("migration", "", "", "", None)
    location = hq or (basis if _LOCATION_PATTERN.fullmatch(basis.strip()) else None)
    country = judge_country(item, Extracted(company_name=company_name, hq_text=location, hq_quote=basis))[0]
    if (country == COUNTRY_PENDING and hq and not _headquarters_location(basis)
            and not re.search(r"\b(?:registered|incorporated|investors?|fund|venture capital)\b|注册|投资方", basis, re.I)):
        # 旧 hq 已是保存过的地点字段，依据为空或仅有通用说明时仍可按地点回填。
        return _classify_location(hq, company_name=company_name) or COUNTRY_PENDING
    return country


def registrable_domain(url_or_host: Optional[str]) -> Optional[str]:
    """antimattr.one/project…?utm → antimattr.one；去 www；小写。"""
    if not url_or_host or not url_or_host.strip():
        return None
    value = url_or_host.strip()
    try:
        parsed = urlsplit(value if "://" in value or value.startswith("//") else "//" + value)
        if parsed.scheme and parsed.scheme.lower() not in {"http", "https"}:
            return None
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        # 不能把邮箱、IP 或占位文本当公司官网。
        if parsed.username or not host or "." not in host or ":" in host:
            return None
        if re.fullmatch(r"[\d.]+", host) or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", part) for part in host.split(".")):
            return None
        parts = host.split(".")
        suffix_length = 2 if ".".join(parts[-2:]) in _MULTIPART_SUFFIXES else 1
        if len(parts) <= suffix_length:
            return None
        return ".".join(parts[-suffix_length - 1:])
    except (ValueError, UnicodeError):
        return None


def name_key(name: str) -> str:
    """小写、去标点、去 inc/llc/ltd/corp/co/company/technologies/labs/hq 后缀；不去 AI。"""
    value = unicodedata.normalize("NFKC", name).casefold()
    value = re.sub(r"[.'’]", "", value)
    value = "".join(char if char.isalnum() or char.isspace() else " " for char in value)
    parts = value.split()
    suffixes = {"inc", "incorporated", "llc", "ltd", "corp", "co", "company", "technologies", "labs", "hq"}
    # Inc.、L.L.C. 之类带标点的尾缀也去掉。
    while parts:
        if parts[-1] in suffixes:
            parts.pop()
        elif len(parts) >= 3 and parts[-3:] == ["l", "l", "c"]:
            del parts[-3:]
        else:
            break
    return " ".join(parts)


def parse_amount_usd(amount_text: Optional[str]) -> Optional[float]:
    """仅原文明确是美元时返回数值，不在此函数换算外币。"""
    if not _present(amount_text):
        return None
    currency, value = parse_amount(amount_text)
    return value if currency == "USD" else None


def parse_amount(text: Optional[str]) -> tuple[Optional[str], Optional[float]]:
    """识别原文币种和金额；无币种或单独 ¥ 返回空币种。"""
    return fx.parse_amount(text) if _present(text) else (None, None)


def record_fetch(conn: sqlite3.Connection, run_id: str, outcome: FetchOutcome, simulated: bool) -> None:
    """写 fetch_run（每条 FetchRunRecord 一行）；停抓时更新 source 表抓取状态/停抓时间/原因。"""
    sync_sources(conn, [outcome.source])
    known = {row[0] for row in conn.execute("SELECT external_id FROM raw_item WHERE source_id=?", (outcome.source.id,))}
    new_count = len({item.external_id for item in outcome.items if item.url.strip()} - known)
    baseline = first_issue_generated_at(conn)
    for index, run in enumerate(outcome.runs):
        started, ended = _stamp(run.started_at), _stamp(run.ended_at)
        result = _enum_value(run.result)
        # 同一 run_id 重放幂等；封禁的首次失败和两分钟后的重试仍分别保留。
        duplicate = conn.execute(
            "SELECT 1 FROM fetch_run WHERE run_id=? AND source_id=? AND started_at=? AND ended_at=? AND result=?",
            (run_id, run.source_id, started, ended, result),
        ).fetchone()
        if duplicate:
            continue
        missed_start, missed_end = missed_interval(run.missed_range, run.missed_start, run.missed_end,
                                                   reference=run.started_at)
        missed = run.missed_range if has_missed(run.missed_range, baseline, missed_start, missed_end,
                                               reference=run.started_at) else ""
        conn.execute(
            "INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, http_code, count, new_count, "
            "result, error_type, error_summary, note, latest_at, is_backfill, missed_range, simulated, missed_start, missed_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, run.source_id, started, ended, run.http_code, run.count,
             new_count if index == len(outcome.runs) - 1 else 0, result,
             _enum_value(run.error_type), run.error_summary, run.note,
             _stamp(run.latest_at) if run.latest_at else None, int(run.is_backfill), missed, int(simulated),
             _stamp(missed_start) if missed and missed_start else None, _stamp(missed_end) if missed and missed_end else None),
        )
    if outcome.stopped_now:
        stopped_at = _stamp(outcome.runs[-1].ended_at)
        conn.execute(
            "UPDATE source SET crawl_status='已停抓', stopped_at=?, stop_reason='封禁', stop_notice_date=NULL "
            "WHERE id=? AND crawl_status!='已停抓'", (stopped_at, outcome.source.id),
        )
    if outcome.ok and outcome.yc_batch is not None:
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"yc_last_batch:{outcome.source.id}", outcome.yc_batch))
        if outcome.yc_batch_total is not None:
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (f"yc_last_batch_total:{outcome.source.id}", str(outcome.yc_batch_total)))


def upsert_raw_item(conn: sqlite3.Connection, item: RawItem, first_seen: datetime, is_baseline: bool) -> tuple[int, bool]:
    """返回 (id, 是否新条目)，已有条目不覆盖；无来源链接返回 (0, False)。"""
    if not item.url or not item.url.strip():
        return 0, False
    cursor = conn.execute(
        "INSERT INTO raw_item(source_id, external_id, url, title, published_at, first_seen_at, excerpt, "
        "is_baseline, original_date_et, structured_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id, external_id) DO NOTHING",
        (item.source_id, item.external_id, item.url, item.title,
         _stamp(item.published_at) if item.published_at else None, _stamp(first_seen), item.excerpt,
         int(is_baseline), item.original_date_et, _json(item.structured)),
    )
    row = conn.execute("SELECT id FROM raw_item WHERE source_id=? AND external_id=?",
                       (item.source_id, item.external_id)).fetchone()
    return row[0], bool(cursor.rowcount)


def ingest(conn: sqlite3.Connection, item_id: int, item: RawItem, ext: Extracted, *, now: datetime,
           is_baseline: bool, is_backfill: bool, latest_yc_batches: list[str], model: Optional[str],
           first_ingested_at: datetime | str | None = None, fx_rates: fx.FxRates | None = None) -> Optional[int]:
    """把一条（已抽取的）条目并进 company / funding_event / event_source，按 6.1 判断事件是否
    「待推送」（pending）。回放首次写库时间可指定，默认 now，已有事件不覆盖；无链接返回 None。"""
    if not item.url or not item.url.strip() or not item_id:
        return None
    raw = conn.execute("SELECT * FROM raw_item WHERE id=?", (item_id,)).fetchone()
    if raw is None:
        raise KeyError(item_id)
    existing = conn.execute("SELECT event_id FROM event_source WHERE raw_item_id=?", (item_id,)).fetchone()
    if existing:
        return existing[0]
    if first_ingested_at is None:
        inserted_at = _dt(_stamp(now))
    elif isinstance(first_ingested_at, datetime):
        inserted_at = first_ingested_at.replace(tzinfo=BJ) if first_ingested_at.tzinfo is None else first_ingested_at.astimezone(BJ)
    else:
        inserted_at = _dt(first_ingested_at)
    # 以首次入库的条目为准；重放不能把基线条目升级成非基线。
    baseline = bool(raw["is_baseline"])
    stored_item = _raw_from_row(raw)
    # 正文只用于本次关键词判断，不入库。
    stored_item.body = item.body
    effective = _effective_extracted(stored_item, ext, latest_yc_batches)
    company_name = effective.company_name
    if not company_name or not company_name.strip():
        conn.execute("UPDATE raw_item SET skip_reason='来源没写公司名' WHERE id=?", (item_id,))
        return None
    company_id = _find_company(conn, company_name, effective.website, _dt(raw["first_seen_at"]))
    source = conn.execute("SELECT name FROM source WHERE id=?", (stored_item.source_id,)).fetchone()
    source_name = source[0]
    report_url = _report_url(stored_item)
    report_name = _report_name(stored_item, source_name)
    credibility = _credibility(stored_item, effective, source_name)
    round_norm = normalize_round(effective.round_text, is_fund=effective.is_fund_raise, is_debt_only=effective.is_debt_only)
    event = _find_event(conn, company_id, round_norm, effective.round_text, report_url)
    created = event is None
    if created:
        previous_rounds = {row[0] for row in conn.execute("SELECT round_norm FROM funding_event WHERE company_id=?", (company_id,))}
        marks = ["新一轮"] if previous_rounds and round_norm in {"天使轮", "种子轮", "A 轮"} and previous_rounds - {round_norm, "待核"} else []
        cursor = conn.execute(
            "INSERT INTO funding_event(company_id, round_norm, round_text, event_date, first_seen, first_ingested_at, "
            "marks_json, baseline_unpushed, pending) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (company_id, round_norm, effective.round_text, bj_date(_dt(raw["first_seen_at"])).isoformat(),
             raw["first_seen_at"], inserted_at.strftime("%Y-%m-%d %H:%M:%S"), _json(marks), int(baseline), int(not baseline)),
        )
        event_id = cursor.lastrowid
        event = conn.execute("SELECT * FROM funding_event WHERE id=?", (event_id,)).fetchone()
    event_id = event["id"]
    from .extract import validate_product_terms
    effective.product_terms = validate_product_terms(stored_item, effective.product_terms,
        company_name=company_name, investors=[*effective.leads, *effective.follows])
    data = asdict(effective)
    text = " ".join((stored_item.title, stored_item.excerpt, stored_item.body,
                     effective.one_liner, " ".join(map(str, stored_item.structured.get("tags", [])))))
    data["_keyword_hits"] = {dim: keyword_hit(text, dim) for dim in ("ai", "social")}
    data["_country"], data["_hq_basis"] = judge_country(stored_item, effective)
    # 正文不持久化，只保存按原文上下文确定的币种，供后续冻结和补算使用。
    data["_amount_currency"] = fx.parse_amount(effective.amount_text, context="\n".join(
        (stored_item.title, stored_item.excerpt, stored_item.body, effective.amount_text or "")))[0]
    data["_backfill"] = bool(is_backfill)
    data["_latest_yc_batches"] = latest_yc_batches[:4]
    data["_source_first_seen_at"] = raw["first_seen_at"]
    duplicate = conn.execute("SELECT * FROM event_source WHERE event_id=? AND url=?", (event_id, report_url)).fetchone()
    if duplicate is None:
        conn.execute(
            "INSERT INTO event_source(event_id, raw_item_id, url, source_name, extraction_json, credibility, model) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, item_id, report_url, report_name, _json(data), credibility, model or effective.model_used),
        )
    else:
        old_raw = conn.execute("SELECT * FROM raw_item WHERE id=?", (duplicate["raw_item_id"],)).fetchone()
        if (credibility, _item_time(stored_item, _dt(raw["first_seen_at"]))) < (
            duplicate["credibility"], _item_time(_raw_from_row(old_raw), _dt(old_raw["first_seen_at"]))
        ):
            old_data = json.loads(duplicate["extraction_json"])
            data["_source_first_seen_at"] = _stamp(min(
                _dt(raw["first_seen_at"]), _dt(old_data.get("_source_first_seen_at", old_raw["first_seen_at"]))
            ))
            conn.execute(
                "UPDATE event_source SET raw_item_id=?, source_name=?, extraction_json=?, credibility=?, model=? "
                "WHERE event_id=? AND url=?",
                (item_id, report_name, _json(data), credibility, model or effective.model_used, event_id, report_url),
            )
    if not baseline and duplicate is None:
        last_pushed = event["last_pushed_date"] or event["push_date"]
        if last_pushed is None or (bj_date(now) - date.fromisoformat(last_pushed)).days > 90:
            conn.execute("UPDATE funding_event SET pending=1, baseline_unpushed=0 WHERE id=?", (event_id,))
            marker = conn.execute("SELECT backfill FROM company WHERE id=?", (company_id,)).fetchone()[0]
            if event["original_first_ingested_at"] and (marker == "补充未推送" or last_pushed is not None):
                # K 家之后第一次新报道、或补充推送九十天后的新报道，恢复普通 6.1 流程。
                conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)",
                             (f"backfill:normal:{event_id}", _stamp(now)))
            if last_pushed is not None:
                marks = [mark for mark in json.loads(event["marks_json"]) if mark != "新一轮"]
                conn.execute("UPDATE funding_event SET marks_json=? WHERE id=?", (_json(marks), event_id))
    _refresh_company(conn, company_id, fx_rates=fx_rates)
    nk = conn.execute("SELECT name_key FROM company WHERE id=?", (company_id,)).fetchone()[0]
    # 同名不同域名各自收录，保留身份提示，但不重新推已列出的事件。
    for other in conn.execute("SELECT id FROM company WHERE name_key=? AND id!=?", (nk, company_id)).fetchall():
        _refresh_company(conn, other[0], fx_rates=fx_rates)
    return event_id


def pending_events(conn: sqlite3.Connection, *, event_ids=None, include_backfill=False) -> list[sqlite3.Row]:
    """所有待列入名单的非基线事件，含推送满 91 天后由新报道重新入队的事件。"""
    where, params = "e.pending=1 AND e.baseline_unpushed=0", []
    if event_ids is not None:
        ids = list(dict.fromkeys(event_ids))
        where, params = "e.id IN (" + ",".join("?" for _ in ids) + ")", ids
    if not include_backfill:
        where += (" AND NOT (e.original_first_ingested_at IS NOT NULL AND c.backfill!='' "
                  "AND NOT EXISTS (SELECT 1 FROM meta WHERE key='backfill:normal:'||e.id))")
    return conn.execute(
        "SELECT e.*, c.name, c.summary, c.ai_label, c.social_label, c.status, c.pending_reason, "
        "c.hq, c.hq_basis, c.country FROM funding_event e JOIN company c ON c.id=e.company_id "
        f"WHERE {where} ORDER BY e.id", params
    ).fetchall()


def mark_listed(conn: sqlite3.Connection, event_ids: list[int], push_date: date) -> None:
    """写推送日期（只写空的，同日重跑幂等）。"""
    conn.executemany(
        "UPDATE funding_event SET push_date=COALESCE(push_date, ?), last_pushed_date=?, pending=0, baseline_unpushed=0 "
        "WHERE id=? AND pending=1", [(push_date.isoformat(), push_date.isoformat(), eid) for eid in set(event_ids)],
    )


def card_sort_data(conn: sqlite3.Connection, event_id: int) -> tuple[Optional[float], datetime]:
    """返回事件排序用的主金额和带北京时区的首次写库时刻；找不到事件抛 KeyError。"""
    row = conn.execute("SELECT * FROM funding_event WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    return _sort_data(row, _evidence_rows(conn, event_id=event_id))


def collect_listing(conn: sqlite3.Connection, push_date: date, *, weekly: bool,
                    base_holiday_mark: bool = False, event_ids=None, include_backfill=False) -> dict:
    """只读组装全部待推送卡片和待核/过滤名单，排序和前十家由 render 处理。

    first_seen 沿用 render 参数名，值改用 first_ingested_at；同公司多事件取最早值。
    weekly、base_holiday_mark 保留参数兼容，V15e 一律没有假期补发。"""
    rows = pending_events(conn, event_ids=event_ids, include_backfill=include_backfill)
    round_pending, cards, filtered_names, event_ids = [], [], [], []
    filtered = Counter()
    eligible, evidence_by_event, first_seen = {}, {}, {}
    for row in rows:
        stored_reasons = json.loads(row["filter_reasons_json"])
        reasons = display_filter_reasons(stored_reasons)
        if stored_reasons and not reasons:
            # 旧国家过滤事件等 R 一次性重判，不提前纳入或清掉 pending。
            continue
        event_ids.append(row["id"])
        company_id = row["company_id"]
        evidence = _evidence_rows(conn, event_id=row["id"])
        evidence_by_event[row["id"]] = evidence
        _, seen = _sort_data(row, evidence)
        first_seen[company_id] = min(first_seen.get(company_id, seen), seen)
        amount = amount_label(conn, row, evidence)
        if reasons:
            filtered[reasons[0]] += 1
            filtered_names.append((row["name"], reasons[0]))
        elif row["round_norm"] == "待核" and row["status"] != "总部待核" and not include_backfill:
            round_pending.append(PendingLine(row["company_id"], row["name"], amount or "金额未披露", "来源没写轮次"))
        else:
            eligible[row["id"]] = row
    collapsed, batch = _collapsed_yc(conn, eligible)
    for event_id, row in eligible.items():
        if event_id in collapsed:
            continue
        evidence = evidence_by_event[event_id]
        amount = amount_label(conn, row, evidence)
        amount_status = row["amount_status"]
        amount_usd, _ = _sort_data(row, evidence)
        marks = _event_marks(json.loads(row["marks_json"]), row["round_norm"], amount_status, amount_usd)
        tag_label, group = overall(row["ai_label"], row["social_label"])
        from .verify import valid_investor
        leads = [v for v in json.loads(row["leads_json"]) if valid_investor(v)]
        lead_label = ("Y Combinator" if leads == ["Y Combinator"] else
                      "、".join(leads) + (" 共同领投" if len(leads) > 1 else " 领投") if leads else "领投未披露")
        summary = placenames.normalize_place_text(row["summary"] or "", protected_names=placenames.company_names(conn, row["company_id"]))[0]
        cards.append(Card(
            company_id=row["company_id"], name=row["name"], marks=marks,
            round_label="轮次待确认" if row["round_norm"] == "待核" else row["round_norm"], amount_label=amount or "金额未披露", lead_label=lead_label,
            tag_label=tag_label, group=group, summary=fx.format_amount_text(summary, _fx_snapshot(conn, row["id"])) if summary else "",
            sources=[CardSource(registry.display_name(record["raw"]["source_id"]), _item_time(record["item"], _dt(record["raw"]["first_seen_at"])).date(), record["url"])
                     for record in sorted(evidence, key=_source_order)],
            amount_usd=amount_usd, event_date=date.fromisoformat(row["event_date"]),
            country=COUNTRY_PENDING if row["status"] == "总部待核" else row["country"],
            amount_status=amount_status,
        ))
    return {"cards": cards, "round_pending": round_pending,
            "filtered": dict(filtered), "yc_new_batch": batch, "event_ids": event_ids,
            "filtered_names": filtered_names, "filtered_total": sum(filtered.values()), "first_seen": first_seen}


def upsert_digest(conn: sqlite3.Connection, row: dict, *, first_issue: bool = False) -> None:
    """按北京日期写 digest；首期传 first_issue=True，记录首期日期并按 push=true 写。"""
    allowed = {column[1] for column in conn.execute("PRAGMA table_info(digest)")}
    values = dict(row)
    if "event_ids" in values:
        values["event_ids_json"] = values.pop("event_ids")
    if "date" not in values:
        raise ValueError("digest 必须有北京日期")
    if first_issue:
        values["push"] = 1
    if unknown := values.keys() - allowed:
        raise ValueError(f"未知 digest 字段：{', '.join(sorted(unknown))}")
    for key, value in values.items():
        if isinstance(value, datetime):
            stamp = _dt(_stamp(value))
            values[key] = stamp.strftime("%Y-%m-%d %H:%M:%S") if key in {"generated_at", "sent_at", "resend_sent_at"} else _stamp(value)
        elif isinstance(value, date):
            values[key] = value.isoformat()
        elif isinstance(value, (list, dict)):
            values[key] = _json(value)
        elif key in {"generated_at", "sent_at", "resend_sent_at"} and value:
            values[key] = _dt(value).strftime("%Y-%m-%d %H:%M:%S")
    if "push" not in values and conn.execute("SELECT 1 FROM digest WHERE date=?", (values["date"],)).fetchone() is None:
        values["push"] = int(is_push_day(date.fromisoformat(values["date"])))
    columns = list(values)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "date")
    conn.execute(f"INSERT INTO digest({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
                 + (f"ON CONFLICT(date) DO UPDATE SET {updates}" if updates else "ON CONFLICT(date) DO NOTHING"),
                 list(values.values()))
    if first_issue:
        conn.execute("INSERT INTO meta(key, value) VALUES ('first_issue_date', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (values["date"],))


def missed_runs_in_window(conn: sqlite3.Connection, window_start: Optional[datetime], window_end: datetime) -> list[dict]:
    """读取本期窗口内仍有效的漏抓行，不删除历史，也不包含已停抓地址。"""
    baseline = first_issue_generated_at(conn)
    since = window_start or baseline
    # 只读导出可能面对尚未迁移的旧库（没有起止两列）：按两列为空读取，不写库。
    columns = {column[1] for column in conn.execute("PRAGMA table_info(fetch_run)")}
    bounds = ("f.missed_start", "f.missed_end") if "missed_start" in columns else ("NULL", "NULL")
    rows = conn.execute(
        f"SELECT f.*,{bounds[0]} AS missed_start,{bounds[1]} AS missed_end,s.name FROM fetch_run f "
        "JOIN source s ON s.id=f.source_id WHERE s.enabled=1 AND s.crawl_status!='已停抓' AND "
        f"(TRIM(COALESCE(f.missed_range,''))!='' OR {bounds[0]} IS NOT NULL OR {bounds[1]} IS NOT NULL)"
    )
    names = [column[0] for column in rows.description]
    result = []
    for values in rows:
        row = dict(zip(names, values))
        started = _dt(row["started_at"])
        if (since is not None and started <= since) or started > window_end:
            continue
        if has_missed(row["missed_range"], baseline, row["missed_start"], row["missed_end"], reference=started):
            start, end = missed_interval(row["missed_range"], row["missed_start"], row["missed_end"], reference=started)
            result.append({**row, "missed_start": start, "missed_end": end})
    return result


def failed_days_in_window(conn: sqlite3.Connection, window_start: Optional[datetime], push_date: date, *,
                          outbox_dir: Path | None = None) -> int:
    """窗口起日之后、推送日前的中国工作日中，整天失败、缺 digest 或有漏抓的天数。

    首期当天及以前、上线前不计失败；可指定 outbox 目录，否则检查数据库同级 outbox。
    首期 digest 通过 upsert_digest(first_issue=True) 标识，兼容旧的 push=true 基线记录。"""
    first = bj_date(window_start) + timedelta(days=1) if window_start is not None else baseline_cutoff(push_date)
    first_dates = [date.fromisoformat(row[0]) for row in conn.execute(
        "SELECT value FROM meta WHERE key='first_issue_date' UNION ALL "
        "SELECT date FROM digest WHERE push=1 AND status='基线'"
    )]
    directories = [Path(outbox_dir)] if outbox_dir is not None else []
    if outbox_dir is None:
        filename = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
        if filename:
            parent = Path(filename).parent
            directories = [parent / "outbox", parent / "outbox-test"]
            if parent.name == "test":
                directories.append(parent.parent / "outbox-test")
    for directory in directories:
        if first_date := outbox.first_issue_date(directory):
            first_dates.append(first_date)
    if first_dates:
        first = max(first, max(first_dates) + timedelta(days=1))
    runs = conn.execute("SELECT started_at, missed_range, missed_start, missed_end FROM fetch_run").fetchall()
    if not runs:
        return 0
    first = max(first, bj_date(min(_dt(row["started_at"]) for row in runs)))
    if first >= push_date:
        return 0
    statuses = {row["date"]: row["status"] for row in conn.execute(
        "SELECT date, status FROM digest WHERE date>=? AND date<?", (first.isoformat(), push_date.isoformat())
    )}
    baseline = first_issue_generated_at(conn)
    missed = {bj_date(_dt(row["started_at"])) for row in runs if has_missed(
        row["missed_range"], baseline, row["missed_start"], row["missed_end"], reference=_dt(row["started_at"]))}
    count = 0
    for offset in range((push_date - first).days):
        day = first + timedelta(days=offset)
        if not is_cn_workday(day):
            continue
        normal_pattern = re.compile(re.escape(str(day)) + r"(?:-\d{6})?\.json")
        first_pattern = re.compile(re.escape(str(day)) + r"(?:-\d{6})?-first\.json")
        paths = [path for directory in directories for path in directory.glob(f"{day}*.json") if path.is_file()]
        first_issue = any(first_pattern.fullmatch(path.name) for path in paths) or any(
            (directory / f"{day}-first.sent").exists() for directory in directories)
        if first_issue:
            continue
        primary_paths = [path for path in paths if normal_pattern.fullmatch(path.name)]
        has_push = any(json.loads(path.read_text(encoding="utf-8")).get("push") is True for path in primary_paths)
        # 只有按周规则应推送的日子才走取件程序x兜底；空目录同样不能当作已交接。
        missed_push = is_push_day(day) and not has_push
        missing_file = outbox_dir is not None and not primary_paths
        if (day.isoformat() not in statuses or statuses[day.isoformat()] in {"没跑完", "全部失败"}
                or day in missed or missed_push or missing_file):
            count += 1
    return count


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _enum_value(value):
    return getattr(value, "value", value)


def _stamp(value: datetime) -> str:
    bj_date(value)
    return value.astimezone(BJ).isoformat()


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=BJ) if parsed.tzinfo is None else parsed.astimezone(BJ)


def _raw_from_row(row: sqlite3.Row) -> RawItem:
    return RawItem(source_id=row["source_id"], external_id=row["external_id"], url=row["url"],
                   title=row["title"], published_at=_dt(row["published_at"]) if row["published_at"] else None,
                   excerpt=row["excerpt"], original_date_et=row["original_date_et"], structured=json.loads(row["structured_json"]))


def _item_time(item: RawItem, first_seen: datetime) -> datetime:
    return (first_seen if _is_yc_directory(item) else item.published_at or first_seen).astimezone(BJ)


def _effective_extracted(item: RawItem, ext: Extracted, batches: list[str]) -> Extracted:
    result = replace(ext, company_name=ext.company_name or item.structured.get("company_name"),
                     website=ext.website or item.structured.get("website"),
                     one_liner=ext.one_liner or item.structured.get("one_liner", ""))
    batch = item.structured.get("yc_batch")
    recent = bool(batch and batch_in_window(batch, batches))
    if (_is_yc_directory(item) or _is_yc_launch(item)) and recent and (
        not _present(result.round_text) or "yc" in result.round_text.casefold()
    ):
        result = replace(result, round_text="YC 标准投资", amount_text=None, leads=["Y Combinator"])
    return result


def _canonical_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parsed.port and parsed.port not in {80, 443}:
        host += f":{parsed.port}"
    query = sorted((key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                   if not key.casefold().startswith("utm_") and key.casefold() not in {"fbclid", "gclid", "mc_cid", "mc_eid"})
    return urlunsplit((parsed.scheme.casefold(), host, parsed.path.rstrip("/") or "/", urlencode(query), ""))


def _report_url(item: RawItem) -> str:
    return _canonical_url(item.structured.get("original_link") or item.url)


def _known_source_name(url: str) -> Optional[str]:
    if registrable_domain(url) == "fortune.com":
        return "Term Sheet" if "term-sheet" in url or "/newsletter/" in url else "Fortune"
    return {"techcrunch.com": "TechCrunch", "crunchbase.com": "Crunchbase News", "fortune.com": "Term Sheet",
            "alleywatch.com": "AlleyWatch", "strictlyvc.com": "StrictlyVC", "venturebeat.com": "VentureBeat",
            "x.com": "X", "twitter.com": "X"}.get(registrable_domain(url))


def _report_name(item: RawItem, fallback: str) -> str:
    original = item.structured.get("original_link")
    return (_known_source_name(original) or registrable_domain(original) or fallback) if original else fallback


def _credibility(item: RawItem, ext: Extracted, source_name: str) -> int:
    if _is_yc_directory(item):
        return 0
    original = item.structured.get("original_link")
    domain = registrable_domain(original or item.url)
    if "official" in item.source_id.casefold() or "官方" in source_name or (domain and domain == registrable_domain(ext.website)):
        return 0
    if _is_yc_launch(item) or "launch_hn" in item.source_id.casefold() or "launch hn" in source_name.casefold():
        return 1
    name = (_known_source_name(original) if original else source_name) or ""
    name = name.casefold()
    for rank, aliases in enumerate((
        ("crunchbase",), ("term sheet", "termsheet"), ("techcrunch",), ("alleywatch",),
        ("strictlyvc",), ("venturebeat",),
    ), 2):
        if any(alias in name for alias in aliases):
            return rank
    if _is_x(item) or domain in {"x.com", "twitter.com"}:
        return 10
    if "mail" in item.source_id.casefold() or "newsletter" in name or "邮件" in source_name:
        return 9
    return 8


def _slug(value: str) -> str:
    return re.sub(r"[^\w]+", "-", value.casefold(), flags=re.UNICODE).strip("-_") or "company"


def _find_company(conn: sqlite3.Connection, name: str, website: Optional[str], first_seen: datetime) -> str:
    domain, nk = registrable_domain(website), name_key(name)
    same_name = conn.execute("SELECT * FROM company WHERE name_key=? ORDER BY first_seen, id", (nk,)).fetchall()
    if domain:
        existing = conn.execute("SELECT * FROM company WHERE domain_key=?", (domain,)).fetchone()
        no_domain = [row for row in same_name if row["domain_key"] is None]
        if existing:
            for old in no_domain:
                _merge_company(conn, old["id"], existing["id"])
            return existing["id"]
        if no_domain:
            chosen = no_domain[0]
            conn.execute("UPDATE company SET domain_key=?, website=? WHERE id=?", (domain, website, chosen["id"]))
            return chosen["id"]
    elif len(same_name) == 1:
        return same_name[0]["id"]
    elif same_name:
        without_domain = [row for row in same_name if row["domain_key"] is None]
        if without_domain:
            return without_domain[0]["id"]
    stem = domain.split(".")[0] if domain else nk
    candidate, number = _slug(stem), 1
    base = candidate
    while conn.execute("SELECT 1 FROM company WHERE id=?", (candidate,)).fetchone():
        number += 1
        candidate = f"{base}-{number}"
    conn.execute("INSERT INTO company(id, domain_key, name_key, name, website, first_seen, status) VALUES (?, ?, ?, ?, ?, ?, '已纳入')",
                 (candidate, domain, nk, name.strip(), website, bj_date(first_seen).isoformat()))
    return candidate


def _find_event(conn: sqlite3.Connection, company_id: str, round_norm: str, round_text: Optional[str], url: str):
    existing = conn.execute("SELECT e.* FROM funding_event e JOIN event_source s ON s.event_id=e.id "
                            "WHERE e.company_id=? AND s.url=?", (company_id, url)).fetchone()
    if existing:
        return existing
    events = conn.execute("SELECT * FROM funding_event WHERE company_id=? ORDER BY id DESC", (company_id,)).fetchall()
    for event in events:
        if event["round_norm"] == round_norm and (round_norm != "过滤" or name_key(event["round_text"] or "") == name_key(round_text or "")):
            return event
    for event in events:
        if event["round_norm"] == "待核":
            return event
    return events[0] if round_norm == "待核" and events else None


def _merge_company(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    for event in conn.execute("SELECT * FROM funding_event WHERE company_id=?", (old_id,)).fetchall():
        target = conn.execute("SELECT * FROM funding_event WHERE company_id=? AND round_norm=? ORDER BY id LIMIT 1",
                              (new_id, event["round_norm"])).fetchone()
        if target is None:
            conn.execute("UPDATE funding_event SET company_id=? WHERE id=?", (new_id, event["id"]))
            continue
        for source in conn.execute("SELECT * FROM event_source WHERE event_id=? ORDER BY credibility", (event["id"],)).fetchall():
            duplicate = conn.execute("SELECT * FROM event_source WHERE event_id=? AND url=?", (target["id"], source["url"])).fetchone()
            if duplicate is None:
                conn.execute("INSERT INTO event_source VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (target["id"], source["raw_item_id"], source["url"], source["source_name"], source["extraction_json"], source["credibility"], source["model"]))
            else:
                # 同一报道随公司合并时，字段选高可信度，但来源首次入库时间不能丢。
                source_data, duplicate_data = json.loads(source["extraction_json"]), json.loads(duplicate["extraction_json"])
                source_seen = source_data.get("_source_first_seen_at") or conn.execute(
                    "SELECT first_seen_at FROM raw_item WHERE id=?", (source["raw_item_id"],)
                ).fetchone()[0]
                duplicate_seen = duplicate_data.get("_source_first_seen_at") or conn.execute(
                    "SELECT first_seen_at FROM raw_item WHERE id=?", (duplicate["raw_item_id"],)
                ).fetchone()[0]
                chosen = source if source["credibility"] < duplicate["credibility"] else duplicate
                data = source_data if chosen is source else duplicate_data
                data["_source_first_seen_at"] = _stamp(min(_dt(source_seen), _dt(duplicate_seen)))
                conn.execute("UPDATE event_source SET raw_item_id=?, source_name=?, extraction_json=?, credibility=?, model=? WHERE event_id=? AND url=?",
                             (chosen["raw_item_id"], chosen["source_name"], _json(data), chosen["credibility"], chosen["model"], target["id"], source["url"]))
        push_dates = [value for value in (target["push_date"], event["push_date"]) if value]
        last_dates = [value for value in (target["last_pushed_date"], event["last_pushed_date"]) if value]
        pending = bool(target["pending"] or event["pending"])
        if last_dates:
            last = date.fromisoformat(max(last_dates))
            pending = pending and any(not record["raw"]["is_baseline"] and (bj_date(_dt(record["raw"]["first_seen_at"])) - last).days > 90
                                      for record in _evidence_rows(conn, event_id=target["id"]))
        marks = set(json.loads(target["marks_json"])) | set(json.loads(event["marks_json"]))
        conn.execute("UPDATE funding_event SET push_date=?, last_pushed_date=?, pending=?, baseline_unpushed=?, first_seen=?, marks_json=?, "
                     "original_first_ingested_at=COALESCE(original_first_ingested_at,?) WHERE id=?",
                     (min(push_dates) if push_dates else None, max(last_dates) if last_dates else None,
                      int(pending), int(bool(target["baseline_unpushed"] and event["baseline_unpushed"])),
                      min(target["first_seen"], event["first_seen"]), _json([mark for mark in MARK_ORDER if mark in marks]),
                      event["original_first_ingested_at"], target["id"]))
        for digest in conn.execute("SELECT date, event_ids_json,supplement_event_ids_json FROM digest").fetchall():
            for column in ("event_ids_json", "supplement_event_ids_json"):
                ids = json.loads(digest[column])
                if event["id"] in ids:
                    ids = list(dict.fromkeys(target["id"] if eid == event["id"] else eid for eid in ids))
                    conn.execute(f"UPDATE digest SET {column}=? WHERE date=?", (_json(ids), digest["date"]))
        for rank_row in conn.execute("SELECT key,value FROM meta WHERE key LIKE 'backfill:rank:%'").fetchall():
            value = json.loads(rank_row["value"])
            value["event_ids"] = list(dict.fromkeys(target["id"] if eid == event["id"] else eid for eid in value["event_ids"]))
            conn.execute("UPDATE meta SET value=? WHERE key=?", (_json(value), rank_row["key"]))
        ordinary = conn.execute("SELECT value FROM meta WHERE key=?", (f"backfill:normal:{event['id']}",)).fetchone()
        if ordinary:
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (f"backfill:normal:{target['id']}", ordinary[0]))
            conn.execute("DELETE FROM meta WHERE key=?", (f"backfill:normal:{event['id']}",))
        conn.execute("DELETE FROM funding_event WHERE id=?", (event["id"],))
        conn.execute("UPDATE verify_log SET event_id=? WHERE event_id=?", (target["id"], event["id"]))
        conn.execute("UPDATE verify_log SET target_event_id=? WHERE target_event_id=?", (target["id"], event["id"]))
        frozen = conn.execute("SELECT value FROM meta WHERE key=?", (f"fx:event:{event['id']}",)).fetchone()
        if frozen:
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (f"fx:event:{target['id']}", frozen[0]))
            conn.execute("DELETE FROM meta WHERE key=?", (f"fx:event:{event['id']}",))
    # 阶段 2 的外键也随合并转移，同日已有结果时保留目标公司的记录。
    for table in ("deepdive", "deepdive_pages"):
        for old in conn.execute(f"SELECT * FROM {table} WHERE company_id=?", (old_id,)).fetchall():
            values = dict(old)
            values["company_id"] = new_id
            conn.execute(f"INSERT OR IGNORE INTO {table}({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                         list(values.values()))
    conn.execute("DELETE FROM deepdive_pages WHERE company_id=?", (old_id,))
    conn.execute("DELETE FROM deepdive WHERE company_id=?", (old_id,))
    conn.execute("UPDATE tag_change SET company_id=? WHERE company_id=?", (new_id, old_id))
    for table in ("verify_log", "verify_search", "homepage_check"):
        for record in conn.execute(f"SELECT * FROM {table} WHERE company_id=?", (old_id,)).fetchall():
            values = {key: record[key] for key in record.keys() if key != "id"}
            values["company_id"] = new_id
            conn.execute(f"INSERT OR IGNORE INTO {table}({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})", list(values.values()))
        conn.execute(f"DELETE FROM {table} WHERE company_id=?", (old_id,))
    old_backfill = conn.execute("SELECT backfill,backfill_push_date,backfill_rank FROM company WHERE id=?", (old_id,)).fetchone()
    conn.execute("UPDATE company SET backfill=CASE WHEN backfill='' THEN ? ELSE backfill END,"
                 "backfill_push_date=COALESCE(backfill_push_date,?),backfill_rank=COALESCE(backfill_rank,?) WHERE id=?", (*old_backfill, new_id))
    old_rank = conn.execute("SELECT value FROM meta WHERE key=?", (f"backfill:rank:{old_id}",)).fetchone()
    if old_rank:
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (f"backfill:rank:{new_id}", old_rank[0]))
        conn.execute("DELETE FROM meta WHERE key=?", (f"backfill:rank:{old_id}",))
    conflict = conn.execute("SELECT value FROM meta WHERE key=?", (f"verify:conflicts:{old_id}",)).fetchone()
    if conflict:
        conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (f"verify:conflicts:{new_id}", conflict[0]))
        conn.execute("DELETE FROM meta WHERE key=?", (f"verify:conflicts:{old_id}",))
    old_first = conn.execute("SELECT first_seen FROM company WHERE id=?", (old_id,)).fetchone()[0]
    conn.execute("UPDATE company SET first_seen=MIN(first_seen, ?) WHERE id=?", (old_first, new_id))
    conn.execute("DELETE FROM company WHERE id=?", (old_id,))


def _evidence_rows(conn: sqlite3.Connection, *, event_id: Optional[int] = None, company_id: Optional[str] = None) -> list[dict]:
    condition, value = ("s.event_id", event_id) if event_id is not None else ("e.company_id", company_id)
    result = []
    for row in conn.execute(f"SELECT s.* FROM event_source s JOIN funding_event e ON e.id=s.event_id WHERE {condition}=?", (value,)):
        raw = conn.execute("SELECT * FROM raw_item WHERE id=?", (row["raw_item_id"],)).fetchone()
        result.append({**dict(row), "raw": raw, "item": _raw_from_row(raw), "ext": json.loads(row["extraction_json"])})
    return sorted(result, key=lambda record: (record["credibility"],
                  _item_time(record["item"], _dt(record["raw"]["first_seen_at"])), record["raw_item_id"]))


def _source_order(record: dict) -> tuple:
    source_id = record["raw"]["source_id"] if record.get("raw") is not None else record.get("source_id", "")
    return (registry.display_rank(source_id),
            _dt(record["ext"].get("_source_first_seen_at", record["raw"]["first_seen_at"])), record["raw_item_id"])


def _amount_info(evidence: list[dict]) -> tuple[Optional[str], str, Optional[float]]:
    if evidence and all(record["ext"].get("_amount_status") == "金额待确认" for record in evidence):
        return None, "金额待确认", None
    evidence = [r for r in evidence if r["ext"].get("_amount_status") != "金额待确认"]
    ordered = sorted(evidence, key=_source_order)
    disclosed = [record for record in evidence if _present(record["ext"].get("amount_text"))]
    if not disclosed:
        return None, "未披露", None
    primary = ordered[0]
    primary_value = _source_amount(primary)["amount_usd"]
    amounts = [(record, _source_amount(record)["amount_usd"]) for record in disclosed]
    numeric = [(record, value) for record, value in amounts if value is not None]
    conflicts = [(first, second) for index, (first, a) in enumerate(numeric) for second, b in numeric[index + 1:]
                 if min(a, b) > 0 and abs(a - b) > min(a, b) * 0.1 + 1e-6]
    if conflicts:
        other = next((second if first is primary else first for first, second in conflicts
                      if first is primary or second is primary), None)
        relevant = [primary, other] if other else [primary, *sorted(conflicts[0], key=_source_order)]
        displayed = list({record["raw_item_id"]: record for record in relevant}.values())
        text = "，".join(f"{record['source_name']} 写 " + (record["ext"]["amount_text"]
                         if _present(record["ext"].get("amount_text")) else "金额未披露") for record in displayed)
        return text, "金额待核", primary_value
    # 普通卡片的显示金额沿用 6.1：可信度优先，同级更早发布者优先。
    chosen = disclosed[0]["ext"]["amount_text"]
    amount = _source_amount(disclosed[0])
    status = ("币种待确认" if amount["amount_currency"] is None else
              "暂未换算" if amount["amount_usd"] is None else "披露")
    return chosen, status, amount["amount_usd"]


def _source_amount(record: dict) -> dict:
    if record["ext"].get("_amount_status") == "金额待确认":
        return fx.Amount().to_dict()
    return record["ext"].get("_amount_fx") or fx.convert_amount(record["ext"].get("amount_text")).to_dict()


def _fx_snapshot(conn, event_id):
    stored = conn.execute("SELECT value FROM meta WHERE key=?", (f"fx:event:{event_id}",)).fetchone()
    return json.loads(stored[0]) if stored else None


def amount_label(conn: sqlite3.Connection, row, evidence: list[dict] | None = None) -> str:
    """从冻结的美元值和各来源金额生成卡片、网页共用的金额文字。"""
    if row["amount_status"] == "金额待确认":
        return "金额待确认"
    if row["amount_status"] == "未披露":
        return "金额未披露"
    if row["amount_status"] == "金额待核":
        if label := verified_amount_label(conn, row["company_id"], row["id"]):
            return label
    if row["amount_status"] == "金额待核" and " 写 " in (row["amount_text"] or ""):
        evidence = _evidence_rows(conn, event_id=row["id"]) if evidence is None else evidence
        if evidence:
            ordered = sorted(evidence, key=_source_order)
            # 选取原始冲突串里的来源，保持主来源和后续来源顺序。
            displayed = [record for record in ordered if f"{record['source_name']} 写 " in row["amount_text"]]
            if displayed:
                return "，".join(f"{registry.display_name(record['raw']['source_id'])} 写 " + fx.format_amount(
                    _source_amount(record), record["ext"].get("amount_text")) for record in displayed)
        return fx.format_amount_text(row["amount_text"])
    return fx.format_amount(dict(row), row["amount_text"])


def verified_amount_label(conn: sqlite3.Connection, company_id: str, event_id: int) -> str | None:
    """读取查证的金额差异，兼容站点的普通只读SQLite连接。"""
    stored = conn.execute("SELECT value FROM meta WHERE key=?", ("verify:conflicts:" + company_id,)).fetchone()
    conflict = next((c for c in json.loads(stored[0]) if c["field"] == "金额" and c.get("event_id") == event_id), None) if stored else None
    if conflict:
        return "，".join(dict.fromkeys(f"{v['name']} 写 {v['value']}" for v in conflict["values"]))
    return None


def _freeze_amounts(conn, event, evidence, event_date, fx_rates):
    missing = [record for record in evidence if "_amount_fx" not in record["ext"]]
    retry = [record for record in evidence if event["amount_status"] == "暂未换算"
             and record["ext"].get("_amount_fx", {}).get("amount_currency") not in {None, "USD"}
             and record["ext"]["_amount_fx"].get("amount_usd") is None]
    pending = list({record["raw_item_id"]: record for record in [*missing, *retry]}.values())
    if not pending:
        return
    key = f"fx:event:{event['id']}"
    stored = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    snapshot = json.loads(stored[0]) if stored else None
    for record in pending:
        text = record["ext"].get("amount_text")
        item = record["item"]
        context = "\n".join((item.title, item.excerpt, item.body, text or ""))
        currency = record["ext"].get("_amount_currency", fx.parse_amount(text, context=context)[0])
        if "_amount_currency" in record["ext"]:
            # 沿用整条原文的判断；摘录不完整时也不能把原来的歧义反向确认。
            if currency in {"CNY", "JPY"}:
                context = currency + " " + (text or "")
            elif currency is None:
                context = "CNY JPY"
        if currency not in {None, "USD"} and (snapshot is None or currency not in snapshot.get("rates", {})):
            fresh = fx_rates.snapshot(event_date, currencies=(currency,)) if fx_rates is not None else {
                "base": "USD", "rates": {}, "stale": 0}
            # 成功换算的来源值始终冻结；仅待换算的来源采用新取得的表。
            if snapshot is None or not snapshot.get("rates"):
                snapshot = fresh
                conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (key, _json(snapshot)))
            chosen = fresh
        else:
            chosen = snapshot
        record["ext"]["_amount_fx"] = fx.convert_amount(text, chosen, context=context).to_dict()
        conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                     (_json(record["ext"]), event["id"], record["raw_item_id"]))


def retry_unconverted_amounts(conn: sqlite3.Connection, fx_rates: fx.FxRates) -> None:
    """每次运行补算暂未换算的事件，保留推送日期、待推送状态和成功冻结值。"""
    events = conn.execute("SELECT e.*, c.country FROM funding_event e JOIN company c ON c.id=e.company_id "
                          "WHERE e.amount_status='暂未换算'").fetchall()
    for event in events:
        _refresh_event(conn, event, event["country"], fx_rates=fx_rates)
        from .verify import apply_saved
        apply_saved(conn, event["company_id"], fx_rates=fx_rates)


def revalue_amounts(conn: sqlite3.Connection, event_id: int, fx_rates: fx.FxRates) -> None:
    """仅供显式补充重判清除该事件的冻结金额，普通入库不调用。"""
    event = conn.execute("SELECT * FROM funding_event WHERE id=?", (event_id,)).fetchone()
    if event is None:
        raise KeyError(event_id)
    conn.execute("DELETE FROM meta WHERE key=?", (f"fx:event:{event_id}",))
    for record in _evidence_rows(conn, event_id=event_id):
        record["ext"].pop("_amount_fx", None)
        conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                     (_json(record["ext"]), event_id, record["raw_item_id"]))
    company = conn.execute("SELECT country FROM company WHERE id=?", (event["company_id"],)).fetchone()
    _refresh_event(conn, event, company[0], fx_rates=fx_rates)


def _sort_data(row: sqlite3.Row, evidence: list[dict]) -> tuple[Optional[float], datetime]:
    if row["amount_status"] in {"币种待确认", "暂未换算", "金额待确认"}:
        amount = None
    elif row["amount_status"] == "金额待核" and " 写 " in (row["amount_text"] or ""):
        ordered = sorted(evidence, key=_source_order)
        amount = _source_amount(ordered[0])["amount_usd"] if ordered else None
    elif row["fx_source"] is not None or row["amount_currency"] is not None:
        amount = row["amount_usd"]
    else:
        amount = parse_amount_usd(row["amount_text"]) if row["amount_status"] == "披露" else None
    return amount, _dt(row["first_ingested_at"] or row["first_seen"])


def _event_marks(stored: list[str], round_norm: str, amount_status: str, amount_usd: Optional[float]) -> list[str]:
    marks = (set(stored) & set(MARK_ORDER)) - {"大额", "金额待核"}
    if amount_status not in {"币种待确认", "暂未换算", "金额待确认"} and amount_usd is not None and ((round_norm == "种子轮" and amount_usd >= 50e6)
                                   or (round_norm == "A 轮" and amount_usd >= 100e6)):
        marks.add("大额")
    if amount_status == "金额待核":
        marks.add("金额待核")
    return [mark for mark in MARK_ORDER if mark in marks]


def _extracted(data: dict) -> Extracted:
    values = {key: value for key, value in data.items() if key in Extracted.__dataclass_fields__}
    for dim in ("ai", "social"):
        values[dim] = LabelDim(**values.get(dim, {}))
    return Extracted(**values)


def _location_identity(value: str) -> str:
    city = _US_CITY_PATTERN.search(value)
    if city:
        normalized = city[0].casefold()
        return _CITY_ALIASES.get(normalized, normalized)
    foreign = _FOREIGN_PATTERN.search(value)
    return foreign[0].casefold() if foreign else name_key(value)


def _hq_location(record: dict) -> str:
    if _is_yc_directory(record["item"]):
        location = record["item"].structured.get("location") or record["item"].structured.get("all_locations") or ""
        if isinstance(location, list):
            location = location[0] if location else ""
        return str(location).split(";", 1)[0].strip()
    data = record["ext"]
    return (_headquarters_location(data.get("hq_quote", "")) or _headquarters_location(data.get("_hq_basis", ""))
            or data.get("hq_text") or data.get("_hq_basis", ""))


def _hq_conflicts(a: dict, b: dict) -> bool:
    if a["ext"]["_country"] != b["ext"]["_country"]:
        return True
    left, right = _hq_location(a), _hq_location(b)
    if not left or not right:
        return False
    if (_US_COUNTRY_PATTERN.fullmatch(left) or _US_COUNTRY_PATTERN.fullmatch(right)
            or _FOREIGN_COUNTRY_PATTERN.fullmatch(left) or _FOREIGN_COUNTRY_PATTERN.fullmatch(right)):
        return False
    x, y = _location_identity(left), _location_identity(right)
    if a["ext"]["_country"] == "美国":
        cities = (_US_CITY_PATTERN.search(left), _US_CITY_PATTERN.search(right))
        if all(cities):
            return x != y
        states = (_location_state(left), _location_state(right))
        if all(states):
            return states[0] != states[1]
        if any(cities):
            return False
    return x != y and x not in y and y not in x


def _location_state(value: str) -> Optional[str]:
    match = _US_STATE_PATTERN.search(value)
    if match:
        return _STATE_BY_ALIAS[match[0].casefold()]
    code = _US_CODE_PATTERN.search(value)
    if code:
        return code[0]
    return _CITY_STATES.get(_location_identity(value))


def _refresh_company(conn: sqlite3.Connection, company_id: str, *, fx_rates: fx.FxRates | None = None) -> None:
    company = conn.execute("SELECT * FROM company WHERE id=?", (company_id,)).fetchone()
    evidence = _evidence_rows(conn, company_id=company_id)
    if not evidence:
        return
    for record in evidence:
        # 国家判断可随配置修正；金额冻结与此独立，旧判断不能覆盖新表回填的国家。
        item = replace(record["item"], body=record["ext"].get("_hq_basis", ""))
        record["ext"]["_country"], record["ext"]["_hq_basis"] = judge_country(item, _extracted(record["ext"]))
    hq_evidence = [record for record in evidence if record["ext"]["_country"] != COUNTRY_PENDING]
    country, basis, hq, country_reason = COUNTRY_PENDING, "", None, "来源没写明国家"
    if hq_evidence:
        chosen = hq_evidence[0]
        country = chosen["ext"]["_country"]
        basis = chosen["ext"]["_hq_basis"]
        hq = _hq_location(chosen)
        if chosen["credibility"] != 0 and any(_hq_conflicts(chosen, other) for other in hq_evidence[1:]):
            country, country_reason = COUNTRY_PENDING, "国家来源有冲突"
            basis = "国家来源有冲突：" + "；".join(dict.fromkeys(record["ext"]["_hq_basis"] for record in hq_evidence))
    else:
        country_reason = evidence[0]["ext"].get("_hq_basis") or country_reason
    domains = {row[0] for row in conn.execute("SELECT domain_key FROM company WHERE name_key=? AND domain_key IS NOT NULL", (company["name_key"],))}
    identity_note = "名字相同但官网不同，可能不是同一家" if len(domains) > 1 else ""
    chosen_fields = {}
    for field in ("company_name", "website", "summary", "one_liner"):
        chosen_fields[field] = next((record["ext"][field] for record in evidence if _present(record["ext"].get(field))), None)
    # 已发出的简介保持原样；后续来源仍保留在 event_source。
    was_pushed = conn.execute("SELECT 1 FROM funding_event WHERE company_id=? AND last_pushed_date IS NOT NULL", (company_id,)).fetchone()
    summary = company["summary"] if was_pushed and company["summary"] else chosen_fields["summary"] or company["summary"]
    labels, quotes = {}, {}
    for dimension in ("ai", "social"):
        record = next((record for record in evidence if record["ext"].get(dimension, {}).get("verdict") is not None), evidence[0])
        dim = LabelDim(**record["ext"].get(dimension, {}))
        labels[dimension] = decide_dim(record["ext"].get("_keyword_hits", {}).get(dimension, False), dim)
        quotes[dimension] = {"quote": dim.quote, "source": record["url"]}
    batch = next((record["item"].structured["yc_batch"] for record in evidence if record["item"].structured.get("yc_batch")), company["yc_batch"])
    conn.execute(
        "UPDATE company SET name=?, summary=?, one_liner=?, website=COALESCE(?, website), hq=?, hq_basis=?, "
        "country=?, yc_batch=?, ai_label=?, social_label=?, label_quotes_json=? WHERE id=?",
        (chosen_fields["company_name"] or company["name"], summary, chosen_fields["one_liner"] or company["one_liner"],
         chosen_fields["website"], hq, basis or country_reason, country, batch, labels["ai"], labels["social"], _json(quotes), company_id),
    )
    for event in conn.execute("SELECT * FROM funding_event WHERE company_id=?", (company_id,)).fetchall():
        _refresh_event(conn, event, country, fx_rates=fx_rates)
    events = conn.execute("SELECT round_norm, filter_reasons_json FROM funding_event WHERE company_id=?", (company_id,)).fetchall()
    nonfiltered = [event for event in events if not json.loads(event["filter_reasons_json"])]
    if nonfiltered and all(event["round_norm"] == "待核" for event in nonfiltered):
        status = "轮次待核"
    else:
        status = "已纳入"
    conn.execute("UPDATE company SET status=?, pending_reason=? WHERE id=?",
                 (status, "来源没写轮次" if status == "轮次待核" else identity_note, company_id))
    # 产品词已在入库前对照完整正文校验；摘录可能不含该词，不能在重合并时误删。
    terms = list(dict.fromkeys(term for record in evidence for term in record["ext"].get("product_terms", [])))[:3]
    conn.execute("UPDATE company SET product_terms_json=?,city=NULL WHERE id=?", (_json(terms), company_id))
    # 查证不写进信源表；新来源合并后重放已确认的补充值，保留信源优先级。
    from .verify import apply_saved
    apply_saved(conn, company_id, fx_rates=fx_rates)


def _refresh_event(conn: sqlite3.Connection, event: sqlite3.Row, country: str, *, fx_rates: fx.FxRates | None = None) -> None:
    evidence = _evidence_rows(conn, event_id=event["id"])
    if not evidence:
        return
    chosen = next((record for record in evidence if normalize_round(
        record["ext"].get("round_text"), is_fund=record["ext"].get("is_fund_raise", False),
        is_debt_only=record["ext"].get("is_debt_only", False)) != "待核"), evidence[0])
    ext = _extracted(chosen["ext"])
    round_norm = normalize_round(ext.round_text, is_fund=ext.is_fund_raise, is_debt_only=ext.is_debt_only)
    # YC 标准投资不是 led by；有实际领投报道时，先选报道里的领投。
    lead_records = [record for record in evidence if record["ext"].get("leads")]
    lead_source = next((record for record in lead_records if record["ext"].get("round_text") != "YC 标准投资"),
                       lead_records[0] if lead_records else None)
    leads = lead_source["ext"]["leads"] if lead_source else []
    from .verify import valid_investor
    leads = list(dict.fromkeys(v for v in leads if valid_investor(v)))
    investors = list(dict.fromkeys(investor for record in evidence for investor in record["ext"].get("leads", []) + record["ext"].get("follows", []) if valid_investor(investor)))
    follows = [investor for investor in investors if investor not in leads]
    batch_record = next((record for record in evidence if record["item"].structured.get("yc_batch")), None)
    batch = batch_record["item"].structured["yc_batch"] if batch_record else None
    batches = next((record["ext"]["_latest_yc_batches"] for record in evidence if record["ext"].get("_latest_yc_batches")), [])
    item = replace(chosen["item"], structured={**chosen["item"].structured, "yc_batch": batch})
    reasons = filter_reasons(item, ext, country, round_norm, batches)
    if "总部非美国" in json.loads(event["filter_reasons_json"]):
        # 只由 R 的补充命令解除历史国家过滤，普通合并保留原记录。
        reasons = json.loads(event["filter_reasons_json"])
    nonbaseline = [record for record in evidence if not record["raw"]["is_baseline"]]
    event_date = min(_item_time(record["item"], _dt(record["raw"]["first_seen_at"])).date() for record in nonbaseline) if nonbaseline else min(
        bj_date(_dt(record["raw"]["first_seen_at"])) for record in evidence)
    _freeze_amounts(conn, event, evidence, event_date, fx_rates)
    amount_text, amount_status, amount_usd = _amount_info(evidence)
    primary = sorted(evidence, key=_source_order)[0] if amount_status == "金额待核" and " 写 " in (amount_text or "") else next(
        (record for record in evidence if _present(record["ext"].get("amount_text"))), evidence[0])
    converted = _source_amount(primary)
    marks = set(json.loads(event["marks_json"]))
    if any(record["ext"].get("_backfill") for record in evidence):
        marks.add("补抓")
    marks = _event_marks(list(marks), round_norm, amount_status, amount_usd)
    first_seen = min(_dt(event["first_seen"]), *(_dt(record["raw"]["first_seen_at"]) for record in evidence))
    conn.execute(
        "UPDATE funding_event SET round_norm=?, round_text=?, amount_text=?, amount_status=?, leads_json=?, follows_json=?, "
        "event_date=?, yc_batch=?, marks_json=?, filter_reasons_json=?, first_seen=?, "
        "amount_currency=?, amount_original=?, amount_usd=?, fx_rate=?, fx_date=?, fx_source=?, fx_stale=? WHERE id=?",
        (round_norm, ext.round_text, amount_text, amount_status, _json(leads), _json(follows), event_date.isoformat(), batch,
         _json(marks), _json(reasons), _stamp(first_seen),
         *(converted[key] for key in ("amount_currency", "amount_original", "amount_usd", "fx_rate", "fx_date", "fx_source", "fx_stale")),
         event["id"]),
    )


def _collapsed_yc(conn: sqlite3.Connection, eligible: dict) -> tuple[set[int], Optional[tuple[str, int]]]:
    groups = defaultdict(set)
    event_groups = defaultdict(set)
    # first_seen_at 是一次抓取共享的时钟；与 launched_at 无关。
    for row in conn.execute("SELECT s.event_id, e.company_id, r.* FROM event_source s "
                            "JOIN funding_event e ON e.id=s.event_id JOIN raw_item r ON r.id=s.raw_item_id WHERE r.is_baseline=0"):
        item = _raw_from_row(row)
        if _is_yc_directory(item):
            key = (row["source_id"], row["first_seen_at"], item.structured.get("yc_batch", ""))
            groups[key].add(row["company_id"])
            event_groups[row["event_id"]].add(key)
    large = {key for key, companies in groups.items() if len(companies) > 30}
    collapsed = {event_id for event_id in eligible if event_groups[event_id] & large}
    if not collapsed:
        return set(), None
    companies = {eligible[event_id]["company_id"] for event_id in collapsed}
    batches = sorted(key[2] for event_id in collapsed for key in event_groups[event_id] & large)
    return collapsed, (batches[0], len(companies))
