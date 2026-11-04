"""阶段 1 回放：固定抽取结果经过同样的校验、过滤、去重和渲染。"""
from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime
import json
from pathlib import Path
import sys
import tempfile

from . import db, extract, fx, outbox, render, scan, placenames
from .config import BJ, Config, is_test_run
from .models import DigestData, HttpResponse, LabelDim, RawItem, SourceDef
from .pipeline import (Clock, ModelSession, _Budget, _FictionalData, _RunLog, _listing_for_issue,
                       _stamp, _stale_paths, cleanup_stale, model_client, preflight, rule_extract, run_lock, validate_delivery)
from .sources.registry import ALL_SOURCES
from .sources.yc_directory import batch_order, canonical_latest_batches


def _raw(data: dict) -> RawItem:
    values = {field.name: data[field.name] for field in fields(RawItem) if field.name in data}
    values.setdefault("published_at", None)
    if values["published_at"] is not None:
        values["published_at"] = _stamp(values["published_at"])
    return RawItem(**values)


def fixed_extraction(item: RawItem, data: dict):
    """固定结果也必须校验原句和金额，不能绕过标签待核规则。"""
    ext = rule_extract(item)
    cleaned, _ = extract.validate_model_output(item, data)
    for key in ("company_name", "website", "round_text", "amount_text", "leads", "follows",
                "hq_text", "one_liner"):
        if not getattr(ext, key) and cleaned.get(key):
            setattr(ext, key, cleaned[key])
            ext.quotes[key] = cleaned["quotes"][key]
    ext.hq_quote = ext.quotes.get("hq_text", ext.hq_quote)
    ext.is_fund_raise |= cleaned["is_fund_raise"]
    ext.is_debt_only |= cleaned["is_debt_only"]
    ext.summary = extract.sanitize_summary(item, cleaned["summary"])
    ext.ai, ext.social = LabelDim(**cleaned["ai"]), LabelDim(**cleaned["social"])
    ext.product_terms = extract.validate_product_terms(item, cleaned["product_terms"],
        company_name=ext.company_name, investors=[*ext.leads, *ext.follows])
    ext.summary, _, problems = placenames.check_text(ext.summary, company=ext.company_name or "",
        protected_names=placenames.protected_names({"name": ext.company_name, "product_terms": ext.product_terms,
                                                    "leads": ext.leads, "follows": ext.follows}))
    placenames.record_remaining(ext.company_name or "", problems)
    return ext


def run_replay(cfg: Config, args, clock) -> int:
    """回放 JSON 到临时库，仅向 outbox-test 交付结果。"""
    payload = json.loads(Path(args.replay).read_text(encoding="utf-8"))
    day = args.date or date.fromisoformat(payload["date"])
    replay_clock = clock if args.date else Clock(day, clock.now().timetz())
    cfg = replace(cfg, test_mode=is_test_run(args, cfg), base_url="" if args.no_links else cfg.base_url,
                  total_timeout_s=args.total_timeout if args.total_timeout is not None else cfg.total_timeout_s)
    if message := preflight(cfg, args, day):
        if _stale_paths(cfg):
            with run_lock(cfg):
                cleanup_stale(cfg)
        print(message, file=sys.stderr)
        return 3
    records = [(data, _raw(data)) for data in payload["items"]]
    sources = {source.id: source for source in ALL_SOURCES}
    for data, item in records:
        if item.source_id not in sources:
            sources[item.source_id] = SourceDef(item.source_id, data.get("source_name", item.source_id),
                                                "replay", item.url, "page", None, len(sources))
    batches = canonical_latest_batches(
        (str(item.structured["yc_batch"]) for _, item in records if item.structured.get("yc_batch")),
        as_of=day,
    )
    with run_lock(cfg), tempfile.TemporaryDirectory(prefix="vcradar-replay-") as directory, placenames.place_audit(
            _RunLog(cfg.log_dir / f"{day}.log").write):
        cleanup_stale(cfg)
        if args.reset_test_db:
            from .pipeline import reset_test_db
            reset_test_db(cfg)
        conn = db.connect(Path(directory) / "replay.db")
        session = None
        try:
            db.sync_sources(conn, list(sources.values()))
            # 回放只读测试汇率缓存；可在输入中提供手写的 ECB 响应，不访问网络。
            def rates_fetcher(url, **kwargs):
                spec = payload.get("fx_responses", {}).get(url)
                if spec is None:
                    return HttpResponse(url, None, error="conn:no fixture")
                return HttpResponse(url, spec.get("status", 200), body=spec.get("body", ""), error=spec.get("error"))
            rates = fx.rates_for_run(cfg, args, fetcher=rates_fetcher, now=replay_clock.now())
            fixed_rates = payload.get("固定汇率", payload.get("fixed_rates", payload.get("rates")))
            if fixed_rates is not None:
                rates.cache, _ = fx._normalise_cache(fixed_rates)
                rates.fixed = True
            rates.refresh()
            if args.live_model:
                # 回放不受历史日期的 08:50 限制，只使用本次总超时。
                budget_cfg = replace(cfg, run_deadline="23:59:59")
                budget = _Budget(budget_cfg, replay_clock)
                log = _RunLog(cfg.log_dir / f"{day}.log")
                session = ModelSession(model_client(cfg, None), cfg, replay_clock, budget, log)
            for data, item in records:
                stamp = replay_clock.now()
                item_id, new = scan.upsert_raw_item(conn, item, stamp, False)
                if not new:
                    continue
                ext = fixed_extraction(item, data.get("fixed_extraction", {}))
                if session is not None:
                    ext = rule_extract(item)
                    country, _ = scan.judge_country(item, ext)
                    normalized = scan.normalize_round(ext.round_text, is_fund=ext.is_fund_raise, is_debt_only=ext.is_debt_only)
                    if not scan.filter_reasons(item, ext, country, normalized, batches):
                        ext = extract.model_extract(item, ext, session, "summary" if ext.complete_by_rules else "full",
                                                    budget_s=budget.remaining())
                scan.ingest(conn, item_id, item, ext, now=stamp, is_baseline=False,
                            is_backfill=bool(item.structured.get("backfill")), latest_yc_batches=batches,
                            model=ext.model_used, first_ingested_at=data.get("first_ingested_at"), fx_rates=rates)
            from .verify import run_verification
            run_verification(conn, cfg, replay_clock,
                company_ids=[r[0] for r in conn.execute("SELECT id FROM company")],
                fixtures=getattr(args, "search_fixtures", None), no_verify=getattr(args, "no_verify", False), rates=rates)
            listing = _listing_for_issue(conn, day)
            digest = DigestData(day, None, replay_clock.now(), "有新增" if listing["event_ids"] else "没有新增",
                                cards=listing["cards"], round_pending=listing["round_pending"],
                                filtered=listing["filtered"], yc_new_batch=listing["yc_new_batch"], base_url=cfg.base_url,
                                review_site_passed=cfg.review_site_passed and not args.no_links)
            text = render.render_push(digest, filtered_count=listing["filtered_total"], first_seen=listing["first_seen"], links_file=cfg.links_file)
            validate_delivery(conn, text, test_mode=cfg.test_mode)
            path = outbox.write_main(cfg.outbox_dir, day, push=True, alarm=False, generated_at=digest.window_end,
                                     text=text, test=True, real_now=datetime.now(BJ))
            outbox.write_atomic(cfg.state_dir / "test" / "status.json", {"date": str(day), "fx": dict(rates.status)})
            print(text)
            print(path)
            return 0
        except _FictionalData:
            print("回放失败：交付数据包含禁用虚构名", file=sys.stderr)
            return 1
        finally:
            if session is not None:
                session.close()
            conn.close()
