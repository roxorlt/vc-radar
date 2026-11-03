"""T：只用手写事实和固定返回覆盖PRD 13-#40，不访问外部服务。"""
from dataclasses import replace
from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from vcradar import backfill, db, extract, fx, scan, site, verify, wording
from vcradar.cli import parse_args
from vcradar.config import BJ, Config
from vcradar.deep_fetch import Budget
from vcradar.llm import LLMResult
from vcradar.models import Extracted, LabelDim, RawItem, SourceDef
from vcradar.replay import fixed_extraction


class Clock:
    def __init__(self, day="2026-11-03"):
        self.value = datetime.fromisoformat(day + "T08:40:00").replace(tzinfo=BJ)
        self.simulated = True

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class Services:
    def __init__(self, rows=(), findings=(), *, responses=None, models=None, body="", clock=None):
        self.rows, self.findings = list(rows), list(findings)
        self.responses, self.models = responses, models
        self.queries, self.model_requests, self.page_requests = [], [], []
        self.body, self.clock = body, clock

    def search(self, query, timeout_s):
        i = len(self.queries)
        self.queries.append(query)
        response = self.responses[min(i, len(self.responses) - 1)] if self.responses else {"status": 200, "results": self.rows}
        if self.clock and response.get("delay_s"):
            # 到点的调用至少耗掉给它的全部时间；timeout_s 已扣掉真实流逝的几毫秒，只推进它会差一点点到不了整步上限（偶发多搜一次）。
            delay = response["delay_s"]
            self.clock.advance(delay if delay >= timeout_s else min(delay, timeout_s))
        return response

    def model(self, context, round_no, system, user, timeout_s):
        self.model_requests.append(json.loads(user))
        spec = self.models[min(len(self.model_requests) - 1, len(self.models) - 1)] if self.models else {"fields": self.findings}
        return LLMResult(False, error="timeout") if spec.get("error") else LLMResult(True, content=json.dumps(spec))

    def page(self, url, timeout_s):
        self.page_requests.append(url)
        return self.body


@pytest.fixture
def env(tmp_path):
    cfg = Config(state_dir=tmp_path, test_mode=True, links_file=tmp_path / "links.json")
    conn = db.connect(cfg.db_path)
    clock = Clock()
    yield conn, cfg, clock
    conn.close()


def add_company(env, name="Aster", *, country=None, round_text="Seed", amount="$10M",
                terms=("trust administration",), leads=("Greycroft",), follows=(), website=None,
                baseline=False, filtered=False, label=False, source="techcrunch.com", suffix="", when=None):
    conn, cfg, clock = env
    now = when or clock.now()
    sid = source + suffix
    db.sync_sources(conn, [SourceDef(sid, "TechCrunch" if source == "techcrunch.com" else source, "synthetic", "https://" + source, "page", None, 1)])
    text = f"{name} builds trust administration for small firms."
    if country:
        text += f" {name} is headquartered in {country}."
    if amount:
        text += f" The business raised {amount}."
    text += " " + " ".join(leads) + " " + " ".join(follows)
    item = RawItem(sid, name + suffix, f"https://{source}/{name.lower()}{suffix}", f"{name} announces synthetic financing {suffix}", now,
                   excerpt=text, body=text)
    ext = Extracted(company_name=name, website=website, round_text="Series B" if filtered else round_text,
                    amount_text=amount, hq_text=country, hq_quote=f"{name} is headquartered in {country}." if country else "",
                    leads=list(leads), follows=list(follows), product_terms=list(terms),
                    ai=LabelDim("拿不准" if label else "否"), social=LabelDim("否"), summary="产品为小型团队提供工作工具。")
    rid, _ = scan.upsert_raw_item(conn, item, now, baseline)
    cid = scan.ingest(conn, rid, item, ext, now=now, is_baseline=baseline, is_backfill=False,
                      latest_yc_batches=[], model="fixture")
    # ingest返回事件描述，取稳定公司键用于测试。
    cid = conn.execute("SELECT company_id FROM event_source s JOIN funding_event e ON e.id=s.event_id WHERE raw_item_id=?", (rid,)).fetchone()[0]
    return cid


def rows_for(name="Aster", city="New York, USA", *, official=False, amount=None, round_text=None):
    a = f"{name} supplies trust administration tools; its headquarters are in {city}."
    # 使用headquartered in句式，两个来源的正文刻意不同，不能误当转载。
    a = f"{name} is headquartered in {city}. Its trust administration toolkit supports regional practices."
    b = f"For operators using trust administration, {name} provides a dashboard and is based in {city}."
    if amount:
        a += f" A newly completed {round_text or 'Seed'} round raised {amount}."
        b += f" Funding of {amount} arrived in the {round_text or 'Seed'} financing."
    elif round_text:
        a += f" It completed a {round_text} financing."
        b += f" Investors supported the {round_text} round."
    return [
        {"url": "https://businesswire.com/synthetic-a" if official else "https://finsmes.com/synthetic-a", "title": f"{name} releases product information", "snippet": a},
        {"url": "https://venturebeat.com/synthetic-b", "title": f"A discussion of {name} and workflow tools", "snippet": b},
    ]


def findings_for(rows, *, country="美国", city="New York", amount=None, round_text=None):
    result = []
    for row in rows:
        for field, value in (("国家", country), ("城市", city), ("金额", amount), ("轮次", round_text)):
            if value:
                result.append({"field": field, "value": value, "quote": row["snippet"], "url": row["url"]})
    return result


def run(env, cid, service, **kwargs):
    conn, cfg, clock = env
    return verify.run_verification(conn, cfg, clock, company_ids=[cid], services=service, **kwargs)


def company(env, cid):
    return dict(env[0].execute("SELECT * FROM company WHERE id=?", (cid,)).fetchone())


def event(env, cid):
    return dict(env[0].execute("SELECT * FROM funding_event WHERE company_id=? ORDER BY id DESC", (cid,)).fetchone())


def logs(env, cid):
    return {r["field"]: dict(r) for r in env[0].execute("SELECT * FROM verify_log WHERE company_id=? ORDER BY id", (cid,))}


# #40① 触发和范围，包含非推送日入库、未披露和undisclosed。
@pytest.mark.parametrize("changes,expected", [
    ({}, {"国家", "城市"}), ({"country": "Colombia"}, set()),
    ({"country": "USA", "round_text": None}, {"轮次"}),
    ({"country": "USA", "amount": None}, {"金额"}),
    ({"country": "USA", "amount": "undisclosed"}, {"金额"}),
    ({"country": "USA", "amount": "10 million"}, {"币种"}),
    ({"country": "USA", "label": True}, {"标签"}),
    ({"country": "USA", "website": None}, set()),
    ({"baseline": True}, set()), ({"filtered": True}, set()),
])
def test_scope_and_triggers(env, changes, expected):
    cid = add_company(env, **changes)
    service = Services()
    run(env, cid, service)
    assert set(logs(env, cid)) == expected
    assert bool(service.queries) == bool(expected)
    assert all(len(q) <= 128 and "trust administration" in q for q in service.queries)


def test_amount_dispute_triggers_and_single_search_group(env):
    cid = add_company(env, country="USA")
    env[0].execute("UPDATE funding_event SET amount_status='金额待核'")
    service = Services()
    run(env, cid, service)
    assert set(logs(env, cid)) == {"金额"}
    assert len(service.queries) <= 3


# #40②③ 定向搜索、三个请求、第二轮换域名、无身份和跟投方。
def test_multi_field_queries_and_excluded_domains(env):
    cid = add_company(env, round_text=None, amount=None, label=True)
    rows = rows_for()
    service = Services(rows, [])
    run(env, cid, service)
    assert len(service.queries) == 3
    assert len(service.model_requests) == 2
    assert len(set(service.queries)) == 3
    assert "-site:finsmes.com" in service.queries[1]
    assert "-site:venturebeat.com" in service.queries[2]
    assert set(logs(env, cid)) == {"国家", "城市", "轮次", "金额", "标签"}


@pytest.mark.parametrize("investors", [("Y Combinator",), ("YC", "General Catalyst", "others", "未披露")])
def test_no_distinguishing_fact_does_not_search_or_show_zero(env, investors):
    cid = add_company(env, terms=(), leads=investors)
    service = Services()
    run(env, cid, service)
    assert not service.queries
    assert logs(env, cid)["国家"]["reason"] == verify.NO_IDENTITY
    stats, details = site._verification(env[0], env[2].now())
    assert stats["companies"] == 1 and stats["searches"] == 0
    assert all("已查过" not in v["text"] for v in details[cid])


def test_follow_investor_and_website_fallback(env):
    cid = add_company(env, terms=(), leads=("Y Combinator",), follows=("Rare Ventures",))
    service = Services()
    run(env, cid, service)
    assert all("Rare Ventures" in q for q in service.queries)
    other = add_company(env, "Birch", terms=(), leads=(), website="https://app.birch.co.uk")
    service = Services()
    run(env, other, service)
    assert all("birch.co.uk" in q for q in service.queries)


@pytest.mark.parametrize("name,wrong", [("Rocker", "Sweden"), ("Sava", "London")])
def test_homonyms_are_rejected(env, name, wrong):
    cid = add_company(env, name)
    rows = [{"url": "https://finsmes.com/bank", "title": name + " banking product", "snippet": f"{name} is headquartered in {wrong}. It produces blood glucose sensors and banking tools."},
            {"url": "https://venturebeat.com/bank", "title": "Other company " + name, "snippet": f"{name} is based in {wrong} and offers mobile banking."}]
    service = Services(rows, findings_for(rows, country="英国" if wrong == "London" else "瑞典", city=wrong))
    run(env, cid, service)
    assert company(env, cid)["country"] == scan.COUNTRY_PENDING
    assert all(not r["known_fact_hit"] for r in logs(env, cid).values())
    assert not service.model_requests


# #40④ 程序同时核对值、原句和URL，不能用模型记忆。
@pytest.mark.parametrize("bad", ["url", "quote", "value", "case"])
def test_hallucinated_evidence_is_deleted(env, bad):
    cid = add_company(env)
    rows = rows_for()
    findings = findings_for(rows)
    for f in findings:
        f[{"url": "url", "quote": "quote", "value": "value", "case": "quote"}[bad]] = {
            "url": "https://not-in-results.example/x", "quote": "A fabricated location", "value": "法国", "case": f["quote"].upper()}[bad]
    service = Services(rows, findings)
    run(env, cid, service)
    assert company(env, cid)["country"] == scan.COUNTRY_PENDING
    assert logs(env, cid)["国家"]["result"] == verify.UNCONFIRMED


# #40⑤⑥ 独立媒体、聚合站、信源算一个、各类转载和确认后的依据。
def test_two_independent_sources_confirm_country_and_city(env):
    cid = add_company(env)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == "美国"
    assert company(env, cid)["city"] == "New York"
    assert logs(env, cid)["国家"]["result"] == verify.CONFIRMED
    context = verify.company_context(env[0], cid, env[1])
    assert len(context["records"]) == 1  # 查证不冒充信源。
    listing = scan.collect_listing(env[0], env[2].now().date(), weekly=True)
    assert len(listing["cards"][0].sources) == 1
    assert listing["cards"][0].sources[0].url.startswith("https://techcrunch.com/")
    stats, details = site._verification(env[0], env[2].now())
    assert details[cid][0]["text"] == "入库时已查证"
    assert json.loads(logs(env, cid)["国家"]["urls_json"]) == [r["url"] for r in rows]


@pytest.mark.parametrize("mode", ["aggregators", "blog", "one", "same_domain", "same_title", "dateline", "crosslink", "similar", "missing_metadata"])
def test_non_independent_sources_never_confirm(env, mode):
    cid = add_company(env)
    rows = rows_for()
    if mode in {"aggregators", "blog"}:
        rows[0]["url"] = "https://parsers.vc/aster"
        rows[1]["url"] = "https://tracxn.com/aster" if mode == "aggregators" else "https://person.medium.com/aster"
    elif mode == "one":
        rows = rows[:1]
    elif mode == "same_domain":
        rows[1]["url"] = "https://news.finsmes.com/second"
    elif mode == "same_title":
        rows[1]["title"] = rows[0]["title"].upper() + "!"
    elif mode == "dateline":
        for row in rows:
            row["snippet"] = "NEW YORK--(BUSINESS WIRE)--" + row["snippet"]
    elif mode == "crosslink":
        rows[1]["snippet"] += " " + rows[0]["url"]
    elif mode == "similar":
        rows[1]["snippet"] = rows[0]["snippet"] + " New."
    else:
        rows[1]["title"] = ""
    run(env, cid, Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == scan.COUNTRY_PENDING
    assert logs(env, cid)["国家"]["result"] == verify.UNCONFIRMED


def test_source_plus_search_is_two_and_city_conflict_only_blocks_city(env):
    cid = add_company(env, "Sava", country="New York, USA")
    add_company(env, "Sava", country="San Francisco, USA", source="strictlyvc.com", suffix="-two")
    assert company(env, cid)["country"] == scan.COUNTRY_PENDING
    rows = rows_for("Sava")
    run(env, cid, Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == "美国"
    assert company(env, cid)["city"] == wording.SITE_TEXT["city_pending"]
    assert logs(env, cid)["城市"]["result"] == verify.UNCONFIRMED
    assert "来源说法不一" in logs(env, cid)["城市"]["reason"]


@pytest.mark.parametrize("official", [True, False])
def test_funding_requires_official_and_independent_media(env, official):
    cid = add_company(env, round_text=None, amount=None)
    rows = rows_for(official=official, amount="$120M", round_text="Series A")
    run(env, cid, Services(rows, findings_for(rows, amount="$120M", round_text="Series A")))
    result = event(env, cid)
    assert result["round_norm"] == ("A 轮" if official else "待核")
    assert result["amount_usd"] == (120_000_000 if official else None)
    if official:
        card = scan.collect_listing(env[0], env[2].now().date(), weekly=True)["cards"][0]
        assert card.amount_label == "1.2 亿美元" and "大额" in card.marks
        assert len(card.sources) == 1


def test_series_b_confirmation_filters_company(env):
    cid = add_company(env, round_text=None)
    rows = rows_for(official=True, round_text="Series B")
    run(env, cid, Services(rows, findings_for(rows, round_text="Series B")))
    assert json.loads(event(env, cid)["filter_reasons_json"]) == ["轮次不在范围"]
    assert not scan.collect_listing(env[0], env[2].now().date(), weekly=True)["cards"]


def test_aggregate_round_conflicts_cannot_confirm(env):
    cid = add_company(env, "Rocker", round_text=None)
    rows = rows_for("Rocker")
    rows[0].update(url="https://parsers.vc/a", snippet="Rocker trust administration business completed a Seed round.")
    rows[1].update(url="https://tracxn.com/a", snippet="Series A financing for Rocker supports its trust administration business.")
    fs = [{"field": "轮次", "value": "Seed" if i == 0 else "Series A", "url": r["url"], "quote": r["snippet"]} for i, r in enumerate(rows)]
    run(env, cid, Services(rows, fs))
    assert event(env, cid)["round_norm"] == "待核"
    assert logs(env, cid)["轮次"]["reason"].startswith("来源说法不一")


def test_currency_confirmation_uses_program_parsing(env):
    cid = add_company(env, country="USA", amount="10 million")
    rows = rows_for(official=True, amount="$10 million")
    fs = [{"field": "币种", "value": "USD", "url": r["url"], "quote": r["snippet"]} for r in rows]
    run(env, cid, Services(rows, fs))
    assert event(env, cid)["amount_currency"] == "USD"
    assert event(env, cid)["amount_usd"] == 10_000_000


def test_labels_require_fetched_official_page(env):
    cid = add_company(env, country="USA", label=True)
    rows = rows_for(official=True)
    quote = "Aster builds AI agents for trust administration."
    fields = [{"field": "标签", "value": {"ai": {"verdict": "是", "quote": quote}, "social": {"verdict": "否", "quote": ""}},
               "url": rows[0]["url"], "quote": quote}]
    service = Services(rows, fields, body=quote)
    run(env, cid, service)
    assert company(env, cid)["ai_label"] == "是"
    assert len(service.page_requests) == 1
    assert "".join(service.model_requests[0]["pages"].values()) == quote


# #40⑦ 混合结果、失败后重查和详情文案。
@pytest.mark.parametrize("status,reason", [("timeout", "搜索超时"), (500, "搜索失败"), (429, "搜索失败"),
                                          (401, "key 或余额问题（401 / 402）"), (402, "key 或余额问题（401 / 402）")])
def test_search_failures_are_retryable(env, status, reason):
    cid = add_company(env)
    service = Services(responses=[{"status": status}])
    run(env, cid, service)
    assert logs(env, cid)["国家"]["result"] == verify.SKIPPED
    assert logs(env, cid)["国家"]["reason"] == reason
    assert len(service.queries) == 1
    assert site._verification(env[0], env[2].now())[1][cid][0]["text"] == "尚未查，下次运行再查"


def test_first_round_ok_second_timeout_and_partial_confirmed_fields(env):
    cid = add_company(env, round_text=None)
    rows = rows_for()
    service = Services(rows, findings_for(rows), responses=[{"status": 200, "results": rows}, {"status": "timeout"}])
    run(env, cid, service)
    assert logs(env, cid)["国家"]["result"] == verify.CONFIRMED
    assert logs(env, cid)["轮次"]["result"] == verify.SKIPPED
    assert len(service.queries) == 2


def test_model_failure_and_unconfirmed_counts(env):
    cid = add_company(env)
    rows = rows_for()
    run(env, cid, Services(rows, models=[{"error": "timeout"}]))
    assert logs(env, cid)["国家"]["reason"] == "模型失败"
    env[2].advance(1)
    run(env, cid, Services())
    assert site._verification(env[0], env[2].now())[1][cid][0]["text"] == "已查过 4 次，仍无法确认"


def test_retry_already_pushed_does_not_requeue_or_rewrite_digest(env):
    cid = add_company(env)
    run(env, cid, Services(responses=[{"status": 500}]))
    env[0].execute("UPDATE funding_event SET push_date='2026-11-03',last_pushed_date='2026-11-03',pending=0")
    env[0].execute("INSERT INTO digest(date,text) VALUES ('2026-11-03','unchanged')")
    env[2].advance(86400)
    rows = rows_for()
    verify.run_verification(env[0], env[1], env[2], services=Services(rows, findings_for(rows)))
    assert company(env, cid)["country"] == "美国"
    assert event(env, cid)["pending"] == 0
    assert env[0].execute("SELECT text FROM digest").fetchone()[0] == "unchanged"


# #40⑧ 信源冲突不覆盖，金额超过10%标记。
def test_conflicts_keep_signal_values_and_show_quotes(env):
    cid = add_company(env, country="USA", label=True)
    rows = rows_for(city="London, UK", official=True, amount="$15M", round_text="Series A")
    fs = findings_for(rows, country="英国", city="London", amount="$15M", round_text="Series A")
    run(env, cid, Services(rows, fs, body="Aster builds trust administration."))
    result = event(env, cid)
    assert result["round_norm"] == "种子轮" and result["amount_usd"] == 10_000_000
    assert company(env, cid)["country"] == "美国"
    assert result["amount_status"] == "金额待核" and "金额待核" in json.loads(result["marks_json"])
    conflicts = json.loads(env[0].execute("SELECT value FROM meta WHERE key=?", ("verify:conflicts:" + cid,)).fetchone()[0])
    assert {c["field"] for c in conflicts} >= {"国家", "轮次", "金额"}
    assert all(v["quote"] and v["url"] for c in conflicts for v in c["values"])


# #40⑨⑩ 限时、日上限、401当天停止和不同来源隔离。
@pytest.mark.parametrize("day,limit", [("2026-11-02", 120), ("2026-11-03", 60), ("2026-10-08", 120), ("2026-10-10", 60)])
def test_chinese_calendar_limit(day, limit):
    assert verify.daily_limit(Config(), date.fromisoformat(day)) == limit


def test_daily_limit_counts_all_runs_only_daily(env):
    conn, cfg, clock = env
    cfg.verify_daily_limit = 5
    a, b, c = [add_company(env, n) for n in ("Aster", "Birch", "Cedar")]
    run(env, a, Services())
    run(env, b, Services())
    run(env, c, Services())
    assert db.daily_verify_search_count(conn, clock.now().date()) == 5
    assert logs(env, c)["国家"]["reason"] == "超当天上限"
    clock.advance(1)
    run(env, c, Services(), first_issue=True)
    assert db.daily_verify_search_count(conn, clock.now().date()) == 5
    assert conn.execute("SELECT COUNT(*) FROM verify_search WHERE source='首期'").fetchone()[0] == 3


def test_step_deadline_keeps_attempt_record_and_skips_remainder(env):
    conn, cfg, clock = env
    cfg.verify_step_timeout = .1
    cid = add_company(env)
    service = Services(responses=[{"status": 200, "delay_s": .1, "results": rows_for()}], clock=clock)
    run(env, cid, service)
    assert logs(env, cid)["国家"]["reason"] == "整步到点"
    assert conn.execute("SELECT COUNT(*) FROM verify_search").fetchone()[0] == 1


def test_auth_error_stops_later_runs_same_day(env):
    a, b = add_company(env), add_company(env, "Birch")
    run(env, a, Services(responses=[{"status": 401}]))
    service = Services()
    run(env, b, service)
    assert not service.queries
    assert logs(env, b)["国家"]["reason"] == "key 或余额问题（401 / 402）"


# #40⑪ 7/30个北京自然日，新增event_source绕过缓存。
def test_unconfirmed_seven_days_and_new_source(env):
    cid = add_company(env)
    run(env, cid, Services())
    env[2].advance(6 * 86400)
    service = Services()
    run(env, cid, service)
    assert not service.queries
    env[2].advance(86400)
    run(env, cid, service)
    assert len(service.queries) == 3
    add_company(env, suffix="-new", source="venturebeat.com")
    service = Services()
    run(env, cid, service)
    assert len(service.queries) == 3


def test_confirmed_thirty_days_excluded_from_other_field_queries(env):
    cid = add_company(env, round_text=None)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    env[2].advance(7 * 86400)
    service = Services(rows)
    run(env, cid, service)
    assert service.model_requests and all("国家" not in p["fields"] for p in service.model_requests)
    env[2].advance(23 * 86400)
    service = Services(rows)
    run(env, cid, service)
    assert "国家" in service.model_requests[0]["fields"]


# #40⑫⑬ 费用和状态页五项，首期与补充不占日计数。
def test_costs_statistics_and_same_day_rerun(env):
    conn, cfg, clock = env
    cid = add_company(env)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    stats, details = site._verification(conn, clock.now())
    assert stats == {"companies": 1, "fields": 2, "unsearched": 0, "searches": 1,
                     "month_used": .0085, "first_issue_cost": 0, "backfill_cost": 0}
    # 重查失败的下一次完成后，本次未查家数应清零。
    b = add_company(env, "Birch")
    run(env, b, Services(responses=[{"status": 500}]))
    assert site._verification(conn, clock.now())[0]["unsearched"] == 1
    clock.advance(1)
    run(env, b, Services())
    assert site._verification(conn, clock.now())[0]["unsearched"] == 0
    before = site._verification(conn, clock.now())[0]
    c = add_company(env, "Cedar")
    run(env, c, Services(rows_for("Cedar")), first_issue=True)
    after = site._verification(conn, clock.now())[0]
    assert all(after[k] == before[k] for k in ("companies", "fields", "unsearched", "searches"))
    assert after["first_issue_cost"] > 0 and after["month_used"] > before["month_used"]


def test_zero_results_unbilled_and_peak_model_price(env):
    cid = add_company(env)
    run(env, cid, Services())
    assert env[0].execute("SELECT SUM(cost) FROM verify_search").fetchone()[0] == 0
    env[2].advance(7 * 86400 + 3600)
    rows = rows_for()
    run(env, cid, Services(rows, findings_for(rows)))
    assert env[0].execute("SELECT model_cost FROM verify_search ORDER BY id DESC LIMIT 1").fetchone()[0] == .007


# #40⑭ 币种反例由程序处理，不因没有身份显示已查过0次。
@pytest.mark.parametrize("text,currency", [("¥8000 万（约 110 万美元）", None), ("¥500 万日元", "JPY"), ("5 crore", None)])
def test_ambiguous_currency_examples(text, currency):
    assert fx.parse_amount(text)[0] == currency


# #40⑮ 单页超时或403只影响标签；国家无需页面仍可确认。
@pytest.mark.parametrize("status,delay", [(200, 16), (403, 0)])
def test_fixture_page_failure_does_not_abort_other_fields(env, tmp_path, status, delay):
    conn, cfg, clock = env
    cid = add_company(env, label=True)
    rows = rows_for(official=True)
    path = tmp_path / "fixtures"
    (path / "search").mkdir(parents=True)
    (path / "search" / "00.json").write_text(json.dumps({"match": "Aster", "results": rows}))
    (path / "verify-model").mkdir()
    for r in (1, 2):
        (path / "verify-model" / f"aster-{r}.json").write_text(json.dumps({"fields": findings_for(rows)}))
    pages = path / "verify-pages"
    pages.mkdir()
    digest = hashlib.sha1(rows[0]["url"].encode()).hexdigest()
    (pages / (digest + ".html")).write_text("<p>Aster builds AI agents for trust administration.</p>")
    (pages / (digest + ".meta.json")).write_text(json.dumps({"status": status, "delay_s": delay}))
    run(env, cid, verify.FixtureServices(path, clock))
    assert logs(env, cid)["国家"]["result"] == verify.CONFIRMED
    assert logs(env, cid)["标签"]["result"] == verify.SKIPPED
    assert logs(env, cid)["标签"]["reason"] == "抓页失败"
    assert company(env, cid)["ai_label"] == "待核"


# 配置、产品词、迁移和测试入口。
@pytest.mark.parametrize("domain,expected", [("https://docs.firm.co.uk/a", "firm.co.uk"), ("x.company.com.au", "company.com.au"),
                                           ("alice.github.io", "alice.github.io"), ("www.sec.gov", "sec.gov")])
def test_registered_domain_table(domain, expected):
    assert verify.registered_domain(domain) == expected


def test_product_terms_validation_and_storage(env):
    item = RawItem("test", "x", "https://example.test", "Aster trust administration and Durable Actors", None)
    data = {"company_name": "Aster", "product_terms": ["Aster", "TRUST administration", "Durable Actors", "Seed", "invented", "Greycroft"],
            "leads": ["Greycroft"]}
    cleaned, _ = extract.validate_model_output(item, data)
    assert cleaned["product_terms"] == ["TRUST administration", "Durable Actors"]
    ext = fixed_extraction(item, data)
    assert ext.product_terms == cleaned["product_terms"]
    cid = add_company(env)
    assert json.loads(company(env, cid)["product_terms_json"]) == ["trust administration"]
    assert "product_terms" in extract.build_prompt(item, Extracted(), "summary")[0]


def test_old_database_migration_is_repeatable_without_data_loss(tmp_path):
    path = tmp_path / "old.db"
    conn = db.connect(path)
    conn.execute("INSERT INTO company(id,name_key,name,first_seen) VALUES ('old','old','Old','2026-10-01')")
    conn.execute("INSERT INTO verify_log(company_id,run_date,run_started_at,field,result,verified_at) VALUES ('old','2026-10-01','2026-10-01','国家','本次未查','2026-10-01')")
    conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    for col in ("product_terms_json", "city"):
        conn.execute(f"ALTER TABLE company DROP COLUMN {col}")
    conn.execute("DROP INDEX verify_log_identity")
    for col in ("value_json", "event_id", "source_ids_json"):
        conn.execute(f"ALTER TABLE verify_log DROP COLUMN {col}")
    conn.commit()
    conn.close()
    for _ in range(2):
        conn = db.connect(path)
        assert conn.execute("SELECT name,product_terms_json FROM company").fetchone()[:] == ("Old", "[]")
        assert conn.execute("SELECT COUNT(*) FROM verify_log").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
        conn.close()


def test_test_entry_never_constructs_live_services(env, monkeypatch):
    cid = add_company(env)
    monkeypatch.setattr(verify, "LiveServices", lambda *a, **k: pytest.fail("测试不应创建线上服务"))
    verify.run_verification(env[0], env[1], env[2], company_ids=[cid])
    assert logs(env, cid)["国家"]["reason"] == "测试跳过"
    assert parse_args(["--search-fixtures", "/tmp/search.json"]).test_flags
    assert parse_args(["--no-verify"]).test_flags
    assert parse_args(["--dry-run", "--no-verify"]).no_verify
