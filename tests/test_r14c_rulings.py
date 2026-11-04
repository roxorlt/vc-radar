"""R14c/R14d：按 PM 与 Q 的编号逐项离线验收，全部使用手写合成数据。"""
from dataclasses import replace
from datetime import date, datetime, time as day_time, timedelta
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import pytest

from vcradar import db, fetcher, outbox, pipeline, render, scan, site, wording
from vcradar.config import BJ, Config
from vcradar.models import CardSource, DigestData, ErrorType, PushSection, SourceState, SourceStatusLine
from vcradar.robots import RobotsChecker
from test_er_render import card
from test_pipe_pipeline import arguments, offline, run_at, write_fixture  # noqa: F401
from test_site_build import NOW, add_company, add_deep, add_event, node, render_page, seeded, snapshot  # noqa: F401
from test_sources_support import SOURCES, fetch
from test_site_build import html_text, summary_titles


BASELINE = datetime(2026, 10, 31, 0, 36, 50, tzinfo=BJ)
MONDAY = datetime(2026, 11, 2, 8, 40, tzinfo=BJ)
FRIDAY = datetime(2026, 11, 6, 8, 40, tzinfo=BJ)
STATUS_KEYS = {"date", "status", "push", "alarm", "outbox", "simulated", "fx", "sources", "not_connected",
               "pending_ratios", "verification", "homepage", "deepdive_progress", "deepdive_budget",
               "extract_budget", "quality_pending", "new_evidence", "missed"}
COUNT_KEYS = {"admitted", "ai", "social", "new_round", "large", "round_pending", "filtered", "baseline",
              "supplement_m", "supplement_k", "new_evidence", "failed_days", "incomplete_until"}


def data(**kwargs):
    values = dict(push_date=FRIDAY.date(), window_start=BASELINE, window_end=FRIDAY,
                  status="有新增", cards=[card()], review_site_passed=True, base_url="https://radar.test/private")
    values.update(kwargs)
    return DigestData(**values)


def page_html(path, *, filters=None):
    return node("const fs=require('fs'),ui=require(process.argv[1]),d=JSON.parse(fs.readFileSync(process.argv[2]));"
                "console.log(JSON.stringify(ui.listHtml(d,JSON.parse(process.argv[3]))));",
                path / "data.json", json.dumps(filters or {"period": "all", "tag": "all", "round": "all", "status": "all"}))


def summaries(html):
    return summary_titles(html)


def test_a1_n17_02_status_keys_match_v16o_and_have_no_digest_counts(seeded):
    conn, cfg = seeded
    runtime = {"date": "2026-10-31", "status": "有新增", "push": False, "alarm": False,
               "outbox": "synthetic-outbox", "simulated": False,
               "fx": {"date": "2026-10-30", "ok": True, "source": "synthetic", "message": "汇率日期 10-30"}}
    status = site.write_status(conn, cfg, NOW, runtime=runtime)
    assert set(status) == STATUS_KEYS
    assert not {"counts", "counts_json", "admitted", "baseline", "supplement_m", "supplement_k"}.intersection(status)
    assert status["outbox"] == runtime["outbox"] and status["fx"] == runtime["fx"]
    assert status["deepdive_progress"]["done"] == conn.execute("SELECT count(*) FROM deepdive WHERE run_date='2026-10-31' AND status='完成'").fetchone()[0]
    assert status["deepdive_budget"]["month_used"] == 2.75
    assert status["homepage"]["total"] >= 0 and status["new_evidence"][0]["company_id"] == "mach9"
    source = next(s for s in status["sources"] if s["name"] == "TechCrunch")
    assert source["state"] == "成功" and source["technical"] == "HTTP 200"
    path, payload = snapshot(seeded)
    assert "status" not in payload and not (path / "status.json").exists()
    assert json.loads((cfg.state_dir / "status.json").read_text()) == status


def test_a2_n17_01_test_site_every_snapshot_has_current_status_and_keeps_older(seeded):
    conn, cfg = seeded
    cfg.test_mode = True
    first = datetime(2026, 10, 31, 9, 30, tzinfo=BJ)
    site.build_site(conn, cfg, now=first, snapshot_slot="0930")
    root = cfg.state_dir / "test-site/2026-10-31"
    before = (root / "0930/status.json").read_bytes()
    conn.execute("UPDATE deepdive SET status='暂时没有' WHERE status='生成中'")
    site.build_site(conn, cfg, now=first.replace(hour=10, minute=30), snapshot_slot="1030")
    latest = json.loads((root / "1030/status.json").read_text())
    assert latest["deepdive_progress"]["running"] == 0 and latest["deepdive_progress"]["timeout"] == 2
    assert (root / "0930/status.json").read_bytes() == before
    for slot in ("0930", "1030"):
        assert {"index.html", "app.js", "style.css", "data.json", "c", "status.json"} <= {p.name for p in (root / slot).iterdir()}
        assert set(json.loads((root / slot / "status.json").read_text())) == STATUS_KEYS
        assert "status" not in json.loads((root / slot / "data.json").read_text())
    site.build_site(conn, cfg, now=first)
    assert (root / "093000/status.json").exists()
    assert (cfg.state_dir / "test/status.json").exists()
    assert not (cfg.state_dir / "status.json").exists()


def test_a2_failure_path_and_non_push_run_rewrite_status(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31", old=True)
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    before = (cfg.state_dir / "test/status.json").stat().st_mtime_ns
    write_fixture(fixture, "2026-11-02")
    result, _ = run_at(cfg, fixture, "2026-11-02", no_deep=True)
    status = json.loads((cfg.state_dir / "test/status.json").read_text())
    assert result["push"] is False and status["push"] is False
    assert status["date"] == "2026-11-02" and set(status) == STATUS_KEYS
    assert (cfg.state_dir / "test/status.json").stat().st_mtime_ns >= before
    def fail(*args, **kwargs):
        raise RuntimeError("合成抓取故障")
    monkeypatch.setattr(pipeline.fetcher, "fetch_all", fail)
    assert pipeline.run(cfg, arguments(fixture, no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40))) == 1
    status = json.loads((cfg.state_dir / "test/status.json").read_text())
    assert status["status"] == "全部失败" and status["alarm"] and set(status) == STATUS_KEYS


def test_a2_n17_01_sync_sent_and_manual_deep_end_rewrite_status(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06")
    run_at(cfg, fixture, "2026-11-06", no_deep=True)
    path = cfg.state_dir / "test/status.json"
    path.unlink()
    assert pipeline.run(cfg, arguments(fixture, sync_sent=True), pipeline.Clock(FRIDAY.date(), day_time(9, 10))) == 0
    assert set(json.loads(path.read_text())) == STATUS_KEYS
    from vcradar import deep
    calls = []
    monkeypatch.setattr(deep, "run_cli", lambda *args: calls.append(True) or 0)
    path.unlink()
    assert pipeline.run(cfg, arguments(fixture, deep_only=True), pipeline.Clock(FRIDAY.date(), day_time(9, 15))) == 0
    assert calls == [True] and set(json.loads(path.read_text())) == STATUS_KEYS


def test_a2_calendar_failure_rewrites_complete_internal_status(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")
    assert pipeline.run(cfg, arguments(fixture), pipeline.Clock(date(2027, 1, 1), day_time(8, 40))) == 5
    status = json.loads((cfg.state_dir / "test/status.json").read_text())
    assert set(status) == STATUS_KEYS and status["alarm"] is True and status["push"] is False
    assert status["date"] == "2027-01-01" and status["status"] == "环境缺失"


def test_a3_migration_keeps_old_two_counts_empty_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(db.SCHEMA_SQL)
    legacy.executemany("INSERT INTO digest(date,status,push,text,generated_at) VALUES (?,?,?,?,?)", [
        ("2026-10-31", "基线", 1, "旧首期", "2026-10-31 00:36:50"),
        ("2026-11-02", "有新增", 0, "旧非推送", "2026-11-02 08:45:00")])
    legacy.commit()
    legacy.close()
    for _ in range(2):
        conn = db.connect(path)
        assert [tuple(r) for r in conn.execute("SELECT text,counts_json,card_sources_json FROM digest ORDER BY date")] == [("旧首期", "{}", "{}"), ("旧非推送", "{}", "{}")]
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "15"
        assert {"missed_start", "missed_end"} <= {r[1] for r in conn.execute("PRAGMA table_info(fetch_run)")}
        conn.close()


def test_a3_n17_08_counts_fixed_keys_nulls_and_yc_excluded(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31", old=True)
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    with db.connect(cfg.db_path) as conn:
        counts = json.loads(conn.execute("SELECT counts_json FROM digest").fetchone()[0])
        assert set(counts) == COUNT_KEYS and counts["baseline"] == 6 and counts["admitted"] == 0
        assert counts["supplement_m"] is None and counts["supplement_k"] is None
    write_fixture(fixture, "2026-11-02", new=["New Monday"])
    run_at(cfg, fixture, "2026-11-02", no_deep=True)
    with db.connect(cfg.db_path) as conn:
        assert json.loads(conn.execute("SELECT counts_json FROM digest WHERE date='2026-11-02'").fetchone()[0]) == {}
    listing = {"filtered_total": 3}
    section = PushSection("backfill", wording.BACKFILL_TITLE, "", [card("Supplement")], overflow_count=2)
    counts = pipeline._digest_counts(data(extra_sections=[section], yc_new_batch=("Fall 2026", 40)), listing, push=True, failed_days=1)
    assert set(counts) == COUNT_KEYS and counts["baseline"] is None
    assert counts["admitted"] == 1 and counts["supplement_m"] == 1 and counts["supplement_k"] == 2
    assert counts["filtered"] == 3 and counts["failed_days"] == 1
    log = [json.loads(line) for line in (cfg.log_dir / "2026-10-31.log").read_text().splitlines()]
    assert any(e.get("event") == "digest_counts" and e["counts"]["baseline"] == 6 for e in log)


def newsletter(tmp_path, source_id, *, after=False, mixed=False, baseline=BASELINE):
    strict = source_id == "strictlyvc"
    latest = MONDAY - timedelta(hours=1)
    old = [BASELINE - timedelta(days=i) for i in range(1, 6)]
    newer = [BASELINE + timedelta(hours=i) for i in (5, 4, 3, 2, 1)] if after or mixed else []
    times = [latest, *newer, *old]
    urls = [f"https://newsletter.strictlyvc.com/p/r14c-{i}" if strict else f"https://content.fortune.com/newsletter/termsheet/?post_id={700 + i}" for i in range(len(times))]
    listing = ("<urlset>" + "".join(f"<url><loc>{url}</loc><lastmod>{stamp.isoformat()}</lastmod></url>" for url, stamp in zip(urls, times)) + "</urlset>") if strict else json.dumps([
        {"id": 700 + i, "date_gmt": stamp.astimezone(__import__('datetime').timezone.utc).isoformat()} for i, stamp in enumerate(times)])
    overrides = {SOURCES[source_id].url: {"body": listing, "content_type": "application/xml" if strict else "application/json"}}
    for url, stamp in zip(urls, times):
        body = ('<script type="application/ld+json">{"datePublished":"' + stamp.isoformat() + '"}</script>' if strict else "")
        body += "<h2>Smaller Fundings</h2>" if strict else "<h2>VENTURE DEALS</h2>"
        body += "<p>Synthetic, a Boston-based software company, raised $2M in seed funding.</p>"
        overrides[url] = {"body": body, "content_type": "text/html"}
    state = SourceState(source_id, known_issue_urls={urls[0]}, baseline_at=baseline)
    outcome, client = fetch(tmp_path, source_id, overrides, state=state, now=MONDAY)
    return outcome, client


@pytest.mark.parametrize("source_id", ["strictlyvc", "ts_web"])
def test_b1_r14b_pre_baseline_old_issues_non_push_have_no_alarm_or_status_missed(tmp_path, source_id):
    outcome, _ = newsletter(tmp_path, source_id)
    assert outcome.ok and outcome.missed_range == "" and outcome.runs[0].missed_range == ""
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    with db.connect(cfg.db_path) as conn:
        scan.upsert_digest(conn, {"date": BASELINE.date(), "generated_at": BASELINE, "status": "基线"}, first_issue=True)
        scan.record_fetch(conn, "old-issues", outcome, True)
        line = pipeline._source_lines([outcome], {source_id: SourceState(source_id)})[0]
        digest = data(sources=[line], baseline_at=BASELINE)
        assert not render.is_alarm(digest, push=False)
        assert render.render_push(digest).splitlines()[1].startswith("1. ")
        status = site.write_status(conn, cfg, MONDAY)
        assert status["missed"] == []
        path = site.build_site(conn, cfg, now=MONDAY)
        assert "没抓到" not in render_page(path, "weekHtml")


@pytest.mark.parametrize("source_id", ["strictlyvc", "ts_web"])
def test_b1_r14b_post_baseline_missing_issues_have_alarm_and_structured_range(tmp_path, source_id):
    outcome, _ = newsletter(tmp_path, source_id, after=True)
    assert outcome.ok and outcome.missed_range and outcome.missed_end >= BASELINE
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    with db.connect(cfg.db_path) as conn:
        scan.upsert_digest(conn, {"date": BASELINE.date(), "generated_at": BASELINE, "status": "基线"}, first_issue=True)
        scan.record_fetch(conn, "missing-new", outcome, True)
        row = conn.execute("SELECT missed_start,missed_end FROM fetch_run").fetchone()
        assert row[0] and row[1]
        line = pipeline._source_lines([outcome], {source_id: SourceState(source_id)})[0]
        digest = data(sources=[line], baseline_at=BASELINE)
        assert render.is_alarm(digest, push=False)
        assert "的内容没补上" in render.render_push(digest).splitlines()[1]
        assert site.write_status(conn, cfg, MONDAY)["missed"]


def test_b1_mixed_issues_clip_to_baseline_and_no_baseline_keeps_legacy(tmp_path):
    mixed, _ = newsletter(tmp_path / "mixed", "ts_web", mixed=True)
    assert mixed.missed_start >= BASELINE and mixed.missed_end >= BASELINE
    unknown, _ = newsletter(tmp_path / "unknown", "ts_web", baseline=None)
    assert unknown.missed_range


@pytest.mark.parametrize("after,expected", [(False, 0), (True, 1)])
def test_b1_r14d_1031_baseline_1102_history_1106_failed_days_and_push(tmp_path, after, expected):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    with db.connect(cfg.db_path) as conn:
        db.sync_sources(conn, [SOURCES["ts_web"], SOURCES["strictlyvc"]])
        scan.upsert_digest(conn, {"date": BASELINE.date(), "status": "基线", "generated_at": BASELINE}, first_issue=True)
        text = "2026-11-01 08:00 ~ 2026-11-02 08:00（北京时间）" if after else "2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）"
        for sid in ("ts_web", "strictlyvc"):
            conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range) VALUES ('history',?,?,?,'成功',?)", (sid, MONDAY.isoformat(), MONDAY.isoformat(), text))
        for day in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
            scan.upsert_digest(conn, {"date": day, "status": "没有新增", "push": False})
            outbox.write_atomic(cfg.outbox_dir / f"{day}.json", {"push": False})
        assert scan.failed_days_in_window(conn, BASELINE, FRIDAY.date(), outbox_dir=cfg.outbox_dir) == expected
        assert conn.execute("SELECT missed_range FROM fetch_run LIMIT 1").fetchone()[0] == text
        status = site.write_status(conn, cfg, FRIDAY)
        assert bool(status["missed"]) is after
        # 历史失败天数只进计数，不触发推送日当天的报警。
        digest = data(baseline_at=BASELINE)
        assert not render.is_alarm(digest, failed_days=expected)
        assert render.render_push(digest, failed_days=expected).splitlines()[1].startswith("1. ")


@pytest.mark.parametrize("after", [False, True])
def test_b1_preview_push_uses_baseline_for_historical_ranges(tmp_path, after):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    with db.connect(cfg.db_path) as conn:
        db.sync_sources(conn, [SOURCES["strictlyvc"]])
        scan.upsert_digest(conn, {"date": BASELINE.date(), "status": "基线", "generated_at": BASELINE}, first_issue=True)
        missed = "2026-11-01 08:00 ~ 2026-11-02 08:00（北京时间）" if after else "2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）"
        conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,missed_range) VALUES ('preview','strictlyvc',?,?,'成功',?)",
                     (MONDAY.isoformat(), MONDAY.isoformat(), missed))
    args = arguments(preview_push=True, to="test", no_links=True)
    assert pipeline.preview_push(cfg, args, pipeline.Clock(FRIDAY.date(), day_time(8, 40))) == 0
    payload = json.loads(next(cfg.outbox_dir.glob("*-preview-*.json")).read_text())
    assert payload["alarm"] is after and ("没抓到：" in payload["text"]) is after
    with db.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT missed_range FROM fetch_run").fetchone()[0] == missed


@pytest.mark.parametrize("failed", [False, True])
def test_b2_b4_n17_04_failure_line_push_only_and_normal_has_none(seeded, failed):
    source = SourceStatusLine("YC 公司目录", not failed, False, "", ErrorType.RATE_LIMITED if failed else None)
    text = render.render_push(data(sources=[source]))
    if failed:
        assert text.splitlines()[1] == "YC 公司目录没抓到：对方限制了访问" + wording.FETCH_WARNING_END
        assert text.splitlines()[2].startswith("1. ") and text.count("没抓到") == 1
    else:
        assert text.splitlines()[1].startswith("1. ") and "没抓到" not in text
    path, payload = snapshot(seeded)
    assert "fetch_warning" not in payload["week"]
    for html in (render_page(path, "weekHtml"), page_html(path)):
        assert "没抓到" not in html and "本周汇总可能不全" not in html


@pytest.mark.parametrize("different_type,different_date,reason", [(False, False, "最新一条还是 10-30 的"),
    (False, True, "网站出了问题"), (True, False, "网站出了问题")])
def test_b2_n17_04_merge_by_type_and_parameters_in_source_order(different_type, different_date, reason):
    sources = [SourceStatusLine("YC 公司目录", False, False, "", ErrorType.TIMEOUT if different_type else ErrorType.STALE, date(2026, 10, 29) if different_date else date(2026, 10, 30)),
               SourceStatusLine("TechCrunch", False, False, "", ErrorType.STALE, date(2026, 10, 30))]
    assert render.fetch_warning(data(sources=sources)) == "TechCrunch、YC 公司目录没抓到：" + reason + wording.FETCH_WARNING_END


@pytest.mark.parametrize("state", ["部分失败", "没跑完", "全部失败"])
@pytest.mark.parametrize("reason,phrase", [("封禁", "对方不再允许抓取"), ("异议", "对方要求停止抓取")])
def test_b2_n17_05_stop_sentence_survives_other_errors(state, reason, phrase):
    digest = data(status=state, ts_stopped_notice=reason,
                  sources=[SourceStatusLine("TechCrunch", False, False, "", ErrorType.TIMEOUT)])
    text = render.render_push(digest)
    assert "Term Sheet 没抓到：" + phrase + wording.FETCH_WARNING_END in text.splitlines()[1]
    assert render.add_resend_notice(text) == text
    assert render.is_alarm(digest)


def test_n17_05_stop_notice_is_marked_only_after_valid_receipt(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31", old=True)
    run_at(cfg, fixture, "2026-10-31", first_issue=True, no_deep=True)
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE source SET crawl_status='已停抓',enabled=0,stop_reason='封禁' WHERE id='ts_web'")
        assert pipeline._ts_notice(conn, FRIDAY.date(), True) == "封禁"
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] is None
    original = outbox.write_main
    def fail(*args, **kwargs):
        raise OSError("合成落盘失败")
    monkeypatch.setattr(outbox, "write_main", fail)
    with db.connect(cfg.db_path) as conn:
        with pytest.raises(OSError):
            pipeline._failure_file(conn, cfg, arguments(no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40)), FRIDAY.date())
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] is None
    monkeypatch.setattr(outbox, "write_main", original)
    with db.connect(cfg.db_path) as conn:
        pipeline._failure_file(conn, cfg, arguments(no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 40)), FRIDAY.date())
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] is None
        path = pipeline._main_file(cfg, FRIDAY.date())
        outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": "2026-11-06 08:59:30", "result": "ok", "note": ""})
        pipeline.sync_sent(conn, cfg, FRIDAY.date())
        assert conn.execute("SELECT stop_notice_date FROM source WHERE id='ts_web'").fetchone()[0] == "2026-11-06"
        assert pipeline._ts_notice(conn, FRIDAY.date() + timedelta(days=7), True) is None


@pytest.mark.parametrize("start,end,expected", [
    (datetime(2026, 10, 23, 17, 28, tzinfo=BJ), datetime(2026, 10, 30, 19, 56, tzinfo=BJ), "10-23 17:28 到 10-30 19:56 的内容没补上"),
    (None, datetime(2026, 10, 30, 19, 56, tzinfo=BJ), "10-30 19:56 之前的内容没补上"),
    (None, None, "有一段时间的内容没补上"),
])
def test_b3_missing_interval_placeholder_formats(start, end, expected):
    source = SourceStatusLine("TechCrunch", True, False, "", missed_range="合成缺期", missed_start=start, missed_end=end)
    text = render.fetch_warning(data(sources=[source]))
    assert text == "TechCrunch 没抓到：" + expected + wording.FETCH_WARNING_END
    assert "北京时间" not in text and "2026" not in text


def test_c1_c2_missing_publication_no_date_and_all_sources_ordered(seeded):
    conn, _ = seeded
    conn.execute("UPDATE raw_item SET published_at=NULL WHERE id=3")
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) VALUES (99,'cbn_venture','second','https://news.test/second','合成第二来源','2026-10-27T23:00:00Z','2026-10-30 08:42:00')")
    conn.execute("INSERT INTO event_source VALUES (3,99,'https://news.test/second','Crunchbase News','{}',1,NULL)")
    path, _ = snapshot(seeded)
    html = render_page(path, "weekHtml")
    article = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if ">Flapping Airplanes</a>" in a)
    assert "来源：TechCrunch、Crunchbase News · 10-28" in html_text(article)
    assert "TechCrunch ·" not in article
    assert html.index('data-date=""') < html.index(">Flapping Airplanes</a>")


def test_c3_v17b_card_row_table_and_tail_badge_order(seeded):
    path, payload = snapshot(seeded)
    event = next(e for e in payload["events"] if e["id"] == 3)
    event.update(backfill="此前漏掉", status="轮次待确认", pending_reason="来源没写轮次", next_push_label="下次推送：11-06 09:00")
    (path / "data.json").write_text(json.dumps(payload))
    html = page_html(path)
    article = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', html, re.S) if ">Flapping Airplanes</a>" in a)
    header = re.search(r'<div class="co-r1">(.*?)</div>', article, re.S)[1]
    pieces = ["Flapping Airplanes", "美国", "AI相关", "此前漏掉", "轮次待确认 · 来源没写轮次", "下次推送：11-06 09:00"]
    assert [header.index(p) for p in pieces] == sorted(header.index(p) for p in pieces)
    positions = [article.index("产品："), article.index('class="co-fund"'), article.index("简介："), article.index("来源："), article.index('class="co-more"')]
    assert positions == sorted(positions)


def test_c4_n17_06_archive_events_keep_each_issue_and_freeze_sources(seeded):
    conn, cfg = seeded
    # 两期各一个事件；同公司另一条本期事件只选最新一张。
    conn.execute("UPDATE funding_event SET push_date='2026-10-23' WHERE id=15")
    conn.execute("UPDATE raw_item SET published_at='2026-10-22T23:00:00Z' WHERE id=15")
    conn.execute("UPDATE digest SET event_ids_json='[15]' WHERE date='2026-10-23'")
    conn.execute("UPDATE digest SET event_ids_json='[2,3]' WHERE date='2026-10-30'")
    frozen = pipeline._card_source_snapshot(conn, [2, 3])
    conn.execute("UPDATE digest SET card_sources_json=? WHERE date='2026-10-30'", (json.dumps(frozen),))
    path, _ = snapshot(seeded)
    before = page_html(path)
    assert before.count(">Mach9</a>") == 2
    conn.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,published_at,first_seen_at) VALUES (100,'tc_venture','late','https://news.test/late','合成晚到来源','2026-11-02T01:00:00Z','2026-11-02 08:42:00')")
    conn.execute("INSERT INTO event_source VALUES (2,100,'https://news.test/late','TechCrunch','{}',1,NULL)")
    path, _ = snapshot(seeded)
    after = page_html(path)
    assert after == before and "news.test/late" not in after
    assert "news.test/late" in render_page(path, "detailHtml", "mach9")
    only_week = page_html(path, filters={"period": "week", "tag": "all", "round": "all", "status": "all"})
    assert only_week.count(">Mach9</a>") == 1


def test_c4_n17_06_sent_same_day_rerun_preserves_sources_and_counts(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-11-06")
    original, path = run_at(cfg, fixture, "2026-11-06", no_deep=True)
    outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": "2026-11-06 09:00:00", "result": "ok"})
    with db.connect(cfg.db_path) as conn:
        before = dict(conn.execute("SELECT * FROM digest WHERE date='2026-11-06'").fetchone())
        eid = json.loads(before["event_ids_json"])[0]
        conn.execute("INSERT INTO raw_item(source_id,external_id,url,title,published_at,first_seen_at) VALUES ('tc_venture','late-rerun','https://news.test/late-rerun','合成晚到来源','2026-11-06T02:00:00Z','2026-11-06 10:00:00')")
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO event_source VALUES (?,?,?,'TechCrunch','{}',1,NULL)", (eid, rid, "https://news.test/late-rerun"))
    rerun, _ = run_at(cfg, fixture, "2026-11-06", no_deep=True)
    assert rerun == original
    with db.connect(cfg.db_path) as conn:
        after = dict(conn.execute("SELECT * FROM digest WHERE date='2026-11-06'").fetchone())
    assert after["card_sources_json"] == before["card_sources_json"]
    assert after["counts_json"] == before["counts_json"]
    # 沿用上面已有的回执造法，同时验证失败路径也不能改写已送达期的快照。
    with db.connect(cfg.db_path) as conn:
        pipeline._failure_file(conn, cfg, arguments(no_deep=True), pipeline.Clock(FRIDAY.date(), day_time(8, 45)), FRIDAY.date())
        failed = dict(conn.execute("SELECT * FROM digest WHERE date='2026-11-06'").fetchone())
        assert failed["card_sources_json"] == before["card_sources_json"]
        assert failed["counts_json"] == before["counts_json"]
    assert json.loads(path.read_text()) == original
    log = [json.loads(line) for line in (cfg.log_dir / "2026-11-06.log").read_text().splitlines()]
    assert [e for e in log if e.get("event") == "digest_counts"][-1]["counts"] == json.loads(before["counts_json"])
    index = json.loads((cfg.state_dir / "test/site/data.json").read_text())
    event = next(e for e in index["events"] if e["id"] == eid)
    assert all(s["url"] != "https://news.test/late-rerun" for s in event["sources"])


def test_c5_n17_07_empty_week_and_filtered_archive_have_exact_copy(seeded):
    conn, _ = seeded
    conn.execute("UPDATE digest SET event_ids_json='[5,6]' WHERE date='2026-10-30'")
    path, _ = snapshot(seeded)
    week = render_page(path, "weekHtml")
    assert week.startswith('<p class="empty-line">' + wording.NO_NEW + "</p>")
    assert summaries(week) == ["轮次待确认", "不在收录范围"]
    archive = page_html(path, filters={"period": "date:1900-01-01", "tag": "all", "round": "all", "status": "all"})
    assert archive.endswith('<p class="empty-line">没有符合条件的公司</p></div>')


@pytest.mark.parametrize("passed,base,length", [(False, "https://radar.test", 2), (True, "", 2), (True, "https://radar.test", 3)])
def test_d1_no_new_before_and_after_review_gate(passed, base, length):
    text = render.render_push(data(status="没有新增", cards=[], review_site_passed=passed, base_url=base))
    assert len(text.splitlines()) == length and text.splitlines()[1] == wording.NO_NEW
    if length == 3:
        assert text.splitlines()[-1] == "[看本周全部](https://radar.test/#week)"


def test_d2_json_stats_absent_and_rendered_summary_titles_exact(seeded):
    path, payload = snapshot(seeded)
    assert summaries(render_page(path, "weekHtml")) == ["轮次待确认", "不在收录范围"]
    assert not {"counts_line", "hints_line", "baseline_notice", "filtered_counts", "fetch_warning"}.intersection(payload["week"])
    assert "status" not in payload


def test_d2_browser_summary_titles_and_default_fold(seeded, tmp_path):
    if os.environ.get("VCRADAR_BROWSER_TESTS") != "1":
        pytest.skip("设 VCRADAR_BROWSER_TESTS=1 执行 Chromium DOM 验收")
    browser = Path("/opt/ungoogled-chromium/chrome")
    if not browser.exists():
        pytest.skip("本机没有 Chromium")
    path, _ = snapshot(seeded)
    (path / "r14c-check.js").write_text("setTimeout(()=>document.documentElement.setAttribute('data-r14c',JSON.stringify([...document.querySelectorAll('main details')].map(d=>({title:d.querySelector('summary').innerText,open:d.open})))),100);")
    index = path / "index.html"
    index.write_text(index.read_text().replace("</body>", '<script defer src="./r14c-check.js"></script></body>'))
    result = subprocess.run([str(browser), "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking", "--host-resolver-rules=MAP * ~NOTFOUND", "--allow-file-access-from-files", "--virtual-time-budget=1000", f"--user-data-dir={tmp_path / 'profile'}", "--dump-dom", index.as_uri()], capture_output=True, text=True, timeout=25)
    if result.returncode and "Operation not permitted" in result.stderr:
        pytest.skip("沙箱禁止 Chromium 启动")
    assert result.returncode == 0
    class Reader(HTMLParser):
        result = None
        def handle_starttag(self, tag, attrs):
            if tag == "html" and "data-r14c" in dict(attrs):
                self.result = json.loads(dict(attrs)["data-r14c"])
    reader = Reader()
    reader.feed(result.stdout)
    assert reader.result == [{"title": "轮次待确认", "open": False}, {"title": "不在收录范围", "open": False}]


def test_n17_03_d17_02_d8_backfill_notice_has_no_count(seeded):
    path, _ = snapshot(seeded)
    html = page_html(path, filters={"period": "all", "tag": "all", "round": "all", "status": "all", "unsentOnly": True})
    assert '<div class="notice">补充未推送 <a ' in html
    assert not re.search(r"补充未推送\s*\d+", html)


def test_d3_copy_scan_excludes_company_facts():
    company = card(summary="公司把已过滤的日志交给团队，另有产品用于协作。第二句。")
    text = render.render_push(data(cards=[company]))
    assert "已过滤" in text and "另有" in text
    system_lines = [line for line in text.splitlines() if not re.match(r"\d+\. ", line)]
    assert all("已过滤" not in line and "另有" not in line for line in system_lines)


def test_d4_archive_week_filter_uses_this_week_label(seeded):
    path, _ = snapshot(seeded)
    html = page_html(path, filters={"period": "week", "tag": "all", "round": "all", "status": "all"})
    assert re.search(r'data-filter="period" data-value="week">本周</button>', html)
    assert ">本期</button>" not in html


def test_c7_empty_summary_has_neither_product_nor_description_row(seeded):
    conn, _ = seeded
    conn.execute("UPDATE company SET summary='' WHERE id='flapping'")
    path, _ = snapshot(seeded)
    article = next(a for a in re.findall(r'<article\b[^>]*>.*?</article>', render_page(path, "weekHtml"), re.S) if ">Flapping Airplanes</a>" in a)
    assert "产品：" not in article and "简介：" not in article
    assert 'class="co-fund"' in article and "来源：" in article and 'class="co-more"' in article


def test_test_site_is_test_only_and_repeated_exports_do_not_overwrite(seeded):
    conn, cfg = seeded
    site.build_site(conn, cfg, now=NOW)
    assert not (cfg.state_dir / "test-site").exists()
    cfg.test_mode = True
    site.build_site(conn, cfg, now=NOW)
    site.build_site(conn, cfg, now=NOW)
    site.build_site(conn, cfg, now=NOW)
    folders = list((cfg.state_dir / "test-site/2026-10-31").iterdir())
    assert len(folders) == 3 and len({p.name for p in folders}) == 3
    assert all((p / "status.json").exists() and (p / "c/mach9.json").exists() for p in folders)

