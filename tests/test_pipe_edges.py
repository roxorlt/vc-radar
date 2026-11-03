"""交接窗口、并发、计费和正文补抓的离线边界测试。"""
from datetime import date, datetime, time as daytime, timedelta
import json
from pathlib import Path
from threading import Lock
import time

import pytest

from test_pipe_pipeline import (SOURCES, arguments, offline, rows, run_at, write_fixture)
from vcradar import cal, cli, config, db, http, llm, outbox, pipeline, render, scan
from vcradar.config import BJ, Config
from vcradar.models import RawItem
from vcradar.robots import RobotsChecker


def production_transport(monkeypatch, fixture):
    monkeypatch.setattr(http, "UrllibTransport", lambda: http.FixtureTransport(fixture))
    monkeypatch.setattr(config, "load_api_key", lambda cfg: None)


def production_run(cfg, day, **overrides):
    args = arguments(**overrides)
    assert pipeline.run(cfg, args, pipeline.Clock(date.fromisoformat(day), daytime(8, 40))) == 0


def test_production_sent_guard_keeps_later_items_pending_and_backs_up(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state")
    fixture = write_fixture(tmp_path / "fixture", old=True)
    production_transport(monkeypatch, fixture)
    production_run(cfg, "2026-10-26", first_issue=True)
    write_fixture(fixture, "2026-10-30", new=["Birch"])
    production_run(cfg, "2026-10-30")
    main = cfg.outbox_dir / "2026-10-30.json"
    before = main.read_bytes()
    outbox.write_atomic(main.with_suffix(".sent"), {"sent_at": "2026-10-30 08:59:00", "result": "ok"})
    write_fixture(fixture, "2026-10-30", new=["Birch", "Citrine"])
    for day in ("2026-10-15", "2026-10-16"):
        path = cfg.raw_dir / day / "synthetic.out"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("handwritten")
    production_run(cfg, "2026-10-30")
    assert main.read_bytes() == before
    digest = next(row for row in rows(cfg, "digest") if row["date"] == "2026-10-30")
    assert digest["text"] == json.loads(before)["text"] and digest["sent_result"] == "ok"
    companies = {row["id"]: row["name"] for row in rows(cfg, "company")}
    assert next(row for row in rows(cfg, "funding_event") if companies[row["company_id"]] == "Citrine")["pending"] == 1
    assert (cfg.state_dir / "backup" / "vc-radar-20261030.db").exists()
    assert not (cfg.raw_dir / "2026-10-15").exists() and (cfg.raw_dir / "2026-10-16").exists()
    assert "缺少模型密钥" in (cfg.log_dir / "2026-10-30.log").read_text()
    usage = json.loads(cfg.usage_file.read_text())["months"]["2026-10"]
    assert usage["calls"] > 0 and usage["calls"] == usage["failed_calls"] and usage["estimated_cost"] == 0
    assert next(row for row in rows(cfg, "company") if row["name"] == "Citrine")["ai_label"] == "待核"


def test_first_sent_file_and_resend_keep_main_window(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state")
    fixture = write_fixture(tmp_path / "fixture", old=True)
    production_transport(monkeypatch, fixture)
    production_run(cfg, "2026-10-26", first_issue=True)
    first = cfg.outbox_dir / "2026-10-26-first.json"
    outbox.write_atomic(first.with_suffix(".sent"), {"sent_at": "2026-10-26 08:49:00", "result": "ok"})
    first_bytes = first.read_bytes()
    assert pipeline.run(cfg, arguments(first_issue=True), pipeline.Clock(date(2026, 10, 26), daytime(8, 40))) == 3
    assert first.read_bytes() == first_bytes
    write_fixture(fixture, "2026-10-30", new=["Birch"])
    production_run(cfg, "2026-10-30")
    original = next(row for row in rows(cfg, "digest") if row["date"] == "2026-10-30")
    write_fixture(fixture, "2026-10-30", new=["Birch", "Citrine"])
    production_run(cfg, "2026-10-30", resend=True)
    resend = json.loads((cfg.outbox_dir / "2026-10-30-resend-1.json").read_text())
    assert "重发" in resend["text"].splitlines()[2].split(" · ") and "1. Birch" in resend["text"] and "Citrine" not in resend["text"]
    digest = next(row for row in rows(cfg, "digest") if row["date"] == "2026-10-30")
    assert digest["generated_at"] == original["generated_at"] and digest["window_start"] == original["window_start"]
    assert digest["text"] == original["text"] and digest["resend_count"] == 1


def test_source_aggregation_counts_addresses_and_one_error_is_whole_source_error(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30", fail=["tc_venture"])
    captured = []
    original = render.render_push

    def capture(data, **kwargs):
        captured.append(data)
        return original(data, **kwargs)

    monkeypatch.setattr(render, "render_push", capture)
    run_at(cfg, fixture, "2026-10-30", first_issue=True)
    sources = {line.name: line for line in captured[0].sources}
    assert len(sources) == 8 and not sources["TechCrunch"].ok
    assert sources["Crunchbase News"].count_label == "6 条"
    assert sources["YC 公司目录"].count_label == "当期 1 家"


def test_models_run_at_most_four_in_parallel_and_never_twice_for_complete_rules(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26", first_issue=True)
    write_fixture(fixture, "2026-10-27", new=["Birch", "Citrine"])
    guard = Lock()
    running = 0
    peak = 0

    class MeasuredMock(llm.MockLLMClient):
        def complete_json(self, *args, **kwargs):
            nonlocal running, peak
            with guard:
                running += 1
                peak = max(peak, running)
            try:
                return super().complete_json(*args, **kwargs)
            finally:
                with guard:
                    running -= 1

    mock = MeasuredMock([{"delay_s": .01, "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                          "ai": {"verdict": "拿不准"}, "social": {"verdict": "拿不准"}}] * 20)
    monkeypatch.setattr(pipeline, "model_client", lambda *args: mock)
    run_at(cfg, fixture, "2026-10-27")
    assert mock.calls == 12 and peak == 4
    log = [json.loads(line) for line in (cfg.log_dir / "2026-10-27.log").read_text().splitlines()]
    assert sum(record["event"] == "model_start" for record in log) == 12
    assert sum(record["event"] == "model_end" for record in log) == 12
    usage = json.loads(cfg.usage_file.read_text())["months"]["2026-10"]
    assert usage["calls"] == 12 and usage["prompt_tokens"] == 1200
    assert usage["estimated_cost"] == pytest.approx(.00216)


@pytest.mark.parametrize("filtered", [False, True])
def test_foreign_company_calls_models_unless_round_filtered(tmp_path, monkeypatch, filtered):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26", first_issue=True)
    write_fixture(fixture, "2026-10-27", new=["Foreign"])
    manifest = json.loads((fixture / "manifest.json").read_text())
    for source in SOURCES.values():
        if source.kind == "rss":
            manifest[source.url]["body"] = manifest[source.url]["body"].replace(
                "Foreign is a Boston-based", "Foreign is a London-based").replace("<guid>editorial</guid>", "<guid>editorial2</guid>")
            if filtered:
                manifest[source.url]["body"] = manifest[source.url]["body"].replace(
                    "Foreign Raises $2M in Seed funding", "Foreign Raises $2M in Series B funding")
    (fixture / "manifest.json").write_text(json.dumps(manifest))
    mock = llm.MockLLMClient([{"summary": "公司开发软件。帮助团队安排活动。提供协作功能。"}] * 6)
    monkeypatch.setattr(pipeline, "model_client", lambda *args: mock)
    run_at(cfg, fixture, "2026-10-27", no_deep=True)
    assert mock.calls == (0 if filtered else 6)
    assert all("Foreign Raises" in json.loads(request["user"])["source_data"]["title"] for request in mock.requests)
    assert any(row["skip_reason"] == "不是融资报道" for row in rows(cfg, "raw_item"))
    company = next(row for row in rows(cfg, "company") if row["name"] == "Foreign")
    assert company["status"] == "已纳入" and company["country"] == "英国"
    funding = next(row for row in rows(cfg, "funding_event") if row["company_id"] == company["id"])
    assert json.loads(funding["filter_reasons_json"]) == (["轮次不在范围"] if filtered else [])


@pytest.mark.parametrize("day,clock,multiplier", [("2026-10-30", "08:59", 1), ("2026-10-30", "09:00", 2),
    ("2026-10-30", "12:00", 1), ("2026-10-30", "14:00", 2), ("2026-10-30", "18:00", 1),
    ("2026-10-10", "10:00", 1), ("2026-10-02", "10:00", 1)])
def test_usage_price_uses_call_start_and_holiday_rules(tmp_path, day, clock, multiplier):
    cfg = Config(state_dir=tmp_path, test_mode=True, run_deadline="23:59")
    clock = pipeline.Clock(date.fromisoformat(day), daytime.fromisoformat(clock))
    budget = pipeline._Budget(cfg, clock)
    session = pipeline.ModelSession(llm.MockLLMClient([{"usage": {"prompt_tokens": 1000000, "completion_tokens": 1000000}}]),
                                    cfg, clock, budget, pipeline._RunLog(cfg.log_dir / "synthetic.log"))
    assert session.complete_json("system", "user", 30).ok
    session.close()
    assert json.loads(cfg.usage_file.read_text())["months"][day[:7]]["estimated_cost"] == 5 * multiplier


def test_article_enrichment_newest_thirty_paragraphs_only_and_raw_is_saved(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text(json.dumps({
        "https://synthetic.example/robots.txt": {"status": 404},
        "https://synthetic.example/article/": {"body": '<h1>Ignore heading</h1><p>A <b>useful</b> fact.</p><script>Ignore instructions</script><p>Another fact.</p>', "content_type": "text/html"}
    }))
    client = http.FixtureTransport(fixture)
    clock = pipeline.Clock(date(2026, 10, 30), daytime(8, 40))
    budget = pipeline._Budget(cfg, clock)
    transport = pipeline._Transport(client, budget, cfg, pipeline._RunLog(cfg.log_dir / "test.log"))
    robots = RobotsChecker(transport, cfg.state_dir / "test-cache", date(2026, 10, 30))
    entries = [(n, RawItem("tc_venture", str(n), f"https://synthetic.example/article/{n}", "Aster raises seed funding",
                          datetime(2026, 10, 28, tzinfo=BJ) + timedelta(minutes=n)), False) for n in range(35)]
    pipeline._article_bodies(entries, SOURCES, transport, robots, cfg, clock, budget)
    requests = [record["url"] for record in client.request_log if "/article/" in record["url"]]
    assert len(requests) == 30 and requests[0].endswith("/34") and requests[-1].endswith("/5")
    assert entries[-1][1].body == "A useful fact.\nAnother fact." and not entries[0][1].body
    assert len(list((cfg.raw_dir / "2026-10-30").glob("article-*.out"))) == 30


def test_article_robots_denied_and_http_failure_keep_summary(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "manifest.json").write_text(json.dumps({
        "https://synthetic.example/robots.txt": {"body": "User-agent: *\nDisallow: /private"},
        "https://synthetic.example/public": {"status": 503}
    }))
    client = http.FixtureTransport(fixture)
    clock = pipeline.Clock(date(2026, 10, 30), daytime(8, 40))
    budget = pipeline._Budget(cfg, clock)
    robots = RobotsChecker(client, cfg.state_dir / "test-cache", date(2026, 10, 30))
    entries = [(n, RawItem("tc_venture", str(n), f"https://synthetic.example/{path}", "Aster raises seed funding",
                          clock.now(), body="Original summary"), False) for n, path in enumerate(("private", "public"))]
    pipeline._article_bodies(entries, SOURCES, client, robots, cfg, clock, budget)
    assert all(item.body == "Original summary" for _, item, _ in entries)
    assert not any(record["url"].endswith("/private") for record in client.request_log)


def test_fetch_state_carries_last_success_and_raw_redacts_synthetic_search_key(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    run_at(cfg, fixture, "2026-10-30", first_issue=True)
    conn = db.connect(cfg.db_path)
    try:
        assert db.load_source_states(conn)["tc_venture"].last_latest_at == datetime(2026, 10, 30, 8, tzinfo=BJ)
    finally:
        conn.close()
    assert not any("synthetic-public-placeholder" in path.read_text() for path in cfg.raw_dir.rglob("*.out"))


def test_missing_calendar_is_environment_exit_without_fetch(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")
    assert pipeline.run(cfg, arguments(fixture), pipeline.Clock(date(2027, 1, 1), daytime(8, 40))) == 5
    payload = json.loads(next(cfg.outbox_dir.glob("*.json")).read_text())
    assert payload["alarm"] is True and payload["push"] is False and rows(cfg, "fetch_run") == []


def test_unexpected_fetch_failure_still_writes_alarm_file(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture")

    def fail(*args, **kwargs):
        raise RuntimeError("handwritten failure")

    monkeypatch.setattr(pipeline.fetcher, "fetch_all", fail)
    assert pipeline.run(cfg, arguments(fixture), pipeline.Clock(date(2026, 10, 30), daytime(8, 40))) == 1
    payload = json.loads(next(cfg.outbox_dir.glob("*.json")).read_text())
    assert payload["alarm"] is True and payload["push"] is True and "今天所有来源都没抓到" in payload["text"]


def test_clock_date_and_time_simulation_keep_real_elapsed_seconds(monkeypatch):
    ticks = [100.0]
    monkeypatch.setattr(pipeline.time, "monotonic", lambda: ticks[0])
    clock = pipeline.Clock(date(2026, 10, 30), daytime(8, 40))
    ticks[0] += 1.5
    assert clock.now() == datetime(2026, 10, 30, 8, 40, 1, 500000, tzinfo=BJ)
    clock.advance(120)
    assert clock.now().minute == 42 and clock.simulated


def test_previous_window_uses_first_main_ignores_resend_and_seven_days_is_week(tmp_path):
    for name, push, stamp in [("2026-10-20-first.json", True, "2026-10-20 08:40:00"),
        ("2026-10-23.json", True, "2026-10-23 08:45:00"),
        ("2026-10-29-resend-1.json", True, "2026-10-29 08:40:00"),
        ("2026-10-29.json", False, "2026-10-29 08:40:00")]:
        outbox.write_atomic(tmp_path / name, {"push": push, "generated_at": stamp})
    assert outbox.last_push_generated_at(tmp_path, date(2026, 10, 30)) == datetime(2026, 10, 23, 8, 45, tzinfo=BJ)
    assert not cal.since_last_push(date(2026, 10, 23), date(2026, 10, 30))
    assert cal.since_last_push(date(2026, 10, 22), date(2026, 10, 30))


def test_failed_days_excludes_prelaunch_but_includes_missing_handoff(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-28")
    run_at(cfg, fixture, "2026-10-28", first_issue=True)
    write_fixture(fixture, "2026-10-29")
    _, path = run_at(cfg, fixture, "2026-10-29")
    conn = db.connect(cfg.db_path)
    try:
        assert scan.failed_days_in_window(conn, None, date(2026, 10, 30), outbox_dir=cfg.outbox_dir) == 0
        path.unlink()
        assert scan.failed_days_in_window(conn, None, date(2026, 10, 30), outbox_dir=cfg.outbox_dir) == 1
    finally:
        conn.close()


def test_zero_baseline_omits_notice_but_keeps_repeat_coverage_hint(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, list(SOURCES.values()))
    conn.execute("UPDATE source SET enabled=0 WHERE id='yc_directory'")
    conn.commit()
    conn.close()
    first, _ = run_at(cfg, fixture, "2026-10-30", first_issue=True)
    second, _ = run_at(cfg, fixture, "2026-10-30", resend=True)
    assert "首次运行收录 0 家以前的公司，不算新增" not in first["text"] and "首次运行收录 0 家以前的公司，不算新增" not in second["text"]
    assert "本周一到 10-29 日的内容没抓全" in second["text"]


def test_model_timeout_handoff_does_not_leave_background_file_writes_and_can_retry(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    conn = db.connect(cfg.db_path)
    db.sync_sources(conn, list(SOURCES.values()))
    conn.execute("UPDATE source SET enabled=0 WHERE id!='tc_venture'")
    conn.commit()
    conn.close()
    mock = llm.MockLLMClient([{"delay_s": .2}] * 4)
    monkeypatch.setattr(pipeline, "model_client", lambda *args: mock)
    result, _ = run_at(cfg, fixture, "2026-10-30", total_timeout=.05)
    assert result["alarm"] is True and "今天没抓完" in result["text"]
    log = cfg.log_dir / "2026-10-30.log"
    before = log.read_bytes()
    time.sleep(.08)
    assert log.read_bytes() == before
    assert any(row["skip_reason"] == "这次没轮到抽取" for row in rows(cfg, "raw_item"))
    fast = llm.MockLLMClient([{"ai": {"verdict": "拿不准"}, "social": {"verdict": "拿不准"}}] * 4)
    monkeypatch.setattr(pipeline, "model_client", lambda *args: fast)
    result, _ = run_at(cfg, fixture, "2026-10-30", total_timeout=1)
    assert "1. Anchor" in result["text"]
    assert not any(row["skip_reason"] == "这次没轮到抽取" for row in rows(cfg, "raw_item"))


def test_budget_uses_first_of_run_deadline_and_total_timeout():
    class Frozen:
        def now(self):
            return datetime(2026, 10, 30, 8, 49, 59, tzinfo=BJ)
    assert 0 < pipeline._Budget(Config(total_timeout_s=60), Frozen()).remaining() <= 1
    assert 0 < pipeline._Budget(Config(total_timeout_s=.1), Frozen()).remaining() <= .1


def test_replay_twelve_ties_use_fixed_ingestion_time_and_case_insensitive_names(tmp_path):
    from vcradar.replay import run_replay
    names = ["Kestrel", "alpha", "Bravo", "charlie", "Delta", "echo", "Foxtrot", "golf", "Hotel", "india", "Juliet", "lima"]
    items = []
    for name in names:
        quote = f"{name} is a Boston-based company."
        items.append({"source_id": "tc_venture", "external_id": name, "url": f"https://synthetic.example/{name}",
                      "title": f"{name} raises $2M in Seed funding", "excerpt": quote,
                      "published_at": "2026-10-29T20:00:00Z", "first_ingested_at": "2026-10-30 08:40:00",
                      "fixed_extraction": {"company_name": name, "quotes": {"company_name": quote}}})
    path = tmp_path / "replay.json"
    path.write_text(json.dumps({"date": "2026-10-30", "items": items}))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    assert run_replay(cfg, arguments(replay=path), pipeline.Clock(date(2026, 10, 30), daytime(8, 40))) == 0
    text = json.loads(next(cfg.outbox_dir.glob("*.json")).read_text())["text"]
    actual = [line.split(". ", 1)[1].split("｜")[0] for line in text.splitlines() if line[:1].isdigit() and ". " in line]
    assert actual == sorted(names, key=str.casefold)[:10]
    assert "本周共 12 家" in text


def test_v15f_nondisputed_amount_uses_highest_credibility(tmp_path):
    from vcradar.replay import run_replay
    payload = json.loads((Path(__file__).parent / "fixtures" / "pipe" / "replay.json").read_text())
    payload["items"][1]["title"] = "Aster raises $12.6M in Series A funding"
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(payload))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    assert run_replay(cfg, arguments(replay=path), pipeline.Clock(date(2026, 10, 30), daytime(8, 40))) == 0
    text = json.loads(next(cfg.outbox_dir.glob("*.json")).read_text())["text"]
    assert "A 轮 · 1260 万美元" in text and "金额待核" not in text


def test_same_company_multiple_rounds_in_one_issue_selects_latest_round_once(tmp_path):
    from vcradar.replay import run_replay
    payload = json.loads((Path(__file__).parent / "fixtures" / "pipe" / "replay.json").read_text())
    payload["items"][0]["title"] = "Aster raises $2M in Seed funding"
    payload["items"][0]["fixed_extraction"] = {}
    payload["items"][1]["fixed_extraction"] = {}
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(payload))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    assert run_replay(cfg, arguments(replay=path), pipeline.Clock(date(2026, 10, 30), daytime(8, 40))) == 0
    text = json.loads(next(cfg.outbox_dir.glob("*.json")).read_text())["text"]
    assert "本周共 2 家" in text and text.count("1. Aster") == 1
    assert text.count("2. Composal") == 1
    assert "Aster【新一轮】" in text and "A 轮 · 1600 万美元" in text

