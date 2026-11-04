"""R14e / PRD V17c：信源、快照、报警和卡片契约的离线合成回归。"""
from dataclasses import replace
from datetime import date, datetime, time as day_time, timedelta, timezone
import json
import re

import pytest

from vcradar import db, deep, fetcher, fx, homepage, http, outbox, pipeline, render, scan, site, wording
from vcradar.config import BJ, Config
from vcradar.models import DigestData, ErrorType, FetchOutcome, FetchResult, FetchRunRecord, PushSection, SourceState, SourceStatusLine
from vcradar.sources import registry
from test_core_v15e import add_item
from test_er_render import card
from test_pipe_pipeline import arguments, offline, run_at, write_fixture  # noqa: F401
from test_r14c_rulings import COUNT_KEYS, STATUS_KEYS, page_html
from test_site_build import add_company, add_deep, add_event, node, render_page
from test_sources_support import SOURCES, fetch
from test_site_build import html_text, summary_titles


BASELINE = datetime(2026, 10, 31, 0, 36, tzinfo=BJ)
FRIDAY = datetime(2026, 11, 6, 8, 40, tzinfo=BJ)


@pytest.fixture
def catalog(tmp_path):
    cfg = Config(state_dir=tmp_path, links_file=tmp_path / "links.json")
    cfg.links_file.write_text("[]")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, registry.P0_SOURCES)
    yield conn, cfg
    conn.close()


def digest_data(**overrides):
    values = dict(push_date=FRIDAY.date(), window_start=BASELINE, window_end=FRIDAY,
                  status="有新增", cards=[card()], baseline_at=BASELINE)
    values.update(overrides)
    return DigestData(**values)


def digest_row(conn, day="2026-11-06"):
    return dict(conn.execute("SELECT * FROM digest WHERE date=?", (day,)).fetchone())


def extra_source(conn, eid, *, source_id="tc_venture", name="TechCrunch", published="2026-11-06T01:00:00Z"):
    rid = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM raw_item").fetchone()[0]
    url = f"https://synthetic.example/r14e-{rid}"
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, source_id, f"r14e-{rid}", url, "手写融资来源", published, "2026-11-06 08:50:00"))
    conn.execute("INSERT INTO event_source VALUES (?,?,?,?,?,1,NULL)", (eid, rid, url, name, "{}"))
    return url


@pytest.mark.parametrize("source_id,rank,name", [
    ("tc_venture", 0, "TechCrunch"), ("tc_startups", 0, "TechCrunch"),
    ("cbn_main", 1, "Crunchbase News"), ("cbn_seed", 1, "Crunchbase News"),
    ("cbn_venture", 1, "Crunchbase News"), ("yc_launches", 2, "YC Launches"),
    ("yc_directory", 3, "YC 公司目录"), ("launch_hn", 4, "Launch HN"),
    ("strictlyvc", 5, "StrictlyVC"), ("alleywatch", 6, "AlleyWatch"),
    ("ts_web", 7, "Term Sheet"), ("future_source", 8, "future_source"),
])
def test_e1_registry_display_order_is_separate_from_fetch_priority(source_id, rank, name):
    assert registry.display_rank(source_id) == rank
    assert registry.display_name(source_id) == name
    assert SOURCES["ts_web"].order == 0
    assert tuple(pipeline._SOURCE_ORDER) == registry.DISPLAY_ORDER


def test_e1_source_id_controls_primary_amount_links_homepage_and_deep(catalog):
    conn, cfg = catalog
    eid, strict_id = add_item(conn, "strictlyvc", amount="$90M", seen=FRIDAY - timedelta(days=1))
    add_item(conn, "alleywatch", amount="$10M", seen=FRIDAY)
    add_item(conn, "ts_web", amount="$10M", seen=FRIDAY)
    _, tc_id = add_item(conn, "tc_venture", amount="$10M", seen=FRIDAY + timedelta(minutes=1))
    original_url = "https://techfundingnews.com/synthetic-r14e"
    conn.execute("UPDATE event_source SET source_name='techfundingnews.com',url=? WHERE raw_item_id=?", (original_url, strict_id))
    row = conn.execute("SELECT * FROM funding_event WHERE id=?", (eid,)).fetchone()
    scan._refresh_event(conn, row, "美国")
    listing = scan.collect_listing(conn, FRIDAY.date(), weekly=True)
    company_card = listing["cards"][0]
    assert [s.name for s in company_card.sources] == ["TechCrunch", "StrictlyVC", "AlleyWatch", "Term Sheet"]
    assert company_card.sources[1].url == original_url
    assert company_card.amount_usd == 10_000_000
    assert company_card.amount_label == "TechCrunch 写 1000 万美元，StrictlyVC 写 9000 万美元"
    assert f"[来源]({company_card.sources[0].url})" in render.render_push(digest_data(cards=[company_card]))
    evidence = scan._evidence_rows(conn, event_id=eid)
    # 原文站点即使碰巧也叫 TechCrunch，仍然排在它所属的 StrictlyVC 位置。
    next(r for r in evidence if r["raw_item_id"] == strict_id)["source_name"] = "TechCrunch"
    assert [r["raw_item_id"] for r in sorted(evidence, key=scan._source_order)][:2] == [tc_id, strict_id]
    assert homepage.company_context(conn, "synthetic", cfg)["source_url"] == company_card.sources[0].url
    context = deep.select_companies(conn, FRIDAY.date(), company_ids=["synthetic"], now=FRIDAY)[0]
    assert [s["source_id"] for s in context["sources"]] == ["tc_venture", "strictlyvc", "alleywatch", "ts_web"]
    source = site._sources(conn)[eid][1]
    assert source["name"] == "StrictlyVC" and source["origin_name"] == "techfundingnews.com"
    snapshot = site._snapshot(conn, cfg, FRIDAY)
    assert snapshot["events"][0]["amount"] == company_card.amount_label
    index, _ = site._site_payloads(snapshot)
    assert "origin_name" not in json.dumps(index)


def test_e1_same_source_uses_ingestion_time_and_alarm_groups_keep_minimum_rank(catalog):
    conn, _ = catalog
    eid, later = add_item(conn, "tc_venture", seen=FRIDAY + timedelta(minutes=2))
    _, earlier = add_item(conn, "tc_startups", seen=FRIDAY)
    assert [r["raw_item_id"] for r in sorted(scan._evidence_rows(conn, event_id=eid), key=scan._source_order)] == [earlier, later]
    sources = [SOURCES[sid] for sid in ("ts_web", "tc_startups", "strictlyvc", "tc_venture", "alleywatch")]
    outcomes = [FetchOutcome(s, [], [], False, ErrorType.TIMEOUT) for s in sources]
    lines = pipeline._source_lines(outcomes, {s.id: SourceState(s.id) for s in sources})
    assert [registry.display_rank(s.source_id) for s in lines] == [0, 5, 6, 7]
    assert lines[0].source_id in {"tc_startups", "tc_venture"}
    # 有 source_id 时不回退到名字猜序号；旧调用方没有它时照样按信源名排序。
    lines = [SourceStatusLine("原文甲", False, False, "", ErrorType.TIMEOUT, source_id="strictlyvc"),
             SourceStatusLine("原文乙", False, False, "", ErrorType.TIMEOUT, source_id="tc_venture")]
    assert render.fetch_warning(digest_data(sources=lines)).startswith("原文乙、原文甲没抓到：")
    legacy = [SourceStatusLine("Term Sheet", False, False, "", ErrorType.TIMEOUT),
              SourceStatusLine("TechCrunch", False, False, "", ErrorType.TIMEOUT)]
    assert render.fetch_warning(digest_data(sources=legacy)).startswith("TechCrunch、Term Sheet 没抓到：")


def test_e2_51_52_first_issue_and_non_push_snapshots(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31")
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    with db.connect(cfg.db_path) as conn:
        first = digest_row(conn, "2026-10-31")
        frozen = json.loads(first["card_sources_json"])
        assert set(frozen) == {str(eid) for eid in json.loads(first["event_ids_json"])}
        assert frozen and all(set(s) == {"name", "url", "published_at"} for sources in frozen.values() for s in sources)
        assert all(s["published_at"] is None or re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", s["published_at"]) for sources in frozen.values() for s in sources)
        yc = conn.execute("SELECT event_id FROM event_source s JOIN raw_item r ON r.id=s.raw_item_id WHERE r.source_id='yc_directory'").fetchone()[0]
        assert pipeline._card_source_snapshot(conn, [yc])[str(yc)][0]["published_at"] is None
    write_fixture(fixture, "2026-11-02", new=["Monday"])
    run_at(cfg, fixture, "2026-11-02", no_deep=True)
    with db.connect(cfg.db_path) as conn:
        row = digest_row(conn, "2026-11-02")
        assert json.loads(row["card_sources_json"]) == json.loads(row["counts_json"]) == {}
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "15"


@pytest.mark.parametrize("failure", [False, True], ids=["normal-rerun", "failure-rerun"])
def test_e2_52_unsent_rerun_replaces_snapshot_and_resend_keeps_it(tmp_path, monkeypatch, failure):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06")
    run_at(cfg, fixture, "2026-11-06", no_deep=True)
    with db.connect(cfg.db_path) as conn:
        before = digest_row(conn)
        eid = json.loads(before["event_ids_json"])[0]
        url = extra_source(conn, eid)
    if failure:
        def fail(*args, **kwargs):
            raise RuntimeError("合成抓取故障")
        monkeypatch.setattr(fetcher, "fetch_all", fail)
        assert pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40))) == 1
    else:
        run_at(cfg, fixture, "2026-11-06", no_deep=True)
    with db.connect(cfg.db_path) as conn:
        after = digest_row(conn)
        frozen = json.loads(after["card_sources_json"])
        assert any(s["url"] == url for s in frozen[str(eid)])
        assert before["card_sources_json"] != after["card_sources_json"]
        extra_source(conn, eid, source_id="cbn_main", name="Crunchbase News")
    run_at(cfg, fixture, "2026-11-06", resend=True, no_deep=True)
    with db.connect(cfg.db_path) as conn:
        assert digest_row(conn)["card_sources_json"] == after["card_sources_json"]
        if failure:
            pipeline._failure_file(conn, cfg, arguments(resend=True), pipeline.Clock(FRIDAY.date(), day_time(8, 45)), FRIDAY.date())
            assert digest_row(conn)["card_sources_json"] == after["card_sources_json"]


@pytest.mark.parametrize("push", [False, True], ids=["non-push", "push-with-supplement"])
def test_e2_e5_failure_snapshot_and_counts_include_supplement_only_on_push(catalog, push):
    conn, cfg = catalog
    add_company(conn, "ordinary", "Ordinary")
    add_event(conn, 1, "ordinary", pushed=None)
    add_company(conn, "supplement", "Supplement")
    add_event(conn, 2, "supplement", pushed=None)
    conn.execute("UPDATE company SET backfill='补充未推送',backfill_rank=1 WHERE id='supplement'")
    conn.execute("UPDATE funding_event SET original_first_ingested_at=first_ingested_at WHERE id=2")
    conn.execute("UPDATE funding_event SET pending=1")
    day = FRIDAY.date() if push else date(2026, 11, 3)
    pipeline._failure_file(conn, cfg, arguments(), pipeline.Clock(day, day_time(8, 40)), day)
    row = digest_row(conn, str(day))
    frozen, counts = json.loads(row["card_sources_json"]), json.loads(row["counts_json"])
    if push:
        assert json.loads(row["event_ids_json"]) == [1]
        assert json.loads(row["supplement_event_ids_json"]) == [2]
        assert set(frozen) == {"1", "2"} and all(set(s) == {"name", "url", "published_at"} for sources in frozen.values() for s in sources)
        assert set(counts) == COUNT_KEYS and counts["supplement_m"] == 1 and counts["supplement_k"] == 0
        assert "1. Supplement" in row["text"]
    else:
        assert frozen == counts == {}


def test_e3_e4_54_backfill_is_per_event_and_unsent_badge_has_no_reason(catalog):
    conn, cfg = catalog
    add_company(conn, "returning", "Returning")
    conn.execute("UPDATE company SET backfill='首期漏掉' WHERE id='returning'")
    for eid, pushed in [(1, "2026-11-06"), (2, "2026-11-13"), (3, None)]:
        add_event(conn, eid, "returning", pushed=pushed, round_norm="种子轮" if eid == 1 else "A 轮")
    conn.execute("UPDATE funding_event SET original_first_ingested_at=first_ingested_at WHERE id=1")
    add_company(conn, "waiting", "Waiting", status="轮次待核")
    add_event(conn, 4, "waiting", pushed=None, round_norm="待核")
    add_company(conn, "unsent", "Unsent", status="轮次待核")
    add_event(conn, 5, "unsent", pushed=None, round_norm="待核")
    conn.execute("UPDATE company SET backfill='补充未推送',backfill_rank=11,pending_reason='不应显示的旧原因' WHERE id='unsent'")
    conn.execute("UPDATE funding_event SET original_first_ingested_at=first_ingested_at WHERE id=5")
    scan.upsert_digest(conn, {"date": "2026-11-06", "push": True, "event_ids": [], "supplement_event_ids_json": [1], "generated_at": FRIDAY})
    scan.upsert_digest(conn, {"date": "2026-11-13", "push": True, "event_ids": [2], "generated_at": FRIDAY + timedelta(days=7)})
    path = site.build_site(conn, cfg, now=FRIDAY + timedelta(days=7, hours=1))
    data = json.loads((path / "data.json").read_text())
    events = {e["id"]: e for e in data["events"]}
    assert events[1]["backfill"] == "此前漏掉"
    assert events[2]["backfill"] == events[3]["backfill"] == ""
    assert events[5]["status"] == "补充未推送" and events[5]["pending_reason"] == events[5]["backfill"] == ""
    assert not events[5]["next_push_label"]
    selected = node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                    "console.log(JSON.stringify(ui.selectEvents(d,{period:'all',tag:'all',round:'all',status:'all',backfill:true}).map(e=>e.id)));", path / "data.json")
    assert selected == [1]
    detail = json.loads((path / "c/returning.json").read_text())["companies"][0]
    assert detail["backfill"] == "此前漏掉"
    html = page_html(path)
    waiting = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if ">Waiting</a>" in a)
    assert waiting.index("轮次待确认 · 来源没写轮次") < waiting.index("下次推送：") < waiting.index('class="co-fund"')
    unsent = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if ">Unsent</a>" in a)
    assert '<span class="state-flag">补充未推送</span>' in unsent
    assert not any(word in unsent for word in ("此前漏掉", "下次推送", "不应显示的旧原因"))
    # 兼容旧导出数据：前端也要抑制补充未推送卡片残留的原因和日期。
    events[5].update(backfill="此前漏掉", pending_reason="不应显示的旧原因", next_push_label="下次推送：错误日期")
    (path / "data.json").write_text(json.dumps(data))
    unsent = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', page_html(path), re.S) if ">Unsent</a>" in a)
    assert '<span class="state-flag">补充未推送</span>' in unsent
    assert not any(word in unsent for word in ("此前漏掉", "下次推送", "不应显示的旧原因"))


@pytest.mark.parametrize("kind", ["first", "new", "supplement", "empty", "non-push"])
def test_e5_51_counts_have_thirteen_keys_and_inapplicable_nulls(kind):
    extra = [PushSection("backfill", wording.BACKFILL_TITLE, "", [card("Supplement")], overflow_count=3)] if kind == "supplement" else []
    data = digest_data(cards=[] if kind == "empty" else [card(marks=["新一轮", "大额"])], baseline_count=9, extra_sections=extra)
    counts = pipeline._digest_counts(data, {"filtered_total": 2}, push=kind != "non-push", failed_days=1,
                                     first_issue=kind == "first", incomplete_until=date(2026, 10, 29))
    if kind == "non-push":
        assert counts == {}
        return
    assert set(counts) == COUNT_KEYS
    assert counts["baseline"] == (9 if kind == "first" else None)
    assert counts["incomplete_until"] == ("2026-10-29" if kind == "first" else None)
    assert counts["supplement_m"] == (1 if kind == "supplement" else None)
    assert counts["supplement_k"] == (3 if kind == "supplement" else None)
    assert counts["admitted"] == (0 if kind == "empty" else 1)
    assert counts["filtered"] == 2 and counts["failed_days"] == 1


def test_e5_51_folded_yc_companies_do_not_count_as_admitted(catalog):
    conn, _ = catalog
    for eid in range(1, 36):
        cid = f"yc-{eid}"
        add_company(conn, cid, f"Yc {eid}")
        add_event(conn, eid, cid, pushed=None, source="yc_directory")
    conn.execute("UPDATE raw_item SET published_at=NULL,structured_json=?", (json.dumps({"yc_batch": "Fall 2026"}),))
    conn.execute("UPDATE funding_event SET yc_batch='Fall 2026'")
    conn.execute("UPDATE funding_event SET pending=1")
    listing = scan.collect_listing(conn, FRIDAY.date(), weekly=True)
    assert listing["yc_new_batch"] == ("Fall 2026", 35) and listing["cards"] == []
    counts = pipeline._digest_counts(digest_data(cards=listing["cards"]), listing, push=True)
    assert counts["admitted"] == 0


@pytest.mark.parametrize("snapshot_failure", [False, True])
def test_e6_k1_50_status_is_always_written_with_all_eighteen_keys(tmp_path, monkeypatch, snapshot_failure):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06")
    writes = []
    original_write, original_snapshot = outbox.write_atomic, site._snapshot
    def capture(path, value):
        if path.name == "status.json":
            writes.append(value)
        return original_write(path, value)
    attempts = 0
    def snapshot(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if snapshot_failure and attempts == 1:
            raise RuntimeError("合成状态快照故障")
        return original_snapshot(*args, **kwargs)
    monkeypatch.setattr(outbox, "write_atomic", capture)
    monkeypatch.setattr(site, "_snapshot", snapshot)
    monkeypatch.setattr(pipeline, "_after_handoff", lambda *args: None)
    result = pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40)))
    assert result == (1 if snapshot_failure else 0)
    assert len(writes) == 1 and set(writes[0]) == STATUS_KEYS
    status = json.loads((cfg.state_dir / "test/status.json").read_text())
    assert set(status["fx"]) in ({"date", "source", "ok", "message"}, {"date", "source", "ok", "message", "currencies"})
    assert fx.status_from_files(cfg) == status["fx"]
    assert status["date"] == "2026-11-06" and status["outbox"]


def test_n17b06_50_deep_progress_matches_today_table_at_each_export(catalog):
    conn, cfg = catalog
    cfg.test_mode = True
    statuses = ["完成", "生成中", "失败", "失败", "失败", "失败", "暂时没有", "不做", None]
    for eid, status in enumerate(statuses, 1):
        cid = f"progress-{eid}"
        add_company(conn, cid, f"Progress {eid}")
        add_event(conn, eid, cid, pushed=None, seen="2026-11-06 08:41:00")
        if status:
            add_deep(conn, cid, status, run_date="2026-11-06")
    for eid, error in zip(range(3, 7), ["company_timeout", "worker_failed", "stale_run", "superseded"]):
        conn.execute("UPDATE deepdive SET error=? WHERE company_id=?", (error, f"progress-{eid}"))
    add_company(conn, "previous", "Previous")
    add_event(conn, 10, "previous", pushed=None)
    add_deep(conn, "previous", "完成", run_date="2026-11-05")
    for hour, expected in [(9, {"total": 8, "done": 1, "running": 1, "failed": 4, "timeout": 1, "skipped": 1}),
                           (10, {"total": 8, "done": 1, "running": 0, "failed": 4, "timeout": 2, "skipped": 1})]:
        if hour == 10:
            conn.execute("UPDATE deepdive SET status='暂时没有',error='final_deadline' WHERE status='生成中'")
        site.build_site(conn, cfg, now=FRIDAY.replace(hour=hour, minute=30), snapshot_slot=f"{hour:02}30")
        path = cfg.state_dir / f"test-site/2026-11-06/{hour:02}30"
        status = json.loads((path / "status.json").read_text())
        assert set(status) == STATUS_KEYS
        assert status["deepdive_progress"] == {"date": "2026-11-06", **expected}
        assert sum(expected[key] for key in ("done", "running", "failed", "timeout")) <= expected["total"]


@pytest.mark.parametrize("start,end,baseline,expected", [
    (BASELINE - timedelta(days=2), BASELINE - timedelta(seconds=1), BASELINE, False),
    (BASELINE - timedelta(days=2), BASELINE, BASELINE, True),
    (BASELINE - timedelta(days=2), FRIDAY, BASELINE, True),
    (BASELINE - timedelta(seconds=1), None, BASELINE, False),
    (BASELINE, None, BASELINE, True), (FRIDAY, None, BASELINE, True),
    (None, BASELINE - timedelta(seconds=1), BASELINE, False),
    (None, FRIDAY, BASELINE, True), (None, None, BASELINE, True),
    (BASELINE - timedelta(days=2), None, None, True),
])
def test_e8_48_has_missed_and_mark_missed_use_endpoint_then_start(start, end, baseline, expected):
    assert fetcher.has_missed("合成漏抓段", baseline, start, end) is expected
    session = fetcher._Session(SOURCES["tc_venture"], SourceState("tc_venture", baseline_at=baseline), None, None, FRIDAY, None, None)
    session.mark_missed(end, start)
    assert bool(session.stats.missed) is expected
    if expected:
        assert session.stats.missed_end == end
        assert session.stats.missed_start == (max(start, baseline) if start and baseline else start)
    assert not fetcher.has_missed("", baseline)
    if start or end:
        assert fetcher.has_missed("", baseline, start, end) is expected


@pytest.mark.parametrize("start,end,expected", [
    (datetime(2026, 10, 29, 10, tzinfo=BJ), datetime(2026, 11, 1, 9, tzinfo=BJ), "10-31 00:36 到 11-01 09:00 的内容没补上"),
    (datetime(2026, 11, 4, 19, 56, 1, tzinfo=BJ), datetime(2026, 11, 4, 19, 56, 59, tzinfo=BJ), "11-04 19:56 的内容没补上"),
    (None, datetime(2026, 11, 4, 19, 56, tzinfo=BJ), "11-04 19:56 之前的内容没补上"),
    (datetime(2026, 11, 4, 19, 56, tzinfo=BJ), None, "11-04 19:56 之后的内容没补上"),
    (None, None, "有一段时间的内容没补上"),
])
def test_e7_e8_48_49_missed_reason_clips_and_formats_without_parentheses(start, end, expected):
    source = SourceStatusLine("StrictlyVC", True, False, "", missed_range="合成缺期", missed_start=start, missed_end=end)
    text = render.fetch_warning(digest_data(sources=[source]))
    assert text == "StrictlyVC 没抓到：" + expected + wording.FETCH_WARNING_END
    assert not any(char in text for char in "()（）")


def newsletter_case(tmp_path, source_id, missing_times, *, fail_selected=False):
    strict = source_id == "strictlyvc"
    times = [FRIDAY - timedelta(hours=1), datetime(2026, 11, 5, 19, tzinfo=BJ),
             datetime(2026, 11, 5, 8, tzinfo=BJ), *missing_times]
    urls = [f"https://newsletter.strictlyvc.com/p/r14e-{i}" if strict else
            f"https://content.fortune.com/newsletter/termsheet/?post_id={800 + i}" for i in range(len(times))]
    if strict:
        listing = "<urlset>" + "".join(f"<url><loc>{url}</loc>" + (f"<lastmod>{stamp.isoformat()}</lastmod>" if stamp else "") + "</url>" for url, stamp in zip(urls, times)) + "</urlset>"
    else:
        listing = json.dumps([{"id": 800 + i, **({"date_gmt": stamp.astimezone(timezone.utc).isoformat()} if stamp else {})} for i, stamp in enumerate(times)])
    overrides = {SOURCES[source_id].url: {"content_type": "application/xml" if strict else "application/json", "body": listing}}
    for url, stamp in zip(urls, times):
        body = ('<script type="application/ld+json">{"datePublished":"' + stamp.isoformat() + '"}</script>') if strict and stamp else ""
        body += "<h2>Smaller Fundings</h2>" if strict else "<h2>VENTURE DEALS</h2>"
        body += "<p>Synthetic, a Boston-based software company, raised $2M in seed funding.</p>"
        overrides[url] = {"content_type": "text/html", "body": body}
    if fail_selected:
        overrides[urls[1]] = {"status": 503}
    state = SourceState(source_id, known_issue_urls={urls[0]}, baseline_at=BASELINE)
    return fetch(tmp_path, source_id, overrides, state=state, now=FRIDAY)[0], state


@pytest.mark.parametrize("source_id", ["ts_web", "strictlyvc"])
@pytest.mark.parametrize("missing_times,span", [
    ([datetime(2026, 10, 23, 17, 28, tzinfo=BJ), datetime(2026, 10, 28, 9, tzinfo=BJ)], None),
    ([datetime(2026, 11, 4, 19, 56, tzinfo=BJ)], "11-04 19:56"),
    ([datetime(2026, 11, 4, 19, 56, tzinfo=BJ), datetime(2026, 11, 3, 8, tzinfo=BJ)], "11-03 08:00 到 11-04 19:56"),
    ([None], "unknown"),
], ids=["before-baseline", "single", "multiple", "unknown"])
def test_e8_n17b04_48_newsletter_range_uses_only_missing_issues(tmp_path, source_id, missing_times, span):
    outcome, state = newsletter_case(tmp_path, source_id, missing_times)
    assert outcome.ok
    if span is None:
        assert outcome.missed_range == "" and outcome.missed_start is outcome.missed_end is None
    else:
        known = [stamp for stamp in missing_times if stamp]
        assert outcome.missed_start == (min(known) if known else None)
        assert outcome.missed_end == (max(known) if known else None)
        if not known:
            assert outcome.missed_range == "时间未知 ~ 时间未知（北京时间）"
        line = pipeline._source_lines([outcome], {source_id: state})[0]
        reason = "有一段时间的内容没补上" if span == "unknown" else span + " 的内容没补上"
        assert render.fetch_warning(digest_data(sources=[line])) == registry.display_name(source_id) + " 没抓到：" + reason + wording.FETCH_WARNING_END
    assert bool(render.is_alarm(digest_data(sources=pipeline._source_lines([outcome], {source_id: state})))) is (span is not None)


def test_e8_newsletter_failed_page_combines_all_unfetched_pending_issues(tmp_path):
    earliest = datetime(2026, 11, 3, 8, tzinfo=BJ)
    outcome, _ = newsletter_case(tmp_path, "ts_web", [earliest], fail_selected=True)
    assert not outcome.ok
    assert outcome.missed_start == earliest
    assert outcome.missed_end == datetime(2026, 11, 5, 19, tzinfo=BJ)


@pytest.mark.parametrize("start,end,expected", [
    (datetime(2026, 10, 28, 9, tzinfo=BJ), None, False),
    (datetime(2026, 11, 4, 19, 56, tzinfo=BJ), None, True),
    (datetime(2026, 10, 23, 17, 28, tzinfo=BJ), datetime(2026, 10, 30, 19, 56, tzinfo=BJ), False),
    (datetime(2026, 10, 29, 10, tzinfo=BJ), datetime(2026, 11, 1, 9, tzinfo=BJ), True),
    (None, None, True),
])
def test_e8_48_structured_range_drives_fetch_run_status_alarm_and_failed_days(catalog, start, end, expected):
    conn, cfg = catalog
    scan.upsert_digest(conn, {"date": BASELINE.date(), "push": True, "status": "基线", "generated_at": BASELINE}, first_issue=True)
    stamp = datetime(2026, 11, 4, 8, 40, tzinfo=BJ)
    state = SourceState("strictlyvc", baseline_at=BASELINE)
    session = fetcher._Session(SOURCES["strictlyvc"], state, None, None, stamp, None, None)
    session.mark_missed(end, start)
    stats = session.stats
    run = FetchRunRecord("strictlyvc", stamp, stamp, 200, 1, FetchResult.SUCCESS,
                         missed_range=stats.missed, missed_start=stats.missed_start, missed_end=stats.missed_end)
    outcome = FetchOutcome(SOURCES["strictlyvc"], [run], [], True, missed_range=stats.missed,
                           missed_start=stats.missed_start, missed_end=stats.missed_end)
    scan.record_fetch(conn, "r14e-missed", outcome, True)
    row = conn.execute("SELECT missed_range,missed_start,missed_end FROM fetch_run").fetchone()
    assert bool(row["missed_range"]) is expected
    if expected and start:
        assert site._moment(row["missed_start"]) == max(start, BASELINE)
    if end is None or not expected:
        assert row["missed_end"] is None
    else:
        assert site._moment(row["missed_end"]) == end
    for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
        scan.upsert_digest(conn, {"date": day, "push": False, "status": "没有新增"})
        outbox.write_atomic(cfg.outbox_dir / f"{day}.json", {"push": False})
    assert bool(site.write_status(conn, cfg, FRIDAY)["missed"]) is expected
    data = digest_data(sources=pipeline._source_lines([outcome], {"strictlyvc": state}))
    assert render.is_alarm(data) is expected
    assert scan.failed_days_in_window(conn, BASELINE, FRIDAY.date(), outbox_dir=cfg.outbox_dir) == int(expected)
    if start == datetime(2026, 11, 4, 19, 56, tzinfo=BJ) and end is None:
        assert render.fetch_warning(data) == "StrictlyVC 没抓到：11-04 19:56 之后的内容没补上" + wording.FETCH_WARNING_END


@pytest.mark.parametrize("failure_path", [False, True])
def test_e5_e8_48_non_push_timeout_counts_one_failed_day_without_future_alarm(tmp_path, monkeypatch, failure_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31", old=True)
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    # 三个地址各消耗 25 秒，推进模拟钟而不真实睡眠，触发 60 秒总预算。
    original_client, original_get = http.FixtureTransport, pipeline._Transport.get
    class TimedFixture(original_client):
        def __init__(self, path):
            super().__init__(path)
            self.delays = {url: spec.pop("delay_s", 0) for url, spec in self.manifest.items()}

        def get(self, url, headers=None, timeout=30.0, data=None):
            delay = max((self.delays[key] for key in self.delays if url.startswith(key)), default=0)
            if delay > timeout:
                return http.HttpResponse(url, None, elapsed_s=timeout, error="timeout")
            return replace(super().get(url, headers, timeout, data), elapsed_s=delay)

    def timed_get(self, *args, **kwargs):
        result = original_get(self, *args, **kwargs)
        self.budget.clock.advance(result.elapsed_s)
        return result

    monkeypatch.setattr(http, "FixtureTransport", TimedFixture)
    monkeypatch.setattr(pipeline._Transport, "get", timed_get)
    original = pipeline.P0_SOURCES
    monkeypatch.setattr(pipeline, "P0_SOURCES", [SOURCES["tc_venture"], SOURCES["cbn_main"], SOURCES["alleywatch"]])
    for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
        write_fixture(fixture, day, delay=25 if day == "2026-11-03" else 0)
        run_at(cfg, fixture, day, total_timeout=60, no_deep=True)
    with db.connect(cfg.db_path) as conn:
        assert digest_row(conn, "2026-11-03")["status"] == "没跑完"
    monkeypatch.setattr(pipeline, "P0_SOURCES", original)
    write_fixture(fixture, "2026-11-06")
    if failure_path:
        def fail(*args, **kwargs):
            raise RuntimeError("合成失败路径")
        monkeypatch.setattr(fetcher, "fetch_all", fail)
        assert pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40))) == 1
    else:
        payload, _ = run_at(cfg, fixture, "2026-11-06", no_deep=True)
        assert not payload["alarm"] and "没抓到" not in payload["text"]
    with db.connect(cfg.db_path) as conn:
        row = digest_row(conn)
        assert json.loads(row["counts_json"])["failed_days"] == 1
        assert "11-03" not in row["text"]


@pytest.mark.parametrize("kind,expected", [
    ("normal", ""),
    ("yc", "YC 公司目录没抓到：对方限制了访问"),
    ("mixed", "TechCrunch、YC 公司目录没抓到：网站出了问题"),
    ("different-date", "TechCrunch、Crunchbase News 没抓到：网站出了问题"),
    ("same-date", "TechCrunch、Crunchbase News 没抓到：最新一条还是 10-28 的"),
    ("same-type", "TechCrunch、AlleyWatch 没抓到：页面打不开"),
    ("known-break", "YC 公司目录、StrictlyVC 没抓到：网站出了问题"),
    ("all-failed", "所有来源都没抓到：今天每个网站都出了问题"),
    ("unfinished", "部分来源没抓到：今天没能按时抓完"),
])
def test_e7_46_49_alarm_merge_order_parameters_and_spacing(kind, expected):
    sources = []
    if kind in {"yc", "mixed", "known-break"}:
        sources.append(SourceStatusLine("YC 公司目录", False, False, "", ErrorType.RATE_LIMITED, source_id="yc_directory"))
    if kind == "mixed":
        sources.append(SourceStatusLine("TechCrunch", False, False, "", ErrorType.UNREACHABLE, source_id="tc_venture"))
    if kind in {"same-date", "different-date"}:
        sources.extend([SourceStatusLine("Crunchbase News", False, False, "", ErrorType.STALE, date(2026, 10, 28 if kind == "same-date" else 27)),
                        SourceStatusLine("TechCrunch", False, False, "", ErrorType.STALE, date(2026, 10, 28))])
    if kind == "same-type":
        sources.extend(SourceStatusLine(name, False, False, "", ErrorType.UNREACHABLE) for name in ("AlleyWatch", "TechCrunch"))
    if kind == "known-break":
        sources.append(SourceStatusLine("StrictlyVC", False, False, "", ErrorType.NEWSLETTER_STALE, date(2026, 10, 28)))
    status = "全部失败" if kind == "all-failed" else "没跑完" if kind == "unfinished" else "有新增"
    data = digest_data(status=status, sources=sources)
    line = expected + wording.FETCH_WARNING_END if expected else ""
    assert render.fetch_warning(data) == line
    text = render.render_push(data)
    if line:
        assert text.splitlines()[1] == line and text.splitlines()[2].startswith("1. ")
    else:
        assert text.splitlines()[1].startswith("1. ")
    assert render.is_alarm(data) is bool(line)
    assert all(word not in line for word in ("HTTP", "fixture", "timeout", "403", "429", "（", "）"))


@pytest.mark.parametrize("status", ["部分失败", "没跑完", "全部失败"])
def test_e7_49_stop_sentence_stays_on_same_line_without_touching_notice_date(status):
    start = datetime(2026, 10, 23, 17, 28, tzinfo=BJ)
    end = datetime(2026, 10, 30, 19, 56, tzinfo=BJ)
    sources = [SourceStatusLine(name, True, False, "", missed_range="手写缺期", missed_start=start, missed_end=end)
               for name in ("AlleyWatch", "StrictlyVC")]
    data = digest_data(status=status, sources=sources, baseline_at=None, ts_stopped_notice="封禁")
    lead = {"没跑完": "部分来源没抓到：今天没能按时抓完", "全部失败": "所有来源都没抓到：今天每个网站都出了问题"}.get(status,
        "StrictlyVC、AlleyWatch 没抓到：10-23 17:28 到 10-30 19:56 的内容没补上")
    expected = lead + wording.FETCH_WARNING_END + "Term Sheet 没抓到：对方不再允许抓取" + wording.FETCH_WARNING_END
    assert render.render_push(data).splitlines()[1] == expected
    assert render.add_resend_notice(render.render_push(data)) == render.render_push(data)
    assert all(pattern not in expected for pattern in ("没有新公司。" + " ", "AlleyWatch" + "没抓到", "19:56" + "的"))
    yc = digest_data(sources=[SourceStatusLine("YC 公司目录", False, False, "", ErrorType.RATE_LIMITED)], ts_stopped_notice="封禁")
    assert render.fetch_warning(yc) == "YC 公司目录没抓到：对方限制了访问" + wording.FETCH_WARNING_END + "Term Sheet 没抓到：对方不再允许抓取" + wording.FETCH_WARNING_END


@pytest.mark.parametrize("name", ["AlleyWatch", "Source 2", "YC 公司目录", "部分来源", "所有来源都"])
def test_e7_warning_templates_and_legacy_text_obey_same_spacing(name):
    expected = name + (" " if name in {"AlleyWatch", "Source 2"} else "") + "没抓到：页面打不开" + wording.FETCH_WARNING_END
    assert wording.ERROR_TEXT[ErrorType.UNREACHABLE].format(X=name) == expected
    legacy = name + "没抓到：页面打不开" + wording.FETCH_WARNING_END
    assert render.warning_from_text(legacy) == expected
    old_range = "AlleyWatch" + "没抓到：10-23 17:28 到 10-30 19:56" + "的内容没补上" + wording.FETCH_WARNING_END + " " + wording.ERROR_TEXT[ErrorType.TS_STOPPED].format(reason="对方不再允许抓取")
    assert render.warning_from_text(old_range) == "AlleyWatch 没抓到：10-23 17:28 到 10-30 19:56 的内容没补上" + wording.FETCH_WARNING_END + "Term Sheet 没抓到：对方不再允许抓取" + wording.FETCH_WARNING_END


def test_e7_46_non_fetch_failures_do_not_add_alarm(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06")
    payload, _ = run_at(cfg, fixture, "2026-11-06", rates_fail=True, no_verify=True, no_deep=True)
    assert not payload["alarm"] and "没抓到" not in payload["text"]
    with db.connect(cfg.db_path) as conn:
        conn.execute("INSERT INTO deepdive(company_id,run_date,status,error) SELECT id,'2026-11-06','失败','worker_failed' FROM company")
        current = json.loads((cfg.state_dir / "test/status.json").read_text())
        status = site.write_status(conn, cfg, FRIDAY)
        assert status["deepdive_progress"]["failed"] > 0 and status["alarm"] is False
        assert status["verification"] == current["verification"]
        assert status["homepage"] == current["homepage"]


def test_k2_53_empty_week_and_archive_are_allowed_exact_copy(catalog):
    conn, cfg = catalog
    path = site.build_site(conn, cfg, now=FRIDAY)
    assert render_page(path, "weekHtml") == '<p class="empty-line">' + wording.NO_NEW + "</p>"
    assert "no_push" not in wording.SITE_TEXT
    assert page_html(path).endswith('<p class="empty-line">没有符合条件的公司</p></div>')
    add_company(conn, "pending", "Pending", status="轮次待核")
    add_event(conn, 1, "pending", pushed="2026-11-06", round_norm="待核")
    for status in ("没有新增", "部分失败"):
        scan.upsert_digest(conn, {"date": "2026-11-06", "status": status, "push": True, "event_ids": [1], "generated_at": FRIDAY})
        path = site.build_site(conn, cfg, now=FRIDAY)
        html = render_page(path, "weekHtml")
        assert html.startswith('<p class="empty-line">' + wording.NO_NEW + "</p>")
        assert summary_titles(html) == ["轮次待确认"]
        assert 'class="dgroup-h"' not in html and "没抓到" not in html


def test_e1_e2_52_54_frozen_sources_group_date_and_empty_summary(catalog):
    conn, cfg = catalog
    add_company(conn, "example", "Example")
    add_event(conn, 1, "example", source="strictlyvc", seen="2026-11-04 08:00:00", pushed="2026-11-06")
    conn.execute("UPDATE raw_item SET published_at='2026-11-03T23:00:00Z' WHERE id=1")
    conn.execute("UPDATE event_source SET source_name='techfundingnews.com',url='https://techfundingnews.com/r14e' WHERE event_id=1")
    extra_source(conn, 1, source_id="alleywatch", name="AlleyWatch", published="2026-11-04T01:00:00Z")
    tc = extra_source(conn, 1, published="2026-11-03T23:30:00Z")
    add_company(conn, "directory", "Directory")
    add_event(conn, 10, "directory", source="yc_directory", pushed="2026-11-06")
    conn.execute("UPDATE raw_item SET published_at=NULL WHERE id=10")
    conn.execute("UPDATE company SET summary='' WHERE id='example'")
    frozen = pipeline._card_source_snapshot(conn, [1, 10])
    scan.upsert_digest(conn, {"date": "2026-11-06", "push": True, "event_ids": [1, 10], "generated_at": FRIDAY + timedelta(hours=1), "card_sources_json": frozen})
    extra_source(conn, 1, published="2026-11-05T01:00:00Z")
    path = site.build_site(conn, cfg, now=FRIDAY + timedelta(hours=2))
    index = json.loads((path / "data.json").read_text())
    event = next(e for e in index["events"] if e["id"] == 1)
    assert event["sources"] == frozen["1"]
    assert event["sources"][0]["url"] == tc and event["sources"][0]["published_at"] == "2026-11-04 07:30:00"
    html = render_page(path, "weekHtml")
    article = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if ">Example</a>" in a)
    assert "来源：TechCrunch · 11-04、StrictlyVC · 11-04、AlleyWatch · 11-04" in html_text(article)
    assert 'href="https://techfundingnews.com/r14e"' in article
    assert "产品：" not in article and "简介：" not in article
    assert html.index('data-date="2026-11-04"') < html.index(">Example</a>") < html.index('data-date=""')
    assert "YC 公司目录" in html_text(html) and "YC 公司目录 ·" not in html_text(html)
    assert len(json.loads((path / "c/example.json").read_text())["companies"][0]["sources"]) == 4


def test_e2_52_legacy_issue_uses_generated_at_cutoff_without_backfill(catalog):
    conn, cfg = catalog
    add_company(conn, "legacy", "Legacy")
    add_event(conn, 1, "legacy", source="alleywatch", pushed="2026-10-31")
    scan.upsert_digest(conn, {"date": "2026-10-31", "push": True, "event_ids": [1], "generated_at": BASELINE})
    late = extra_source(conn, 1, source_id="strictlyvc", name="techfundingnews.com")
    path = site.build_site(conn, cfg, now=FRIDAY)
    event = json.loads((path / "data.json").read_text())["events"][0]
    assert [s["name"] for s in event["sources"]] == ["AlleyWatch"]
    assert late not in render_page(path, "weekHtml") and late in render_page(path, "detailHtml", "legacy")
    row = digest_row(conn, "2026-10-31")
    assert row["card_sources_json"] == row["counts_json"] == "{}"
