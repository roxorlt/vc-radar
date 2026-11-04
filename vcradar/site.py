"""从只读业务数据生成回看站点；所有页面资源均随快照保存。

data.json 只含列表索引和本周名单；纯基线保留详情定位和公司名。
c/<id>.json 由详情路由按需读取；纯基线详情只含关键字段和来源。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from . import cal, fx, guard, scan, wording, placenames, db, outbox
from .fetcher import merge_missed_ranges
from .config import BJ, ET, Config, effective_links_file
from .labels import overall
from .render import push_first_lines, read_link_records, official_links, sort_cards, fetch_warning, warning_from_text
from .models import Card, DigestData, ErrorType, SourceStatusLine
from .sources.registry import DISPLAY_ORDER, P0_SOURCES, display_name, display_rank, name_for_url


ASSETS = Path(__file__).with_name("site_assets")
# 兼容旧调用方的覆盖点；默认路径和显式配置都由 Config 管理。
LINK_RECORDS = Config().links_file
ITEM_NAMES = ("产品是什么", "解决谁的什么问题", "怎么赚钱", "团队", "融资", "竞品", "标签依据", "风险")
MAX_DATA_BYTES = 500_000
MARKS = wording.MARK_ORDER
SOURCE_NAMES = DISPLAY_ORDER
FIELD_NAMES = {"hq": "国家", "country": "国家", "总部": "国家", "round": "轮次", "round_norm": "轮次", "amount": "金额",
               "amount_text": "金额", "website": "官网", "ai": "AI相关", "ai_label": "AI相关",
               "social": "社交/社群相关", "social_label": "社交/社群相关"}
FUNDING_FIELDS = {"round": "轮次", "amount": "金额", "leads": "领投", "follows": "跟投", "date": "日期"}
PAST_FUNDING = re.compile(r"\b(?:previous(?:ly)?|earlier|prior|last\s+year)\b|之前|此前|上一轮|以前|早前|先前|上轮|前一轮|去年", re.I)
ERROR_TEXT = wording.SOURCE_ERRORS

def _load_not_connected() -> tuple[tuple[str, str], ...]:
    path = Path(__file__).resolve().parent.parent / "config" / "not-connected.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = payload.get("sources", payload) if isinstance(payload, dict) else payload
        return tuple((str(item["name"]), str(item["reason"])) for item in entries if item.get("name") and item.get("reason"))
    except (OSError, ValueError, TypeError, KeyError):
        return ()


NOT_CONNECTED = _load_not_connected()


def _json(value, default):
    return json.loads(value) if value else default


def _moment(value) -> datetime | None:
    if not value:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)


def _stamp(value) -> str | None:
    stamp = _moment(value)
    return stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp else None


def _safe_url(value, *, normalize=False) -> str:
    if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\\]", value):
        return ""
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        parts.port
    except ValueError:
        return ""
    if not normalize:
        return value
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), ""))


def _object_url(value, trusted: set[str]) -> str:
    url = _safe_url(value, normalize=True)
    domain = scan.registrable_domain(url) if url else None
    if not domain or domain == "crunchbase.com":
        return ""
    return url if domain in {scan.registrable_domain(u) for u in trusted} else ""


def _source_order(source: dict) -> tuple:
    return display_rank(source["source_id"]), source["first_seen_at"] or "", source["raw_item_id"]


def _tags(company: dict) -> list[str]:
    manual = company.get("manual_label")
    if manual:
        # 人工标签优先；不把被覆盖的自动待核显示出来。
        return [tag for tag in ("AI相关", "社交/社群相关", "都不是", "标签待核") if tag in manual]
    return overall(company["ai_label"], company["social_label"])[0].split(" + ")


def _next_push(conn, now: datetime, cfg: Config) -> date:
    overrides = {row["date"]: row["type"] for row in _rows(conn, "SELECT * FROM calendar WHERE country='中国'")}

    def workday(day):
        kind = overrides.get(day.isoformat())
        return kind == "调休上班" if kind else cal.is_cn_workday(day)

    for offset in range(370):
        candidate = now.date() + timedelta(days=offset)
        if candidate.weekday() > 4 or not workday(candidate):
            continue
        friday = candidate + timedelta(days=4 - candidate.weekday())
        if any(workday(candidate + timedelta(days=i)) for i in range(1, (friday - candidate).days + 1)):
            continue
        if offset == 0:
            sent = conn.execute("SELECT 1 FROM digest WHERE date=? AND push=1", (candidate.isoformat(),)).fetchone()
            if sent or now.strftime("%H:%M") >= cfg.push_time:
                continue
        return candidate
    raise ValueError("找不到下一个推送日")


def _rows(conn, query: str) -> list[dict]:
    cursor = conn.execute(query)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _link_records(now: datetime, cfg: Config) -> list[dict]:
    records = []
    path = effective_links_file(cfg)
    path = LINK_RECORDS if path == Config().links_file else path
    for record in read_link_records(path):
        try:
            verified = _moment(record.get("verified_at"))
            if (record.get("verified") is True and _safe_url(record.get("url"))
                    and isinstance(record.get("company"), str) and isinstance(record.get("name", ""), str)
                    and verified and verified.date() <= now.date()):
                records.append(record)
        except (ValueError, TypeError):
            continue
    return records


def _sources(conn) -> dict[int, list[dict]]:
    result = defaultdict(list)
    for row in _rows(conn, "SELECT s.*, r.source_id, r.title, r.published_at, r.first_seen_at, "
                     "r.original_date_et, r.structured_json, r.is_baseline FROM event_source s "
                     "JOIN raw_item r ON r.id=s.raw_item_id"):
        structured = _json(row["structured_json"], {})
        extraction = _json(row["extraction_json"], {})
        stamp = _moment(row["first_seen_at"] if row["source_id"] == "yc_directory"
                        else row["published_at"] or row["first_seen_at"])
        result[row["event_id"]].append({
            "raw_item_id": row["raw_item_id"], "source_id": row["source_id"],
            "name": display_name(row["source_id"]), "origin_name": row["source_name"], "url": row["url"],
            "title": row["title"], "beijing_at": _stamp(stamp), "date": stamp.date().isoformat(),
            "published_at": _stamp(row["published_at"]),
            "original_date_et": row["original_date_et"] or (
                _moment(row["published_at"]).astimezone(ET).date().isoformat() if row["published_at"] else None),
            "first_seen_at": _stamp(extraction.get("_source_first_seen_at") or row["first_seen_at"]),
            "is_baseline": bool(row["is_baseline"]), "structured": structured, "extraction": extraction,
        })
    return {key: sorted(value, key=_source_order) for key, value in result.items()}


def _collapsed(conn) -> set[int]:
    # 与入库模块一样，只把同次目录抓取中新出现超过 30 家的批次折叠。
    groups, events = defaultdict(set), defaultdict(set)
    for row in _rows(conn, "SELECT s.event_id, e.company_id, r.source_id, r.first_seen_at, r.structured_json "
                     "FROM event_source s JOIN raw_item r ON r.id=s.raw_item_id "
                     "JOIN funding_event e ON e.id=s.event_id WHERE r.is_baseline=0"):
        if row["source_id"] != "yc_directory":
            continue
        key = row["source_id"], row["first_seen_at"], _json(row["structured_json"], {}).get("yc_batch", "")
        groups[key].add(row["company_id"])
        events[key].add(row["event_id"])
    return set().union(*(events[k] for k, v in groups.items() if len(v) > 30))


def _sorted_events(events: list[dict]) -> list[dict]:
    # 以事件编号定位，复用推送的全部排序规则，避免页面和推送各维护一份。
    by_id = {str(e["id"]): e for e in events}
    cards = [Card(str(e["id"]), e["name"], e["marks"], e["round"], e["amount"], "", "", "", "", [],
                  amount_usd=e["amount_usd"], amount_status=e["amount_status"]) for e in events]
    first_seen = {str(e["id"]): _moment(e["first_ingested_at"]) for e in events}
    return [by_id[c.company_id] for c in sort_cards(cards, first_seen=first_seen)]


def _city(company: dict) -> str:
    # 只使用已存所在地，不从公司简介、投资方或模型记忆推断城市。
    country = company["country"]
    if country == scan.COUNTRY_PENDING or company["status"] == "总部待核":
        return wording.SITE_TEXT["city_pending"]
    if company.get("city"):
        return placenames.city_label(company["city"], company.get("hq") or company.get("hq_basis") or "")
    location = company.get("hq") or scan._headquarters_location(company.get("hq_basis") or "") or ""
    matches = {scan._CITY_ALIASES.get(m[0].casefold(), m[0].casefold()): m[0]
               for m in scan._CITY_PATTERN.finditer(location)
               if scan._COUNTRY_BY_CITY.get(m[0].casefold()) in {country, "有歧义"}}
    return placenames.city_label(next(iter(matches.values())), location) if len(matches) == 1 else wording.SITE_TEXT["city_pending"]


def _display_header(header: dict) -> dict:
    return {**header, "marks": [wording.MARK_LABELS.get(m, m) for m in header["marks"]],
            "tags": [wording.display_text(t) for t in header["tags"]]}


def _round_display(value: str, reasons=(), *, outside=False) -> str:
    if outside or value == "过滤":
        return wording.SITE_TEXT["out_of_scope"] + (f" · {reasons[0]}" if reasons else "")
    return wording.SITE_TEXT["round_pending"] if value == "待核" or not value else value


def _basis_is_sentence(text: str) -> bool:
    return (len(text) > 60 or bool(re.search(r"\b(?:that|raised|startup|company|firm|based|we|is|has)\b|[。！？!?]", text, re.I))
            or bool(re.search(r"[a-z]{2,}\.(?:\s|$)|\b[A-Za-z]+(?:[\s-]+[A-Za-z]+){5,}\b", text)))


def _short_location(text: str) -> str:
    location = scan._headquarters_location(text)
    if not location:
        match = re.search(r"\b([A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*(?:,\s*[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)*)*)[-–—]based\b", text)
        if not match:
            match = re.search(r"\bbased\s+in\s+([^;。\n]+)", text, re.I)
        location = match[1] if match else ""
    return re.split(r",?\s+(?:that|which|with|and|but)\b|[。!?]|\.\s", location, maxsplit=1, flags=re.I)[0].strip(" .,，")


def _country_basis(company: dict, sources: list[dict]) -> str:
    basis = (company.get("hq_basis") or "").strip()

    def concise(text, *, conflict=False):
        text = text.strip()
        if not _basis_is_sentence(text):
            return text
        if conflict:
            location = _short_location(text)
            if location and not _basis_is_sentence(location):
                return placenames.normalize_place_text(location, protected_names=())[0]
        source = next((source for source in sources if text in {
            str(source["extraction"].get(key) or "").strip() for key in ("hq_quote", "_hq_basis")}), sources[0] if sources else None)
        name = (display_name(source["source_id"]) if source.get("source_id") else source["name"]) if source else "来源"
        return f"{name} 报道"

    if basis.startswith("国家来源有冲突："):
        return "国家来源有冲突：" + "；".join(concise(part, conflict=True) for part in basis.split("：", 1)[1].split("；"))
    return concise(basis)


def _event(row: dict, company: dict, sources: list[dict], collapsed: set[int], next_label: str, snapshot=None) -> dict:
    from .verify import valid_investor
    first = _moment(row["first_ingested_at"] or row["first_seen"])
    # 金额待核才用来源表顺序中的主金额；普通卡片直接用已合并的显示金额。
    if row["amount_status"] == "金额待确认":
        amount_text = None
    elif row["amount_status"] == "金额待核":
        amount_text = sources[0]["extraction"].get("amount_text") if sources else None
    else:
        amount_text = row["amount_text"] if row["amount_status"] == "披露" else None
    disputed = row["amount_status"] == "金额待核" and " 写 " in (row["amount_text"] or "")
    if row["amount_status"] in {"币种待确认", "暂未换算", "金额待确认"}:
        amount = None
    elif disputed and sources:
        primary = sources[0]["extraction"]
        amount = (primary.get("_amount_fx") or fx.convert_amount(primary.get("amount_text")).to_dict())["amount_usd"]
    else:
        amount = row["amount_usd"] if row["fx_source"] or row["amount_currency"] else scan.parse_amount_usd(amount_text)
    label = fx.format_amount_text(row["amount_text"]) if row["amount_status"] == "金额待核" and " 写 " in (row["amount_text"] or "") else fx.format_amount(row, row["amount_text"])
    if row["amount_status"] == "金额待核" and sources and " 写 " in (row["amount_text"] or ""):
        displayed = [s for s in sources if f"{s['origin_name']} 写 " in row["amount_text"]]
        if displayed:
            label = "，".join(f"{s['name']} 写 " + fx.format_amount(
                s["extraction"].get("_amount_fx") or fx.convert_amount(s["extraction"].get("amount_text")).to_dict(),
                s["extraction"].get("amount_text")) for s in displayed)
    stored_reasons = _json(row["filter_reasons_json"], [])
    reasons = [wording.filter_reason(reason, round_text=row["round_text"] or "",
                   extractions=[s["extraction"] for s in sources]) for reason in scan.display_filter_reasons(stored_reasons)]
    reasons = [reason for reason in reasons if reason]
    outside = bool(reasons) or row["round_norm"] == "过滤"
    legacy_country_filtered = bool(stored_reasons) and not reasons
    marks = set(_json(row["marks_json"], []))
    if row["amount_status"] in {"币种待确认", "暂未换算", "金额待确认"}:
        marks.discard("大额")
        marks.discard("金额待核")
    if row["amount_status"] == "金额待核":
        marks.add("金额待核")
    state = ("已过滤" if outside or stored_reasons else "已纳入" if company["status"] == "总部待核"
             else "轮次待核" if row["round_norm"] == "待核" or company["status"] == "轮次待核" else "已纳入")
    backfill_event = bool(row.get("original_first_ingested_at") and company.get("backfill") and not row.get("backfill_normal"))
    supplement_unsent = backfill_event and company.get("backfill") == "补充未推送"
    if supplement_unsent and not stored_reasons:
        state = wording.SITE_TEXT["supplement_unsent"]
    return {
        "id": row["id"], "company_id": row["company_id"], "name": company["name"],
        "summary": fx.format_amount_text(placenames.normalize_place_text(company["summary"],
            protected_names=company.get("_place_names", placenames.protected_names(company, [row])))[0], snapshot) if company["summary"] else "",
        "_place_names": company.get("_place_names", placenames.protected_names(company, [row])),
        "round": _round_display(row["round_norm"], reasons, outside=outside), "round_text": row["round_text"],
        "out_of_scope": outside, "_round_pending": row["round_norm"] == "待核",
        "_ai": company["ai_label"], "_social": company["social_label"],
        "_amount_snapshot": snapshot or ({"base": "USD", "rates": {row["amount_currency"]: 1 / row["fx_rate"]},
            "date": row["fx_date"], "source": row["fx_source"], "stale": row["fx_stale"]}
            if row.get("amount_currency") and row.get("fx_rate") else {}),
        "amount": label if row["amount_status"] != "未披露" else "金额未披露",
        "amount_status": row["amount_status"], "amount_usd": amount,
        "leads": [v for v in _json(row["leads_json"], []) if valid_investor(v)],
        "follows": [v for v in _json(row["follows_json"], []) if valid_investor(v)],
        "hq": company["hq"], "hq_basis": company["hq_basis"], "date": row["event_date"],
        "country": scan.COUNTRY_PENDING if company["status"] == "总部待核" else company["country"],
        "legacy_country_pending": company["status"] == "总部待核",
        "country_basis": _country_basis(company, sources), "city": _city(company),
        "backfill": wording.SITE_TEXT["supplement"] if company.get("backfill") == "首期漏掉" else "",
        "backfill_event": backfill_event, "supplement_unsent": supplement_unsent,
        "backfill_rank": company.get("backfill_rank"),
        "company_status": wording.STATUS_LABELS.get(company["status"], wording.display_text(company["status"])),
        "admitted": company["status"] in {"已纳入", "总部待核"} and company.get("backfill") != "补充未推送",
        "legacy_country_filtered": legacy_country_filtered,
        "first_seen": company["first_seen"], "first_ingested_at": _stamp(first),
        "push_date": row["push_date"], "next_push_label": next_label if not row["push_date"] and not supplement_unsent else None,
        "baseline_unpushed": bool(row["baseline_unpushed"]), "yc_batch": row["yc_batch"],
        "marks": [m for m in MARKS if m in marks], "tags": _tags(company),
        "status": state, "pending_reason": "" if supplement_unsent else company["pending_reason"] or ("来源没写轮次" if state == "轮次待核" else ""),
        "filter_reasons": reasons, "sources": sources, "collapsed": row["id"] in collapsed,
    }


def _same_page(url: str) -> str:
    """比较两个地址是否同一页面：不分 http/https、www.、末尾斜杠。"""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return url or ""
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return f"{host}{parts.path.rstrip('/')}" + (f"?{parts.query}" if parts.query else "")


def _name_pages(pages: dict[str, dict], sources: list[dict]) -> None:
    """深度解读读过的页面补上信源名：与卡片来源同一页时沿用那条来源，否则按信源表认，认不出保留域名。"""
    by_page = {}
    for source in sources:
        by_page.setdefault(_same_page(source.get("url", "")), source)
    for page in pages.values():
        source = by_page.get(_same_page(page.get("url", "")))
        if source:
            page.update(name=source["name"], title=source.get("title"), published_at=source.get("published_at"),
                        original_date_et=source.get("original_date_et"))
        else:
            known = page.get("name") if page.get("name") not in (None, "", "来源") else None
            try:
                host = (urlsplit(page.get("url", "")).hostname or "").removeprefix("www.")
            except ValueError:
                host = None
            page["name"] = name_for_url(page.get("url", "")) or (known or host or "来源").removeprefix("www.")
            page.setdefault("title", None)
            page.setdefault("published_at", None)


def _page_map(pages: list[dict], materials: list[dict]) -> dict[str, dict]:
    result = {str(p["page_no"]): {"url": p["url"], "beijing_at": _stamp(p["fetched_at"]),
                                    "original_date_et": None, "name": urlsplit(p["url"]).hostname or "来源"}
              for p in pages}
    for i, page in enumerate(materials, 1):
        number = str(page.get("page_no", page.get("page", i)))
        result.setdefault(number, {"url": page.get("url", ""), "name": page.get("name", "来源"),
                                   "beijing_at": _stamp(page.get("fetched_at", page.get("beijing_at"))),
                                   "original_date_et": page.get("original_date_et")})
    return result


def _evidence(points, pages: dict) -> list[dict]:
    if isinstance(points, dict):
        points = [points]
    result = []
    for point in points or []:
        number = str(point.get("page", point.get("source", "")))
        source = pages.get(number)
        if source and point.get("quote"):
            result.append({"quote": point["quote"], "page_no": number, **source})
    return result


def _funding_point_is_current(point: dict, event: dict) -> bool:
    """只保留本轮融资字段及无字段泛述；原句或要点明确回溯、轮次不同的均隐藏。"""
    field = point.get("field")
    if field and field not in FUNDING_FIELDS:
        return False
    evidence = point.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    text = " ".join(str(point.get(key) or "") for key in ("zh", "point", "text", "quote"))
    text += " " + " ".join(str(entry.get("quote") or "") for entry in evidence)
    if PAST_FUNDING.search(text):
        return False
    if field == "round" and point.get("value"):
        return scan.normalize_round(str(point["value"])) == scan.normalize_round(event.get("round"))
    return True


def _current_funding_points(points: list[dict], event: dict) -> list[dict]:
    """过滤显示数据并按 deep.py 的五个子字段补位，不修改存储结果。"""
    kept = [point for point in points if _funding_point_is_current(point, event)]
    present = {point.get("field") for point in kept}
    return kept + [{"field": field, "text": wording.SITE_TEXT["missing"]}
                   for field in FUNDING_FIELDS if field not in present]


def _country_value(value) -> str:
    text = str(value or "").strip()
    if not text or text == scan.COUNTRY_PENDING:
        return scan.COUNTRY_PENDING
    location = _short_location(text) or text
    # 旧地名表尚无 The Hague；这里只补任务指定的显示对照，不参与入库判定。
    return scan._classify_location(location) or {"the hague": "荷兰"}.get(location.casefold()) or placenames.normalize_place_text(location[:60], protected_names=())[0]


def _amount_value(value, event: dict) -> str:
    text = str(value or "").strip()
    if not text:
        return wording.SITE_TEXT["amount_missing"]
    if "（原 " in text:
        return fx.format_amount_text(text)
    return fx.format_amount(fx.convert_amount(text, event.get("_amount_snapshot")), text)


def _website_value(value) -> str:
    text = str(value or "").strip()
    try:
        return (urlsplit(text if "://" in text else "https://" + text).hostname or "").lower().removeprefix("www.") or wording.SITE_TEXT["missing"]
    except ValueError:
        return wording.SITE_TEXT["missing"]


def _label_value(value) -> str:
    text = str(value if value is not None else "").strip()
    if text.casefold() in {"是", "true", "yes", "1"}:
        return "是"
    if text.casefold() in {"否", "false", "no", "0"}:
        return "否"
    return wording.SITE_TEXT["tag_pending"]


def _header_label(header: dict, label: str, fallback) -> str:
    tags = {wording.display_text(tag) for tag in header["tags"]}
    if label in tags:
        return "是"
    return _label_value(fallback) if wording.SITE_TEXT["tag_pending"] in tags else "否"


def _new_evidence(entries: list[dict], event: dict, judgement: dict, pages: dict) -> list[dict]:
    header = _frozen_header(event, judgement)
    pushed = {
        "国家": header["country"],
        "轮次": _round_display(judgement.get("round", judgement.get("round_norm", event["round"])), event["filter_reasons"]),
        "金额": judgement.get("amount_display") or (_amount_value(judgement["amount"], event) if "amount" in judgement else event["amount"]),
        "官网": _website_value(judgement.get("website", event.get("_website"))),
        "AI相关": _header_label(header, "AI相关", judgement.get("ai", judgement.get("ai_label", event.get("_ai") if "tags" not in judgement else None))),
        "社交/社群相关": _header_label(header, "社交/社群相关", judgement.get("social", judgement.get("social_label", event.get("_social") if "tags" not in judgement else None))),
    }
    result, shown = [], set()
    for entry in entries:
        field = FIELD_NAMES.get(entry.get("field"), entry.get("field"))
        if field not in pushed or field in shown:
            continue
        value = entry.get("value")
        if field == "国家":
            found = _country_value(value)
        elif field == "轮次":
            normalized = scan.normalize_round(str(value or ""))
            reasons = [wording.filter_reason("轮次不在范围", round_text=str(value))] if normalized == "过滤" else []
            found = _round_display(normalized, reasons)
        elif field == "金额":
            found = _amount_value(value, event)
        elif field == "官网":
            found = _website_value(value)
        else:
            found = _label_value(value)
        if pushed[field] != found:
            result.append({"field": field, "pushed": pushed[field], "found": found,
                           "evidence": _evidence(entry.get("evidence", [entry]), pages)})
            shown.add(field)
    return result


def _deepdive(row: dict | None, pages: list[dict], event: dict, records: list[dict], now: datetime) -> dict:
    if row is None:
        return {"status": "不做" if event["collapsed"] else "还没开始", "online_at": None,
                "items": [], "sources": [], "entities": [], "new_evidence": [], "quality_pending": False}
    materials = _json(row["materials_json"], [])
    pages_by_no = _page_map(pages, materials)
    _name_pages(pages_by_no, event["sources"])
    trusted = {p["url"] for p in pages_by_no.values() if _safe_url(p["url"])}
    for page in pages:
        links = _json(page["links_json"], [])
        if isinstance(links, dict):
            links = list(links)
        trusted.update(link if isinstance(link, str) else link.get("url", link.get("href", "")) for link in links)
        trusted.update(re.findall(r"https?://[^\s<>\"']+", page["body"]))
    for source in event["sources"]:
        trusted.update(source[key].get("website", "") for key in ("structured", "extraction"))
    trusted = {url for url in trusted if _safe_url(url)}
    for record in records:
        if record.get("company", "").casefold() in {event["name"].casefold(), event["company_id"].casefold()}:
            trusted.add(record["url"])
    result, _, _ = placenames.normalize_document(_json(row["result_json"], {}), names=event.get("_place_names", [event["name"]]))
    raw_items = result.get("items", result)
    if isinstance(raw_items, list):
        raw_items = {item.get("name", item.get("title")): item for item in raw_items}
    items = []
    for i, name in enumerate(ITEM_NAMES, 1):
        raw = raw_items.get(name, raw_items.get(f"item{i}", {}))
        points = raw if isinstance(raw, list) else raw.get("points", []) if isinstance(raw, dict) else []
        if name == "融资":
            points = _current_funding_points(points, event)
        normalized = []
        missing = list(raw.get("missing", [])) if isinstance(raw, dict) else []
        for point in points:
            if name == "融资" and point.get("field") in FUNDING_FIELDS and point.get("text") == wording.SITE_TEXT["missing"]:
                missing.append(point["field"])
                continue
            evidence = _evidence(point.get("evidence", [point]), pages_by_no)
            entities = [{"name": e.get("name", ""), "url": _object_url(e.get("url", ""), trusted)}
                        for e in point.get("entities", []) if e.get("name")]
            normalized.append({"text": point.get("zh", point.get("point", point.get("text", "来源里没写"))),
                               "evidence": evidence, "entities": entities})
        if name == "融资":
            present = {point.get("field") for point in points if point.get("text") != wording.SITE_TEXT["missing"]}
            missing = [m for m in missing if m not in present]
        names = {**FIELD_NAMES, **FUNDING_FIELDS} if name == "融资" else FIELD_NAMES
        items.append({"name": name, "points": normalized, "missing": list(dict.fromkeys(names.get(m, wording.display_text(m)) for m in missing))})
    judgement = _json(row["push_judgement_json"], {})
    evidence = _new_evidence(_json(row["new_evidence_json"], []), event, judgement, pages_by_no)
    entities = [{"name": e["name"], "url": _object_url(e.get("url", ""), trusted)}
                for e in result.get("entities", []) if e.get("name")]
    return {"status": row["status"], "online_at": _stamp(row["online_at"]) if row["status"] == "完成" else None, "items": items,
            "sources": list(pages_by_no.values()), "entities": entities,
            "new_evidence": evidence, "found_at": _stamp(row["new_evidence_found_at"]),
            "quality_pending": bool(row["quality_pending"]),
            "judgement": judgement}


def _frozen_header(event: dict, judgement: dict) -> dict:
    header = {k: event[k] for k in ("name", "marks", "tags", "country")}
    for key in header:
        if key in judgement:
            header[key] = judgement[key]
    if "country" not in judgement and "hq" in judgement:
        country = _country_value(judgement["hq"])
        header["country"] = country if country in scan._COUNTRY_BY_ALIAS.values() else scan.COUNTRY_PENDING
    if "tags" not in judgement and {"ai", "ai_label", "social", "social_label", "manual_label"}.intersection(judgement):
        header["tags"] = _tags({"ai_label": judgement.get("ai", judgement.get("ai_label", event["_ai"])),
                                "social_label": judgement.get("social", judgement.get("social_label", event["_social"])),
                                "manual_label": judgement.get("manual_label")})
    header["marks"] = [m for m in MARKS if m in header["marks"]]
    if event["legacy_country_pending"] and not {"country", "hq"}.intersection(judgement):
        header["country"] = scan.COUNTRY_PENDING
    return header


def _conflicts(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        for field, label in (("amount_text", "金额"), ("round_text", "轮次"), ("_country", "国家"), ("website", "官网")):
            values = [{"value": source["extraction"][field], "name": source["name"], "url": source["url"]}
                      for source in event["sources"] if source["extraction"].get(field)
                      and source["extraction"][field] != scan.COUNTRY_PENDING]
            if field == "amount_text":
                for value in values:
                    source = next(s for s in event["sources"] if s["url"] == value["url"])
                    value["value"] = fx.format_amount(source["extraction"].get("_amount_fx") or
                                                       fx.convert_amount(value["value"]).to_dict(), value["value"])
            if len({v["value"] for v in values}) > 1:
                result.append({"field": label, "values": values})
    return result


def _fetch_status(conn, window_start, window_end) -> tuple[list[dict], list[dict]]:
    baseline = db.first_issue_generated_at(conn)
    missed = defaultdict(list)
    for run in scan.missed_runs_in_window(conn, window_start, window_end):
        missed[run["source_id"]].append((run["missed_range"], run["missed_start"], run["missed_end"]))
    ranges = {sid: merge_missed_ranges(spans, baseline, reference=window_end) for sid, spans in missed.items()}
    sources = {s.id: {"id": s.id, "name": s.name, "column_name": s.column, "url": s.url,
                       "enabled": 1, "stage": "P0", "crawl_status": "正常", "stop_reason": None, "stopped_at": None}
               for s in P0_SOURCES}
    sources.update({s["id"]: s for s in _rows(conn, "SELECT * FROM source")})
    latest = {}
    for run in _rows(conn, "SELECT * FROM fetch_run"):
        old = latest.get(run["source_id"])
        if old is None or (_moment(run["ended_at"]), run["id"]) > (_moment(old["ended_at"]), old["id"]):
            latest[run["source_id"]] = run
    grouped, inactive = defaultdict(list), []
    for source in sources.values():
        if not source["enabled"] and source["crawl_status"] != "已停抓":
            inactive.append({"name": source["name"], "reason": "尚未启用"})
            continue
        run = latest.get(source["id"])
        stopped = source["crawl_status"] == "已停抓"
        name = "Term Sheet 网页版" if source["name"] == "Term Sheet" else source["name"]
        state = "已停抓" if stopped else run["result"] if run else "还没抓取"
        message = (f"{name}已停止抓取：{wording.STOP_REASONS.get(source['stop_reason'], source['stop_reason'] or '人工停止抓取')}，{(_stamp(source['stopped_at']) or '')[:10]}"
                   if stopped else ERROR_TEXT.get(run["error_type"], run["result"]) if run else "还没抓取")
        if message == "没轮到":
            message = wording.FETCH_LABELS["没轮到"]
        technical = f"HTTP {run['http_code']}" if run and run["http_code"] is not None else ""
        if stopped and run and run["note"] in {"人工停抓", "拦截页"}:
            technical = wording.display_text(run["note"])
        grouped[name].append({"id": source["id"], "url": source["url"], "state": wording.FETCH_LABELS.get(state, state),
                              "result": wording.display_text(message), "technical": technical,
                              "last_at": _stamp(run["ended_at"]) if run else None,
                              "count": run["count"] if run else 0,
                              "latest_at": _stamp(run["latest_at"]) if run else None,
                              "missed_range": ranges.get(source["id"], ("", None, None))[0]})
    result = []
    for name, addresses in grouped.items():
        bad = [a for a in addresses if a["state"] not in {"成功", "已停止抓取"}]
        representative = max(bad or addresses, key=lambda a: a["last_at"] or "")
        missed_range, _, _ = merge_missed_ranges(
            [ranges[a["id"]] for a in addresses if a["id"] in ranges], baseline, reference=window_end)
        result.append({"name": name, "addresses": addresses, "state": representative["state"],
                       "result": representative["result"],
                       "technical": " · ".join(dict.fromkeys(a["technical"] for a in addresses if a["technical"])),
                       "last_at": max((a["last_at"] or "" for a in addresses), default="") or None,
                       "latest_at": max((a["latest_at"] or "" for a in addresses), default="") or None,
                       "count": sum(a["count"] for a in addresses),
                       "missed_ranges": [missed_range] if missed_range else []})
    result.sort(key=lambda s: min(display_rank(a["id"]) for a in s["addresses"]))
    active_names = {s["name"] for s in result}
    inactive = [s for s in inactive if s["name"] not in active_names]
    inactive += [{"name": name, "reason": reason} for name, reason in NOT_CONNECTED if name not in active_names]
    return result, inactive


def _usage(path: Path, now: datetime) -> dict:
    if not path.exists():
        return {"month_used": None, "day_used": None}
    data = json.loads(path.read_text(encoding="utf-8"))
    bucket = data.get("months", {}).get(now.strftime("%Y-%m"), {})
    used = bucket.get("estimated_cost", bucket.get("cost", 0))
    if not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
        raise ValueError("花费记录无效")
    day = data.get("days", {}).get(now.date().isoformat(), {})
    return {"month_used": used, "day_used": day.get("estimated_cost", day.get("cost"))}


def _verification(conn, now, cfg=None):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    logs = _rows(conn, "SELECT * FROM verify_log ORDER BY verified_at,id") if "verify_log" in tables else []
    searches = _rows(conn, "SELECT * FROM verify_search ORDER BY requested_at,id") if "verify_search" in tables else []
    searches = [r for r in searches if r.get("purpose", "查证") == "查证"]
    day, month = str(now.date()), now.strftime("%Y-%m")
    daily_logs = [r for r in logs if r["run_date"] == day and r["source"] == "日常"]
    daily_searches = [r for r in searches if r["run_date"] == day and r["source"] == "日常"]
    latest = {(r["company_id"], r["field"]): r for r in daily_logs}
    costs = Counter()
    for row in searches:
        if (row.get("search_date") or row["run_date"])[:7] == month and row["source"] in {"日常", "首期"}:
            costs[row["source"]] += row["cost"] + row["model_cost"]
    if cfg is not None and not cfg.test_mode:
        try:
            lines = (cfg.outbox_dir / "backfill-spend.log").read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            try:
                spent_day, _, amount = line.split("\t")
                if spent_day[:7] == month:
                    costs["补充命令"] += float(amount)
            except (ValueError, TypeError):
                continue
    counted_logs = [r for r in daily_logs if r["result"] == "本次未查" or
                    (r["result"] == "查过仍无法确认" and r["reason"] == "来源里没有能区分同名公司的信息")]
    stats = {"companies": len({r["company_id"] for r in counted_logs + daily_searches}),
             "fields": sum(r["result"] == "已确认" for r in daily_logs),
             "unsearched": len({r["company_id"] for r in latest.values() if r["result"] == "本次未查"}),
             "searches": len(daily_searches), "month_used": round(sum(costs.values()), 6),
             "first_issue_cost": round(costs["首期"], 6), "backfill_cost": round(costs["补充命令"], 6)}
    by_company = defaultdict(dict)
    for row in logs:
        if row["field"] == "费用" or row.get("old_new_json") not in {None, "null"}:
            continue
        by_company[row["company_id"]][row["field"]] = row
    search_counts = Counter(r["company_id"] for r in searches if r.get("purpose", "查证") == "查证")
    details = {}
    for cid, fields in by_company.items():
        details[cid] = []
        for field, row in fields.items():
            reason = wording.display_text(row["reason"])
            if row["result"] == "已确认":
                text = wording.SITE_TEXT["verified"]
            elif row["result"] == "本次未查":
                text = wording.SITE_TEXT["not_verified"]
            elif row["reason"] == "来源里没有能区分同名公司的信息":
                text = wording.SITE_TEXT["verify_identity_missing"]
                reason = ""
            elif search_counts[cid] and row["reason"] != "来源里没有能区分同名公司的信息":
                text = wording.SITE_TEXT["verify_count"].format(count=search_counts[cid])
            else:
                text = wording.SITE_TEXT["verify_unconfirmed"]
            details[cid].append({"field": field, "text": text, "reason": reason,
                                 "urls": [_safe_url(u) for u in _json(row["urls_json"], []) if _safe_url(u)],
                                 "quotes": _json(row["quotes_json"], [])})
    corrections = {}
    for row in logs:
        if row["field"] == "金额" and row.get("old_new_json") not in {None, "null"}:
            corrections[row["company_id"], row["event_id"]] = row
    for (cid, eid), row in corrections.items():
        unknown = row["result"] == "金额待确认" or _json(row["old_new_json"], {}).get("new", {}).get("amount_status") == "金额待确认"
        details.setdefault(cid, []).append({"field": "金额", "event_id": eid, "correction": True,
            "text": wording.SITE_TEXT["amount_not_written" if unknown else "amount_corrected"], "reason": "",
            "urls": [] if unknown else [_safe_url(u) for u in _json(row["urls_json"], []) if _safe_url(u)],
            "quotes": [] if unknown else _json(row["quotes_json"], [])})
    return stats, details


def _snapshot(conn, cfg: Config, now: datetime) -> dict:
    from .verify import pending_fields
    company_rows = {c["id"]: c for c in _rows(conn, "SELECT * FROM company")}
    for company in company_rows.values():
        company["_place_names"] = placenames.company_names(conn, company["id"])
    sources = _sources(conn)
    folded = _collapsed(conn)
    try:
        next_day = _next_push(conn, now, cfg)
    except cal.CalendarMissing:
        next_day = None
    next_label = (f"下次推送：{next_day:%m-%d} 09:00"
                  if next_day else None)
    ordinary = {int(row[0].rsplit(":", 1)[1]) for row in conn.execute("SELECT key FROM meta WHERE key LIKE 'backfill:normal:%'")}
    events = [_event({**e, "backfill_normal": e["id"] in ordinary}, company_rows[e["company_id"]], sources.get(e["id"], []), folded, next_label,
                     scan._fx_snapshot(conn, e["id"]))
              for e in _rows(conn, "SELECT * FROM funding_event")]
    by_id = {e["id"]: e for e in events}
    for event in events:
        if event["amount_status"] == "金额待核":
            if label := scan.verified_amount_label(conn, event["company_id"], event["id"]):
                event["amount"] = label
    verification, verify_details = _verification(conn, now, cfg)
    digests = _rows(conn, "SELECT * FROM digest WHERE push=1 ORDER BY date DESC")
    listed = {eid for d in digests for eid in _json(d["event_ids_json"], [])}
    # 纯基线在往期所有筛选中隐藏；有非基线来源或推送记录后按普通事件展示。
    eligible = {e["id"] for e in events if not (e["baseline_unpushed"] and all(s["is_baseline"] for s in e["sources"]))
                and (e["push_date"] or e["id"] in listed or not e["baseline_unpushed"]
                     or any(not s["is_baseline"] for s in e["sources"]))}
    for event in events:
        event["baseline_unpushed"] = event["id"] not in eligible
        if event["baseline_unpushed"]:
            event["next_push_label"] = None
    latest = digests[0] if digests else None
    counts_line, hints_line = push_first_lines(latest["text"]) if latest else ("", "")
    legacy_counts = counts_line
    counts_line, hints_line = map(wording.legacy_to_display, (counts_line, hints_line))
    period_label = "上次推送后" if counts_line.startswith("上次推送后") else "本周"
    hints_line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", hints_line)
    latest_events = [by_id[eid] for eid in _json(latest["event_ids_json"], []) if eid in by_id] if latest else []
    supplement_ids = set(_json(latest.get("supplement_event_ids_json"), [])) if latest else set()
    applied = conn.execute("SELECT 1 FROM meta WHERE key='backfill_applied'").fetchone() is not None
    first_row = conn.execute("SELECT value FROM meta WHERE key='first_issue_date'").fetchone()
    first_date = first_row[0] if first_row else digests[-1]["date"] if digests else None
    week_events = [e for e in latest_events if not e["legacy_country_filtered"] and e["id"] in eligible
                   and e["id"] not in supplement_ids and not e["backfill_event"]
                   and (latest["date"] != first_date or e["filter_reasons"] or e["push_date"] == latest["date"])]
    if (re.search(r"总部待核|总部非美国", legacy_counts)
            or any(e["legacy_country_filtered"] for e in latest_events) or applied):
        # 历史推送正文不回写；网页用当前名单重算计数，避免沿用旧总部和过滤数量。
        counts_line = ""
    buckets = {key: [] for key in ("cards", "round_pending", "filtered")}
    for event in _sorted_events(week_events):
        key = {"已纳入": "cards", "轮次待核": "round_pending", "已过滤": "filtered"}[event["status"]]
        buckets[key].append(event["id"])
    deep_rows = _rows(conn, "SELECT * FROM deepdive ORDER BY run_date DESC")
    deep_by_company = {}
    for row in deep_rows:
        deep_by_company.setdefault(row["company_id"], row)
    pages = defaultdict(list)
    for page in _rows(conn, "SELECT * FROM deepdive_pages ORDER BY page_no"):
        pages[page["company_id"], page["run_date"]].append(page)
    records = _link_records(now, cfg)
    link_path = effective_links_file(cfg)
    websites = official_links(LINK_RECORDS if link_path == Config().links_file else link_path)
    by_company = defaultdict(list)
    for event in events:
        by_company[event["company_id"]].append(event)
    companies = []
    for cid, history in by_company.items():
        history.sort(key=lambda e: (e["first_ingested_at"], e["id"]), reverse=True)
        event = history[0]
        company_verification = [v for v in verify_details.get(cid, []) if not v.get("event_id") or v["event_id"] == event["id"]]
        if any(v.get("correction") for v in company_verification):
            company_verification = [v for v in company_verification if v["field"] != "金额" or v.get("correction")]
        pending = pending_fields(company_rows[cid], {"round_norm": "待核" if event["_round_pending"] else "",
                                                   "amount_status": event["amount_status"]})
        if any(v.get("correction") for v in company_verification):
            pending = [field for field in pending if field != "金额"]
        in_backfill_scope = bool(company_rows[cid].get("backfill") or conn.execute(
            "SELECT 1 FROM meta WHERE key=?", (f"backfill:rejudged:{cid}",)).fetchone())
        if in_backfill_scope and pending and not any(not v.get("correction") for v in company_verification):
            company_verification.append({"field": "", "text": wording.SITE_TEXT["verification_omitted"],
                                         "reason": "", "urls": [], "quotes": []})
        verify_sources = [{"url": url, "quote": v["quotes"][i] if i < len(v["quotes"]) else ""}
                          for v in company_verification if not v.get("correction") for i, url in enumerate(v["urls"])]
        verify_sources = list({s["url"]: s for s in verify_sources}.values())
        conflict_row = conn.execute("SELECT value FROM meta WHERE key=?", ("verify:conflicts:" + cid,)).fetchone()
        verify_conflicts = _json(conflict_row[0], []) if conflict_row else []
        for conflict in verify_conflicts:
            verify_sources.extend({"url": v["url"], "quote": v.get("quote", "")} for v in conflict["values"] if not v.get("original"))
        verify_sources = list({s["url"]: s for s in verify_sources}.values())
        if all(e["baseline_unpushed"] for e in history):
            unique_sources = {s["url"]: s for e in history for s in e["sources"]}
            companies.append({"id": cid, "event_id": event["id"], "baseline_unpushed": True,
                              "header": {"name": event["name"], "marks": [], "tags": event["tags"], "country": event["country"]},
                              "country": event["country"], "country_basis": event["country_basis"], "backfill": event["backfill"],
                              **({"website": websites[event["name"].strip().casefold()]} if event["name"].strip().casefold() in websites else {}),
                              **({"verification": company_verification, "verification_sources": verify_sources} if company_verification else {}),
                              "sources": list(unique_sources.values())})
            continue
        row = deep_by_company.get(cid)
        event["_website"] = websites.get(event["name"].strip().casefold(), "")
        deep = _deepdive(row, pages.get((cid, row["run_date"]), []) if row else [], event, records, now)
        trusted = {s[key].get("website", "") for e in history for s in e["sources"] for key in ("structured", "extraction")}
        trusted.update(r["url"] for r in records if r.get("company", "").casefold() == event["name"].casefold()
                       and r.get("name", "").casefold() == event["name"].casefold())
        unique_sources = {}
        for source in [s for e in history for s in e["sources"]] + deep["sources"]:
            unique_sources.setdefault(_same_page(source["url"]), source)
        header = _frozen_header(event, deep.get("judgement", {}))
        confirmed_fields = {v["field"] for v in company_verification if v["text"] == wording.SITE_TEXT["verified"]}
        judgement = deep.get("judgement", {})
        for field, key in (("国家", "country"), ("标签", "tags"), ("金额", "marks")):
            frozen_keys = {"country", "hq"} if key == "country" else {"tags", "ai", "ai_label", "social", "social_label", "manual_label"} if key == "tags" else {"marks"}
            if field in confirmed_fields and not frozen_keys.intersection(judgement):
                header[key] = event[key]
        companies.append({"id": cid, "event_id": event["id"], "header": header,
                          "country": event["country"], "country_basis": event["country_basis"], "backfill": event["backfill"],
                          "verification": company_verification,
                          "verification_sources": verify_sources,
                          "identity_note": "名字相同但官网不同，可能不是同一家" if
                          company_rows[cid]["pending_reason"] == "名字相同但官网不同，可能不是同一家" else "",
                          "website": websites.get(event["name"].strip().casefold(), ""), "summary": event["summary"],
                          "deepdive": deep, "history": [e["id"] for e in history],
                          "conflicts": _conflicts(history) + verify_conflicts, "sources": list(unique_sources.values())})
    rows_today = [r for r in deep_rows if r["run_date"] == now.date().isoformat()]
    due = {e["company_id"] for e in events if e["id"] in eligible and e["status"] == "已纳入" and not e["collapsed"]
           and e["first_ingested_at"][:10] == now.date().isoformat()}
    due.update(r["company_id"] for r in rows_today if r["status"] != "不做")
    due.difference_update(r["company_id"] for r in rows_today if r["status"] == "不做")
    counts = Counter(r["status"] for r in rows_today)
    progress = {"date": now.date().isoformat(), "total": len(due), "done": counts["完成"],
                "running": counts["生成中"], "failed": counts["失败"], "timeout": counts["暂时没有"], "skipped": counts["不做"]}
    included = {e["company_id"] for e in week_events if not e["filter_reasons"]}
    ratios = None
    if latest:
        ratios = {"date": latest["date"], "denominator": len(included)}
        for key, count in (("country", sum(company_rows[c]["country"] == scan.COUNTRY_PENDING or company_rows[c]["status"] == "总部待核" for c in included)),
                           ("round", sum(company_rows[c]["status"] == "轮次待核" for c in included)),
                           ("tag", sum(not company_rows[c]["manual_label"] and "待核" in
                                       (company_rows[c]["ai_label"], company_rows[c]["social_label"]) for c in included))):
            ratios[key] = {"count": count, "percent": round(count * 100 / len(included), 1) if included else 0}
    cutoff = now - timedelta(days=7)
    window_start = _moment(latest["window_start"]) if latest and latest["window_start"] else None
    if window_start is None and len(digests) > 1:
        window_start = _moment(digests[1]["generated_at"])
    if window_start:
        cutoff = min(cutoff, window_start)
    evidence_by_company = {}
    for row in sorted(deep_rows, key=lambda r: _moment(r["new_evidence_found_at"]) or datetime.min.replace(tzinfo=BJ), reverse=True):
        found = _moment(row["new_evidence_found_at"])
        if found is None or not cutoff <= found <= now or (found == window_start and found < now - timedelta(days=7)):
            continue
        fields = list(dict.fromkeys(FIELD_NAMES.get(e.get("field"), e.get("field")) for e in _json(row["new_evidence_json"], [])))
        fields = [f for f in fields if f in FIELD_NAMES.values()]
        if fields:
            evidence_by_company.setdefault(row["company_id"], {"company_id": row["company_id"],
                "name": company_rows[row["company_id"]]["name"], "fields": fields, "found_at": _stamp(found)})
    current = conn.execute("SELECT window_start FROM digest WHERE date<=? "
                           "ORDER BY date DESC LIMIT 1", (str(now.astimezone(BJ).date()),)).fetchone()
    fetch_start = _moment(current[0]) if current and current[0] else window_start
    fetch, inactive = _fetch_status(conn, fetch_start, now)
    warning = warning_from_text(latest["text"]) if latest else ""
    if latest and not warning:
        errors = {value: ErrorType(key) for key, value in wording.SOURCE_ERRORS.items()}
        source_lines = [SourceStatusLine(s["name"].removesuffix(" 网页版"), s["state"] == "成功",
                        s["state"] == "已停止抓取", "", errors.get(s["result"]),
                        missed_range="；".join(s["missed_ranges"])) for s in fetch
                        if (s["last_at"] or "")[:10] == latest["date"]]
        warning = fetch_warning(DigestData(date.fromisoformat(latest["date"]), None, now,
                                           latest["status"], sources=source_lines))
    root = cfg.state_dir / "test" if cfg.test_mode else cfg.state_dir
    deep_budget = _usage(root / "deep-usage.json", now)
    if deep_budget["month_used"] is None:
        deep_budget = {"month_used": sum(r["estimated_cost"] for r in deep_rows if r["run_date"][:7] == now.strftime("%Y-%m")),
                       "day_used": sum(r["estimated_cost"] for r in rows_today)}
    elif deep_budget["day_used"] is None:
        deep_budget["day_used"] = sum(r["estimated_cost"] for r in rows_today)
    baseline_notice = ""
    if latest and latest["status"] == "基线":
        baseline = re.search(r"(?:已收录|首次运行收录) (\d+) 家", latest["text"])
        if baseline:
            baseline_notice = wording.BASELINE_TOP.format(count=baseline[1])
    from .homepage import status as homepage_status
    homepage = homepage_status(conn, cfg, now, latest, [by_id[eid] for eid in buckets["cards"] if not by_id[eid]["collapsed"]])
    return {
        "meta": {"published_at": _stamp(now), "simulated": cfg.test_mode, "today": now.date().isoformat(),
                 "recent_cutoff": (now.date() - timedelta(days=29)).isoformat(), "next_push_date": next_day.isoformat() if next_day else None},
        "digests": [{"date": d["date"], "event_ids": _json(d["event_ids_json"], []),
                     "supplement_ids": _json(d.get("supplement_event_ids_json"), []),
                     "generated_at": d["generated_at"], "card_sources": _json(d.get("card_sources_json"), {})} for d in digests],
        "week": {"date": latest["date"] if latest else None, **buckets,
                 "status": wording.DIGEST_LABELS.get(latest["status"], latest["status"]) if latest else None,
                 "status_key": {"基线": "baseline", "没跑完": "incomplete", "全部失败": "failed",
                 "部分失败": "partial", "有新增": "new", "没有新增": "empty"}.get(latest["status"]) if latest else None,
                 "baseline_notice": baseline_notice, "counts_line": counts_line, "hints_line": hints_line,
                 "fetch_warning": warning,
                 "period_label": period_label,
                 "event_ids": [e["id"] for e in week_events],
                 "yc": [e["id"] for e in week_events if e["collapsed"] and e["status"] == "已纳入"],
                 "filtered_counts": dict(Counter(e["filter_reasons"][0] for e in week_events if e["filter_reasons"]))},
        "events": events, "listing": [e["id"] for e in _sorted_events(events)
                                      if e["id"] in eligible and not e["out_of_scope"] and not e["legacy_country_filtered"]],
        "companies": companies,
        "status": {"sources": fetch, "not_connected": inactive, "pending_ratios": ratios,
                   "fx": fx.status_from_files(cfg, currencies=[e["amount_currency"] for e in _rows(conn, "SELECT amount_currency FROM funding_event WHERE amount_status='暂未换算'")]),
                   "verification": verification,
                   "homepage": homepage,
                   "deepdive_progress": progress, "deepdive_budget": deep_budget,
                   "extract_budget": _usage(root / "extract-usage.json", now),
                   "quality_pending": [{"company_id": cid, "name": company_rows[cid]["name"]}
                                       for cid, row in deep_by_company.items() if row["quality_pending"]],
                   "new_evidence": list(evidence_by_company.values()),
                   "missed": [{"name": s["name"], "range": r} for s in fetch for r in s["missed_ranges"]]},
    }


def _display_source(source: dict) -> dict:
    # 白名单防止抓取、抽取新增的内部字段进入静态站点。
    return {key: source.get(key) for key in ("name", "beijing_at", "published_at", "original_date_et", "url", "title")}


def _display_event(event: dict, *, card=False) -> dict:
    fields = ("id", "company_id", "name", "summary", "round", "amount", "leads", "push_date",
              "marks", "tags", "status", "pending_reason", "filter_reasons", "out_of_scope", "country", "country_basis", "backfill", "supplement_unsent", "backfill_rank", "company_status", "admitted")
    if card:
        fields += ("first_ingested_at", "next_push_label")
    else:
        fields += ("round_text", "follows", "hq", "hq_basis", "date", "first_seen", "yc_batch", "city", "baseline_unpushed")
    result = {**{key: event[key] for key in fields},
              "sources": [_display_source(s) for s in event["sources"]]}
    result["round"] = wording.SITE_TEXT["round_pending"] if result["round"] == "待核" else result["round"]
    result["status"] = wording.STATUS_LABELS.get(result["status"], result["status"])
    result["marks"] = [wording.MARK_LABELS.get(m, m) for m in result["marks"]]
    result["tags"] = [wording.display_text(t) for t in result["tags"]]
    result["pending_reason"] = wording.display_text(result["pending_reason"])
    # 历史国家原因不改变轮次显示，但这些事件也没有作为卡片推送过。
    if event["out_of_scope"] or event["legacy_country_filtered"]:
        result["push_date"] = None
    return result


def _site_payloads(data: dict) -> tuple[dict, dict[str, dict]]:
    # 列表不加载融资历史和深度原句；各详情文件自足，不依赖列表中的事件。
    by_company = defaultdict(list)
    for event in data["events"]:
        by_company[event["company_id"]].append(_display_event(event))
    details, companies = {}, []
    for company in data["companies"]:
        cid = company["id"]
        # company_id 来自稳定 slug；同时拒绝损坏的数据形成跨目录写入。
        if not re.fullmatch(r"[\w-]+", cid):
            raise ValueError("公司编号不是有效的 slug")
        detail_url = f"./c/{quote(cid, safe='')}.json"
        if company.get("baseline_unpushed"):
            detail = {**company, "header": _display_header(company["header"]),
                      "sources": [_display_source(s) for s in company["sources"]]}
            fields = ("id", "company_id", "round", "round_text", "amount", "leads", "follows", "hq",
                      "hq_basis", "date", "first_seen", "push_date", "country", "country_basis", "city")
            details[f"c/{cid}.json"] = {"companies": [detail],
                                       "events": [{key: e[key] for key in fields} for e in by_company[cid]]}
            companies.append({"id": cid, "detail_url": detail_url, "header": {"name": company["header"]["name"]},
                              "country": company["country"], "country_basis": company["country_basis"]})
            continue
        deep = company["deepdive"]
        deep = {key: deep[key] for key in ("status", "online_at", "items", "entities", "new_evidence")}
        detail = {key: value for key, value in company.items() if key not in {"deepdive", "sources"}}
        detail["header"] = _display_header(detail["header"])
        detail.update(deepdive=deep, sources=[_display_source(s) for s in company["sources"]])
        details[f"c/{cid}.json"] = {"companies": [detail], "events": by_company[cid]}
        companies.append({"id": cid, "header": {key: company["header"][key] for key in ("name", "country")},
                          "country": company["country"], "country_basis": company["country_basis"],
                          "deepdive": {key: deep[key] for key in ("status", "online_at")},
                          "detail_url": detail_url})
    visible = set(data["listing"])
    filtered = set(data["week"]["filtered"])
    events, listing = [], []
    by_id = {event["id"]: event for event in data["events"]}
    supplement_ids = {eid for issue in data["digests"] for eid in issue.get("supplement_ids", [])}
    for eid in data["listing"]:
        event = by_id[eid]
        # 事件编号保持稳定；同公司不同期的融资事件分别保留，摘要重列同一事件不复制卡片。
        issue = next((d for d in data["digests"] if d["date"] == event["push_date"]
                      and (eid in d["event_ids"] or eid in d.get("supplement_ids", []))), None)
        card = _display_event(event, card=True)
        card["issue_date"] = event["push_date"]
        if eid not in supplement_ids or event.get("supplement_unsent"):
            card["backfill"] = ""
        if event.get("supplement_unsent"):
            card.update(status=wording.SITE_TEXT["supplement_unsent"], pending_reason="", next_push_label=None)
        if issue:
            frozen = issue.get("card_sources", {}).get(str(eid))
            if frozen is not None:
                card["sources"] = frozen
            elif issue.get("generated_at"):
                # 旧期没有快照时，只采用当时已经入库的来源，不回写旧 digest。
                cutoff = _moment(issue["generated_at"])
                card["sources"] = [_display_source(s) for s in event["sources"]
                                   if not s.get("first_seen_at") or _moment(s["first_seen_at"]) <= cutoff]
        events.append(card)
        listing.append(card["id"])
    for event in data["events"]:
        if event["id"] in filtered and event["id"] not in visible:
            events.append({key: event[key] for key in ("id", "company_id", "name", "filter_reasons", "country", "country_basis")})
    # 某一期选择器只需要日期；历史过滤事件仍留在数据库和各公司详情中。
    index = {"meta": {key: value for key, value in data["meta"].items() if key != "next_push_date"},
             "listing": listing,
             "week": {key: data["week"][key] for key in
                      ("date", "cards", "round_pending", "filtered", "event_ids", "yc")},
             "companies": companies, "events": events,
             "digests": [{"date": d["date"]} for d in data["digests"]], "wording": wording.SITE_TEXT}
    return index, details


_HTTP_URL = re.compile(
    r"""https?://(?:[^\s<>"'\\\x60()\[\]{}，。；！？、（）【】]|\([^\s<>"'\\()]*\)|\[[0-9a-f:.]+\])+""", re.I)
_IPV4_OCTET = r"(?:25[0-5]|2[0-4][0-9]|[01]?[0-9]?[0-9])"
_PUBLISH_RULES = (
    ("本机路径", re.compile(r"/srv/private|/srv/private|/cursor/|/tmp/|/root/|/Users/|vc-radar-data", re.I), True),
    ("密钥/环境变量", re.compile(r"DEEPSEEK|API_KEY|sk-[a-z0-9]{16,}|\.env|Authorization|Bearer ", re.I), False),
    ("邮箱", re.compile(r"[\w.+%'-]+@[\w-]+(?:\.[\w-]+)+"), False),
    ("IPv4 地址", re.compile(rf"(?<![0-9.]){_IPV4_OCTET}(?:\.{_IPV4_OCTET}){{3}}(?![0-9.])"), False),
    ("localhost", re.compile(r"localhost", re.I), False),
    ("内部人名/bot 名", re.compile(r"ownerx|维护者x|取件程序x|assistx|codegenx|QA-x|UI-x|PD-x", re.I), True),
)


def _non_url_text(content: str) -> str:
    def mask(match):
        # 只豁免有主机的 HTTP(S) 地址；损坏的网址仍按普通文字扫描。
        try:
            parts = urlsplit(match[0])
            if parts.hostname:
                parts.port
                return " "
        except ValueError:
            pass
        return match[0]

    return _HTTP_URL.sub(mask, content)


def _validate_outputs(outputs: dict[str, str]) -> None:
    problems = []
    for name, content in sorted(outputs.items()):
        text = f"{name}\n{content}"
        non_url = _non_url_text(text)
        categories = [label for label, pattern, exempt_urls in _PUBLISH_RULES
                      if pattern.search(non_url if exempt_urls else text)]
        if guard.check_no_fictional(text):
            categories.append("禁用名")
        if guard.check_no_placeholder(text):
            categories.append("占位地址")
        if categories:
            # 只报文件和类别，不把可能的密钥正文写入异常。
            problems.append(f"{name}：{'、'.join(categories)}")
    if problems:
        raise ValueError("站点产物检查失败：" + "；".join(problems))


RUNTIME_STATUS_KEYS = ("date", "status", "push", "alarm", "outbox", "simulated", "fx")
V16_STATUS_KEYS = ("sources", "not_connected", "pending_ratios", "fx", "verification", "homepage",
                   "deepdive_progress", "deepdive_budget", "extract_budget", "quality_pending", "new_evidence", "missed")


def write_status(conn, cfg: Config, now: datetime, *, runtime=None, snapshot=None) -> dict:
    """原运行字段与 V16o 状态合并写入内部文件，不混入 digest 计数。"""
    root = cfg.state_dir / "test" if cfg.test_mode else cfg.state_dir
    path = root / "status.json"
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    latest = next(iter(_rows(conn, "SELECT date,status,push FROM digest ORDER BY date DESC LIMIT 1")), None)
    values = {"date": str(now.astimezone(BJ).date()), "status": latest["status"] if latest else "没有新增",
              "push": bool(latest["push"]) if latest else False, "alarm": False,
              "outbox": "", "simulated": bool(cfg.test_mode), "fx": fx.status_from_files(cfg)}
    values.update({key: previous[key] for key in RUNTIME_STATUS_KEYS if key in previous})
    values.update({key: value for key, value in (runtime or {}).items() if key in RUNTIME_STATUS_KEYS})
    # 汇率的读入口仍是原文件；生成状态时可看到本次运行的新值。
    status = snapshot["status"] if snapshot is not None else _snapshot(conn, cfg, now)["status"]
    values.update({key: status[key] for key in V16_STATUS_KEYS if key != "fx"})
    outbox.write_atomic(path, values)
    return values


def _test_snapshot_path(cfg, moment, slot=None):
    parent = cfg.state_dir / "test-site" / str(moment.date())
    name = slot or moment.strftime("%H%M")
    if slot is None and name in {"0930", "1030"} or (parent / name).exists():
        name = moment.strftime("%H%M%S")
    candidate = parent / name
    suffix = 2
    while candidate.exists():
        candidate = parent / f"{name}-{suffix}"
        suffix += 1
    return candidate


def build_site(conn: sqlite3.Connection, cfg: Config, *, out_dir=None, now=None, snapshot_slot=None) -> Path:
    """生成完整静态快照并替换站点；失败保留原站点，成功保留上一份到 site.prev。"""
    moment = now or datetime.now(BJ)
    if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now 必须是带时区的 datetime")
    moment = moment.astimezone(BJ)
    dest = Path(out_dir) if out_dir is not None else cfg.state_dir / ("test/site" if cfg.test_mode else "site")
    previous = dest.with_name(dest.name + ".prev")
    if any(p.is_symlink() or (p.exists() and not p.is_dir()) for p in (dest, previous)):
        raise ValueError("站点路径必须是普通目录")
    snapshot = _snapshot(conn, cfg, moment)
    data, details = _site_payloads(snapshot)
    assets = [p for p in ASSETS.rglob("*") if p.is_file()]
    outputs = {p.relative_to(ASSETS).as_posix(): p.read_text(encoding="utf-8") for p in assets if p.suffix != ".woff2"}
    fonts = {p.relative_to(ASSETS).as_posix(): p.read_bytes() for p in assets if p.suffix == ".woff2"}
    outputs["index.html"] = re.sub(r"__COPY_(\w+)__", lambda m: wording.SITE_TEXT[m[1]], outputs["index.html"])
    for name, payload in {"data.json": data, **details}.items():
        outputs[name] = json.dumps({**payload, "wording": wording.SITE_TEXT} if name != "data.json" else payload,
                                   ensure_ascii=False, indent=1, allow_nan=False)
    data_bytes = len(outputs["data.json"].encode("utf-8"))
    if data_bytes >= MAX_DATA_BYTES:
        raise ValueError(f"站点列表超过 500KB：data.json 实际 {data_bytes} 字节")
    # 全部资源和详情先在内存中检查；测试模式同样拒绝，检查失败不创建目录。
    _validate_outputs(outputs)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{dest.name}-", dir=dest.parent))
    retired = dest.with_name(f".{dest.name}-retired-{uuid4().hex}")
    moved_old = moved_previous = False
    try:
        for name, content in outputs.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        for name, content in fonts.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if dest.exists():
            if previous.exists():
                os.replace(previous, retired)
                moved_previous = True
            os.replace(dest, previous)
            moved_old = True
        try:
            os.replace(stage, dest)
        except OSError:
            if moved_old:
                os.replace(previous, dest)
                moved_old = False
            raise
    except BaseException:
        if moved_previous and retired.exists():
            os.replace(retired, previous)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    if retired.exists():
        shutil.rmtree(retired)
    status = write_status(conn, cfg, moment, snapshot=snapshot)
    if cfg.test_mode:
        history = _test_snapshot_path(cfg, moment, snapshot_slot)
        history.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copytree(dest, history)
            outbox.write_atomic(history / "status.json", status)
        except BaseException:
            shutil.rmtree(history, ignore_errors=True)
            raise
    return dest
