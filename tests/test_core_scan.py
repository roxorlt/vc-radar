import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from vcradar import db, scan
from vcradar.config import BJ
from vcradar.models import Extracted, LabelDim, RawItem, SourceDef


NOW = datetime(2026, 10, 30, 8, 30, tzinfo=BJ)
BATCHES = ["Fall 2026", "Summer 2026", "Spring 2026", "Winter 2026"]
SOURCE_DEFS = [
    SourceDef("tc_venture", "TechCrunch", "venture", "https://techcrunch.com/venture/feed", "rss", 2, 1),
    SourceDef("tc_startups", "TechCrunch", "ai", "https://techcrunch.com/ai/feed", "rss", 2, 2),
    SourceDef("cbn_main", "Crunchbase News", "news", "https://news.crunchbase.com/feed", "rss", 2, 3),
    SourceDef("yc_launches", "YC Launches", "launches", "https://www.ycombinator.com/launches", "api", 2, 4),
    SourceDef("yc_directory", "YC 公司目录", "companies", "https://www.ycombinator.com/companies", "api", None, 5),
    SourceDef("launch_hn", "Launch HN", "launches", "https://hn.algolia.com/api", "api", 2, 6),
    SourceDef("strictlyvc", "StrictlyVC", "fundings", "https://www.strictlyvc.com", "page", 2, 7),
    SourceDef("ts_web", "Term Sheet", "venture", "https://content.fortune.com", "api", 2, 0),
    SourceDef("alleywatch", "AlleyWatch", "single", "https://www.alleywatch.com/feed", "rss", 2, 8),
    SourceDef("official", "公司官方", "news", "https://official.invalid", "page", None, 9),
    SourceDef("venturebeat", "VentureBeat", "news", "https://venturebeat.com/feed", "rss", 2, 10),
    SourceDef("other", "其他媒体", "news", "https://other.invalid/feed", "rss", 2, 11),
    SourceDef("email", "其他邮件通讯", "mail", "https://mail.invalid", "page", 2, 12),
    SourceDef("x", "X", "posts", "https://x.com", "api", 2, 13),
]
SOURCES = {source.id: source for source in SOURCE_DEFS}
# 旧用例的参数别名只留在测试辅助入口，写入库的一律是正式 source_id。
SOURCES.update({alias: SOURCES[source_id] for alias, source_id in
                (("tc_ai", "tc_startups"), ("cbn", "cbn_main"), ("term_sheet", "ts_web"))})


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.sync_sources(connection, SOURCE_DEFS)
    yield connection
    connection.close()


def add(conn, *, source="tc_venture", name="Example", website="https://example.ai", hq="San Francisco",
        round_text="Seed", amount="$10M", now=NOW, published=NOW, baseline=False, backfill=False,
        external_id=None, url=None, structured=None, ai=None, social=None, leads=None, follows=None, body="",
        summary="这是一段手写的测试简介。", **overrides):
    serial = str(conn.execute("SELECT COUNT(*) FROM raw_item").fetchone()[0] + 1)
    item = RawItem(SOURCES[source].id, external_id or serial, url or f"{SOURCES[source].url.split('/feed')[0].rstrip('/')}/synthetic/{serial}",
                   f"{name} 的合成融资消息", published, excerpt="手写测试摘录。", body=body, structured=structured or {})
    extracted = Extracted(company_name=name, website=website, round_text=round_text, amount_text=amount,
                          hq_text=hq, hq_quote=f"合成总部字段：{hq}" if hq else "",
                          ai=ai or LabelDim("是", "手写的 AI 产品依据。"), social=social or LabelDim("否", ""),
                          leads=leads or [], follows=follows or [], summary=summary, **overrides)
    item_id, _ = scan.upsert_raw_item(conn, item, now, baseline)
    event_id = scan.ingest(conn, item_id, item, extracted, now=now, is_baseline=baseline,
                           is_backfill=backfill, latest_yc_batches=BATCHES, model="synthetic-model")
    return event_id, item_id, item, extracted


def event(conn, event_id):
    return conn.execute("SELECT * FROM funding_event WHERE id=?", (event_id,)).fetchone()


@pytest.mark.parametrize("original,expected", [
    ("Angel round", "天使轮"), ("Angel", "天使轮"), ("Pre-seed", "种子轮"), ("Seed", "种子轮"),
    ("Seed extension", "种子轮"), ("Seed+", "种子轮"), ("Pre-Series A", "种子轮"), ("YC 标准投资", "种子轮"),
    ("Series A", "A 轮"), ("A-1", "A 轮"), ("A-2", "A 轮"), ("A extension", "A 轮"), ("A+", "A 轮"),
    ("Series B", "过滤"), ("Series C", "过滤"), ("Series D funding", "过滤"), ("Growth", "过滤"),
    ("Debt financing", "过滤"), ("$10B fund", "过滤"), ("Fourth fund", "过滤"),
    (None, "待核"), ("", "待核"), ("Undisclosed", "待核"), ("未知", "待核"),
])
def test_every_round_classification(original, expected):
    assert scan.normalize_round(original) == expected


@pytest.mark.parametrize("flags", [{"is_fund": True}, {"is_debt_only": True}])
def test_fund_and_debt_flags_override_round(flags):
    assert scan.normalize_round("Series A", **flags) == "过滤"


@pytest.mark.parametrize("source,location,hq,body,expected,reason", [
    ("yc_directory", "San Francisco, CA, USA; Remote", None, "", "美国", "San Francisco, CA, USA"),
    ("yc_directory", "Paris, France; New York, USA", "New York", "", "法国", "Paris, France"),
    ("yc_directory", "", None, "", "国家待确认", "YC 公司目录里地点为空"),
    ("tc_venture", None, "San Francisco", "", "美国", ""),
    ("strictlyvc", None, None, "Example, a Boston-based company, raised a synthetic round.", "美国", ""),
    ("strictlyvc", None, None, "Example is a software San Francisco startup.", "美国", ""),
    ("alleywatch", None, None, "Example is a New York-based startup.", "美国", ""),
    ("alleywatch", None, None, "Example received investment from a New York investor.", "国家待确认", "来源没写明国家"),
    ("tc_venture", None, None, "Example is headquartered in San Francisco, with its team in London.", "美国", ""),
    ("tc_venture", None, None, "Example is registered in Delaware but headquartered in Berlin, with a team in Germany.", "德国", ""),
    ("tc_venture", None, "London", "A California investor backed Example.", "英国", ""),
    ("tc_venture", None, None, "A San Francisco investor backed Example.", "国家待确认", "来源没写明国家"),
    ("x", None, "North America", "", "国家待确认", "X 帖子不算国家依据"),
    ("x", None, "San Francisco", "Headquartered in New York.", "国家待确认", "X 帖子不算国家依据"),
])
def test_every_country_judgement_row(source, location, hq, body, expected, reason):
    item = RawItem(source, "one", SOURCES[source].url, "合成总部测试", NOW, body=body,
                   structured={"location": location} if location is not None else {})
    verdict, basis = scan.judge_country(item, Extracted(hq_text=hq))
    assert verdict == expected
    if reason:
        assert basis == reason


@pytest.mark.parametrize("location", [
    "CA, USA", "California", "Calif.", "NYC", "New York, NY", "Boston, Massachusetts",
    "Austin, TX", "Minneapolis, MN", "Denver, CO", "Atlanta, GA", "Seattle, WA",
    "New Jersey", "NC", "Washington, D.C.", "United States", "U.S.", "U.S.A.",
])
def test_us_cities_states_abbreviations(location):
    item = RawItem("tc_venture", "test", "https://techcrunch.com/synthetic", "总部样例", NOW)
    assert scan.judge_country(item, Extracted(hq_text=location))[0] == "美国"


def test_filter_priority_and_recent_four_only():
    item = RawItem("yc_launches", "one", "https://www.ycombinator.com/launches/synthetic", "Zalos", NOW,
                   structured={"yc_batch": "Fall 2025"})
    assert scan.filter_reasons(item, Extracted(), "英国", "过滤", BATCHES) == ["批次超过 12 个月", "轮次不在范围"]
    assert scan.filter_reasons(item, Extracted(), "美国", "种子轮", BATCHES + ["Fall 2025"]) == ["批次超过 12 个月"]
    assert scan.filter_reasons(replace(item, structured={"yc_batch": "Winter 2026"}), Extracted(), "美国", "种子轮", BATCHES) == []


@pytest.mark.parametrize("original,expected", [
    ("https://WWW.Antimattr.one/project?utm_source=test", "antimattr.one"),
    ("news.acme.com/path", "acme.com"), ("https://a.acme.co.uk/path", "acme.co.uk"),
    ("https://demo.github.io/path", "demo.github.io"), ("https://www.example.ai:443/a", "example.ai"),
    (None, None), ("", None), ("https://127.0.0.1", None), ("not a domain", None),
    ("mailto:one@example.com", None), ("https://co.uk", None),
])
def test_registrable_domain(original, expected):
    assert scan.registrable_domain(original) == expected


@pytest.mark.parametrize("original,expected", [
    ("Dream AI, Inc.", "dream ai"), (" Dream AI Technologies Labs HQ LLC ", "dream ai"),
    ("A.C.M.E. Corp.", "acme"), ("Acme L.L.C.", "acme"), ("BeeSafe AI", "beesafe ai"),
    ("Fiction-Co, Ltd.", "fiction"), ("Company Labs", ""),
])
def test_name_normalization(original, expected):
    assert scan.name_key(original) == expected


@pytest.mark.parametrize("original,expected", [
    ("$17M", 17e6), ("$1B", 1e9), ("$27.5 million", 27.5e6), ("US$500K", 500e3),
    ("USD 1,200,000", 1.2e6), ("$10,000", 10000), ("2 million USD", 2e6),
    (None, None), ("未披露", None), ("€17M", None), ("C$17M", None), ("¥17M", None),
])
def test_amount_parsing(original, expected):
    assert scan.parse_amount_usd(original) == expected


def test_new_company_source_model_and_raw_immutability(conn):
    eid, iid, item, extracted = add(conn)
    assert len(scan.pending_events(conn)) == 1
    assert event(conn, eid)["round_norm"] == "种子轮"
    raw = conn.execute("SELECT * FROM raw_item WHERE id=?", (iid,)).fetchone()
    assert "body" not in raw.keys()
    assert raw["excerpt"] == "手写测试摘录。"
    same_id, fresh = scan.upsert_raw_item(conn, replace(item, title="重跑改标题", excerpt="重跑改摘录"), NOW + timedelta(days=1), True)
    assert (same_id, fresh) == (iid, False)
    assert conn.execute("SELECT title FROM raw_item WHERE id=?", (iid,)).fetchone()[0] == item.title
    assert scan.ingest(conn, iid, item, extracted, now=NOW + timedelta(days=100), is_baseline=False, is_backfill=True,
                       latest_yc_batches=BATCHES, model=None) == eid
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM funding_event").fetchone()[0] == 1
    assert conn.execute("SELECT model FROM event_source").fetchone()[0] == "synthetic-model"


@pytest.mark.parametrize("source", ["yc_launches", "yc_directory"])
def test_recent_yc_standard_investment_defaults_and_dates(conn, source):
    eid, _, _, _ = add(conn, source=source, name="Terse", round_text=None, amount="$500K", leads=[], hq=None,
                        published=None, structured={"yc_batch": "Fall 2026", "location": "NYC, USA", "launched_at": 1})
    row = event(conn, eid)
    assert row["round_norm"] == "种子轮"
    assert row["amount_status"] == "未披露"
    assert json.loads(row["leads_json"]) == ["Y Combinator"]
    if source == "yc_directory":
        card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
        assert (card.amount_label, card.lead_label, card.event_date) == ("金额未披露", "Y Combinator", NOW.date())


def test_yc_directory_and_launches_share_one_event_and_push_once(conn):
    eid, *_ = add(conn, source="yc_directory", round_text=None, amount=None,
                  structured={"yc_batch": "Fall 2026", "location": "San Francisco, CA, USA"})
    scan.mark_listed(conn, [eid], NOW.date())
    launched, *_ = add(conn, source="yc_launches", round_text=None, amount=None, now=NOW + timedelta(days=1),
                       structured={"yc_batch": "Fall 2026"})
    assert launched == eid
    assert not scan.pending_events(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 2


def test_foreign_yc_directory_then_launch_is_not_pushed_twice(conn):
    eid, *_ = add(conn, source="yc_directory", round_text=None, amount=None,
                  structured={"yc_batch": "Fall 2026", "location": "Paris, France"})
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert not listing["filtered"]
    assert listing["cards"][0].country == "法国"
    scan.mark_listed(conn, listing["event_ids"], NOW.date())
    add(conn, source="yc_launches", round_text=None, amount=None, hq="San Francisco", structured={"yc_batch": "Fall 2026"})
    assert not scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]
    assert event(conn, eid)["push_date"] == "2026-10-30"


@pytest.mark.parametrize("hq,expected", [("San Francisco", "批次超过 12 个月"), ("London", "批次超过 12 个月")])
def test_old_yc_batch_filter_priority(conn, hq, expected):
    eid, *_ = add(conn, source="yc_launches", round_text=None, hq=hq, structured={"yc_batch": "Fall 2025"})
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["filtered"] == {expected: 1}
    if hq == "London":
        assert json.loads(event(conn, eid)["filter_reasons_json"]) == ["批次超过 12 个月"]


def test_new_round_marks_and_separate_history(conn):
    seed, *_ = add(conn)
    scan.mark_listed(conn, [seed], NOW.date())
    series_a, *_ = add(conn, round_text="Series A", amount="$100M", backfill=True)
    assert seed != series_a
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.marks == ["新一轮", "大额", "补抓"]
    assert card.round_label == "A 轮"


@pytest.mark.parametrize("day,expected", [(89, False), (90, False), (91, True)])
def test_same_round_90_day_boundary(conn, day, expected):
    eid, *_ = add(conn)
    scan.mark_listed(conn, [eid], NOW.date())
    current = NOW + timedelta(days=day)
    again, *_ = add(conn, source="cbn", now=current, published=current)
    assert again == eid
    assert bool(scan.pending_events(conn)) is expected
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 2
    if expected:
        assert "新一轮" not in scan.collect_listing(conn, current.date(), weekly=True)["cards"][0].marks
        scan.mark_listed(conn, [eid], current.date())
        assert event(conn, eid)["push_date"] == NOW.date().isoformat()
        assert event(conn, eid)["last_pushed_date"] == current.date().isoformat()


@pytest.mark.parametrize("baseline_source,new_source", [
    ("tc_venture", "cbn"), ("yc_launches", "yc_launches"), ("yc_directory", "yc_launches"), ("yc_launches", "yc_directory"),
])
def test_baseline_event_first_nonbaseline_item_pushes_once(conn, baseline_source, new_source):
    properties = {"yc_batch": "Fall 2026", "location": "New York, USA"}
    eid, iid, item, extracted = add(conn, source=baseline_source, baseline=True, now=NOW - timedelta(days=11), structured=properties)
    assert not scan.pending_events(conn)
    assert event(conn, eid)["baseline_unpushed"] == 1
    assert event(conn, eid)["event_date"] == "2026-10-19"
    scan.ingest(conn, iid, item, extracted, now=NOW, is_baseline=False, is_backfill=False, latest_yc_batches=BATCHES, model=None)
    assert not scan.pending_events(conn)
    same, *_ = add(conn, source=new_source, structured=properties)
    assert same == eid
    assert event(conn, eid)["baseline_unpushed"] == 0
    assert event(conn, eid)["event_date"] == "2026-10-30"
    assert conn.execute("SELECT first_seen FROM company").fetchone()[0] == "2026-10-19"
    scan.mark_listed(conn, [eid], NOW.date())
    add(conn, source=new_source, structured=properties, now=NOW + timedelta(days=1))
    assert not scan.pending_events(conn)


def test_baseline_deduplicated_companies_and_no_link_not_stored(conn):
    add(conn, baseline=True)
    add(conn, source="cbn", baseline=True)
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1
    assert not db.is_empty(conn)
    assert not scan.collect_listing(conn, NOW.date(), weekly=True)["event_ids"]
    item = RawItem("tc_venture", "no-url", "", "没有来源链接", NOW)
    assert scan.upsert_raw_item(conn, item, NOW, False) == (0, False)
    assert scan.ingest(conn, 0, item, Extracted(), now=NOW, is_baseline=False, is_backfill=False, latest_yc_batches=BATCHES, model=None) is None


def test_different_domains_same_name_are_separate_and_both_admitted(conn):
    first, *_ = add(conn, name="Phonic", website="https://phonic.ai")
    second, *_ = add(conn, name="Phonic Inc.", website="https://phonic.example")
    assert first != second
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert len(listing["cards"]) == 2
    assert {c.country for c in listing["cards"]} == {"美国"}
    assert {r[0] for r in conn.execute("SELECT pending_reason FROM company")} == {"名字相同但官网不同，可能不是同一家"}
    assert {row[0] for row in conn.execute("SELECT id FROM company")} == {"phonic", "phonic-2"}


def test_later_domain_merges_preserving_slug(conn):
    first, *_ = add(conn, name="Fiction Labs", website=None)
    original_id = event(conn, first)["company_id"]
    second, *_ = add(conn, source="cbn", name="Fiction, Inc.", website="https://www.new-name.ai/path?utm_source=x")
    assert first == second
    assert event(conn, second)["company_id"] == original_id == "fiction"
    row = conn.execute("SELECT * FROM company").fetchone()
    assert row["domain_key"] == "new-name.ai"
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1


def test_same_domain_different_names_merge_and_slug_uses_domain(conn):
    first, *_ = add(conn, name="First Name", website="https://brand.ai/path")
    second, *_ = add(conn, source="cbn", name="Second Name", website="https://www.brand.ai")
    assert first == second
    assert event(conn, first)["company_id"] == "brand"
    assert conn.execute("SELECT name FROM company").fetchone()[0] == "Second Name"


def test_company_merge_when_domain_and_name_were_separately_known(conn):
    old, *_ = add(conn, name="Old Name", website=None)
    known, *_ = add(conn, name="Other Name", website="https://unified.ai")
    merged, *_ = add(conn, source="cbn", name="Old Name", website="https://unified.ai")
    assert merged == known
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM funding_event").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 3
    assert event(conn, old) is None


def test_same_report_two_tc_columns_and_strictlyvc_original_count_once(conn):
    report = "https://techcrunch.com/synthetic/report"
    eid, *_ = add(conn, source="tc_venture", url=report)
    scan.mark_listed(conn, [eid], NOW.date())
    add(conn, source="tc_ai", url=report + "/?utm_source=ai")
    add(conn, source="strictlyvc", structured={"original_link": report})
    assert conn.execute("SELECT COUNT(*) FROM raw_item").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 1
    assert not scan.pending_events(conn)


@pytest.mark.parametrize("higher,lower", [
    ("official", "yc_launches"), ("yc_launches", "cbn"), ("launch_hn", "cbn"),
    ("cbn", "term_sheet"), ("term_sheet", "tc_venture"), ("tc_venture", "alleywatch"),
    ("alleywatch", "strictlyvc"), ("strictlyvc", "venturebeat"),
    ("venturebeat", "other"), ("other", "email"), ("email", "x"),
])
def test_field_confidence_order_each_step(conn, higher, lower):
    eid, *_ = add(conn, source=lower, amount="$10M", leads=["Low Lead"])
    add(conn, source=higher, amount="$11M", leads=["High Lead"])
    assert event(conn, eid)["amount_text"] == "$11M"
    assert json.loads(event(conn, eid)["leads_json"]) == ["High Lead"]
    assert event(conn, eid)["amount_status"] == "披露"


def test_same_credibility_uses_earlier_publication_independent_of_ingestion(conn):
    eid, *_ = add(conn, amount="$10M", published=NOW, summary="较晚发布的合成简介。")
    add(conn, source="tc_ai", amount="$11M", published=NOW - timedelta(days=1), summary="较早发布的合成简介。")
    assert event(conn, eid)["amount_text"] == "$11M"
    assert conn.execute("SELECT summary FROM company").fetchone()[0] == "较早发布的合成简介。"


def test_strictlyvc_original_confidence_and_display_source(conn):
    eid, *_ = add(conn, source="term_sheet", amount="$10M")
    add(conn, source="strictlyvc", amount="$11M", structured={"original_link": "https://news.crunchbase.com/synthetic/article"})
    assert event(conn, eid)["amount_text"] == "$11M"
    sources = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].sources
    assert [source.name for source in sources] == ["StrictlyVC", "Term Sheet"]


def test_conflicting_headquarters_require_official_or_directory(conn):
    eid, *_ = add(conn, source="tc_venture", hq="San Francisco")
    add(conn, source="cbn", hq="London")
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["cards"][0].country == "国家待确认"
    assert "国家来源有冲突" in conn.execute("SELECT hq_basis FROM company").fetchone()[0]
    assert not listing["filtered"]
    add(conn, source="official", hq="San Francisco")
    assert len(scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]) == 1
    assert not json.loads(event(conn, eid)["filter_reasons_json"])


def test_yc_directory_overrides_other_headquarters(conn):
    eid, *_ = add(conn, source="tc_venture", hq="San Francisco")
    add(conn, source="yc_directory", hq=None, structured={"location": "Paris, France; New York, USA", "yc_batch": "Fall 2026"})
    assert json.loads(event(conn, eid)["filter_reasons_json"]) == []
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].country == "法国"


@pytest.mark.parametrize("amount,disputed", [("$11M", False), ("$11.0001M", True), ("$12M", True)])
def test_amount_conflict_ten_percent_boundary(conn, amount, disputed):
    eid, *_ = add(conn, amount="$10M")
    add(conn, source="cbn", amount=amount)
    row = event(conn, eid)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert (row["amount_status"] == "金额待核") is disputed
    assert ("金额待核" in card.marks) is disputed
    if disputed:
        displayed = {"$11.0001M": "1100 万美元", "$12M": "1200 万美元"}[amount]
        assert card.amount_label == f"TechCrunch 写 1000 万美元，Crunchbase News 写 {displayed}"
        assert card.amount_usd == 10e6
    else:
        assert card.amount_label == "1100 万美元"


def test_investor_union_and_highest_confidence_leads(conn):
    eid, *_ = add(conn, leads=["Low Lead"], follows=["Shared", "Low Follow"])
    add(conn, source="cbn", leads=["High Lead", "Co Lead"], follows=["Shared", "New Follow"])
    row = event(conn, eid)
    assert json.loads(row["leads_json"]) == ["High Lead", "Co Lead"]
    assert set(json.loads(row["follows_json"])) == {"Low Lead", "Shared", "Low Follow", "New Follow"}
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].lead_label == "High Lead、Co Lead 共同领投"


def test_earliest_nonbaseline_date_and_each_source_beijing_date(conn):
    eid, *_ = add(conn, baseline=True, now=NOW - timedelta(days=11), published=NOW - timedelta(days=20))
    add(conn, source="cbn", published=NOW - timedelta(days=1))
    add(conn, source="term_sheet", published=datetime(2026, 10, 27, 23, tzinfo=timezone.utc))
    assert event(conn, eid)["event_date"] == "2026-10-28"
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert {source.name: source.beijing_date for source in card.sources} == {
        "TechCrunch": date(2026, 10, 10), "Crunchbase News": date(2026, 10, 29), "Term Sheet": date(2026, 10, 28)}


@pytest.mark.parametrize("round_text,amount,big", [("Seed", "$50M", True), ("Seed", "$49.99M", False), ("Series A", "$100M", True), ("Series A", "$99.9M", False)])
def test_big_amount_thresholds(conn, round_text, amount, big):
    add(conn, round_text=round_text, amount=amount)
    assert ("大额" in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].marks) is big


def test_holiday_mark_cancelled_in_all_modes_and_other_marks_keep_order(conn):
    add(conn, amount="$50M", backfill=True, published=datetime(2026, 10, 24, 13, tzinfo=BJ))
    assert scan.collect_listing(conn, NOW.date(), weekly=True, base_holiday_mark=True)["cards"][0].marks == ["大额", "补抓"]
    assert scan.collect_listing(conn, NOW.date(), weekly=False)["cards"][0].marks == ["大额", "补抓"]


@pytest.mark.parametrize("leads,label", [([], "领投未披露"), (["Investor"], "Investor 领投"), (["A", "B"], "A、B 共同领投")])
def test_card_amount_and_lead_formats(conn, leads, label):
    add(conn, amount=None, leads=leads)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "金额未披露"
    assert card.lead_label == label


def test_pending_lines_filtered_count_and_first_listing_dates(conn):
    hq, *_ = add(conn, name="Dream", website="https://dream.example", hq=None, amount="$17M")
    unknown_round, *_ = add(conn, name="Numeracle", website="https://number.example", round_text=None, amount="$28M")
    filtered, *_ = add(conn, name="Zalos", website="https://zalos.example", source="yc_launches", hq="London",
                       round_text="Series B", structured={"yc_batch": "Fall 2025"})
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert [(card.name, card.country) for card in listing["cards"]] == [("Dream", "国家待确认")]
    assert listing["round_pending"][0].reason == "来源没写轮次"
    assert listing["filtered"] == {"批次超过 12 个月": 1}
    assert listing["filtered_total"] == 1
    assert listing["filtered_names"] == [("Zalos", "批次超过 12 个月")]
    assert set(listing["event_ids"]) == {hq, unknown_round, filtered}
    assert all(event(conn, eid)["push_date"] is None for eid in listing["event_ids"])
    scan.mark_listed(conn, listing["event_ids"], NOW.date())
    scan.mark_listed(conn, listing["event_ids"], date(2026, 11, 6))
    assert all(event(conn, eid)["push_date"] == "2026-10-30" for eid in listing["event_ids"])
    assert all(event(conn, eid)["last_pushed_date"] == "2026-10-30" for eid in listing["event_ids"])
    assert not scan.pending_events(conn)


def test_full_listing_has_no_group_or_total_card_limits(conn):
    label_pairs = [("是", "是"), ("否", "是"), ("是", "否"), ("否", "否"), ("拿不准", "否")]
    for group_index, pair in enumerate(label_pairs):
        for index in range(6):
            add(conn, name=f"Synthetic {group_index} {index}", website=f"https://s{group_index}-{index}.example",
                amount=f"${index + 1}M", ai=LabelDim(pair[0], "手写依据。"), social=LabelDim(pair[1], "手写依据。"))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert len(listing["cards"]) == 30
    assert [card.group for card in listing["cards"]] == [group for group in ("AI+社交", "社交", "AI", "都不是", "标签待核") for _ in range(6)]
    assert "overflow" not in listing
    assert set(listing["first_seen"]) == {card.company_id for card in listing["cards"]}
    assert all(stamp == NOW for stamp in listing["first_seen"].values())
    assert len(listing["event_ids"]) == 30
    scan.mark_listed(conn, listing["event_ids"], NOW.date())
    assert not scan.pending_events(conn)


def test_listing_returns_unsorted_cards_for_render_to_rank(conn):
    for name, amount, days in [("Old", "$10M", 3), ("New", "$10M", 1), ("Rich", "$20M", 4), ("Unknown", None, 0)]:
        add(conn, name=name, website=f"https://{name.lower()}.example", amount=amount, published=NOW - timedelta(days=days))
    assert [card.name for card in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]] == ["Old", "New", "Rich", "Unknown"]


@pytest.mark.parametrize("total,folded", [(30, False), (31, True)])
def test_yc_new_batch_threshold_and_all_country_count(conn, total, folded):
    for index in range(total):
        add(conn, source="yc_directory", name=f"YC Synthetic {index}", website=f"https://yc{index}.example", round_text=None,
            amount=None, hq=None, published=None, structured={"yc_batch": "Fall 2026", "location": "San Francisco, CA, USA" if index else "Paris, France"})
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert (listing["yc_new_batch"] is not None) is folded
    if folded:
        assert listing["yc_new_batch"] == ("Fall 2026", total)
        assert not listing["cards"]
    else:
        assert len(listing["cards"]) == total
    assert not listing["filtered"]
    assert len(listing["event_ids"]) == total


def test_separate_yc_fetches_do_not_fold_combined_weekly_total(conn):
    for index in range(32):
        add(conn, source="yc_directory", name=f"Separate {index}", website=f"https://separate{index}.example", round_text=None,
            amount=None, hq=None, published=None, now=NOW - timedelta(days=index // 16),
            structured={"yc_batch": "Fall 2026", "location": "New York, USA"})
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["yc_new_batch"] is None


def test_digest_same_day_overwrites_one_row_preserves_sent_fields(conn):
    scan.upsert_digest(conn, {"date": NOW.date(), "push": True, "status": "有新增", "event_ids": [1], "text": "首版",
                              "generated_at": NOW, "sent_at": "2026-10-30 09:00:00", "sent_result": "ok", "resend_count": 1})
    scan.upsert_digest(conn, {"date": NOW.date(), "status": "部分失败", "event_ids_json": [1, 2], "text": "更新",
                              "error_sources": {"TechCrunch": 1}, "simulated": True})
    rows = conn.execute("SELECT * FROM digest").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert (row["status"], row["text"], row["sent_result"], row["resend_count"]) == ("部分失败", "更新", "ok", 1)
    assert json.loads(row["event_ids_json"]) == [1, 2]
    assert json.loads(row["error_sources"]) == {"TechCrunch": 1}
    assert row["generated_at"] == "2026-10-30 08:30:00"
    scan.upsert_digest(conn, {"date": "2026-10-31", "push": False, "text": ""})
    assert conn.execute("SELECT push FROM digest WHERE date='2026-10-31'").fetchone()[0] == 0


@pytest.mark.parametrize("original,expected", [
    ("Series A with debt financing", "A 轮"), ("Seed equity and debt", "种子轮"),
    ("Series A fundraising", "A 轮"), ("Debt only", "过滤"),
])
def test_debt_component_is_not_debt_only(original, expected):
    assert scan.normalize_round(original) == expected


@pytest.mark.parametrize("text", [
    "Example raised money from a New York-based investor.",
    "Example was backed by London-based investors and led by San Francisco-based firms.",
    "Example raised a synthetic round, led by Boston-based Alpha.",
])
def test_investor_location_does_not_establish_headquarters(text):
    item = RawItem("tc_venture", "investors", "https://techcrunch.com/synthetic", "合成投资消息", NOW, body=text)
    assert scan.judge_country(item, Extracted())[0] == "国家待确认"


def test_explicit_headquarters_overrides_registration_and_us_pronoun():
    item = RawItem("tc_venture", "registered", "https://techcrunch.com/synthetic", "合成注册信息", NOW,
                   body="Example is headquartered in London. A person told us about it.")
    assert scan.judge_country(item, Extracted(hq_text="Delaware", hq_quote="Example is registered in Delaware."))[0] == "英国"


@pytest.mark.parametrize("location,expected", [("Cambridge, UK", "英国"), ("Cambridge, Massachusetts", "美国"), ("London, KY", "美国")])
def test_explicit_country_and_state_disambiguate_cities(location, expected):
    item = RawItem("tc_venture", "place", "https://techcrunch.com/synthetic", "合成地点", NOW)
    assert scan.judge_country(item, Extracted(hq_text=location))[0] == expected


@pytest.mark.parametrize("location", ["California", "Calif.", "CA", "San Francisco, CA, USA", "United States"])
def test_city_and_enclosing_state_are_not_headquarters_conflict(conn, location):
    add(conn, hq="San Francisco")
    add(conn, source="cbn", hq=location)
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert len(listing["cards"]) == 1
    assert listing["cards"][0].country == "美国"


def test_different_us_headquarters_without_official_evidence_country_pending(conn):
    add(conn, hq="San Francisco")
    add(conn, source="cbn", hq="New York")
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].country == "国家待确认"


def test_91_day_repush_of_previous_new_round_drops_new_round_mark(conn):
    seed, *_ = add(conn)
    scan.mark_listed(conn, [seed], NOW.date())
    series_a, *_ = add(conn, round_text="Series A")
    assert "新一轮" in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].marks
    scan.mark_listed(conn, [series_a], NOW.date())
    current = NOW + timedelta(days=91)
    add(conn, source="cbn", round_text="Series A", now=current, published=current)
    card = scan.collect_listing(conn, current.date(), weekly=True)["cards"][0]
    assert "新一轮" not in card.marks


def test_real_lead_preferred_over_yc_standard_investment(conn):
    eid, *_ = add(conn, source="yc_directory", round_text=None, amount=None,
                  structured={"yc_batch": "Fall 2026", "location": "San Francisco, USA"})
    add(conn, source="cbn", leads=["Actual Lead"], follows=["Other Investor"])
    row = event(conn, eid)
    assert json.loads(row["leads_json"]) == ["Actual Lead"]
    assert set(json.loads(row["follows_json"])) == {"Y Combinator", "Other Investor"}


def test_unknown_round_resolves_into_one_event(conn):
    pending, *_ = add(conn, round_text=None)
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["round_pending"]
    known, *_ = add(conn, source="cbn", round_text="Seed")
    assert pending == known
    assert not scan.collect_listing(conn, NOW.date(), weekly=True)["round_pending"]
    assert len(scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]) == 1


def test_baseline_nonempty_library_keeps_incremental_events(conn):
    add(conn, name="Baseline", website="https://baseline.example", baseline=True)
    assert not db.is_empty(conn)
    eid, *_ = add(conn, name="Incremental", website="https://incremental.example")
    assert [row["id"] for row in scan.pending_events(conn)] == [eid]


def test_company_merge_preserves_deepdive_pages_tags_and_digest_references(conn):
    old, *_ = add(conn, name="Original", website=None, baseline=True, now=NOW - timedelta(days=2))
    old_company = event(conn, old)["company_id"]
    known, *_ = add(conn, name="Alias", website="https://unified.example")
    known_company = event(conn, known)["company_id"]
    conn.execute("INSERT INTO deepdive(company_id, run_date, status) VALUES (?, '2026-10-29', '完成')", (old_company,))
    conn.execute("INSERT INTO deepdive_pages(company_id, run_date, page_no, url, fetched_at, body) "
                 "VALUES (?, '2026-10-29', 1, 'https://synthetic.example', '2026-10-29 09:00:00', '手写测试正文')", (old_company,))
    conn.execute("INSERT INTO tag_change(company_id, old, new, operator, changed_at) VALUES (?, '否', '是', '测试用户', '2026-10-29')", (old_company,))
    scan.upsert_digest(conn, {"date": "2026-10-29", "event_ids": [old, known]})
    add(conn, source="cbn", name="Original", website="https://unified.example")
    assert conn.execute("SELECT company_id FROM deepdive").fetchone()[0] == known_company
    assert conn.execute("SELECT company_id FROM deepdive_pages").fetchone()[0] == known_company
    assert conn.execute("SELECT company_id FROM tag_change").fetchone()[0] == known_company
    assert json.loads(conn.execute("SELECT event_ids_json FROM digest").fetchone()[0]) == [known]
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_merging_previously_pushed_company_does_not_requeue_inside_90_days(conn):
    old, *_ = add(conn, name="Original", website=None)
    scan.mark_listed(conn, [old], NOW.date())
    known, *_ = add(conn, name="Alias", website="https://unified.example")
    add(conn, source="cbn", name="Original", website="https://unified.example", now=NOW + timedelta(days=1))
    assert not scan.pending_events(conn)
    assert event(conn, known)["push_date"] == "2026-10-30"


def test_same_source_url_round_conflict_uses_higher_confidence(conn):
    report = "https://news.crunchbase.com/synthetic/round"
    eid, *_ = add(conn, source="tc_venture", round_text="Seed", url=report)
    add(conn, source="cbn", round_text="Series A", url=report)
    assert event(conn, eid)["round_norm"] == "A 轮"
    assert conn.execute("SELECT COUNT(*) FROM funding_event").fetchone()[0] == 1


def test_labels_keyword_only_in_body_are_retained_for_later_merge(conn):
    add(conn, body="This synthetic product uses social engineering in security.", ai=LabelDim("否", ""), social=LabelDim("否", ""))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["cards"][0].group == "标签待核"
    add(conn, source="email")
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].group == "标签待核"


def test_daily_workday_article_is_not_holiday_reissue(conn):
    add(conn, published=NOW - timedelta(days=1))
    assert "假期补发" not in scan.collect_listing(conn, NOW.date(), weekly=False, base_holiday_mark=True)["cards"][0].marks


def test_strictlyvc_fortune_article_is_other_media_not_term_sheet(conn):
    eid, *_ = add(conn, source="tc_venture", amount="$10M")
    add(conn, source="strictlyvc", amount="$11M", structured={"original_link": "https://fortune.com/synthetic/story"})
    assert event(conn, eid)["amount_text"] == "$10M"
    assert {source.name for source in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].sources} == {"TechCrunch", "StrictlyVC"}


def test_company_summary_is_not_rewritten_after_listing(conn):
    eid, *_ = add(conn, summary="已经发出的合成简介。")
    scan.mark_listed(conn, [eid], NOW.date())
    add(conn, source="cbn", summary="后来的合成简介。")
    assert conn.execute("SELECT summary FROM company").fetchone()[0] == "已经发出的合成简介。"


def test_yc_directory_dates_always_use_first_seen_not_published_or_launched(conn):
    eid, *_ = add(conn, source="yc_directory", published=NOW - timedelta(days=100), round_text=None,
                  structured={"yc_batch": "Fall 2026", "location": "New York, USA", "launched_at": 1})
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert event(conn, eid)["event_date"] == "2026-10-30"
    assert card.sources[0].beijing_date == NOW.date()


@pytest.mark.parametrize("reverse", [False, True])
def test_first_run_mixes_old_baseline_and_current_week_items_in_either_order(conn, reverse):
    inputs = [("tc_venture", NOW - timedelta(days=10)), ("cbn", NOW - timedelta(days=2))]
    if reverse:
        inputs.reverse()
    for source, published in inputs:
        item = RawItem(source, source, SOURCES[source].url, "首期合成报道", published)
        add(conn, source=source, published=published,
            baseline=scan.is_baseline_item(item, NOW.date(), first_run=True))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert len(listing["cards"]) == 1
    assert scan.baseline_company_count(conn) == 0
    assert listing["cards"][0].event_date == date(2026, 10, 28)
    assert listing["first_seen"]["example"] == NOW


def test_baseline_count_deduplicates_and_excludes_any_previously_listed_company(conn):
    add(conn, baseline=True)
    add(conn, source="cbn", baseline=True)
    add(conn, round_text="Series A", baseline=True)
    old, *_ = add(conn, name="Old Only", website="https://old-only.example", baseline=True)
    assert scan.baseline_company_count(conn) == 2
    current, *_ = add(conn, round_text="Series A")
    scan.mark_listed(conn, [current], NOW.date())
    assert scan.baseline_company_count(conn) == 1
    assert event(conn, old)["baseline_unpushed"] == 1
    assert not scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]


def test_first_seen_is_event_time_including_baseline_not_company_or_publication_date(conn):
    old = NOW - timedelta(days=10)
    seed, *_ = add(conn, now=old, published=old, baseline=True)
    add(conn, source="cbn", published=NOW - timedelta(days=1))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["first_seen"]["example"] == old
    scan.mark_listed(conn, [seed], NOW.date())
    add(conn, round_text="Series A", now=NOW + timedelta(days=1), published=NOW - timedelta(days=1))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["first_seen"]["example"] == NOW + timedelta(days=1)


def test_disputed_primary_uses_source_table_order_independent_of_publication(conn):
    add(conn, amount="$10M", now=NOW - timedelta(days=2), published=NOW - timedelta(days=3))
    add(conn, source="cbn", amount="$20M", now=NOW - timedelta(days=1), published=NOW - timedelta(days=4))
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "TechCrunch 写 1000 万美元，Crunchbase News 写 2000 万美元"
    assert card.amount_usd == 10e6
    assert [source.name for source in card.sources] == ["TechCrunch", "Crunchbase News"]


def test_disputed_primary_uses_source_table_order_before_raw_id_or_credibility(conn):
    add(conn, source="cbn", amount="$20M", published=NOW)
    add(conn, source="tc_venture", amount="$10M", published=NOW - timedelta(days=1))
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "TechCrunch 写 1000 万美元，Crunchbase News 写 2000 万美元"
    assert card.amount_usd == 10e6


def test_disputed_primary_is_undisclosed_when_first_source_has_no_amount(conn):
    add(conn, amount=None)
    add(conn, source="term_sheet", amount="$10M")
    add(conn, source="cbn", amount="$20M")
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "TechCrunch 写 金额未披露，Crunchbase News 写 2000 万美元，Term Sheet 写 1000 万美元"
    assert card.amount_usd is None
    assert "金额待核" in card.marks


def test_disputed_amount_does_not_use_larger_secondary_amount_for_big_mark(conn):
    add(conn, amount="$49M")
    add(conn, source="cbn", amount="$100M")
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.marks == ["金额待核"]
    assert card.amount_usd == 49e6


def test_all_four_v15_marks_keep_order(conn):
    seed, *_ = add(conn)
    scan.mark_listed(conn, [seed], NOW.date())
    add(conn, round_text="Series A", amount="$100M", backfill=True)
    add(conn, source="cbn", round_text="Series A", amount="$200M")
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].marks == ["新一轮", "大额", "补抓", "金额待核"]


def test_three_disclosed_sources_preserve_primary_and_evidence_of_actual_conflict(conn):
    add(conn, amount="$11M")
    add(conn, source="term_sheet", amount="$10M")
    add(conn, source="cbn", amount="$12M")
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "TechCrunch 写 1100 万美元，Crunchbase News 写 1200 万美元，Term Sheet 写 1000 万美元"
    assert card.amount_usd == 11e6
    assert "金额待核" in card.marks


def test_replacing_syndicated_source_preserves_its_first_ingestion_order(conn):
    original = "https://news.crunchbase.com/synthetic/original"
    add(conn, source="strictlyvc", amount="$10M", structured={"original_link": original},
        now=NOW - timedelta(days=4), published=NOW - timedelta(days=4))
    add(conn, source="term_sheet", amount="$20M", now=NOW - timedelta(days=3), published=NOW - timedelta(days=3))
    add(conn, source="cbn", amount="$10M", url=original,
        now=NOW - timedelta(days=2), published=NOW - timedelta(days=5))
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "Crunchbase News 写 1000 万美元，Term Sheet 写 2000 万美元"
    assert card.amount_usd == 10e6
    assert conn.execute("SELECT COUNT(*) FROM event_source").fetchone()[0] == 2


def test_listing_does_not_regroup_interleaved_tags(conn):
    for index, pair in enumerate([("否", "否"), ("是", "是"), ("否", "是"), ("是", "否")]):
        add(conn, name=f"Interleaved {index}", website=f"https://interleaved{index}.example",
            ai=LabelDim(pair[0], "手写依据。"), social=LabelDim(pair[1], "手写依据。"))
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert [card.name for card in listing["cards"]] == [f"Interleaved {index}" for index in range(4)]
    assert listing["filtered_total"] == 0


def test_company_merge_keeps_duplicate_source_first_seen_when_higher_credibility_copy_wins(conn):
    report = "https://news.crunchbase.com/synthetic/shared-report"
    first = NOW - timedelta(days=2)
    add(conn, source="tc_venture", name="Alias", website="https://unified.example", url=report,
        amount="$10M", now=first, published=first)
    add(conn, source="term_sheet", name="Alias", website="https://unified.example", amount="$20M",
        now=first + timedelta(hours=1), published=first + timedelta(hours=1))
    add(conn, source="cbn", name="Original", website=None, url=report, amount="$10M",
        now=first + timedelta(days=1), published=first)
    add(conn, source="email", name="Original", website="https://unified.example", amount=None)
    card = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0]
    assert card.amount_label == "Crunchbase News 写 1000 万美元，Term Sheet 写 2000 万美元"
    assert card.amount_usd == 10e6
    assert conn.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1
