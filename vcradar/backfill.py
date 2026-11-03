"""V16o 首期补充：清单冻结金额和证据，事务应用，一次性推送。"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import csv
import hashlib
import io
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import sys

from . import cal, db, extract, fx, outbox, scan, wording
from .config import BJ, Config, is_test_run
from .models import PushSection
from .sources.yc_directory import batch_order

_AMOUNTS = ("amount_currency", "amount_original", "amount_usd", "fx_rate", "fx_date", "fx_source", "fx_stale")
_EVENT_FIELDS = ("round_norm", "round_text", "amount_text", "amount_status", *_AMOUNTS,
                 "leads_json", "follows_json", "event_date", "yc_batch", "marks_json", "filter_reasons_json")
_COMPANY_FIELDS = ("country", "hq", "hq_basis", "city", "status", "pending_reason", "ai_label", "social_label", "label_quotes_json")
_ROUNDS = {"天使轮", "种子轮", "A 轮"}


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _time(value):
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)


def _stamp(value):
    return _time(value).strftime("%Y-%m-%d %H:%M:%S")


def memory_copy(conn: sqlite3.Connection) -> sqlite3.Connection:
    """复制已提交的只读库到内存，允许清单和预览兼容尚未迁移的旧库。"""
    work = sqlite3.connect(":memory:")
    work.row_factory = sqlite3.Row
    conn.backup(work)
    db.migrate_backfill(work)
    return work


def _sort_key(event, name):
    bucket = (3 if event["amount_status"] == "金额待确认" else
              0 if event.get("amount_usd") is not None else
              1 if event["amount_status"] in {"币种待确认", "暂未换算"} else 2)
    return (bucket, -(event.get("amount_usd") or 0) if bucket == 0 else 0,
            -date.fromisoformat(event["event_date"]).toordinal(), name.casefold(), event["id"])


def _needed(company, event):
    fields = []
    if company["country"] == scan.COUNTRY_PENDING:
        fields.extend(("国家", "城市"))
    if event["round_norm"] == "待核":
        fields.append("轮次")
    if event["amount_status"] in {"未披露", "金额待核"}:
        fields.append("金额")
    if event["amount_status"] == "币种待确认":
        fields.append("币种")
    if "待核" in (company["ai_label"], company["social_label"]):
        fields.append("标签")
    return fields


def default_verifier(candidate: dict, *, now: datetime, test_mode: bool, search_limit: int) -> dict:
    """任务 T 的注入接口；本任务不搜索，明确记录本次没有查证。"""
    reason = "测试跳过" if test_mode else "搜索失败"
    return {"searches": [], "model_calls": [], "fields": [
        {"field": field, "result": "本次未查", "reason": reason, "urls": [], "tiers": [],
         "quotes": [], "verified_at": _stamp(now)} for field in candidate["pending_fields"]]}


def _amount_values(event):
    return {"currency": event["amount_currency"], "orig_amount": event["amount_original"],
            "usd_amount": event["amount_usd"], "rate_date": event["fx_date"],
            "amount_status": event["amount_status"], "large": "大额" in json.loads(event["marks_json"])}


def _amount_quotes(record):
    ext, raw = record["ext"], record["raw"]
    quote = ext.get("quotes", {}).get("amount_text") or ext.get("quotes", {}).get("amount")
    # 原句优先；标题、摘录中只取金额句，不能把公司名里的数字当作融资额。
    sentences = [s.strip() for text in (raw["title"], raw["excerpt"])
                 for s in re.split(r"(?<=[。!?])|(?<=\.)\s+|\n+", text or "") if s.strip()]
    monetary = [s for s in sentences if fx.AMOUNT_PATTERN.search(s) or re.search(
        r"\braise[sd]?\s+\d[\d,.]*\s*(?:million|billion|thousand|crore|lakh)\b", s, re.I)]
    short_quote = bool(quote and (quote == ext.get("amount_text") or _parse_quote(quote) == [quote.strip()]))
    full_quote = [quote] if quote and (not short_quote or not sentences) else []
    if monetary or full_quote:
        # 短金额字段可能正是旧解析器截掉数量级的结果；有完整金额句时优先原句。
        return list(dict.fromkeys([*full_quote, *monetary]))
    absent = [s for s in sentences if re.search(r"undisclosed|amount.*(?:not|no)|金额.*(?:未|没)|未披露", s, re.I)]
    # 有保存的原文却没有金额数字，不能把旧解析器的错值重新当成证据。
    return absent or sentences or [ext.get("amount_text") or ""]


def _parse_quote(quote):
    matches = list(fx.AMOUNT_PATTERN.finditer(quote))
    # 无符号的数量级只在融资句或整段金额字段中采用，不拿年份和公司名凑金额。
    if not matches:
        bare = re.search(r"(?:\braise[sd]?\s+)(\d[\d,.]*\s*(?:million|billion|thousand|crore|lakh)\b)", quote, re.I)
        if bare:
            return [bare[1]]
        if re.fullmatch(r"\s*\d[\d,.]*(?:\s*(?:million|billion|thousand|crore|lakh|万|亿))?\s*", quote, re.I):
            return [quote.strip()]
    # 估值、累计和此前轮次的金额不参与这轮融资的重解析。
    return [m[0] for m in matches if not re.search(
        r"(?:valued\s+at|valuation(?:\s+of)?|previously\s+raised|此前融资|估值)\s*$",
        quote[max(0, m.start() - 40):m.start()], re.I)]


def _correct_amount(work, event, rates):
    evidence = scan._evidence_rows(work, event_id=event["id"])
    parsed, quotes, urls = [], [], []
    source_updates = []
    for record in evidence:
        sentences = _amount_quotes(record)
        source_values = []
        for quote in sentences:
            quotes.append(quote)
            urls.append(record["url"])
            for text in _parse_quote(quote):
                currency, number = fx.parse_amount(text, context=quote)
                if number is None:
                    continue
                announced = scan._item_time(record["item"], scan._dt(record["raw"]["first_seen_at"])).date()
                snapshot = rates.snapshot(announced, currencies=(currency,)) if currency not in {None, "USD"} else None
                value = fx.convert_amount(text, snapshot, context=quote).to_dict()
                source_values.append((value, text, snapshot))
                parsed.append((value, text, snapshot))
        source_updates.append((record, source_values[0] if source_values else None))
    if not evidence:
        quote = event["amount_text"] or ""
        quotes.append(quote)
        for text in _parse_quote(quote):
            currency, number = fx.parse_amount(text)
            if number is None:
                continue
            snapshot = rates.snapshot(date.fromisoformat(event["event_date"]), currencies=(currency,)) if currency not in {None, "USD"} else None
            parsed.append((fx.convert_amount(text, snapshot).to_dict(), text, snapshot))
    conflicting = False
    for i, (a, _, _) in enumerate(parsed):
        for b, _, _ in parsed[i + 1:]:
            x, y = a["amount_usd"], b["amount_usd"]
            if x is None or y is None:
                if a["amount_currency"] and a["amount_currency"] == b["amount_currency"]:
                    x, y = a["amount_original"], b["amount_original"]
            if x is not None and y is not None and abs(x - y) > min(x, y) * .1 + 1e-6:
                conflicting = True
    unknown = not parsed or conflicting
    converted, text, snapshot = (fx.Amount().to_dict(), None, None) if unknown else parsed[0]
    status = ("金额待确认" if unknown else "币种待确认" if converted["amount_currency"] is None else
              "暂未换算" if converted["amount_usd"] is None else "披露")
    marks = scan._event_marks(json.loads(event["marks_json"]), event["round_norm"], status, converted["amount_usd"])
    work.execute("UPDATE funding_event SET amount_text=?,amount_status=?,marks_json=?," +
                 ",".join(f"{k}=?" for k in _AMOUNTS) + " WHERE id=?",
                 (text, status, _json(marks), *(converted[k] for k in _AMOUNTS), event["id"]))
    for record, value in source_updates:
        ext = record["ext"]
        if unknown or value is None:
            ext.update(amount_text=None, _amount_fx=fx.Amount().to_dict(), _amount_status="金额待确认")
        else:
            amount, source_text, _ = value
            ext.update(amount_text=source_text, _amount_fx=amount, _amount_status=status)
        work.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                     (_json(ext), event["id"], record["raw_item_id"]))
    work.execute("DELETE FROM meta WHERE key=?", (f"fx:event:{event['id']}",))
    if snapshot is not None:
        work.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"fx:event:{event['id']}", _json(snapshot)))
    fresh = dict(work.execute("SELECT * FROM funding_event WHERE id=?", (event["id"],)).fetchone())
    return fresh, "；".join(dict.fromkeys(q for q in quotes if q)), next(iter(urls), "")


def _fill_summary(work, candidate, cfg, now, client):
    candidate["summary"] = None
    company = work.execute("SELECT summary FROM company WHERE id=?", (candidate["company_id"],)).fetchone()
    if company[0]:
        return
    if cfg.test_mode and client is None:
        candidate["model_calls"].append({"kind": "summary", "reason": "测试跳过", "cost": 0})
        return
    records = scan._evidence_rows(work, event_id=candidate["event_id"])
    if not records:
        candidate["model_calls"].append({"kind": "summary", "reason": "来源里没写", "cost": 0})
        return
    item = replace(records[0]["item"], body="\n".join(
        r["raw"]["title"] + "\n" + r["raw"]["excerpt"] for r in records)[:8000])
    ext = scan._extracted(records[0]["ext"])
    if client is None:
        from .pipeline import model_client
        client = model_client(cfg, None)
    system, user = extract.build_prompt(item, ext, "summary")
    minimum = 40 if extract._fact_sentences(item) < 3 else 80
    from . import placenames
    names = placenames.company_names(work, candidate["company_id"])
    place_retry = False
    place_fallback = None
    for attempt in range(2):
        price = cfg.verify_model_price_peak if (now.weekday() < 5 and cal.is_cn_workday(now.date())
            and (9 <= now.hour < 12 or 14 <= now.hour < 18)) else cfg.verify_model_price_idle
        call = {"kind": "summary", "requested_at": _stamp(now), "cost": price}
        candidate["model_calls"].append(call)
        if place_retry:
            placenames.record_retry(candidate["name"])
            place_retry = False
        try:
            response = client.complete_json(system, user, 120)
            call["ok"] = response.ok
            if not response.ok:
                call["reason"] = "模型失败"
                continue
            data = json.loads(response.content)
            summary = extract.sanitize_summary(item, data.get("summary", ""))
            if minimum <= extract.summary_len(summary) <= 140 and 3 <= summary.count("。") <= 4:
                normalized, _, problems = placenames.check_text(summary, company=candidate["name"], protected_names=names)
                if problems and attempt == 0:
                    place_fallback = normalized, problems
                    request = json.loads(user)
                    request["summary_correction"] = {"previous_summary": normalized, "problems": problems,
                        "instruction": "公司在哪里写在<地点>，地名用中文，名称照原文。"}
                    user = json.dumps(request, ensure_ascii=False)
                    place_retry = True
                    continue
                placenames.record_remaining(candidate["name"], problems)
                candidate["summary"] = normalized
                return
            call["reason"] = "简介校验失败"
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            call["reason"] = "模型失败"
    if place_fallback is not None:
        candidate["summary"], problems = place_fallback
        placenames.record_remaining(candidate["name"], problems)


def create_manifest(conn: sqlite3.Connection, cfg: Config, *, now: datetime, rates,
                    verifier=None, summary_client=None, homepage_checker=None, real_now: datetime | None = None) -> dict:
    """只在内存重判、金额更正、排序、搜索和补简介，生成时间使用真实钟。"""
    work = memory_copy(conn)
    try:
        if homepage_checker is None:
            from .homepage import backfill_checker, _LocalClock
            homepage_checker = backfill_checker(work, cfg, _LocalClock(now))
        return _manifest(work, cfg, now, rates, verifier or default_verifier, summary_client, homepage_checker,
                         real_now or datetime.now(BJ))
    finally:
        work.close()


def _manifest(work, cfg, now, rates, verifier, summary_client, homepage_checker, real_now):
    companies = {r["id"]: dict(r) for r in work.execute("SELECT * FROM company")}
    original = {r["id"]: dict(r) for r in work.execute("SELECT * FROM funding_event")}
    targets = {}
    for event in original.values():
        company = companies[event["company_id"]]
        kind = "B" if company["status"] == "总部待核" else "A" if "总部非美国" in json.loads(event["filter_reasons_json"]) else None
        if kind and not company["backfill"]:
            target = targets.setdefault(company["id"], {"types": {kind}, "ids": []})
            target["ids"].append(event["id"])
    for cid, c in companies.items():
        if c["status"] == "总部待核" and not c["backfill"]:
            targets.setdefault(cid, {"types": {"B"}, "ids": []})
    batches = {r[0] for r in work.execute("SELECT yc_batch FROM company UNION SELECT yc_batch FROM funding_event") if r[0]}
    batches.update(json.loads(r[0])["yc_batch"] for r in work.execute("SELECT structured_json FROM raw_item") if json.loads(r[0]).get("yc_batch"))
    batches = sorted(batches, key=batch_order, reverse=True)[:4]
    candidates, fixes_candidate, fixes_noncandidate = [], [], []
    for cid, target in targets.items():
        for eid in target["ids"]:
            work.execute("UPDATE funding_event SET filter_reasons_json=? WHERE id=?",
                         (_json([r for r in json.loads(original[eid]["filter_reasons_json"]) if r != "总部非美国"]), eid))
            for source in scan._evidence_rows(work, event_id=eid):
                ext = scan._effective_extracted(source["item"], scan._extracted(source["ext"]), batches)
                data = {**source["ext"], **asdict(ext), "_latest_yc_batches": batches}
                work.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                             (_json(data), eid, source["raw_item_id"]))
        scan._refresh_company(work, cid, fx_rates=rates)
        for eid in target["ids"]:
            if not scan._evidence_rows(work, event_id=eid):
                old = original[eid]
                norm = scan.normalize_round(old["round_text"] or old["round_norm"])
                reasons = [r for r in json.loads(old["filter_reasons_json"]) if r not in {"总部非美国", "批次超过 12 个月", "轮次不在范围"}]
                if old["yc_batch"] and batches and old["yc_batch"] not in batches:
                    reasons.append("批次超过 12 个月")
                if norm == "过滤":
                    reasons.append("轮次不在范围")
                work.execute("UPDATE funding_event SET round_norm=?,filter_reasons_json=? WHERE id=?", (norm, _json(reasons), eid))
            event = dict(work.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone())
            fresh, quote, url = _correct_amount(work, event, rates)
            old_values, new_values = _amount_values(original[eid]), _amount_values(fresh)
            if old_values != new_values:
                fix = {"event_id": eid, "company_id": cid, "name": companies[cid]["name"],
                       "old": old_values, "new": new_values, "quote": quote, "url": url}
                (fixes_noncandidate if "B" in target["types"] else fixes_candidate).append(fix)
        company = dict(work.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone())
        if not scan._evidence_rows(work, company_id=cid):
            company["country"] = scan.country_from_hq(company["hq"], company["hq_basis"], company_name=company["name"])
        events = [dict(work.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()) for eid in target["ids"]]
        nonfiltered = [e for e in events if not json.loads(e["filter_reasons_json"])]
        eligible = nonfiltered if "A" in target["types"] else []
        if len(eligible) > 1:
            raise ValueError("一家公司有多条补充候选事件：" + company["name"])
        all_events = [dict(r) for r in work.execute("SELECT * FROM funding_event WHERE company_id=?", (cid,))]
        active = [e for e in all_events if not json.loads(e["filter_reasons_json"])]
        company["status"] = "已过滤" if all_events and not active else "轮次待核" if active and all(e["round_norm"] == "待核" for e in active) else "已纳入"
        company["pending_reason"] = "来源没写轮次" if company["status"] == "轮次待核" else ""
        work.execute("UPDATE company SET country=?,status=?,pending_reason=? WHERE id=?",
                     (company["country"], company["status"], company["pending_reason"], cid))
        representative = min(eligible or nonfiltered or events, key=lambda e: _sort_key(e, company["name"]), default=None)
        for event in events:
            event["amount_label"] = scan.amount_label(work, event)
            event["fx_snapshot"] = scan._fx_snapshot(work, event["id"])
            event["event_sources"] = [{"raw_item_id": s["raw_item_id"], "extraction_json": s["extraction_json"]}
                                      for s in work.execute("SELECT * FROM event_source WHERE event_id=?", (event["id"],))]
        reasons = list(dict.fromkeys(r for e in events for r in scan.display_filter_reasons(json.loads(e["filter_reasons_json"]))))
        conclusion = "收录" if nonfiltered else "仍过滤：" + "、".join(wording.display_text(r) for r in reasons)
        if company["status"] == "轮次待核":
            conclusion = "轮次待确认"
        sources = list(dict.fromkeys((s["source_name"], s["url"]) for eid in target["ids"]
                                    for s in work.execute("SELECT * FROM event_source WHERE event_id=? ORDER BY raw_item_id", (eid,))))
        eid = representative["id"] if representative else None
        candidates.append({"company_id": cid, "name": company["name"], "object_types": sorted(target["types"]),
            "event_id": eid, "push_date": original[eid]["push_date"] if eid is not None else None,
            "original_status": companies[cid]["status"],
            "original_reasons": [r for i in target["ids"] for r in json.loads(original[i]["filter_reasons_json"])],
            "rejudge": {**{k: company[k] for k in _COMPANY_FIELDS}, "events": events, "conclusion": conclusion,
                        "supplement_event_ids": [e["id"] for e in eligible]},
            "rank": None, "in_supplement": False, "sources": [{"name": n, "url": u} for n, u in sources],
            "is_baseline": bool(events) and all(e["baseline_unpushed"] for e in events),
            "pending_fields": _needed(company, representative) if representative else ["国家"],
            "searches": [], "model_calls": [], "fields": [], "planned_searches": 0,
            "verification_status": "本次未查", "_sort": _sort_key(representative, company["name"]) if representative else (4, 0, 0, company["name"].casefold())})
    ranked = sorted((c for c in candidates if c["rejudge"]["supplement_event_ids"]), key=lambda c: c["_sort"])
    rank = 0
    for candidate in ranked:
        rank = max(rank + 1, 11 if candidate["_sort"][0] == 3 else 1)
        candidate.update(rank=rank, in_supplement=rank <= 10)
    budget = max(0, cfg.backfill_verify_limit)
    for c in sorted(candidates, key=lambda c: (c["rank"] or float("inf"), c["name"].casefold())):
        c.pop("_sort")
        pending_company = "B" in c["object_types"]
        c["search_group"] = "A" if pending_company else "B" if c["in_supplement"] and c["pending_fields"] else "C" if c["in_supplement"] else "D"
        needs_search = c["rejudge"]["country"] == scan.COUNTRY_PENDING if pending_company else c["in_supplement"] and bool(c["pending_fields"])
        limit = min(3, budget) if pending_company else 3
        if needs_search and limit:
            c["planned_searches"] = limit
            try:
                result = verifier(c, now=now, test_mode=cfg.test_mode, search_limit=limit)
            except Exception:
                result = default_verifier(c, now=now, test_mode=cfg.test_mode, search_limit=limit)
            for key in ("searches", "model_calls", "fields"):
                c[key] = result.get(key, [])
            if len(c["searches"]) > limit or len(c["model_calls"]) > 2:
                raise ValueError("补充查证超过单家次数上限")
            if pending_company:
                budget -= len(c["searches"])
            c["verification_status"] = "、".join(dict.fromkeys(f["result"] for f in c["fields"])) or "本次未查"
            from .verify import company_context, save_result
            if c["event_id"] is not None:
                context = company_context(work, c["company_id"], cfg, event_id=c["event_id"])
                save_result(work, context, result, now=now, source="补充命令", fx_rates=rates)
                fresh = dict(work.execute("SELECT * FROM company WHERE id=?", (c["company_id"],)).fetchone())
                c["rejudge"].update({k: fresh[k] for k in _COMPANY_FIELDS})
                for event in c["rejudge"]["events"]:
                    row = dict(work.execute("SELECT * FROM funding_event WHERE id=?", (event["id"],)).fetchone())
                    # 金额更正已在搜索前冻结；查证不能改动待 apply 照抄的值。
                    keys = [k for k in _EVENT_FIELDS if k not in {"amount_text", "amount_status", "marks_json", *_AMOUNTS}]
                    event.update({k: row[k] for k in keys})
        if c["in_supplement"]:
            _fill_summary(work, c, cfg, now, summary_client)
            checked = homepage_checker(c)
            c["homepage_check"] = {k: v for k, v in checked.items() if k != "searches"} if checked else None
            if checked:
                c["searches"].extend({**s, "kind": "E"} for s in checked.get("searches", []))
        else:
            c["homepage_check"] = None
    suspects = []
    scope = {eid for t in targets.values() for eid in t["ids"]}
    for eid, event in original.items():
        if eid in scope or event["amount_usd"] is None or event["amount_usd"] >= 1000:
            continue
        for record in scan._evidence_rows(work, event_id=eid):
            quote = next((q for q in _amount_quotes(record) if re.search(r"million|billion|亿|万", q, re.I)), None)
            if quote:
                suspects.append({"event_id": eid, "name": companies[event["company_id"]]["name"],
                                 "amount": event["amount_text"], "quote": quote, "url": record["url"]})
                break
    candidates.sort(key=lambda c: (c["rank"] or float("inf"), c["name"].casefold()))
    rejudge, searches = [], []
    for c in candidates:
        snapshot = c.pop("rejudge")
        fields, calls, requests = c.pop("fields"), c.pop("model_calls"), c.pop("searches")
        rejudge.append({"company_id": c["company_id"], "name": c["name"],
            "old": {k: companies[c["company_id"]][k] for k in ("country", "status")},
            "new": {k: snapshot[k] for k in ("country", "status")},
            "basis": snapshot["hq_basis"] or "按 6.3 对照表", "rank": c["rank"],
            "in_supplement": c["in_supplement"], "snapshot": snapshot,
            "unsearched_fields": fields if not requests else [],
            "summary_model_calls": [m for m in calls if m.get("kind") == "summary"]})
        primary = next((e for e in snapshot["events"] if e["id"] == c["event_id"]), None)
        c["filter_reasons"] = {"old": json.loads(original[c["event_id"]]["filter_reasons_json"]) if primary else [],
                               "new": json.loads(primary["filter_reasons_json"]) if primary else []}
        last_non_e = max((seq for seq, request in enumerate(requests, 1) if request.get("kind") != "E"), default=0)
        for seq, request in enumerate(requests, 1):
            last = seq == last_non_e
            evidence = []
            for field in fields if last else []:
                for index, url in enumerate(field.get("urls", [])):
                    evidence.append({"field": field["field"], "new_value": field.get("value"), "url": url,
                        "quote": (field.get("quotes", []) + [""] * (index + 1))[index],
                        "tier": (field.get("tiers", []) + ["其他"] * (index + 1))[index]})
            searches.append({**request, "company_id": c["company_id"], "name": c["name"],
                "kind": request.get("kind", c["search_group"]), "seq": seq,
                "fields": request.get("fields", evidence),
                "conclusion": ({f["field"]: {k: v for k, v in f.items() if k != "field"} for f in fields}
                               if last else request.get("conclusion", {})),
                "model_calls": [m for m in calls if m.get("kind") != "summary"] if last else []})
    return {"generated_at": _stamp(real_now), "mode": "测试" if cfg.test_mode else "生产", "candidates": candidates,
            "rejudge": rejudge, "searches": searches,
            "amount_fix_candidate": fixes_candidate, "amount_fix_noncandidate": fixes_noncandidate,
            "out_of_scope_amount_suspects": suspects}


def _working_candidates(manifest):
    rows = {r["company_id"]: r for r in manifest["rejudge"]}
    return [{**c, "rejudge": rows[c["company_id"]]["snapshot"],
             "searches": [s for s in manifest["searches"] if s["company_id"] == c["company_id"]],
             "fields": rows[c["company_id"]].get("unsearched_fields", []),
             "model_calls": rows[c["company_id"]].get("summary_model_calls", [])}
            for c in manifest["candidates"]]


def _summary(manifest):
    candidates = _working_candidates(manifest)
    return {"重判家数": len(candidates), "A 已过滤家数": sum("A" in c["object_types"] for c in candidates),
            "B 总部待核家数": sum("B" in c["object_types"] for c in candidates),
            "进补充段家数": sum(c["in_supplement"] for c in candidates),
            "仍过滤家数": sum(c["rejudge"]["conclusion"].startswith("仍过滤") for c in candidates),
            "只作基线家数": sum(c["is_baseline"] for c in candidates),
            "需要搜索的家数": sum(c["planned_searches"] > 0 for c in candidates),
            "实际搜索次数": sum(len(c["searches"]) for c in candidates)}


def write_reports(manifest: dict, directory: Path) -> tuple[Path, Path]:
    """写人工复核用的同内容 CSV、Markdown，含搜索计划和费用估算。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = manifest["generated_at"][:10] + "-backfill-gap"
    csv_path, md_path = directory / (stem + ".csv"), directory / (stem + ".md")
    columns = ["公司名", "company_id", "对象类型", "首期时的状态或过滤原因", "国家", "国家依据", "轮次", "金额", "原文金额",
               "来源", "来源 URL", "事件日期", "是否基线", "重判结论", "排序名次", "会不会进补充段", "计划搜索次数", "实际搜索次数", "费用", "查证状态"]
    rows = []
    candidates = _working_candidates(manifest)
    for c in candidates:
        r = c["rejudge"]
        primary = r["supplement_event_ids"]
        event = (next(e for e in r["events"] if e["id"] == primary[0]) if primary else
                 min(r["events"], key=lambda e: _sort_key(e, c["name"]), default={}))
        cost = sum(float(s.get("cost", 0)) for s in c["searches"]) + sum(float(m.get("cost", 0)) for m in c["model_calls"])
        rows.append([c["name"], c["company_id"], "/".join(t + (" 已过滤" if t == "A" else " 总部待核") for t in c["object_types"]),
                     "、".join(c["original_reasons"]) or c["original_status"], r["country"], r["hq_basis"],
                     wording.display_text(event.get("round_norm", "待核")), event.get("amount_label", "金额未披露"), event.get("amount_text") or "",
                     "、".join(s["name"] for s in c["sources"]), " ".join(s["url"] for s in c["sources"]), event.get("event_date", ""),
                     "是" if c["is_baseline"] else "否", r["conclusion"], c["rank"] or "", "是" if c["in_supplement"] else "否",
                     c["planned_searches"], len(c["searches"]), f"{cost:.4f}", c["verification_status"]])
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(columns)
    writer.writerows(rows)
    summary = _summary(manifest)
    search_count = summary["需要搜索的家数"]
    countries = Counter(c["rejudge"]["country"] for c in candidates)
    md = ["# 首期补充清单", "", " · ".join(f"{key}：{value}" for key, value in summary.items()),
          "国家分布：" + " · ".join(f"{key} {value}" for key, value in sorted(countries.items())),
          f"实际费用：¥{_cost(manifest):.4f}。",
          f"费用估算：平均 ¥{search_count * (2 * .005 + 1.5 * .0035):.4f}；最坏 ¥{search_count * (3 * .005 + 2 * .0035):.4f}。",
          "按每次搜索 ¥0.005、每次模型调用 ¥0.0035；平均每家 2 次搜索和 1.5 次调用，最坏 3 次搜索和 2 次调用。", "",
          "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    md.extend("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", "<br>") for v in row) + " |" for row in rows)
    for key, title in (("amount_fix_candidate", "金额更正（候选）"), ("amount_fix_noncandidate", "金额更正（非候选）")):
        md.extend(("", "## " + title, "", "| event_id | 公司 | 旧值 → 新值 | 大额（旧 → 新） | 原文句子 | 网址 |", "| --- | --- | --- | --- | --- | --- |"))
        fixes = manifest.get(key, [])
        if fixes:
            writer.writerow([])
            writer.writerow([title])
            writer.writerow(["event_id", "公司名", "旧值", "新值", "大额（旧 → 新）", "原文句子", "网址"])
        for fix in fixes:
            old, new = fix["old"], fix["new"]
            large = f"{'是' if old['large'] else '否'} → {'是' if new['large'] else '否'}"
            values = [fix["event_id"], fix["name"], _json(old), _json(new), large, fix["quote"], fix["url"]]
            writer.writerow(values)
            cells = [values[0], values[1], values[2] + " → " + values[3], *values[4:]]
            md.append("| " + " | ".join(str(v).replace("|", "\\|").replace("\n", "<br>") for v in cells) + " |")
    md.extend(("", "## 范围外疑似金额错误", ""))
    suspects = manifest.get("out_of_scope_amount_suspects", [])
    if suspects:
        writer.writerow([])
        writer.writerow(["范围外疑似金额错误"])
        writer.writerow(["event_id", "公司名", "库里金额", "原文句子", "网址"])
    for suspect in suspects:
        writer.writerow([suspect[k] for k in ("event_id", "name", "amount", "quote", "url")])
        md.append(f"- {suspect['event_id']} · {suspect['name']}：{suspect['amount']} · {suspect['quote']} · {suspect['url']}")
    csv_path.write_text(buffer.getvalue(), encoding="utf-8")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    return csv_path, md_path


def latest_manifest(cfg: Config, *, now: datetime) -> tuple[Path, dict]:
    """按文件内真实生成时间取最新清单；已执行优先于七天检查。"""
    files = []
    for path in cfg.outbox_dir.glob("backfill-verify-*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        files.append((_time(payload["generated_at"]), path.name, path, payload))
    if not files:
        raise ValueError("没有清单文件，请先跑清单模式")
    generated, _, path, payload = max(files)
    if payload.get("mode") != ("测试" if cfg.test_mode else "生产"):
        print(f"使用清单文件：{path}，生成于 {payload['generated_at']}")
        raise ValueError("清单文件模式不符，生产不能使用测试清单")
    if payload.get("applied_at"):
        return path, payload
    print(f"使用清单文件：{path}，生成于 {payload['generated_at']}")
    if (_time(now).date() - generated.date()).days > 7:
        raise ValueError("清单文件已超过 7 天，请重跑清单模式")
    return path, payload


def _write_verification(conn, candidate, manifest, now):
    cid, generated = candidate["company_id"], manifest["generated_at"]
    run_at, search_date = _stamp(now), _time(generated).date().isoformat()
    searches = sorted((s for s in manifest["searches"] if s["company_id"] == cid), key=lambda s: s["seq"])
    ordinary = [s for s in searches if s.get("kind") != "E"]
    conclusion = ordinary[-1].get("conclusion", {}) if ordinary else {}
    evidence = {f["field"]: f for s in ordinary for f in s.get("fields", [])}
    for name, field in conclusion.items():
        hit = evidence.get(name, {})
        verified_at = field.get("verified_at") or ordinary[-1]["requested_at"]
        urls = [hit["url"]] if hit.get("url") else field.get("urls", [])
        quotes = [hit["quote"]] if hit.get("quote") else field.get("quotes", [])
        tiers = [hit["tier"]] if hit.get("tier") else field.get("tiers", [])
        conn.execute("INSERT OR IGNORE INTO verify_log(company_id,source,run_date,run_started_at,search_date,field,result,reason,"
                     "urls_json,tiers_json,known_fact_hit,known_fact_json,quotes_json,verified_at,value_json,target_event_id,source_ids_json) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, "补充命令", run_at[:10], run_at, search_date, name, field["result"], field.get("reason", ""),
                      _json(urls), _json(tiers), bool(field.get("known_fact_hit")),
                      _json(field.get("known_facts", [])), _json(quotes), verified_at,
                      _json(hit.get("new_value", field.get("value"))), candidate.get("event_id"), _json(field.get("source_ids", []))))
    calls = [m for s in ordinary for m in s.get("model_calls", [])]
    for search in searches:
        last = bool(ordinary) and search is ordinary[-1]
        conn.execute("INSERT OR IGNORE INTO verify_search(company_id,run_date,run_started_at,search_date,requested_at,query,status,"
                     "result_count,billed,cost,model_calls,model_cost,source,results_json,purpose) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (cid, run_at[:10], run_at, search_date, search["requested_at"], search["query"], str(search.get("status", "")),
                      search.get("result_count", len(search.get("results", []))), bool(search.get("billed", False)),
                      search.get("cost", 0), len(calls) if last else 0,
                      sum(float(m.get("cost", 0)) for m in calls) if last else 0,
                      "补充命令", _json(search.get("results", [])), "官网" if search.get("kind") == "E" else "查证"))


def _record_path(cfg, day, real_now):
    suffix = f"-{real_now.astimezone(BJ):%H%M%S}" if cfg.test_mode else ""
    path = cfg.outbox_dir / f"{day}{suffix}-backfill.json"
    number = 2
    while path.exists():
        path = cfg.outbox_dir / f"{day}{suffix}-backfill-{number}.json"
        number += 1
    return path


def _cost(manifest):
    try:
        entries = [*manifest["searches"], *(m for s in manifest["searches"] for m in s.get("model_calls", [])),
                   *(m for r in manifest["rejudge"] for m in r.get("summary_model_calls", []))]
        costs = [Decimal(str(s.get("cost", 0))) for s in entries]
        if any(not cost.is_finite() or cost < 0 for cost in costs):
            raise ValueError
        return sum(costs, Decimal(0))
    except (ValueError, InvalidOperation):
        raise ValueError("清单文件费用无效，请重跑清单模式") from None


def _skip_candidates(conn, manifest):
    skipped, messages = set(), []
    for c in manifest["candidates"]:
        if c["rank"] is None:
            continue
        eid, cid = c.get("event_id"), c["company_id"]
        event = conn.execute("SELECT push_date FROM funding_event WHERE id=? AND company_id=?", (eid, cid)).fetchone()
        if not event:
            raise ValueError("清单内事件已变化，请重跑清单模式")
        reason = ""
        if event[0] and event[0] != c.get("push_date"):
            reason = f"清单生成后已推送，{event[0]}"
        else:
            newer = [r[0] for r in conn.execute("SELECT COALESCE(first_ingested_at,first_seen) FROM funding_event WHERE company_id=? AND id<>?", (cid, eid))
                     if _time(r[0]) > _time(manifest["generated_at"])]
            if newer:
                reason = f"清单生成后有新事件，{max(newer, key=_time)}"
        if reason:
            skipped.add(cid)
            messages.append(f"跳过：{c['name']}（{reason}）")
    if messages:
        messages.append(f"共跳过 {len(skipped)} 家")
    return skipped, messages


def _write_amount_fix(conn, fix, manifest, now):
    eid, cid, new = fix["event_id"], fix["company_id"], fix["new"]
    event = conn.execute("SELECT * FROM funding_event WHERE id=? AND company_id=?", (eid, cid)).fetchone()
    if not event:
        raise ValueError("清单内金额事件已变化，请重跑清单模式")
    required = {"currency", "orig_amount", "usd_amount", "rate_date", "amount_status", "large"}
    if set(new) != required or not isinstance(new["large"], bool):
        raise ValueError("清单文件金额字段不完整，请重跑清单模式")
    if new["amount_status"] == "金额待确认" and (new["large"] or any(new[k] is not None for k in ("currency", "orig_amount", "usd_amount", "rate_date"))):
        raise ValueError("清单文件金额待确认值无效，请重跑清单模式")
    marks = [m for m in json.loads(event["marks_json"]) if m not in {"大额", "金额待核"}]
    if new["large"]:
        marks.append("大额")
    if new["amount_status"] == "金额待核":
        marks.append("金额待核")
    marks = [m for m in scan.MARK_ORDER if m in marks]
    # 不重新解析、换算或判大额，逐字段照抄清单。
    conn.execute("UPDATE funding_event SET amount_currency=?,amount_original=?,amount_usd=?,fx_date=?,amount_status=?,marks_json=?,"
                 "amount_text=CASE WHEN ?='金额待确认' THEN NULL ELSE amount_text END WHERE id=?",
                 (new["currency"], new["orig_amount"], new["usd_amount"], new["rate_date"], new["amount_status"], _json(marks), new["amount_status"], eid))
    stamp = manifest["generated_at"]
    result = "金额待确认" if new["amount_status"] == "金额待确认" else "已更正"
    conn.execute("INSERT OR IGNORE INTO verify_log(company_id,source,run_date,run_started_at,search_date,field,result,reason,quotes_json,urls_json,verified_at,event_id,old_new_json) "
                 "VALUES (?,?,?,?,?,'金额',?,'金额更正',?,?,?,?,?)",
                 (cid, "补充命令", _stamp(now)[:10], _stamp(now), _time(manifest["generated_at"]).date().isoformat(),
                  result,
                  _json([fix["quote"]]), _json([fix["url"]] if fix["url"] else []), stamp, eid,
                  _json({"old": fix["old"], "new": new})))


def _skip_rows(conn, manifest, skipped):
    fixes, rejudge, messages = set(), set(), []
    candidates = {c["company_id"] for c in manifest["candidates"] if c["rank"] is not None}
    # 所有比较都先于第一次写库；整家跳过者不重复列行。
    for key in ("amount_fix_candidate", "amount_fix_noncandidate", "rejudge"):
        for row in manifest.get(key, []):
            cid = row["company_id"]
            if cid in skipped:
                continue
            if key == "rejudge":
                current = conn.execute("SELECT country,status FROM company WHERE id=?", (cid,)).fetchone()
                values = dict(current) if current else None
            else:
                current = conn.execute("SELECT * FROM funding_event WHERE id=? AND company_id=?", (row["event_id"], cid)).fetchone()
                values = _amount_values(current) if current else None
            if values != row["old"]:
                label = "公司" if key == "rejudge" else row["event_id"]
                messages.append(f"跳过：{row['name']}，{label}，清单生成后库里已变化")
                (rejudge if key == "rejudge" else fixes).add(cid if key == "rejudge" else row["event_id"])
    if messages:
        messages.append(f"跳过 {len(messages)} 条")
    skipped.update(rejudge & candidates)
    return fixes, rejudge, messages


def _copy_amount_snapshot(conn, event):
    keys = ("amount_text", "amount_status", *_AMOUNTS)
    conn.execute("UPDATE funding_event SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
                 (*(event.get(k) for k in keys), event["id"]))
    for source in event.get("event_sources", []):
        row = conn.execute("SELECT extraction_json FROM event_source WHERE event_id=? AND raw_item_id=?",
                           (event["id"], source["raw_item_id"])).fetchone()
        if row:
            current, saved = json.loads(row[0]), json.loads(source["extraction_json"])
            current.update({k: saved[k] for k in ("amount_text", "_amount_fx", "_amount_status") if k in saved})
            conn.execute("UPDATE event_source SET extraction_json=? WHERE event_id=? AND raw_item_id=?",
                         (_json(current), event["id"], source["raw_item_id"]))
    conn.execute("DELETE FROM meta WHERE key=?", (f"fx:event:{event['id']}",))
    if event.get("fx_snapshot") is not None:
        conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (f"fx:event:{event['id']}", _json(event["fx_snapshot"])))


class BalanceClient:
    """生产余额只读客户端；测试通过注入替换，不触发真实请求。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def balance(self):
        """读取 CNY 总余额，不记录请求头或凭证。"""
        from urllib.request import Request, urlopen
        from .config import load_api_key
        key = load_api_key(self.cfg)
        if not key:
            raise ValueError("余额查询失败：缺少凭证")
        request = Request("https://api.deepseek.com/user/balance", headers={"Authorization": "Bearer " + key}, method="GET")
        try:
            with urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            raise ValueError("余额查询失败") from None


def _balance(client):
    response = client.balance() if hasattr(client, "balance") else client()
    if not isinstance(response, dict) or response.get("is_available") is False:
        raise ValueError("余额查询失败")
    try:
        amounts = [Decimal(str(r["total_balance"])) for r in response.get("balance_infos", []) if r.get("currency") == "CNY"]
    except (KeyError, TypeError, AttributeError, InvalidOperation):
        raise ValueError("余额查询失败") from None
    if not amounts or any(not n.is_finite() for n in amounts):
        raise ValueError("余额查询失败：没有人民币余额")
    return sum(amounts)


def apply_manifest(conn: sqlite3.Connection, cfg: Config, manifest: dict, verify_file: Path, *,
                   now: datetime, real_now: datetime, balance_client=None) -> Path | None:
    """以开始前的快照判跳过；事务照抄清单，成功后原子记 applied_at。"""
    if verify_file.exists():
        saved = json.loads(verify_file.read_text(encoding="utf-8"))
        if saved.get("generated_at") == manifest["generated_at"] and saved.get("applied_at"):
            manifest = saved
    if manifest.get("mode") != ("测试" if cfg.test_mode else "生产"):
        raise ValueError("清单文件模式不符，生产不能使用测试清单")
    if manifest.get("applied_at"):
        print("该清单已执行，没有新的补充")
        return None
    identity = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    receipt_key = "backfill:applied:" + identity
    receipt = conn.execute("SELECT value FROM meta WHERE key=?", (receipt_key,)).fetchone()
    if receipt:
        # 业务已提交、清单最后一次改名失败时，仅修复文件，不再写业务数据。
        _finish_homepages(conn, cfg, identity)
        outbox.write_atomic(verify_file, {**manifest, "applied_at": receipt[0]})
        verify_file.with_name("." + verify_file.name + ".applied.tmp").unlink(missing_ok=True)
        print("该清单已执行，没有新的补充")
        return None
    candidates = _working_candidates(manifest)
    ranks = [c["rank"] for c in candidates if c["rank"] is not None]
    if (len(set(ranks)) != len(ranks) or any(not isinstance(rank, int) or rank < 1 for rank in ranks)
            or any(c["in_supplement"] and (c["rank"] is None or c["rank"] > 10) for c in candidates)):
        raise ValueError("清单文件排序或补充名额无效，请重跑清单模式")
    client = (balance_client or BalanceClient(cfg)) if not cfg.test_mode else None
    estimated = _cost(manifest)
    before = _balance(client) if client else None
    path, records, processed = None, [], set()
    staged_manifest = verify_file.with_name("." + verify_file.name + ".applied.tmp")
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            skipped, messages = _skip_candidates(conn, manifest)
            skipped_fixes, skipped_rejudge, row_messages = _skip_rows(conn, manifest, skipped)
            messages += row_messages
            if messages:
                cfg.log_dir.mkdir(parents=True, exist_ok=True)
                with (cfg.log_dir / f"{_time(now).date()}-backfill.log").open("a", encoding="utf-8") as log:
                    log.write("\n".join(messages) + "\n")
                print("\n".join(messages))
            for c in candidates:
                cid, result = c["company_id"], c["rejudge"]
                company = conn.execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone()
                if company is None:
                    raise ValueError("清单内公司已不存在，请重跑清单模式")
                if cid in skipped or company["backfill"]:
                    continue
                processed.add(cid)
                if cid not in skipped_rejudge:
                    rejudge = next(r for r in manifest["rejudge"] if r["company_id"] == cid)
                    result = {**result, **rejudge["new"]}
                    conn.execute("UPDATE company SET " + ",".join(f"{k}=?" for k in _COMPANY_FIELDS) + " WHERE id=?",
                                 (*(result.get(k) for k in _COMPANY_FIELDS), cid))
                    for event in result["events"]:
                        current = conn.execute("SELECT company_id FROM funding_event WHERE id=?", (event["id"],)).fetchone()
                        if not current or current[0] != cid:
                            raise ValueError("清单内事件已变化，请重跑清单模式")
                        # 金额不在重判快照中批量覆盖，只由未跳过的金额更正行写入。
                        keys = ("round_norm", "round_text", "filter_reasons_json")
                        conn.execute("UPDATE funding_event SET " + ",".join(f"{k}=?" for k in keys) + " WHERE id=?",
                                     (*(event.get(k) for k in keys), event["id"]))
                    _write_verification(conn, c, manifest, now)
                    conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES (?,?)", (f"backfill:rejudged:{cid}", manifest["generated_at"]))
                if c["in_supplement"] and c.get("summary") is not None:
                    conn.execute("UPDATE company SET summary=? WHERE id=?", (c["summary"], cid))
                ids = result["supplement_event_ids"]
                if not ids:
                    continue
                conn.execute("UPDATE company SET backfill='补充未推送',backfill_rank=?,backfill_push_date=NULL WHERE id=?", (c["rank"], cid))
                conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                             (f"backfill:rank:{cid}", _json({"rank": c["rank"], "event_ids": ids, "in_supplement": c["in_supplement"]})))
                for eid in ids:
                    conn.execute("UPDATE funding_event SET original_first_ingested_at=COALESCE(original_first_ingested_at,first_ingested_at,first_seen),"
                                 "first_ingested_at=CASE WHEN ? THEN ? ELSE first_ingested_at END,baseline_unpushed=0,"
                                 "push_date=NULL,last_pushed_date=NULL,pending=0 WHERE id=?", (c["in_supplement"], _stamp(now), eid))
                event = next(e for e in result["events"] if e["id"] == ids[0])
                records.append({"company_id": cid, "name": c["name"], "country": result["country"],
                    "round": wording.display_text(event["round_norm"]), "amount_text": event["amount_label"],
                    "sources": [s["url"] for s in c["sources"]], "rank": c["rank"],
                    "in_supplement": c["in_supplement"], "searches": len(c["searches"])})
            for fix in [*manifest.get("amount_fix_candidate", []), *manifest.get("amount_fix_noncandidate", [])]:
                if fix["company_id"] in processed and fix["event_id"] not in skipped_fixes:
                    saved = next(e for c in candidates if c["company_id"] == fix["company_id"]
                                 for e in c["rejudge"]["events"] if e["id"] == fix["event_id"])
                    _copy_amount_snapshot(conn, saved)
                    _write_amount_fix(conn, fix, manifest, now)
            conn.execute("INSERT OR IGNORE INTO meta(key,value) VALUES ('backfill_applied',?)", (_stamp(now),))
            if records:
                records.sort(key=lambda c: c["rank"])
                path = _record_path(cfg, _time(now).date(), real_now)
                outbox.write_atomic(path, {"date": str(_time(now).date()), "kind": "backfill", "push": False,
                    "generated_at": _stamp(now), "verify_file": str(verify_file),
                    "count": sum(c["in_supplement"] for c in records), "companies": records})
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", (receipt_key, _stamp(real_now)))
            homepage_intents = []
            from .homepage import save_check
            for candidate in candidates:
                check = candidate.get("homepage_check")
                if candidate["company_id"] in processed and check and check.get("result") == "核到":
                    save_check(conn, candidate["company_id"], check, now=now, source="补充命令")
                    homepage_intents.append({"company_id": candidate["company_id"], "name": candidate["name"], "check": check})
            conn.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("backfill:homepage:" + identity, _json(homepage_intents)))
            # 先在事务内完成可能失败的序列化、落盘，提交后只做同目录原子改名。
            outbox.write_atomic(staged_manifest, {**manifest, "applied_at": _stamp(real_now)})
    except BaseException:
        if path:
            path.unlink(missing_ok=True)
        staged_manifest.unlink(missing_ok=True)
        raise
    _finish_homepages(conn, cfg, identity)
    staged_manifest.replace(verify_file)
    if client:
        # 费用对账在业务事务提交之后；超额只报告，保留已提交的业务变更。
        delta = before - _balance(client)
        reason = f"余额差 {delta} 元，清单估算 {estimated} 元"
        first_company = conn.execute("SELECT id FROM company ORDER BY id LIMIT 1").fetchone()
        fee_company = candidates[0]["company_id"] if candidates else first_company[0] if first_company else None
        if fee_company is not None:
            with conn:
                verified_at = real_now.strftime("%Y-%m-%d %H:%M:%S.%f")
                while conn.execute("SELECT 1 FROM verify_log WHERE company_id=? AND field='费用' AND verified_at=?", (fee_company, verified_at)).fetchone():
                    verified_at = (_time(verified_at) + timedelta(microseconds=1)).strftime("%Y-%m-%d %H:%M:%S.%f")
                conn.execute("INSERT INTO verify_log(company_id,source,run_date,run_started_at,search_date,field,result,reason,verified_at) "
                             "VALUES (?,'补充命令',?,?,?,'费用','已确认',?,?)",
                             (fee_company, _stamp(now)[:10], _stamp(now), manifest["generated_at"][:10], reason, verified_at))
        if delta > estimated * 2:
            raise ValueError(reason + "；超过估算两倍，业务写库已提交")
    return path


def _finish_homepages(conn, cfg, identity):
    from .homepage import append_verified
    from .render import read_link_records
    from .config import effective_links_file
    row = conn.execute("SELECT value FROM meta WHERE key=?", ("backfill:homepage:" + identity,)).fetchone()
    for intent in json.loads(row[0]) if row else []:
        written = append_verified(cfg, intent["name"], intent["check"])
        own_record = any(r.get("type") == "公司" and r.get("checker") == "脚本" and r.get("verified") is True
            and str(r.get("company", "")).strip().casefold() == intent["name"].strip().casefold()
            and r.get("verified_at") == intent["check"]["checked_at"][:16]
            and r.get("url") == intent["check"]["final_url"] for r in read_link_records(effective_links_file(cfg)))
        if written or own_record:
            with conn:
                conn.execute("UPDATE homepage_check SET links_written=1 WHERE company_id=? AND checked_at=? AND source='补充命令'",
                             (intent["company_id"], intent["check"]["checked_at"]))


def collect_supplement(conn: sqlite3.Connection, cfg: Config, day: date, *, event_ids=None):
    """按清单排名组装最多十家补充，不占本周的排序或名额。"""
    params = []
    where = ("c.backfill IN ('首期漏掉','补充未推送') AND c.backfill_rank BETWEEN 1 AND 10 "
             "AND e.amount_status<>'金额待确认' AND e.original_first_ingested_at IS NOT NULL "
             "AND NOT EXISTS (SELECT 1 FROM meta WHERE key='backfill:normal:'||e.id)")
    if event_ids is None:
        where += " AND c.backfill_push_date IS NULL AND e.push_date IS NULL"
    else:
        ids = list(event_ids)
        where += " AND e.id IN (" + ",".join("?" for _ in ids) + ")"
        params = ids
    rows = conn.execute("SELECT e.id,e.company_id FROM funding_event e JOIN company c ON c.id=e.company_id WHERE "
                        + where + " AND c.status IN ('已纳入','轮次待核') AND e.baseline_unpushed=0 AND e.filter_reasons_json='[]'", params).fetchall()
    ranks, primary_ids = {}, []
    for row in rows:
        saved = conn.execute("SELECT value FROM meta WHERE key=?", (f"backfill:rank:{row['company_id']}",)).fetchone()
        record = json.loads(saved[0]) if saved else {}
        ranks[row["company_id"]] = record.get("rank", 999)
        if not saved or row["id"] == record["event_ids"][0]:
            primary_ids.append(row["id"])
    listing = scan.collect_listing(conn, day, weekly=True, event_ids=primary_ids, include_backfill=True)
    cards = sorted(listing["cards"], key=lambda card: (ranks[card.company_id], card.name.casefold()))
    unique = {}
    for card in cards:
        unique.setdefault(card.company_id, card)
    cards = list(unique.values())[:10]
    ids = [r["id"] for card in cards for r in rows if r["company_id"] == card.company_id]
    overflow = conn.execute("SELECT COUNT(*) FROM company WHERE backfill='补充未推送' AND backfill_rank>10").fetchone()[0]
    section = PushSection("backfill", wording.BACKFILL_TITLE, wording.BACKFILL_HINT.format(count=len(cards)),
                          cards, preserve_order=True, overflow_count=overflow)
    return ([section] if cards else []), ids


def mark_supplement(conn: sqlite3.Connection, ids: list[int], day: date) -> None:
    """主文件成功后记录补充推送；调用方和 digest 一起提交。"""
    for eid in ids:
        conn.execute("UPDATE funding_event SET push_date=?,last_pushed_date=?,pending=0 WHERE id=?", (str(day), str(day), eid))
        conn.execute("UPDATE company SET backfill='首期漏掉',backfill_push_date=COALESCE(backfill_push_date,?) "
                     "WHERE id=(SELECT company_id FROM funding_event WHERE id=?)", (str(day), eid))


class _migrated_copy:
    """清单模式不写库：把只读库拷进临时文件，按当前代码迁移后只读这份拷贝（旧库未迁移也能出清单）。"""

    def __init__(self, source: sqlite3.Connection):
        self.source = source

    def __enter__(self) -> sqlite3.Connection:
        import tempfile
        self._dir = tempfile.TemporaryDirectory(prefix="vcradar-backfill-")
        path = Path(self._dir.name) / "copy.db"
        target = sqlite3.connect(str(path))
        try:
            self.source.backup(target)
        finally:
            target.close()
        self.conn = db.connect(path)
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()
        self._dir.cleanup()


def run_backfill(cfg: Config, args, clock, *, verifier=None, fetcher=None, summary_client=None, balance_client=None, homepage_checker=None) -> int:
    """持有运行锁执行清单或 apply；失败不生成正常推送文件。"""
    from .pipeline import _read_database, run_lock, _RunLog
    from .placenames import place_audit
    cfg = replace(cfg, test_mode=is_test_run(args, cfg))
    now, real_now = clock.now(), datetime.now(BJ)
    try:
        selected = latest_manifest(cfg, now=now) if args.apply else None
        if selected and selected[1].get("applied_at"):
            print("该清单已执行，没有新的补充")
            return 0
        with run_lock(cfg), place_audit(_RunLog(cfg.log_dir / f"{now.date()}-backfill.log").write):
            if selected:
                verify_file, manifest = selected
                if not cfg.db_path.is_file():
                    raise ValueError("清单文件对应的数据库不存在，请重跑清单模式")
                conn = db.connect(cfg.db_path)
                try:
                    path = apply_manifest(conn, cfg, manifest, verify_file, now=now, real_now=real_now, balance_client=balance_client)
                finally:
                    conn.close()
                print(path if path else "没有新的补充")
            else:
                rates = fx.rates_for_run(cfg, args, fetcher=fetcher, now=now, persist=False)
                with _read_database(cfg.db_path) as source, _migrated_copy(source) as conn:
                    if getattr(args, "no_verify", False):
                        verifier = default_verifier
                    if verifier is None:
                        from .verify import backfill_verifier
                        verifier = backfill_verifier(conn, cfg, clock,
                            fixtures=getattr(args, "search_fixtures", None), rates=rates)
                    if homepage_checker is None:
                        from .homepage import backfill_checker
                        homepage_checker = backfill_checker(conn, cfg, clock, fixtures=getattr(args, "search_fixtures", None))
                    manifest = create_manifest(conn, cfg, now=now, rates=rates, verifier=verifier,
                                               summary_client=summary_client, homepage_checker=homepage_checker)
                if not cfg.test_mode and (manifest["searches"] or _cost(manifest)):
                    cfg.outbox_dir.mkdir(parents=True, exist_ok=True)
                    with (cfg.outbox_dir / "backfill-spend.log").open("a", encoding="utf-8") as log:
                        count = len(manifest["searches"])
                        log.write(f"{real_now.date()}\t{count}\t{_cost(manifest)}\n")
                name = f"backfill-verify-{now.date()}" + (f"-{real_now:%H%M%S}" if cfg.test_mode else "") + ".json"
                path = cfg.outbox_dir / name
                outbox.write_atomic(path, manifest)
                if args.report_dir is not None:
                    write_reports(manifest, args.report_dir)
                print(" · ".join(f"{key}：{value}" for key, value in _summary(manifest).items()))
                print(path)
                if any(s.get("status") == "unavailable" for s in manifest["searches"]):
                    print("本次查证尚未接入：搜索客户端初始化失败")
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        # 业务错误仅输出本模块生成的固定文案，不打印外部服务异常正文。
        message = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(("没有清单", "清单文件", "清单内", "补充查证", "一家公司有多条", "余额")) else type(exc).__name__
        print(f"补充失败：{message}", file=sys.stderr)
        return 1
