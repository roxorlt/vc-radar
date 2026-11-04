"""深度解读：取材、校验、持久化和定时收尾；不改主流程判定。"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import fcntl
import html
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from threading import Lock
from urllib.parse import urlsplit
from uuid import uuid4

from . import cal, config, db, http, labels, llm, scan, placenames
from .config import BJ
from .deep_fetch import Budget, Materials, Page, PageTransport, call_with_timeout, fetch_materials, normalize_url, split_sentences, url_key
from .models import LabelDim
from .robots import RobotsChecker
from .search import SearchClient

SECTIONS = ("产品是什么", "解决谁的什么问题", "怎么赚钱", "团队", "融资", "竞品", "标签依据", "风险")
MISSING = "来源里没写"
SEARCH_WARNING = "没有做搜索，可能漏掉融资报道"
_FIELD_NAMES = {"hq": "总部", "round": "轮次", "amount": "金额", "website": "官网", "ai": "AI相关", "social": "社交/社群相关"}
_ALIASES = dict(zip(SECTIONS, ("product", "problem", "business", "team", "funding", "competitors", "labels", "risks")))
_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?(?:[%％])?")
_LATIN = re.compile(r"(?<![A-Za-z0-9])[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-z0-9À-ÖØ-öø-ÿ'’+./-]*(?:[ \t]+[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-z0-9À-ÖØ-öø-ÿ'’+./-]*)*")
_UNITS = {"M", "B", "K", "USD", "EUR", "MW", "GW", "ms", "x", "CEO", "CTO", "COO"}
_OPINION = re.compile(r"值得(?:投资|关注)|颠覆|顶级|重磅|革命性|前景|有望|将会|可能(?:会|成为|面临|取代)|预测|潜力巨大")
_REMOVAL_TEXT = {
    "bad_point": "要点格式或文字无效", "bad_page": "来源页码不存在或无效",
    "bad_quote_ids": "quote_ids 须为一至两个句子编号", "quote_id_not_found": "引用编号不存在",
    "quote_ids_cross_page": "引用编号跨页", "quote_ids_not_consecutive": "引用编号不连续或顺序错误",
    "quote_page_mismatch": "页码与引用编号不一致", "quote_length": "原句长度不在 10～300 字之间",
    "quote_not_in_source": "原句不是对应页面的逐字片段", "bad_entities": "对象名称格式无效",
    "number_not_in_quote": "数字不在所引原句里", "name_not_in_quote": "人名或机构名不在所引原句里",
    "risk_opinion": "风险含评价或预测",
}
_HQ_STATES = {
    **{name.casefold(): code for name, code in zip(scan._US_STATES, scan._US_CODES)},
    **{code.casefold(): code for code in scan._US_CODES},
    "ala": "AL", "ariz": "AZ", "ark": "AR", "calif": "CA", "colo": "CO",
    "conn": "CT", "del": "DE", "fla": "FL", "ill": "IL", "ind": "IN",
    "kan": "KS", "kans": "KS", "mass": "MA", "mich": "MI", "minn": "MN",
    "miss": "MS", "mont": "MT", "neb": "NE", "nebr": "NE", "nev": "NV",
    "okla": "OK", "ore": "OR", "oreg": "OR", "tenn": "TN", "tex": "TX",
    "wash": "WA", "wva": "WV", "wis": "WI", "wisc": "WI", "wyo": "WY",
}
_HQ_STATE_SUFFIXES = sorted(_HQ_STATES, key=len, reverse=True)


def normalize_quote(value: str) -> str:
    """照搬 check_quotes.py 的空白、实体、弯引号和标点前空格口径。"""
    value = html.unescape(value)
    value = (value.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
             .replace("\u00a0", " ").replace("\u200d", ""))
    value = re.sub(r"\s+", " ", value)
    return re.sub(r" ([,.;:!?])", r"\1", value).strip()


def quote_matches(quote: str, body: str) -> bool:
    """省略号拆段，区分大小写，并要求各段在同一页按序出现。"""
    if not isinstance(quote, str) or not isinstance(body, str):
        return False
    segments = [part for part in (normalize_quote(p) for p in re.split(r"…|\.\.\.", quote)) if part]
    text, pos = normalize_quote(body), 0
    for part in segments:
        found = text.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return bool(segments)


def _numbers_present(text, quote):
    numbers = set(_NUMBER.findall(quote))
    return all(number in numbers for number in _NUMBER.findall(text))


def _company_name_key(name):
    # 深度引用额外允许省略 AI 后缀；主流程去重仍用原来的 name_key。
    name = unicodedata.normalize("NFKC", str(name))
    parts = scan.name_key(name.replace(".", " ")).split()
    while parts and (parts[-1] == "ai" or parts[-2:] == ["a", "i"]):
        parts = scan.name_key(" ".join(parts[:-1] if parts[-1] == "ai" else parts[:-2])).split()
    return "".join(parts)


def _missing_names(text, quote, entities, company_name=""):
    normalized = normalize_quote(quote)
    company_key = _company_name_key(company_name)
    names = [entity.get("name", "") for entity in entities if isinstance(entity, dict)]
    names.extend(match.group().strip().rstrip(".,") for match in _LATIN.finditer(text))
    return [name for name in dict.fromkeys(names)
            if name and name not in _UNITS and normalize_quote(name) not in normalized
            and not (company_key and _company_name_key(name) == company_key)]


def _names_present(text, quote, entities, company_name=""):
    return not _missing_names(text, quote, entities, company_name)


class _CitationError(ValueError):
    def __init__(self, code, reason=None):
        self.code, self.reason = code, reason or _REMOVAL_TEXT[code]
        super().__init__(self.reason)


def _citation(value, pages, sentences):
    if "quote_ids" not in value:
        return value.get("quote", ""), value.get("page"), None
    ids = value["quote_ids"]
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 2
            or any(not isinstance(ident, str) for ident in ids)):
        raise _CitationError("bad_quote_ids")
    positions = []
    for ident in ids:
        if ident not in sentences:
            raise _CitationError("quote_id_not_found", f"id {ident[:60]} 不存在")
        positions.append(tuple(map(int, ident[1:].split(".s"))))
    number = positions[0][0]
    if any(page != number for page, _ in positions):
        raise _CitationError("quote_ids_cross_page")
    if len(ids) == 2 and positions[1][1] != positions[0][1] + 1:
        raise _CitationError("quote_ids_not_consecutive")
    if "page" in value and (isinstance(value["page"], bool) or value["page"] != number):
        raise _CitationError("quote_page_mismatch")
    quote = " ".join(sentences[ident] for ident in ids)
    if len(quote) > 300 or not quote_matches(quote, pages[number].body):
        # 无空格相邻的中文句不能凭空添空格；回退第一句，继续守逐字校验。
        ids = ids[:1]
        quote = sentences[ids[0]][:300]
    return quote, number, ids


def _point(value, pages, section, *, sentences=None, errors=None, company_name=""):
    def reject(code, reason=None):
        if errors is not None:
            errors.append({"code": code, "reason": reason or _REMOVAL_TEXT[code]})
        return None

    if not isinstance(value, dict):
        return reject("bad_point")
    text = value.get("text", value.get("point", value.get("要点", "")))
    if not isinstance(text, str) or not text.strip() or text.strip() == MISSING:
        return reject("bad_point")
    if sentences is None:
        sentences = {item["id"]: item["text"] for page in pages.values()
                     for item in split_sentences(page.body, page.page_no)}
    try:
        quote, number, ids = _citation(value, pages, sentences)
    except _CitationError as exc:
        return reject(exc.code, exc.reason)
    if isinstance(number, bool) or not isinstance(number, int) or number not in pages:
        return reject("bad_page")
    if not isinstance(quote, str) or not 10 <= len(quote) <= 300:
        return reject("quote_length")
    if not quote_matches(quote, pages[number].body):
        return reject("quote_not_in_source")
    entities = value.get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(e, dict) or not isinstance(e.get("name"), str) for e in entities):
        return reject("bad_entities")
    fact = value.get("value")
    content = text + (" " + fact if isinstance(fact, str) else "")
    reference = "、".join(ids) if ids else f"第 {number} 页的原句"
    if not _numbers_present(content, quote):
        number_missing = next(number for number in _NUMBER.findall(content) if number not in _NUMBER.findall(quote))
        return reject("number_not_in_quote", f"数字 {number_missing[:60]} 不在 {reference} 里")
    missing_names = _missing_names(text, quote, entities, company_name)
    if isinstance(fact, str):
        missing_names.extend(_missing_names(fact, quote, [], company_name))
    if missing_names:
        return reject("name_not_in_quote", f"名称 {missing_names[0][:60]} 不在 {reference} 里")
    if section == "风险" and _OPINION.search(text):
        return reject("risk_opinion")
    point = {"text": text.strip(), "quote": quote, "page": number,
             "url": pages[number].url, "fetched_at": pages[number].fetched_at,
             "entities": [{"name": e["name"], "type": str(e.get("type", "公司")),
                           "url": str(e.get("url") or "")} for e in entities]}
    if ids is not None:
        point["quote_ids"] = ids
    for key in ("id", "field", "level", "category"):
        if isinstance(value.get(key), str):
            point[key] = value[key]
    field_name, fact = value.get("field"), value.get("value")
    if field_name in _FIELD_NAMES and isinstance(fact, str) and fact.strip() not in {"", MISSING, "未知", "待核", "拿不准", "未披露"}:
        # 事实值必须来自这条原句；规范化和显示文字由程序处理。
        if normalize_quote(fact) in normalize_quote(quote):
            if field_name != "website" or (normalize_url(fact) and not (urlsplit(normalize_url(fact)).hostname or "").endswith("crunchbase.com")):
                point["value"] = fact
    return point


@dataclass
class Validation:
    """校验后的展示结果与删半、必填项判定。"""

    result: dict = field(default_factory=dict)
    removed_points: int = 0
    total_points: int = 0
    ok: bool = False
    quality_pending: bool = False
    facts: dict = field(default_factory=dict)
    error: str = ""
    removed_reasons: list[dict] = field(default_factory=list)
    previous_errors: list[dict] = field(default_factory=list)


def _required_errors(product_count, funding_count, source_count):
    errors = []
    for title, minimum, count in (("产品是什么", 2, product_count), ("融资", 1, funding_count),
                                  ("全部来源", 1, source_count)):
        if count < minimum:
            requirement = "条来源" if title == "全部来源" else "条有原句的要点"
            errors.append({"section": title, "code": "required_empty",
                           "reason": f"{title}至少需要 {minimum} {requirement}，现在 {count} 条"})
    return errors


def validate_result(payload: dict, pages: list[Page], *, company_name: str = "") -> Validation:
    """逐条校验引用、数字、名称和标签；当前公司名允许在原句中省略。"""
    checked = Validation()
    by_no = {page.page_no: page for page in pages}
    sentences = {item["id"]: item["text"] for page in pages
                 for item in split_sentences(page.body, page.page_no)}
    sections = payload.get("sections", payload) if isinstance(payload, dict) else {}
    if not isinstance(sections, dict):
        checked.error = "bad_sections"
        checked.previous_errors.extend(_required_errors(0, 0, len(pages)))
        return checked
    full_text = "\n".join(page.body for page in pages)

    def check_point(item, title, index):
        checked.total_points += 1
        errors = []
        point = _point(item, by_no, title, sentences=sentences, errors=errors, company_name=company_name)
        if point is None:
            checked.removed_points += 1
            detail = {"section": title, "point_index": index, **errors[0]}
            if isinstance(item, dict) and isinstance(item.get("field"), str):
                detail["field"] = item["field"][:60]
            checked.removed_reasons.append(detail)
            checked.previous_errors.append({**detail, "point": item})
        return point

    for title in SECTIONS:
        raw = sections.get(title, sections.get(_ALIASES[title], []))
        if title == "标签依据":
            raw = raw if isinstance(raw, dict) else {}
            dimensions = {}
            for dimension in ("ai", "social"):
                item = raw.get(dimension, {})
                item = item if isinstance(item, dict) else {}
                verdict = {"yes": "是", "no": "否", "uncertain": "拿不准"}.get(item.get("verdict"), item.get("verdict"))
                reason = item.get("reason") or item.get("text") or ""
                reason = reason if isinstance(reason, str) else ""
                candidate = {**item, "text": reason or ("AI相关" if dimension == "ai" else "社交/社群相关")}
                point = None
                if item.get("quote") or "quote_ids" in item:
                    point = check_point(candidate, title, dimension)
                decided = labels.decide_dim(labels.keyword_hit(full_text, dimension), LabelDim(verdict, point["quote"] if point else ""))
                if decided == "否" and not reason.strip():
                    decided = "待核"
                if point is None and (_NUMBER.search(reason) or not _names_present(reason, "", [], company_name)):
                    # 无引用的标签理由不能夹带新的数字或专名事实。
                    decided, reason = "待核", MISSING
                dimensions[dimension] = {"verdict": decided, "text": reason.strip() or MISSING}
                if point:
                    dimensions[dimension].update(point)
                    if decided in {"是", "否"}:
                        checked.facts[dimension] = {**point, "value": decided}
            checked.result[title] = dimensions
            continue
        if isinstance(raw, dict):
            raw = raw.get("points", [])
        if not isinstance(raw, list):
            raw = []
        kept = []
        for index, item in enumerate(raw, 1):
            if item == MISSING or (isinstance(item, dict) and (item.get("status") == "not_found" or item.get("text") == MISSING)):
                continue
            point = check_point(item, title, index)
            if point is not None:
                kept.append(point)
                if point.get("field") in _FIELD_NAMES and point.get("value"):
                    if point["field"] in {"amount", "round"}:
                        checked.facts.setdefault(point["field"], []).append(point)
                    else:
                        checked.facts[point["field"]] = point
        checked.result[title] = kept or MISSING
    # 收费方式和单价分开，融资缺少的子字段也保留。
    for title, fields in (("怎么赚钱", ("收费方式", "单价")), ("融资", ("round", "amount", "leads", "follows", "date"))):
        points = checked.result[title]
        if title == "怎么赚钱" and points == MISSING:
            points = checked.result[title] = []
        if isinstance(points, list):
            present = {point.get("field") for point in points}
            points.extend({"field": name, "text": MISSING} for name in fields if name not in present)
    checked.result["全部来源"] = [{"page": page.page_no, "url": page.url, "fetched_at": page.fetched_at} for page in pages]
    product = checked.result["产品是什么"]
    funding = checked.result["融资"]
    product_count = sum(bool(point.get("quote")) for point in product) if isinstance(product, list) else 0
    funding_count = sum(bool(point.get("quote")) for point in funding) if isinstance(funding, list) else 0
    checked.previous_errors.extend(_required_errors(product_count, funding_count, len(pages)))
    checked.ok = product_count >= 2 and funding_count >= 1 and bool(pages)
    checked.quality_pending = checked.removed_points > checked.total_points / 2
    checked.error = "" if checked.ok else "required_empty"
    return checked


def load_link_records(path: Path | None = None) -> list[dict]:
    """只读加载人工核对记录；坏文件不授予任何链接出处。"""
    from .render import read_link_records
    path = path if path is not None else config.Config().links_file
    return [{**record, "_record_file": str(path)} for record in read_link_records(path)]


def _person_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return ((host == "linkedin.com" or host.endswith(".linkedin.com")) and parsed.path.startswith("/in/")) or bool(
        re.search(r"/(?:team|about|people|partners?|leadership|our-team)(?:/|$|[-_])", parsed.path, re.I))


def resolve_objects(result: dict, company: dict, pages: list[Page], records: list[dict], today: date) -> dict:
    """对象网址只能逐个取自实际来源或已核对记录，不接受模型自报出处。"""
    proof = {}
    for page in pages:
        for link in [{"url": page.url, "text": "", "body": page.body}, *page.links, *page.website_fields]:
            url = normalize_url(link.get("url", ""))
            if url:
                proof.setdefault(url, []).append({"page": page.page_no, "source_url": page.url,
                                                  "external_link": url, "text": link.get("text", ""),
                                                  "body": link.get("body", ""), "field": link.get("field")})
    for source in company.get("sources", []):
        url = normalize_url(source.get("structured", {}).get("website", ""))
        if url:
            proof.setdefault(url, []).append({"source_url": source["url"], "field": "website", "text": company["name"]})

    def resolve(entity):
        name, kind = entity["name"], entity.get("type", "公司")
        requested = normalize_url(entity.get("url", ""))
        answer = {"name": name, "type": kind, "url": "", "url_provenance": None, "url_source": "", "verified_at": None}
        for url in ([requested] if requested else []) + [u for u in proof if u != requested]:
            if not url or re.search(r"^https?://(?:[^/]+\.)?crunchbase\.com(?::\d+)?/", url, re.I):
                continue
            for origin in proof.get(url, []):
                same_name = name.casefold() in str(origin.get("text", "")).casefold()
                company_field = name == company["name"] and origin.get("field") == "website"
                person_page = kind == "人" and name in origin.get("body", "")
                if not (same_name or company_field or person_page):
                    continue
                if kind == "人" and not _person_url(url):
                    continue
                answer.update(url=url, url_provenance={k: v for k, v in origin.items() if k not in {"text", "body"}}, url_source="来源页")
                return {**answer, "target": "_blank", "rel": "noopener nofollow"}
        for record in records:
            if (record.get("verified") is not True or record.get("name") != name
                    or str(record.get("company", "")).casefold() not in {company["name"].casefold(), company["id"].casefold()}):
                continue
            try:
                verified_at = datetime.strptime(record["verified_at"], "%Y-%m-%d %H:%M")
            except (KeyError, TypeError, ValueError):
                continue
            url = normalize_url(record.get("url", ""))
            if (not url or verified_at.date() > today or "crunchbase.com/" in url.lower()
                    or (kind == "人" and not _person_url(url))):
                continue
            source_kind = "来源页" if str(record.get("verified_by", "")).startswith("来源页") else "核对记录"
            answer.update(url=url, url_provenance={"record": record.get("_record_file", str(config.Config().links_file)), "name": name, "company": record["company"]},
                          url_source=source_kind, verified_at=record["verified_at"])
            return {**answer, "target": "_blank", "rel": "noopener nofollow"}
        return answer

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("entities"), list):
                value["entities"] = [resolve(entity) for entity in value["entities"]]
            for key, child in value.items():
                if key != "entities":
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(result)
    result["objects"] = [resolve({"name": company["name"], "type": "公司", "url": company.get("website", "")})]
    return result


def _hq_identity(value):
    value = str(value or "").casefold()
    value = re.sub(r"\b(?:[a-z]\.\s*){2,}", lambda match: re.sub(r"[^a-z]", "", match[0]), value)
    value = " ".join("".join(char if char.isalnum() else " " for char in value.replace(".", "")).split())
    value = re.sub(r"(?:^|\s)(?:usa|us|united states(?: of america)?)$", "", value).strip()
    state = ""
    for suffix in _HQ_STATE_SUFFIXES:
        if value.endswith(" " + suffix):
            value, state = value[:-len(suffix)].strip(), _HQ_STATES[suffix]
            break
    city = {"nyc": "new york", "new york city": "new york", "纽约": "new york",
            "旧金山": "san francisco"}.get(value, value)
    if not state and city in _HQ_STATES and city != "new york":
        return "", _HQ_STATES[city]
    return city, state or scan._CITY_STATES.get(city, "")


def _same_hq(left, right):
    left_city, left_state = left
    right_city, right_state = right
    if left_state and right_state and left_state != right_state:
        return False
    if left_city and right_city:
        return left_city == right_city
    # 只补充城市或州的细节不算冲突，已有的城市和州仍须相同。
    return left == right or bool(left_state and left_state == right_state)


def _identity(field_name, value):
    value = str(value or "").strip()
    if field_name == "hq":
        return _hq_identity(value)
    if field_name == "round":
        return scan.normalize_round(value)
    if field_name == "amount":
        amount = scan.parse_amount_usd(value)
        return amount if amount is not None else re.sub(r"\s+", "", value).casefold()
    if field_name == "website":
        return re.sub(r"^https?://(?:www\.)?", "", normalize_url(value)).rstrip("/")
    return value


def _fact_points(facts, name):
    points = facts.get(name, [])
    return points if isinstance(points, list) else [points]


def _historical_funding(quote, event_date):
    if re.search(r"\b(?:previous(?:ly)?|prior|earlier)\b|\blast\s+year\b|"
                 r"\b(?:another|last|preceding)\s+"
                 r"(?:(?:pre[- ]?seed|seed|angel|series[- ]?[a-z])\s+)?(?:round|funding|financing|investment)\b|"
                 r"\bpreceding\s+(?:pre[- ]?seed|seed|angel|series[- ]?[a-z])\b|"
                 r"此前|之前|先前|上一轮|早前|另一轮", quote, re.I):
        return True
    if not event_date:
        return False
    try:
        current = date.fromisoformat(str(event_date)[:10])
    except ValueError:
        return False
    dates = []
    for year, month, day in re.findall(r"(?<!\d)(20\d{2})[-/年](\d{1,2})(?:[-/月](\d{1,2}))?", quote):
        try:
            dates.append(date(int(year), int(month), int(day or 1)))
        except ValueError:
            pass
    months = {name: index for index, name in enumerate(("January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"), 1)}
    for name, month in months.items():
        month_pattern = name[:3] + r"(?:" + name[3:] + r")?\.?"
        for day, year in re.findall(r"\b(\d{1,2})\s+" + month_pattern + r"\s+(20\d{2})\b", quote, re.I):
            try:
                dates.append(date(int(year), month, int(day)))
            except ValueError:
                pass
        pattern = r"\b" + month_pattern + r"\s+(?:(\d{1,2}),?\s+)?(20\d{2})\b"
        for day, year in re.findall(pattern, quote, re.I):
            try:
                # 原文只有月份时按月底判断，避免把不足 60 天的同轮报道误排除。
                next_month = date(int(year) + (month == 12), month % 12 + 1, 1)
                dates.append(date(int(year), month, int(day)) if day else next_month - timedelta(days=1))
            except ValueError:
                pass
    return any((current - stamp).days > 60 for stamp in dates)


def _funding_matches(judgement, point, facts):
    quote = point["quote"]
    if _historical_funding(quote, judgement.get("event_date")):
        return False
    current = scan.normalize_round(judgement.get("round"))
    # 给嵌在中文里的 A轮 等写法补边界，仍沿用主流程的轮次归类。
    round_quote = re.sub(r"(?<=[\u3400-\u9fff])(?=[A-Za-z])", " ", quote)
    round_quote = re.sub(r"([A-Za-z](?:[-+]\d*)?)\s*轮", r"\1 轮 ", round_quote)
    mentioned = scan.normalize_round(round_quote)
    if point.get("field") == "round":
        mentioned = scan.normalize_round(point["value"])
    elif point.get("field") == "amount":
        # 累计限定优先排除，即便同句也写了轮次或「本轮」。
        if re.search(r"\b(?:total|to\s+date|so\s+far|overall|cumulative|all\s+told)\b|"
                     r"累计|总计|共计|迄今|至今|总额|总融资", quote, re.I):
            return False
        if mentioned == "待核":
            # 原句自身须有本轮依据，不能借用同页其他句子的轮次。
            return bool(re.search(r"\b(?:(?:this|the)\s+(?:round|funding)|the\s+investment)\b|本轮", quote, re.I))
    # 主流程轮次待核时，当前报道的明确轮次仍属于补充证据。
    return mentioned != "待核" and (current == "待核" or current == mentioned)


def find_new_evidence(judgement: dict, facts: dict, ended_at: str) -> list[dict]:
    """融资只比较本轮证据，六类差异均保留原句且不自动改判。"""
    evidence = []
    for field_name, label in _FIELD_NAMES.items():
        for point in _fact_points(facts, field_name):
            if not point or not point.get("quote") or not point.get("url"):
                continue
            if field_name in {"amount", "round"} and not _funding_matches(judgement, {"field": field_name, **point}, facts):
                continue
            old, value = judgement.get(field_name), point["value"]
            before, after = _identity(field_name, old), _identity(field_name, value)
            if before == after or (field_name == "hq" and _same_hq(before, after)):
                continue
            evidence.append({"field": label, "value": value, "old_value": old, "quote": point["quote"],
                             "page": point["page"], "url": point["url"], "found_at": ended_at})
            break
    return evidence


def usage_cost(usage: dict, started_at: datetime) -> float:
    """按调用开始的北京时间计缓存命中、未命中输入和输出费用。"""
    stamp = started_at.astimezone(BJ)
    peak = (stamp.weekday() < 5 and cal.is_cn_workday_or_weekday(stamp.date())
            and (9 <= stamp.hour < 12 or 14 <= stamp.hour < 18))
    prompt = max(0, int(usage.get("prompt_tokens", 0)))
    hit = min(prompt, max(0, int(usage.get("prompt_cache_hit_tokens", 0))))
    output = max(0, int(usage.get("completion_tokens", 0)))
    price = (Decimal(hit) * Decimal("0.02") + Decimal(prompt - hit) + Decimal(output) * 4) / 1_000_000
    return float(price * (2 if peak else 1))


def _stamp(value):
    parsed = datetime.fromisoformat(str(value))
    return parsed.replace(tzinfo=BJ) if parsed.tzinfo is None else parsed.astimezone(BJ)


def select_companies(conn, day: date, *, company_ids=None, now: datetime | None = None, include_skipped=False) -> list[dict]:
    """按首次入库日选择已纳入公司；指定 id 可重做终态，但仍排除待核和过滤公司。"""
    now = (now or datetime.now(BJ)).astimezone(BJ)
    companies = [dict(row) for row in conn.execute("SELECT * FROM company WHERE status='已纳入' ORDER BY id")]
    known_ids = {row[0] for row in conn.execute("SELECT id FROM company")}
    if company_ids is not None and set(company_ids) - known_ids:
        raise ValueError("公司 id 不存在")
    events = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM funding_event")
              if not json.loads(row["filter_reasons_json"]) and row["round_norm"] != "待核"}
    collapsed, _ = scan._collapsed_yc(conn, events)
    contexts = []
    for company in companies:
        cid = company["id"]
        if company_ids is not None and cid not in company_ids:
            continue
        eligible = [event for event in events.values() if event["company_id"] == cid]
        if not eligible:
            continue
        admitted = min(_stamp(event["first_ingested_at"] or event["first_seen"]).date() for event in eligible)
        if company_ids is None and admitted != day:
            continue
        skipped = all(event["id"] in collapsed for event in eligible)
        if skipped and not include_skipped:
            continue
        history = [dict(row) for row in conn.execute("SELECT * FROM deepdive WHERE company_id=? ORDER BY started_at DESC, run_date DESC", (cid,))]
        if any(row["running"] and row["started_at"] and (now - _stamp(row["started_at"])).total_seconds() < 180 for row in history):
            continue
        if company_ids is None and history and history[0]["status"] not in {"失败", "生成中"}:
            continue
        latest = max(eligible, key=lambda event: (event["event_date"], event["first_ingested_at"] or event["first_seen"], event["id"]))
        judgement = {"name": company["name"], "hq": company["hq"], "round": latest["round_norm"],
                     "amount": latest["amount_text"], "amount_display": scan.amount_label(conn, latest),
                     "website": company["website"],
                     "ai": company["ai_label"], "social": company["social_label"],
                     "event_date": latest["event_date"],
                     "marks": json.loads(latest["marks_json"])}
        source_rows = scan._evidence_rows(conn, event_id=latest["id"])
        source_rows.sort(key=scan._source_order)
        sources = [{"url": row["url"], "source_id": row["raw"]["source_id"], "source_name": row["source_name"],
                    "excerpt": row["raw"]["excerpt"], "fetched_at": row["raw"]["first_seen_at"],
                    "structured": json.loads(row["raw"]["structured_json"])} for row in source_rows]
        contexts.append({**company, "sources": sources, "leads": json.loads(latest["leads_json"]),
                         "_place_names": placenames.company_names(conn, cid),
                         "judgement": judgement, "admitted_on": admitted.isoformat(), "skipped": skipped,
                         "forced": company_ids is not None})
    return contexts


@dataclass
class DeepResult:
    """一次公司的执行结果；后台线程只产生此值，由主线程写 SQLite。"""

    company_id: str
    name: str
    status: str = "失败"
    started_at: str = ""
    ended_at: str = ""
    elapsed_s: float = 0.0
    materials: Materials = field(default_factory=Materials)
    model_calls: int = 0
    usage: list[dict] = field(default_factory=list)
    result: dict = field(default_factory=dict)
    removed_points: int = 0
    quality_pending: bool = False
    new_evidence: list[dict] = field(default_factory=list)
    error: str = ""
    run_token: str = ""
    removed_reasons: list[dict] = field(default_factory=list)
    place_audit: placenames.PlaceAudit = field(default_factory=placenames.PlaceAudit)

    @property
    def cost(self) -> float:
        """本次尝试的搜索和模型费用。"""
        return sum(record.get("cost", 0) for record in self.usage)

    @property
    def tokens(self) -> dict:
        """本次所有模型调用（含失败和重试）的 token。"""
        return {key: sum(record.get(key, 0) for record in self.usage) for key in (
            "prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens")}


def _usage(value):
    result = {}
    for key in ("prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens"):
        try:
            result[key] = max(0, int((value or {}).get(key, 0)))
        except (ValueError, TypeError, OverflowError, AttributeError):
            result[key] = 0
    result["prompt_cache_hit_tokens"] = min(result["prompt_tokens"], result["prompt_cache_hit_tokens"])
    return result


def _work(company, cfg, clock, budget, transport, robots, search_client, client, link_records, output):
    try:
        output.materials = fetch_materials(company, cfg, clock, transport, search_client, robots=robots, budget=budget)
        search = output.materials.search
        output.usage.append({"kind": "search", "started_at": output.started_at, "elapsed_s": search.elapsed_s,
                             "calls": int(search.attempted), "cost": search.cost, "ok": search.ok})
        if not output.materials.pages:
            output.error = "all_pages_failed"
            return output
        prompt = (Path(__file__).parent / "prompts" / "deep.txt").read_text(encoding="utf-8")
        request = {"company": company["name"], "pages": [
            {"page": page.page_no, "url": page.url, "kind": page.kind,
             "sentences": split_sentences(page.body, page.page_no),
             "fetched_at": page.fetched_at} for page in output.materials.pages]}
        user = json.dumps(request, ensure_ascii=False)
        checked = None
        place_retry = False
        place_fallback = None

        def accept_result(value, issues):
            output.result = resolve_objects(value.result, company, output.materials.pages, link_records,
                                            clock.now().astimezone(BJ).date())
            placenames.record_remaining(company["name"], issues, audit=output.place_audit)
            output.quality_pending = value.quality_pending
            if not search.ok:
                risk = output.result["风险"]
                output.result["风险"] = (risk if isinstance(risk, list) else []) + [{"text": SEARCH_WARNING, "system_notice": True}]
            output.status, output.error = "完成", ""

        for attempt in range(2):
            remaining = budget.remaining()
            if remaining <= 0 or (attempt and remaining < 30):
                if remaining > 0 and place_fallback is not None:
                    break
                output.error = "retry_time_exhausted" if attempt else "company_timeout"
                return output
            timeout = min(120.0, remaining)
            started = clock.now().astimezone(BJ)
            output.model_calls += 1
            if place_retry:
                placenames.record_retry(company["name"], audit=output.place_audit)
                place_retry = False
            try:
                response = call_with_timeout(lambda: client.complete_json(prompt, user, timeout), timeout)
            except TimeoutError:
                response = llm.LLMResult(False, error="timeout", elapsed_s=timeout)
            except Exception:
                response = llm.LLMResult(False, error="client_failed")
            counts = _usage(response.usage)
            output.usage.append({"kind": "model", "started_at": started.isoformat(), "elapsed_s": max(0, response.elapsed_s),
                                 "model": getattr(client, "model", "deepseek-flash"), "calls": 1, **counts,
                                 "cost": usage_cost(counts, started), "ok": bool(response.ok)})
            # 只留模型输出文本，不存请求、请求头或客户端配置；坏 JSON 也原样保留。
            safe_id = re.sub(r"[^\w-]", "_", output.company_id)
            _atomic_text(cfg.raw_dir / output.started_at[:10] / f"deep-{safe_id}-call{attempt + 1}.json",
                         response.content)
            if budget.remaining() <= 0:
                output.error = "company_timeout"
                return output
            if not response.ok:
                output.error = "model_failed"
                continue
            try:
                payload = json.loads(response.content)
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, TypeError):
                output.error = "bad_json"
                continue
            checked = validate_result(payload, output.materials.pages, company_name=company["name"])
            output.removed_points += checked.removed_points
            output.removed_reasons.extend({"call": attempt + 1, **detail} for detail in checked.removed_reasons)
            output.quality_pending = checked.quality_pending
            checked.result, _, place_issues = placenames.normalize_document(checked.result,
                names=placenames.protected_names(company), company=company["name"], audit=output.place_audit)
            if checked.ok and not checked.quality_pending and place_issues:
                place_fallback = checked, place_issues
            if (checked.quality_pending or not checked.ok or (place_issues and budget.remaining() >= 30)) and attempt == 0:
                # 必填不足、删半重试和失败重试共用两次总额度。
                output.error = "validation_retry"
                request["previous_errors"] = [*checked.previous_errors, *(
                    {"code": "place_wording", "reason": issue, "instruction": "公司在哪里写在<地点>，地名用中文，名称和原句不动"}
                    for issue in place_issues)]
                user = json.dumps(request, ensure_ascii=False)
                place_retry = bool(place_issues)
                continue
            if not checked.ok:
                if place_fallback is not None:
                    break
                output.error = checked.error
                return output
            accept_result(checked, place_issues)
            break
        if output.status != "完成" and place_fallback is not None and budget.remaining() > 0:
            checked, issues = place_fallback
            accept_result(checked, issues)
        if output.status != "完成":
            return output
        if budget.remaining() <= 0:
            output.status, output.result, output.error = "失败", {}, "company_timeout"
        else:
            output.new_evidence = find_new_evidence(company["judgement"], checked.facts, clock.now().astimezone(BJ).isoformat())
            output.new_evidence, _, _ = placenames.normalize_document(output.new_evidence,
                names=placenames.protected_names(company), company=company["name"], audit=output.place_audit)
        return output
    except Exception:
        # 仅保留固定错误码，禁止异常正文或模型请求意外进入日志。
        output.error = "generation_failed"
        return output
    finally:
        output.elapsed_s = budget.elapsed()
        output.ended_at = clock.now().astimezone(BJ).isoformat()
        for evidence in output.new_evidence:
            evidence["found_at"] = output.ended_at


def _ensure_removed_reasons(conn):
    # 本任务不改 db.py；在深度入口的写事务内幂等增列，旧记录默认没有删除原因。
    if "removed_reasons_json" not in {row[1] for row in conn.execute("PRAGMA table_info(deepdive)")}:
        conn.execute("ALTER TABLE deepdive ADD COLUMN removed_reasons_json TEXT NOT NULL DEFAULT '[]'")


def _claim(conn, company, run_date, clock, *, skipped=False):
    now = clock.now().astimezone(BJ)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        _ensure_removed_reasons(conn)
        for row in conn.execute("SELECT run_date, started_at FROM deepdive WHERE company_id=? AND running=1", (company["id"],)).fetchall():
            if row["started_at"] and (now - _stamp(row["started_at"])).total_seconds() < 180:
                return False
            conn.execute("UPDATE deepdive SET status='失败', running=0, ended_at=?, error='stale_run' WHERE company_id=? AND run_date=?",
                         (now.isoformat(), company["id"], row["run_date"]))
        latest = conn.execute("SELECT status FROM deepdive WHERE company_id=? ORDER BY started_at DESC, run_date DESC LIMIT 1",
                              (company["id"],)).fetchone()
        if not company.get("forced") and latest and latest["status"] in {"完成", "暂时没有", "不做"}:
            return False
        company["run_token"] = uuid4().hex
        conn.execute(
            "INSERT INTO deepdive(company_id, run_date, status, running, started_at, push_judgement_json, simulated, run_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(company_id, run_date) DO UPDATE SET "
            "status=excluded.status, running=excluded.running, started_at=excluded.started_at, "
            "ended_at=NULL, online_at=NULL, error='', simulated=excluded.simulated, run_token=excluded.run_token, "
            "push_judgement_json=CASE WHEN deepdive.push_judgement_json='{}' THEN excluded.push_judgement_json ELSE deepdive.push_judgement_json END",
            (company["id"], run_date, "不做" if skipped else "生成中", int(not skipped), now.isoformat(),
             json.dumps(company["judgement"], ensure_ascii=False), int(bool(getattr(clock, "simulated", False))), company["run_token"]),
        )
    return True


def _save(conn, output, run_date):
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        _ensure_removed_reasons(conn)
        return _save_claimed(conn, output, run_date)


def _save_claimed(conn, output, run_date):
    old = conn.execute("SELECT usage_json, estimated_cost, run_token, status, error FROM deepdive WHERE company_id=? AND run_date=?",
                       (output.company_id, run_date)).fetchone()
    history = json.loads(old["usage_json"])
    # 老库只有汇总费用，也保留到累计账里。
    missing_cost = old["estimated_cost"] - sum(record.get("cost", 0) for record in history)
    if missing_cost > 1e-10:
        history.append({"kind": "legacy", "started_at": output.started_at, "cost": missing_cost})
    new_records = [] if any(record.get("run_token") == output.run_token for record in history) else [
        {**record, "run_token": output.run_token} for record in output.usage]
    history.extend(new_records)
    cumulative_cost = old["estimated_cost"] + sum(record.get("cost", 0) for record in new_records)
    if (old["run_token"] != output.run_token
            or (old["status"] == "暂时没有" and old["error"] == "final_deadline" and output.status != "暂时没有")):
        # 已被接管或到达最终截止的尝试只记费用，不再覆盖结果、状态和页面。
        conn.execute("UPDATE deepdive SET usage_json=?, estimated_cost=? WHERE company_id=? AND run_date=?",
                     (json.dumps(history, ensure_ascii=False), cumulative_cost, output.company_id, run_date))
        return False
    # 存储再守一次唯一键，兼容调用方直接提供取材页的离线回放。
    pages, aliases = {}, {}
    for page in output.materials.pages:
        key = url_key(page.url)
        if key in pages:
            aliases[page.page_no] = pages[key]
        else:
            pages[key] = page
    if aliases:
        def remap(value):
            if isinstance(value, dict):
                old_number = value.get("page")
                page = aliases.get(old_number)
                if page is not None:
                    if isinstance(value.get("quote_ids"), list):
                        value["quote_ids"] = [ident.replace(f"p{old_number}.s", f"p{page.page_no}.s", 1)
                                              for ident in value["quote_ids"]]
                    value.update(page=page.page_no, url=page.url)
                for child in value.values():
                    remap(child)
            elif isinstance(value, list):
                for child in value:
                    remap(child)

        remap(output.result)
        remap(output.new_evidence)
        output.materials.pages = list(pages.values())
        if "全部来源" in output.result:
            output.result["全部来源"] = [{"page": page.page_no, "url": page.url, "fetched_at": page.fetched_at}
                                        for page in output.materials.pages]
    counts = output.tokens
    materials = [*output.materials.records, *(
        {"page": page.page_no, "url": page.url, "fetched_at": page.fetched_at, "http_code": page.http_code,
         "chars": len(page.body), "footer": page.footer, "kind": page.kind, "reused": page.reused}
        for page in output.materials.pages)]
    values = {
        "status": output.status, "running": 0, "result_json": json.dumps(output.result, ensure_ascii=False),
        "materials_json": json.dumps(materials, ensure_ascii=False), "search_count": int(output.materials.search.attempted),
        "model_calls": output.model_calls, "input_tokens": counts["prompt_tokens"], "output_tokens": counts["completion_tokens"],
        **counts, "elapsed_s": output.elapsed_s, "estimated_cost": cumulative_cost,
        "attempt_cost": output.cost, "usage_json": json.dumps(history, ensure_ascii=False),
        "removed_points": output.removed_points, "quality_pending": int(output.quality_pending),
        "removed_reasons_json": json.dumps(output.removed_reasons, ensure_ascii=False),
        "new_evidence_json": json.dumps(output.new_evidence, ensure_ascii=False),
        "new_evidence_found_at": output.ended_at if output.new_evidence else None,
        "ended_at": output.ended_at, "error": output.error,
    }
    with conn:
        conn.execute("UPDATE deepdive SET " + ", ".join(f"{key}=?" for key in values) + " WHERE company_id=? AND run_date=?",
                     (*values.values(), output.company_id, run_date))
        conn.execute("DELETE FROM deepdive_pages WHERE company_id=? AND run_date=?", (output.company_id, run_date))
        conn.executemany(
            "INSERT INTO deepdive_pages(company_id, run_date, page_no, url, fetched_at, http_code, body, links_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(output.company_id, run_date, page.page_no, page.url, page.fetched_at, page.http_code, page.body,
              json.dumps([*page.links, *page.website_fields], ensure_ascii=False)) for page in output.materials.pages],
        )
    return True


def _atomic_json(path, value):
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".deep-", suffix=".tmp", encoding="utf-8", delete=False) as handle:
            name = handle.name
            handle.write(value)
        os.replace(name, path)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


def write_usage(conn, cfg):
    """由已提交的逐次用量重建月账；重复补跑累计，且测试账和抽取账隔离。"""
    directory = cfg.state_dir / "test" if cfg.test_mode else cfg.state_dir
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "deep-usage.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        months = {}
        for row in conn.execute("SELECT run_date, started_at, usage_json, estimated_cost FROM deepdive"):
            records = json.loads(row["usage_json"])
            old_cost = row["estimated_cost"] - sum(record.get("cost", 0) for record in records)
            if old_cost > 1e-10:
                records.append({"kind": "legacy", "started_at": row["started_at"] or row["run_date"], "cost": old_cost})
            for record in records:
                stamp = _stamp(record.get("started_at") or row["run_date"])
                month = months.setdefault(stamp.strftime("%Y-%m"), {"estimated_cost": 0.0, "prompt_tokens": 0,
                    "prompt_cache_hit_tokens": 0, "completion_tokens": 0, "model_calls": 0, "search_count": 0, "days": {}})
                day = month["days"].setdefault(stamp.date().isoformat(), {"estimated_cost": 0.0})
                month["estimated_cost"] += record.get("cost", 0)
                day["estimated_cost"] += record.get("cost", 0)
                for key in ("prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens"):
                    month[key] += record.get(key, 0)
                if record.get("kind") == "model":
                    month["model_calls"] += record.get("calls", 0)
                if record.get("kind") == "search":
                    month["search_count"] += record.get("calls", 0)
        _atomic_json(directory / "deep-usage.json", months)


class _Log:
    def __init__(self, path):
        self.path, self.lock = path, Lock()

    def write(self, record):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class _OfflineTransport:
    def get(self, url, headers=None, timeout=15.0, data=None):
        return http.HttpResponse(url, None, error="conn:no fixture")


def _client(cfg, fixture):
    # 带 --fixture 用回放；--dry-run 不带 fixture = 真实取材 + 真实模型，只写测试库（同主流程口径）。
    if fixture:
        path = Path(fixture) / "deep_responses.json" if fixture else None
        if path and not path.exists():
            path = Path(fixture) / "model_responses.json"
        responses = json.loads(path.read_text()) if path and path.exists() else []
        if isinstance(responses, dict) and "companies" in responses:
            return lambda company: llm.MockLLMClient(responses["companies"].get(company["id"], []), model="deepseek-flash")
        if isinstance(responses, dict):
            responses = responses.get("responses", [])
        if not isinstance(responses, list):
            raise ValueError("深度模型 fixture 必须是数组")
        client = llm.MockLLMClient(responses, model="deepseek-flash")
    else:
        key = config.load_api_key(cfg)
        client = llm.DeepSeekClient(key, model="deepseek-flash") if key else llm.MockLLMClient([{"error": "missing_key"}] * 2)
    return lambda company: client


def _report(output):
    tokens = output.tokens
    fields = "、".join(item["field"] for item in output.new_evidence) or "无"
    reasons = Counter(item["code"] for item in output.removed_reasons)
    top = "；".join(f"{_REMOVAL_TEXT[code]} ×{count}" for code, count in reasons.most_common(3)) or "无"
    print(f"{output.name} | {output.status} | 耗时 {output.elapsed_s:.2f} 秒 | 抓取页数 {output.materials.fetch_count} "
          f"| 模型调用 {output.model_calls} | token 输入/缓存命中/输出 "
          f"{tokens['prompt_tokens']}/{tokens['prompt_cache_hit_tokens']}/{tokens['completion_tokens']} "
          f"| 费用 ¥{output.cost:.6f} | 删掉要点 {output.removed_points} "
          f"| 删除原因 Top3 {top} | 质量待核 {'是' if output.quality_pending else '否'} | 有新证据 {fields}")


def run_for_today(conn, cfg, clock, *, company_ids=None, deep_date: date | None = None, transport=None,
                  client=None, search_client=None, fixture: Path | None = None, link_records=None,
                  source_bodies=None, publish=None, deep_deadline=None, deep_final=None,
                  checkpoint=None, sleep=None) -> list[DeepResult]:
    """并发五家；定时运行守两个时点和最终截止，手动运行只守每家 180 秒。"""
    started = clock.now().astimezone(BJ)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        _ensure_removed_reasons(conn)
    run_date = started.date().isoformat()
    if (deep_deadline is None) != (deep_final is None):
        raise ValueError("定时深度需要两个时点")
    if deep_final is not None and deep_final <= deep_deadline:
        raise ValueError("最终截止必须晚于深度目标时间")
    final_budget = Budget(clock, max(0, (deep_final - started).total_seconds())) if deep_final else None
    deadline_built = False
    log = _Log(cfg.log_dir / f"deep-{run_date}.log")
    place_counts = placenames.PlaceAudit()

    def force_build():
        if checkpoint is not None:
            try:
                checkpoint(conn, cfg, clock)
            except Exception:
                log.write({"event": "deep_checkpoint_failed", "at": clock.now().isoformat()})

    def idle(seconds):
        # fixture 的空闲等待只推进模拟钟；有任务时仍让工作线程正常执行。
        if sleep is not None:
            sleep(seconds)
        elif fixture:
            clock.advance(seconds)
        else:
            time.sleep(seconds)

    def finish_checkpoints():
        nonlocal deadline_built
        if deep_final is None:
            return
        for moment in ([deep_final] if deadline_built else [deep_deadline, deep_final]):
            while clock.now() < moment:
                idle(min(60.0, (moment - clock.now()).total_seconds()))
            force_build()
            deadline_built = True

    companies = select_companies(conn, deep_date or started.date(), company_ids=company_ids, now=started, include_skipped=True)
    if not companies:
        finish_checkpoints()
        print("总计 0 家，模型调用 0，费用 ¥0.000000")
        place_counts.write(log.write)
        return []
    for company in companies:
        for source in company["sources"]:
            if source_bodies and source["url"] in source_bodies:
                source["body"] = source_bodies[source["url"]]
    page_transport = robots = None
    factory = lambda company: client
    if any(not company["skipped"] for company in companies) and (final_budget is None or final_budget.remaining() > 0):
        raw_transport = transport or (http.FixtureTransport(fixture) if fixture else
                                      http.UrllibTransport(raw_dir=cfg.raw_dir) if hasattr(cfg, "raw_dir") else http.UrllibTransport())
        page_transport = PageTransport(raw_transport)
        # 测试库真实运行（--dry-run 不带 --fixture）也用真实 key；只有 fixture 回放才用占位 key。
        search_client = search_client or SearchClient(raw_transport, cfg, api_key=(config.load_api_key(cfg) or None) if (fixture is None and transport is None) else None)
        factory = (lambda company: client) if client is not None else _client(cfg, fixture)
        link_records = load_link_records(cfg.links_file) if link_records is None else link_records
        cache = cfg.state_dir / "test-cache" if cfg.test_mode else cfg.state_dir
        robots = RobotsChecker(page_transport, cache, started.date())
    outputs, pending = [], {}

    def save_output(output, *, build=True):
        place_counts.merge(output.place_audit)
        saved = _save(conn, output, run_date)
        if not saved:
            output.status, output.error = "失败", "superseded"
        write_usage(conn, cfg)
        for record in output.usage:
            log.write({"event": "deep_usage", "company_id": output.company_id, **record})
        for detail in output.removed_reasons:
            log.write({"event": "deep_removed", "company_id": output.company_id, **detail})
        log.write({"event": "deep_end", "company_id": output.company_id, "status": output.status,
                   "elapsed_s": output.elapsed_s, **output.tokens, "estimated_cost": output.cost,
                   "model_calls": output.model_calls, "removed_points": output.removed_points,
                   "quality_pending": output.quality_pending, "error": output.error, "result_saved": saved})
        if saved and publish is not None and build:
            try:
                online = publish(conn, cfg, clock, output.company_id, run_date)
                if online is not None and output.status == "完成":
                    if not isinstance(online, datetime) or online.utcoffset() is None:
                        raise ValueError("发布时间必须带时区")
                    with conn:
                        conn.execute("UPDATE deepdive SET online_at=? WHERE company_id=? AND run_date=? AND status='完成' AND run_token=?",
                                     (online.astimezone(BJ).isoformat(), output.company_id, run_date, output.run_token))
            except Exception:
                log.write({"event": "deep_publish_failed", "company_id": output.company_id})
        outputs.append(output)
        _report(output)

    executor = ThreadPoolExecutor(max_workers=5)
    try:
        while companies or pending:
            if deep_deadline is not None and not deadline_built and clock.now() >= deep_deadline:
                force_build()
                deadline_built = True
            if final_budget is not None and final_budget.remaining() <= 0:
                # 先持久化所有未完成终态，再生成最终页面；迟到线程没有写库权限。
                for company in companies:
                    if not _claim(conn, company, run_date, clock, skipped=company["skipped"]):
                        continue
                    output = DeepResult(company["id"], company["name"], "不做" if company["skipped"] else "暂时没有",
                                        clock.now().isoformat(), clock.now().isoformat(),
                                        error="" if company["skipped"] else "final_deadline", run_token=company["run_token"])
                    save_output(output, build=False)
                companies.clear()
                for future, (_, budget, progress) in pending.items():
                    future.cancel()
                    if future.done() and not future.cancelled():
                        try:
                            completed = future.result()
                        except Exception:
                            completed = None
                        if completed and completed.ended_at and _stamp(completed.ended_at) < deep_final:
                            save_output(completed, build=False)
                            continue
                    output = replace(progress, status="暂时没有", result={}, new_evidence=[], usage=list(progress.usage),
                                     removed_reasons=list(progress.removed_reasons),
                                     ended_at=clock.now().isoformat(), elapsed_s=budget.elapsed(), error="final_deadline")
                    save_output(output, build=False)
                pending.clear()
                break
            while companies and len(pending) < 5:
                company = companies.pop(0)
                budget = Budget(clock, 180.0, parent=final_budget)
                if not _claim(conn, company, run_date, clock, skipped=company["skipped"]):
                    continue
                if company["skipped"]:
                    output = DeepResult(company["id"], company["name"], "不做", budget.started_at.isoformat(), clock.now().isoformat(), run_token=company["run_token"])
                    save_output(output)
                    continue
                log.write({"event": "deep_start", "company_id": company["id"], "started_at": budget.started_at.isoformat()})
                output = DeepResult(company["id"], company["name"], started_at=budget.started_at.astimezone(BJ).isoformat(), run_token=company["run_token"])
                future = executor.submit(_work, company, cfg, clock, budget, page_transport, robots, search_client, factory(company), link_records, output)
                pending[future] = (company, budget, output)
            if not pending:
                continue
            timeout = min(1.0, min(b.remaining() for _, b, _ in pending.values()))
            if deep_deadline is not None and not deadline_built:
                timeout = min(timeout, max(0, (deep_deadline - clock.now()).total_seconds()))
            done, _ = wait(set(pending), timeout=timeout, return_when=FIRST_COMPLETED)
            if final_budget is not None and final_budget.remaining() <= 0:
                continue
            for future, (company, budget, progress) in list(pending.items()):
                if future not in done and budget.remaining() > 0:
                    continue
                pending.pop(future)
                if future.done():
                    try:
                        output = future.result()
                    except Exception:
                        output = replace(progress, status="失败", result={}, new_evidence=[], usage=list(progress.usage),
                                         removed_reasons=list(progress.removed_reasons),
                                         ended_at=clock.now().isoformat(), elapsed_s=budget.elapsed(), error="worker_failed")
                else:
                    future.cancel()
                    # 截止时保存已经发生的调用及可知用量；迟到线程没有写库权限。
                    output = replace(progress, status="失败", result={}, new_evidence=[], usage=list(progress.usage),
                                     removed_reasons=list(progress.removed_reasons),
                                     ended_at=clock.now().isoformat(), elapsed_s=budget.elapsed(), error="company_timeout")
                save_output(output)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    finish_checkpoints()
    with conn:
        conn.execute("DELETE FROM deepdive_pages WHERE run_date < ?", ((started.date() - timedelta(days=30)).isoformat(),))
    write_usage(conn, cfg)
    place_counts.write(log.write)
    total_tokens = {key: sum(output.tokens[key] for output in outputs) for key in ("prompt_tokens", "prompt_cache_hit_tokens", "completion_tokens")}
    print(f"总计 {len(outputs)} 家，完成 {sum(o.status == '完成' for o in outputs)} 家，失败 {sum(o.status == '失败' for o in outputs)} 家，"
          f"累计耗时 {sum(o.elapsed_s for o in outputs):.2f} 秒，抓取页数 {sum(o.materials.fetch_count for o in outputs)}，"
          f"模型调用 {sum(o.model_calls for o in outputs)}，token {total_tokens['prompt_tokens']}/{total_tokens['prompt_cache_hit_tokens']}/{total_tokens['completion_tokens']}，"
          f"费用 ¥{sum(o.cost for o in outputs):.6f}，删掉要点 {sum(o.removed_points for o in outputs)}，"
          f"质量待核 {sum(o.quality_pending for o in outputs)} 家，有新证据 {sum(bool(o.new_evidence) for o in outputs)} 家")
    return outputs


def run_cli(cfg, args, clock) -> int:
    """只跑深度，不抓主流程信源、不生成或覆盖交接文件。"""
    cfg = replace(cfg, test_mode=config.is_test_run(args, cfg))
    print("深度解读阶段 2 实现：手动补跑")
    if args.reset_test_db:
        from .pipeline import reset_test_db
        reset_test_db(cfg)
        (cfg.state_dir / "test" / "deep-usage.json").unlink(missing_ok=True)
    if not cfg.db_path.exists():
        print("总计 0 家，模型调用 0，费用 ¥0.000000")
        return 0
    conn = db.connect(cfg.db_path)
    try:
        run_for_today(conn, cfg, clock, company_ids=args.company, deep_date=getattr(args, "deep_date", None), fixture=args.fixture)
        return 0
    finally:
        conn.close()
