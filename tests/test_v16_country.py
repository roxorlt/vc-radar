"""V16b O：用手写合成信息验证国家标注、历史兼容和完整名单。"""
from dataclasses import asdict, fields, replace
from datetime import timedelta
import json
import sqlite3

import pytest

from test_core_scan import BATCHES, NOW, SOURCES, add, conn, event
from test_pipe_pipeline import offline
from test_site_build import node, render_page
from vcradar import cli, db, deep, extract, pipeline, render, scan, site
from vcradar.config import Config
from vcradar.llm import MockLLMClient
from vcradar.models import DigestData, Extracted, RawItem


@pytest.mark.parametrize("location,country", [
    ("San Francisco, CA, USA; Remote", "美国"), ("NYC", "美国"), ("New York", "美国"),
    ("Boston", "美国"), ("Austin", "美国"), ("London", "英国"), ("Cambridge, UK", "英国"),
    ("Cambridge, Massachusetts", "美国"), ("London, KY", "美国"),
    ("Tel Aviv", "以色列"), ("Paris", "法国"), ("Toulouse", "法国"),
    ("Berlin", "德国"), ("Munich", "德国"), ("Toronto", "加拿大"),
    ("Montreal", "加拿大"), ("Vancouver", "加拿大"), ("Ottawa", "加拿大"), ("Calgary", "加拿大"),
    ("Waterloo, Canada", "加拿大"), ("Bangalore", "印度"), ("Bengaluru", "印度"),
    ("Mumbai", "印度"), ("Delhi", "印度"), ("Hyderabad", "印度"), ("Singapore", "新加坡"),
    ("Stockholm", "瑞典"), ("Amsterdam", "荷兰"), ("Dublin", "爱尔兰"), ("Zurich", "瑞士"),
    ("Sydney", "澳大利亚"), ("Melbourne", "澳大利亚"), ("São Paulo", "巴西"), ("Sao Paulo", "巴西"),
    ("Mexico City", "墨西哥"), ("Lagos", "尼日利亚"), ("Dubai", "阿联酋"),
    ("Copenhagen", "丹麦"), ("Helsinki", "芬兰"), ("Oslo", "挪威"),
    ("Madrid", "西班牙"), ("Barcelona", "西班牙"), ("Lisbon", "葡萄牙"),
    ("Milan", "意大利"), ("Rome", "意大利"), ("Beijing", "中国"), ("Shanghai", "中国"),
    ("Shenzhen", "中国"), ("Hong Kong", "中国香港"), ("Taipei", "中国台湾"),
    ("Tokyo", "日本"), ("Seoul", "韩国"), ("Nairobi", "肯尼亚"), ("Cape Town", "南非"),
    ("United Kingdom", "英国"), ("U.K.", "英国"), ("United Arab Emirates", "阿联酋"),
    ("New Zealand", "新西兰"), ("South Korea", "韩国"),
    ("Cambridge", "国家待确认"), ("Waterloo", "国家待确认"),
    ("North America", "国家待确认"), ("Remote", "国家待确认"), ("Unmapped City", "国家待确认"),
])
def test_location_country_table(location, country):
    item = RawItem("yc_directory", "place", "https://www.ycombinator.com/companies/synthetic", "地点样例", None,
                   structured={"location": location})
    assert scan.judge_country(item, Extracted())[0] == country


@pytest.mark.parametrize("location", [*scan._US_STATES, *scan._US_CODES])
def test_every_us_state_name_and_code(location):
    item = RawItem("news", "state", "https://news.example/state", "地点样例", None)
    qualified = location + ", USA" if location == "Georgia" else location
    assert scan.judge_country(item, Extracted(hq_text=qualified)) == ("美国", qualified)


@pytest.mark.parametrize("country,aliases", scan._COUNTRY_ALIASES.items())
def test_english_country_names_and_chinese_names(country, aliases):
    item = RawItem("news", "country", "https://news.example/country", "地点样例", None)
    for location in (country, *aliases):
        assert scan.judge_country(item, Extracted(hq_text=location))[0] == ("国家待确认" if location == "Georgia" else country)


@pytest.mark.parametrize("body,country", [
    ("Composal, a London-based company, raised a seed round.", "英国"),
    ("Composal is a Tel Aviv-based startup.", "以色列"),
    ("Composal, a Cambridge, UK-based company, raised a seed round.", "英国"),
    ("Composal, a Cambridge-based company, raised a seed round.", "国家待确认"),
    ("Composal is headquartered in Toulouse, with its team in New York.", "法国"),
    ("Composal is registered in Delaware but headquartered in London.", "英国"),
    ("Composal is registered in Delaware.", "国家待确认"),
    ("Composal raised money from a New York-based investor.", "国家待确认"),
    ("Composal was backed by London-based investors.", "国家待确认"),
    ("Composal is an early company. An investor is based in New York.", "国家待确认"),
    ("Composal received funding. The investor is headquartered in New York.", "国家待确认"),
])
def test_only_company_location_is_evidence(body, country):
    item = RawItem("alleywatch", "evidence", "https://www.alleywatch.com/synthetic", "融资消息", NOW, body=body)
    assert scan.judge_country(item, extract.rule_extract(item))[0] == country


def test_model_location_without_location_in_quote_is_not_evidence():
    item = RawItem("news", "guessed", "https://news.example/guessed", "融资消息", NOW,
                   body="Composal makes software for teams.")
    ext = Extracted(hq_text="London", hq_quote=item.body)
    assert scan.judge_country(item, ext)[0] == "国家待确认"


@pytest.mark.parametrize("location", ["North America", "San Francisco", "London"])
def test_x_posts_are_never_country_evidence(location):
    item = RawItem("x", "post", "https://x.com/synthetic/status/1", "公司消息", NOW,
                   body=f"Composal is headquartered in {location}.")
    assert scan.judge_country(item, Extracted(hq_text=location))[0] == "国家待确认"


def test_yc_first_location_and_empty_location(conn):
    add(conn, source="yc_directory", name="Jinko", website="https://jinko.example", hq=None, round_text=None,
        structured={"yc_batch": "Fall 2026", "location": ["Paris, France", "New York, USA"]})
    add(conn, source="yc_directory", name="Viraj Aero", website="https://viraj.example", hq=None, round_text=None,
        structured={"yc_batch": "Fall 2026", "location": ""})
    countries = {c.name: c.country for c in scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]}
    assert countries == {"Jinko": "法国", "Viraj Aero": "国家待确认"}


def test_foreign_company_after_first_issue_is_admitted_and_gets_deepdive(conn):
    now = NOW + timedelta(days=3)
    eid, *_ = add(conn, name="Composal", hq="London", now=now, published=now)
    card = scan.collect_listing(conn, now.date(), weekly=True)["cards"][0]
    assert (card.name, card.country) == ("Composal", "英国")
    assert "Composal｜英国｜种子轮" in render.render_card(card, "")
    assert not json.loads(event(conn, eid)["filter_reasons_json"])
    assert len(deep.select_companies(conn, now.date(), now=now)) == 1
    assert conn.execute("SELECT status FROM company").fetchone()[0] == "已纳入"


def test_unknown_country_is_a_card_and_does_not_block_deepdive(conn):
    add(conn, hq=None)
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert listing["cards"][0].country == "国家待确认"
    assert "hq_pending" not in listing
    assert "hq_pending" not in {f.name for f in fields(DigestData)}
    assert len(deep.select_companies(conn, NOW.date(), now=NOW)) == 1


@pytest.mark.parametrize("authoritative", ["official", "yc_directory"])
def test_country_conflicts_use_official_or_yc(conn, authoritative):
    add(conn, hq="London")
    add(conn, source="cbn", hq="Paris")
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].country == "国家待确认"
    row = conn.execute("SELECT * FROM company").fetchone()
    assert "London" in row["hq_basis"] and "Paris" in row["hq_basis"]
    add(conn, source=authoritative, hq="Berlin", structured={"location": "Berlin, Germany"})
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].country == "德国"


def test_legacy_evidence_is_rejudged_during_new_merge(conn):
    add(conn, hq="London")
    row = conn.execute("SELECT * FROM event_source").fetchone()
    data = json.loads(row["extraction_json"])
    data.pop("_country")
    data["_us"] = "非美国"
    conn.execute("UPDATE event_source SET extraction_json=?", (json.dumps(data),))
    add(conn, source="cbn", hq="London")
    assert scan.collect_listing(conn, NOW.date(), weekly=True)["cards"][0].country == "英国"
    assert not hasattr(scan, "judge_us")


def test_same_name_different_domains_keep_separate_countries(conn):
    add(conn, name="Phonic", website="https://phonic.ai", hq="London")
    add(conn, name="Phonic Inc.", website="https://phonic.example", hq="Paris")
    cards = scan.collect_listing(conn, NOW.date(), weekly=True)["cards"]
    assert len(cards) == 2 and {c.country for c in cards} == {"英国", "法国"}
    assert len({c.company_id for c in cards}) == 2


@pytest.mark.parametrize("country", ["英国", "美国", "国家待确认"])
def test_only_batch_and_round_filtering_remain(country):
    item = RawItem("yc_launches", "old", "https://news.example/old", "融资消息", NOW,
                   structured={"yc_batch": "Fall 2025"})
    assert scan.filter_reasons(item, Extracted(), country, "过滤", BATCHES) == ["批次超过 12 个月", "轮次不在范围"]
    assert scan.filter_reasons(replace(item, structured={}), Extracted(), country, "种子轮", BATCHES) == []


def test_new_company_status_never_uses_headquarters_or_filtered_state(conn):
    add(conn, name="No Location", website="https://no-location.example", hq=None)
    add(conn, name="No Round", website="https://no-round.example", hq=None, round_text=None)
    add(conn, name="Late Round", website="https://late-round.example", hq="London", round_text="Series B")
    rows = conn.execute("SELECT name,status FROM company").fetchall()
    assert {r["name"]: r["status"] for r in rows} == {
        "No Location": "已纳入", "No Round": "轮次待核", "Late Round": "已纳入"}


def test_old_schema_migrates_country_only_and_is_idempotent(tmp_path):
    path = tmp_path / "old.db"
    old_schema = db.SCHEMA_SQL.replace("country TEXT NOT NULL DEFAULT '国家待确认',", "")
    old_schema = old_schema.replace("status TEXT NOT NULL DEFAULT '已纳入'", "status TEXT NOT NULL DEFAULT '总部待核'")
    with sqlite3.connect(path) as old:
        old.executescript(old_schema)
        old.execute("INSERT INTO meta VALUES ('schema_version', '5')")
        old.executemany("INSERT INTO company(id,name_key,name,hq,hq_basis,first_seen,status) VALUES (?,?,?,?,?,?,?)", [
            ("london", "london", "Composal", "London", "Company is headquartered in London.", "2026-10-30", "总部待核"),
            ("empty", "empty", "Viraj Aero", None, "总部来源都没写", "2026-10-30", "总部待核"),
            ("basis", "basis", "Basis", None, "Cambridge, UK", "2026-10-30", "已纳入"),
            ("conflict", "conflict", "Conflict", "London", "总部来源有冲突", "2026-10-30", "总部待核"),
        ])
        old.execute("INSERT INTO funding_event(company_id,round_norm,event_date,first_seen,filter_reasons_json) "
                    "VALUES ('london','种子轮','2026-10-30','2026-10-30 08:40:00',?)", (json.dumps(["总部非美国"]),))
    with db.connect(path) as migrated:
        assert [(r["id"], r["country"], r["status"]) for r in migrated.execute("SELECT * FROM company ORDER BY id")] == [
            ("basis", "英国", "已纳入"), ("conflict", "国家待确认", "总部待核"),
            ("empty", "国家待确认", "总部待核"), ("london", "英国", "总部待核")]
        assert migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
        assert json.loads(migrated.execute("SELECT filter_reasons_json FROM funding_event").fetchone()[0]) == ["总部非美国"]
        migrated.execute("UPDATE company SET country='法国' WHERE id='london'")
    with db.connect(path) as again:
        assert again.execute("SELECT country,status FROM company WHERE id='london'").fetchone()[:] == ("法国", "总部待核")
        db.sync_sources(again, [SOURCES["tc_venture"]])
        add(again, name="New Entry", website="https://new-entry.example", hq=None)
        assert again.execute("SELECT status,country FROM company WHERE id='new-entry'").fetchone()[:] == ("已纳入", "国家待确认")
        assert len([r for r in again.execute("PRAGMA table_info(company)") if r[1] == "country"]) == 1


@pytest.mark.parametrize("hq,basis,country", [
    ("London", "手写旧字段依据", "英国"), ("London", "", "英国"),
    ("London", "An investor is based in London.", "国家待确认"),
    ("Delaware", "Composal is registered in Delaware.", "国家待确认"),
    (None, "Composal is headquartered in Paris.", "法国"),
])
def test_migration_hq_and_basis_follow_location_evidence(hq, basis, country):
    assert scan.country_from_hq(hq, basis) == country


def test_old_headquarters_pending_displays_card_without_mutating_status(conn):
    eid, *_ = add(conn, hq="London", round_text=None)
    conn.execute("UPDATE company SET status='总部待核'")
    listing = pipeline._listing_for_issue(conn, NOW.date())
    assert len(listing["cards"]) == 1 and not listing["round_pending"]
    assert listing["cards"][0].country == "国家待确认"
    assert listing["event_ids"] == [eid]
    assert conn.execute("SELECT status FROM company").fetchone()[0] == "总部待核"


def test_old_country_filters_hide_until_backfill_and_keep_other_reasons(conn):
    hidden, *_ = add(conn, name="Hidden", website="https://hidden.example", hq="London")
    other, *_ = add(conn, name="Other", website="https://other.example", hq="London", round_text="Series B")
    conn.execute("UPDATE funding_event SET filter_reasons_json=? WHERE id=?", (json.dumps(["总部非美国"]), hidden))
    conn.execute("UPDATE funding_event SET filter_reasons_json=? WHERE id=?",
                 (json.dumps(["总部非美国", "轮次不在范围", "批次超过 12 个月"]), other))
    add(conn, name="Hidden", website="https://hidden.example", source="cbn", hq="London")
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    assert not listing["cards"] and listing["event_ids"] == [other]
    assert listing["filtered"] == {"批次超过 12 个月": 1}
    assert listing["filtered_names"] == [("Other", "批次超过 12 个月")]
    assert json.loads(event(conn, hidden)["filter_reasons_json"]) == ["总部非美国"]
    assert len(json.loads(event(conn, other)["filter_reasons_json"])) == 3


@pytest.mark.parametrize("hq,round_text,calls", [("London", "Seed", 1), (None, "Seed", 1), ("London", "Series B", 0)])
def test_pipeline_model_eligibility_does_not_depend_on_country(conn, tmp_path, hq, round_text, calls):
    item = RawItem("tc_venture", "model", "https://techcrunch.com/synthetic", "Composal raises $5M", NOW,
                   structured={"company_name": "Composal", "hq_text": hq, "round_text": round_text, "amount_text": "$5M"})
    item_id, _ = scan.upsert_raw_item(conn, item, NOW, False)
    cfg = Config(state_dir=tmp_path, test_mode=True)
    clock = pipeline.Clock(NOW.date(), NOW.timetz())
    client = MockLLMClient([{"summary": "公司开发协作软件。供组织者使用。产品帮助安排活动。"}])
    assert not pipeline.ingest_items(conn, [(item_id, item, False)], cfg=cfg, clock=clock,
                                     budget=pipeline._Budget(cfg, clock), client=client, batches=BATCHES)
    assert client.calls == calls


def test_replay_model_path_includes_foreign_company(tmp_path, monkeypatch, capsys):
    payload = {"date": "2026-11-06", "items": [{
        "source_id": "tc_venture", "external_id": "foreign", "url": "https://techcrunch.com/synthetic",
        "title": "Composal raises $5M", "published_at": "2026-11-06T08:00:00+08:00",
        "structured": {"company_name": "Composal", "round_text": "Seed", "amount_text": "$5M", "hq_text": "London"},
    }]}
    fixture = tmp_path / "replay.json"
    fixture.write_text(json.dumps(payload))
    client = MockLLMClient([{"summary": "公司开发协作软件。供组织者使用。产品帮助安排活动。"}])
    monkeypatch.setattr("vcradar.replay.model_client", lambda *_: client)
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    args = cli.parse_args(["--replay", str(fixture), "--live-model", "--date", "2026-11-06"])
    from vcradar import replay
    assert replay.run_replay(cfg, args, pipeline.Clock(NOW.date(), NOW.timetz())) == 0
    assert client.calls == 1 and "本周共 1 家" in capsys.readouterr().out


def test_site_exports_country_and_legacy_lists_match_display(conn, tmp_path, monkeypatch):
    uk, *_ = add(conn, name="Composal", website="https://composal.example", hq="London")
    pending, *_ = add(conn, name="Viraj Aero", website="https://viraj.example", hq=None)
    hidden, *_ = add(conn, name="Hidden", website="https://hidden.example", hq="Paris")
    other, *_ = add(conn, name="Other", website="https://other.example", round_text="Series B")
    conn.execute("UPDATE company SET status='总部待核' WHERE name='Viraj Aero'")
    conn.execute("UPDATE funding_event SET filter_reasons_json=? WHERE id=?", (json.dumps(["总部非美国"]), hidden))
    conn.execute("UPDATE funding_event SET filter_reasons_json=? WHERE id=?", (json.dumps(["总部非美国", "轮次不在范围"]), other))
    ids = [uk, pending, hidden, other]
    scan.mark_listed(conn, ids, NOW.date())
    scan.upsert_digest(conn, {"date": NOW.date(), "push": True, "event_ids": ids, "generated_at": NOW,
                              "text": "标题\n上次推送后共 1 家 · 另有 1 家总部待核 · 已过滤 2 家"})
    monkeypatch.setattr(site, "LINK_RECORDS", tmp_path / "missing-links.json")
    path = site.build_site(conn, Config(state_dir=tmp_path, test_mode=True), out_dir=tmp_path / "site", now=NOW)
    data = json.loads((path / "data.json").read_text())
    assert "hq_pending" not in data["week"]
    assert set(data["week"]["cards"]) == {uk, pending}
    assert data["week"]["filtered"] == [other]
    assert data["week"]["filtered_counts"] == {"B 轮及以后": 1}
    assert hidden not in data["listing"] and hidden not in data["week"]["event_ids"]
    company = next(c for c in data["companies"] if c["id"] == "composal")
    assert company["country"] == "英国" and "London" in company["country_basis"]
    detail = json.loads((path / "c/composal.json").read_text())
    assert detail["events"][0]["country"] == "英国"
    assert data["status"]["pending_ratios"]["country"] == {"count": 1, "percent": 50.0}
    week = render_page(path, "weekHtml")
    assert "上次推送后共 2 家" in week
    assert "英国" in week and "总部待核" not in week and "总部非美国" not in week
    assert "已过滤 1 家" in week
    html = render_page(path, "detailHtml", "composal")
    assert "国家和依据</dt>" in html and "英国 · 伦敦 · " in html and "London" in html
    assert "总部</dt>" not in html and "总部依据</dt>" not in html
    options = node("const ui=require(process.argv[1]),fs=require('fs'); const d=JSON.parse(fs.readFileSync(process.argv[2]));"
                   "console.log(JSON.stringify(ui.listHtml(d,{period:'all',tag:'all',round:'all',status:'all'})));", path / "data.json")
    assert "总部待核" not in options


def test_push_highlight_omits_country_and_headquarters_pending_count(conn):
    add(conn, name="Composal", hq="London")
    listing = scan.collect_listing(conn, NOW.date(), weekly=True)
    data = DigestData(NOW.date(), None, NOW, "有新增", cards=listing["cards"])
    text = render.render_push(data)
    assert "英国" not in text and "总部待核" not in text
    assert "country" in asdict(data.cards[0])
