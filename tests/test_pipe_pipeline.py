"""手写合成返回的完整链路与运行边界；禁止联网和读取密钥。"""
from datetime import date, datetime, time as daytime, timedelta
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

import pytest

from vcradar import cli, config, db, http, llm, outbox, pipeline
from vcradar.config import BJ, Config
from vcradar.models import RawItem
from vcradar.sources.registry import P0_SOURCES


SOURCES = {source.id: source for source in P0_SOURCES}
FIXTURES = Path(__file__).parent / "fixtures" / "pipe"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("测试禁止联网、调用真实模型或读取密钥")
    monkeypatch.setattr(config, "load_api_key", forbidden)
    monkeypatch.setattr(llm, "urlopen", forbidden)
    monkeypatch.setattr(http.urllib.request, "urlopen", forbidden)


def write_fixture(directory, day="2026-10-26", *, new=(), stopped=False, fail=(), delay=0,
                  article=None, old=False):
    directory.mkdir(parents=True, exist_ok=True)
    today = date.fromisoformat(day)
    stamp = f"{today}T00:00:00Z"
    old_stamp = "2026-10-25T00:00:00Z"
    manifest = {}
    for source in P0_SOURCES:
        parsed = urlsplit(source.url)
        manifest[f"{parsed.scheme}://{parsed.netloc}/robots.txt"] = {"status": 404}
    manifest["https://synthetic.example/robots.txt"] = {"status": 404}
    manifest["https://handwritten-dsn.algolia.net/robots.txt"] = {"status": 404}

    def xml_item(name, ident, published):
        return (f"<item><guid>{ident}</guid><link>https://synthetic.example/{ident}</link>"
                f"<title>{name} Raises $2M in Seed funding</title><pubDate>{published}</pubDate>"
                f"<description>{name} is a Boston-based software company.</description></item>")

    feed = "<rss><channel>" + xml_item("Anchor", "anchor", old_stamp if old else stamp)
    for name in new:
        feed += xml_item(name, name.casefold(), stamp)
    feed += (f"<item><guid>editorial</guid><link>https://synthetic.example/editorial</link>"
             f"<title>Notes on office furniture</title><pubDate>{stamp}</pubDate>"
             "<description>A handwritten editorial about chairs.</description></item></channel></rss>")
    for source in P0_SOURCES:
        if source.kind == "rss":
            manifest[source.url] = {"body": feed, "content_type": "application/rss+xml", "delay_s": delay}
    manifest["https://synthetic.example/"] = {"body": article or "<p>A handwritten article paragraph.</p>", "content_type": "text/html"}
    manifest[SOURCES["yc_launches"].url] = {"content_type": "application/json", "body": json.dumps({"hits": [
        {"id": 1, "slug": "elm", "title": "Elm launch", "created_at": old_stamp if old else stamp,
         "tagline": "Elm is a Boston-based software company.",
         "company": {"name": "Elm", "url": "https://elm.example", "batch": "Fall 2026"}}
    ]})}
    manifest[SOURCES["launch_hn"].url] = {"content_type": "application/json", "body": json.dumps({"hits": [
        {"objectID": "2", "title": "Launch HN: Fir (YC Fall 2026) - Software",
         "created_at": old_stamp if old else stamp, "story_text": "Fir is a Boston-based software company."}
    ]})}
    manifest[SOURCES["yc_directory"].url] = {"content_type": "text/html", "body":
        '<script>window.AlgoliaOpts = {"app":"HANDWRITTEN","key":"synthetic-public-placeholder"};</script>'}
    manifest["https://handwritten-dsn.algolia.net/"] = {"content_type": "application/json", "sequence": [
        {"body": json.dumps({"results": [{"facets": {"batch": {"Fall 2026": 1}}}]})},
        {"body": json.dumps({"results": [{"nbHits": 1, "hits": [
            {"objectID": "grove", "slug": "grove", "name": "Grove", "batch": "Fall 2026",
             "website": "https://grove.example", "all_locations": "Boston, MA, USA", "launched_at": 1793275200,
             "one_liner": "Grove builds software."}]}]})}
    ]}
    strict = "https://newsletter.strictlyvc.com/p/handwritten"
    manifest[SOURCES["strictlyvc"].url] = {"content_type": "application/xml", "body":
        f"<urlset><url><loc>{strict}</loc><lastmod>{stamp}</lastmod></url></urlset>"}
    manifest[strict] = {"content_type": "text/html", "body":
        '<script type="application/ld+json">{"datePublished":"' + (old_stamp if old else stamp) + '"}</script>'
        '<h2>Smaller Fundings</h2><p>Cedar, a Boston-based software company, raised $2M in seed funding.</p><h2>New Funds</h2>'}
    manifest[SOURCES["ts_web"].url] = {"content_type": "application/json", "body":
        json.dumps([{"id": 7, "date_gmt": old_stamp if old else stamp}])}
    manifest["https://content.fortune.com/newsletter/termsheet/?post_id=7"] = {
        "content_type": "text/html", "body": '<h2>VENTURE CAPITAL</h2><p>Dune, a Boston-based software company, raised $2M in seed funding.</p>'}
    if stopped:
        manifest[SOURCES["ts_web"].url] = {"status": 403}
    for sid in fail:
        manifest[SOURCES[sid].url] = {"status": 503}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def arguments(fixture=None, **overrides):
    argv = ["--fixture", str(fixture)] if fixture else []
    args = cli.parse_args(argv)
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def run_at(cfg, fixture, day, **overrides):
    clock = pipeline.Clock(date.fromisoformat(day), daytime(8, 40))
    args = arguments(fixture, **overrides)
    result = pipeline.run(cfg, args, clock)
    paths = list(cfg.outbox_dir.glob(f"{day}*.json"))
    assert result == 0 and paths
    path = max(paths, key=lambda path: path.stat().st_mtime_ns)
    return json.loads(path.read_text()), path


def rows(cfg, table):
    conn = db.connect(cfg.db_path)
    try:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def test_baseline_then_daily_accumulation_friday_and_same_day_idempotence(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    first, first_path = run_at(cfg, fixture, "2026-10-26", first_issue=True)
    assert first["push"] is True and first["alarm"] is False
    assert "首次运行收录 6 家以前的公司，不算新增" in first["text"]
    assert not any(line.startswith("1.") for line in first["text"].splitlines())
    assert first_path.name.endswith("-first.json")
    assert len(rows(cfg, "company")) == 6
    assert not cfg.usage_file.exists()
    assert all(row["is_baseline"] for row in rows(cfg, "raw_item") if row["external_id"] != "editorial")

    write_fixture(fixture, "2026-10-27", new=["Birch"])
    daily, _ = run_at(cfg, fixture, "2026-10-27")
    assert daily["push"] is False and daily["text"] == ""
    assert next(row for row in rows(cfg, "company") if row["name"] == "Birch")
    assert any(row["pending"] for row in rows(cfg, "funding_event"))

    for day in ("2026-10-28", "2026-10-29"):
        write_fixture(fixture, day, new=["Birch"])
        run_at(cfg, fixture, day)
    write_fixture(fixture, "2026-10-30", new=["Birch"])
    weekly, _ = run_at(cfg, fixture, "2026-10-30")
    assert weekly["push"] is True and weekly["alarm"] is False
    assert "本周共 1 家" in weekly["text"] and "Birch" in weekly["text"]
    assert not any(row["pending"] for row in rows(cfg, "funding_event"))
    counts = [len(rows(cfg, table)) for table in ("company", "funding_event", "event_source")]
    rerun, _ = run_at(cfg, fixture, "2026-10-30")
    assert "本周共 1 家" in rerun["text"] and rerun["text"].count("1. Birch") == 1
    assert counts == [len(rows(cfg, table)) for table in ("company", "funding_event", "event_source")]
    assert len([row for row in rows(cfg, "digest") if row["date"] == "2026-10-30"]) == 1
    assert not (cfg.state_dir / "vc-radar.db").exists()
    assert not (cfg.state_dir / "outbox").exists()
    assert not (cfg.state_dir / "backup").exists()


def test_first_issue_current_week_and_undated_yc_and_scheduled_same_day(tmp_path, capsys):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30")
    first, path = run_at(cfg, fixture, "2026-10-30", first_issue=True)
    assert "本周共 5 家" in first["text"] and "首次运行收录 1 家以前的公司，不算新增" in first["text"]
    assert "Grove" not in first["text"]
    original = rows(cfg, "digest")
    scheduled, _ = run_at(cfg, fixture, "2026-10-30")
    assert scheduled["push"] is False and scheduled["text"] == ""
    assert rows(cfg, "digest") == original
    assert first["text"] in capsys.readouterr().out and path.exists()
    rerun, _ = run_at(cfg, fixture, "2026-10-30", resend=True)
    assert "本周共 5 家" in rerun["text"]


def test_term_sheet_stop_notice_same_day_resend_then_no_more_requests(tmp_path, monkeypatch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30", stopped=True)
    clients = []
    real_fixture = http.FixtureTransport

    def capture(path):
        client = real_fixture(path)
        clients.append(client)
        return client

    monkeypatch.setattr(http, "FixtureTransport", capture)
    result, _ = run_at(cfg, fixture, "2026-10-30", first_issue=True)
    assert "Term Sheet 已停止抓取：对方封禁" in result["text"] and result["alarm"] is True
    records = [row for row in rows(cfg, "fetch_run") if row["source_id"] == "ts_web"]
    assert [row["result"] for row in records] == ["失败", "已停抓"]
    assert records[-1]["ended_at"] <= "2026-10-30T08:45:00+08:00"
    source = next(row for row in rows(cfg, "source") if row["id"] == "ts_web")
    assert source["stop_notice_date"] == "2026-10-30"
    assert rows(cfg, "digest")[0]["status"] != "部分失败"
    again, _ = run_at(cfg, fixture, "2026-10-30", resend=True)
    resend, path = run_at(cfg, fixture, "2026-10-30", resend=True, dry_run=True)
    assert "Term Sheet 已停止抓取：对方封禁" in again["text"] and "Term Sheet 已停止抓取：对方封禁" in resend["text"]
    assert "重发" in resend["text"].splitlines()[2].split(" · ") and path.name.endswith("-first-resend-2.json")
    assert not any(entry["url"].startswith("https://content.fortune.com/") for client in clients[1:] for entry in client.request_log)
    write_fixture(fixture, "2026-11-02", stopped=True)
    daily, _ = run_at(cfg, fixture, "2026-11-02")
    assert daily["alarm"] is False and daily["text"] == ""
    assert len([row for row in rows(cfg, "fetch_run") if row["source_id"] == "ts_web"]) == 2


@pytest.mark.parametrize("fail,expected", [([], "没有新增"), (["tc_venture"], "部分失败"),
    (list(SOURCES), "全部失败")])
def test_no_new_partial_and_all_fail_states(tmp_path, fail, expected):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    run_at(cfg, fixture, "2026-10-26", first_issue=True)
    for day in ("2026-10-27", "2026-10-28", "2026-10-29"):
        write_fixture(fixture, day)
        run_at(cfg, fixture, day)
    write_fixture(fixture, "2026-10-30", fail=fail)
    result, _ = run_at(cfg, fixture, "2026-10-30")
    digest = next(row for row in rows(cfg, "digest") if row["date"] == "2026-10-30")
    assert digest["status"] == expected and result["alarm"] == bool(fail)
    if fail:
        assert "没抓到" in result["text"] or "今天所有来源都没抓到" in result["text"]
    else:
        assert "本周没有发现新的天使轮、种子轮、A 轮融资。" in result["text"]


def test_total_timeout_keeps_file_and_marks_unfinished_addresses(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, total_timeout_s=.08)
    fixture = write_fixture(tmp_path / "fixture", "2026-10-30", delay=.4)
    started = time.monotonic()
    result, path = run_at(cfg, fixture, "2026-10-30")
    assert time.monotonic() - started < .8 and path.exists()
    assert "今天没抓完" in result["text"] and result["alarm"] is True and result["push"] is True
    assert any(row["result"] == "没轮到" for row in rows(cfg, "fetch_run"))


@pytest.mark.parametrize("day,push,fetch", [("2026-10-02", False, False), ("2026-09-30", True, True),
    ("2026-10-10", False, True), ("2026-10-31", False, False)])
def test_calendar_early_push_holiday_and_adjusted_weekend(tmp_path, day, push, fetch):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", day)
    result, _ = run_at(cfg, fixture, day)
    assert result["push"] is push
    assert bool(rows(cfg, "fetch_run")) is fetch
    if day == "2026-09-30":
        assert "提前推送" in result["text"]
    if not fetch:
        assert rows(cfg, "digest")[0]["status"] == "非推送日"


def test_sync_sent_first_and_largest_numeric_resend(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    fixture = write_fixture(tmp_path / "fixture", old=True)
    _, first = run_at(cfg, fixture, "2026-10-26", first_issue=True)
    outbox.write_atomic(first.with_suffix(".sent"), {"sent_at": "2026-10-26T00:49:00Z", "result": "ok"})
    for number, sent_at, result in [(2, "2026-10-26 10:00:00", "ok"), (10, "2026-10-26 09:00:00", "fail")]:
        path = cfg.outbox_dir / f"2026-10-26-first-resend-{number}.json"
        outbox.write_atomic(path, {})
        outbox.write_atomic(path.with_suffix(".sent"), {"sent_at": sent_at, "result": result})
    args = arguments(fixture, sync_sent=True)
    assert pipeline.run(cfg, args, pipeline.Clock(date(2026, 10, 26), daytime(9, 10))) == 0
    row = rows(cfg, "digest")[0]
    assert row["sent_at"] == "2026-10-26 08:49:00" and row["sent_result"] == "ok"
    assert row["resend_count"] == 2 and row["resend_sent_at"] == "2026-10-26 09:00:00" and row["resend_sent_result"] == "fail"


def test_reset_test_db_removes_test_outbox_but_keeps_production(tmp_path):
    cfg = Config(state_dir=tmp_path / "state", test_mode=True)
    paths = [cfg.db_path, Path(str(cfg.db_path) + "-wal"), Path(str(cfg.db_path) + "-shm"),
             cfg.raw_dir / "synthetic.out", cfg.state_dir / "test-cache" / "robots.txt", cfg.outbox_dir / "remove.json"]
    preserved = [cfg.state_dir / "vc-radar.db", cfg.state_dir / "raw" / "keep.out", cfg.state_dir / "outbox" / "keep.json"]
    for path in paths + preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    args = arguments(reset_test_db=True)
    assert pipeline.run(cfg, args, pipeline.Clock()) == 0
    assert not any(path.exists() for path in paths)
    assert all(path.read_text() == "synthetic" for path in preserved)


def test_lock_is_exclusive_and_test_lock_separate(tmp_path):
    production = Config(state_dir=tmp_path)
    test = Config(state_dir=tmp_path, test_mode=True)
    with pipeline.run_lock(production), pipeline.run_lock(test):
        with pytest.raises(RuntimeError):
            with pipeline.run_lock(production):
                pass


def test_replay_is_isolated_and_validates_fixed_evidence(tmp_path, capsys):
    cfg_path = tmp_path / "config.toml"
    state = tmp_path / "state"
    cfg_path.write_text(f'state_dir = "{state}"\n')
    assert cli.main(["--config", str(cfg_path), "--replay", str(FIXTURES / "replay.json")]) == 0
    outputs = list((state / "outbox-test").glob("*.json"))
    assert len(outputs) == 1
    text = json.loads(outputs[0].read_text())["text"]
    assert "本周共 2 家" in text and "已过滤" not in text
    assert "Composal｜" in text
    assert "TechCrunch 写 1200 万美元，Crunchbase News 写 1600 万美元" in text
    assert text.count("2. Aster") == 1 and "AI相关 1" in text
    assert not list(state.rglob("*.db")) and not (state / "outbox").exists()
    assert text in capsys.readouterr().out


def test_fixed_replay_rejects_amount_and_quote_invention():
    from vcradar.replay import fixed_extraction
    item = RawItem("tc_venture", "a", "https://synthetic.example/a", "Aster builds AI software", None)
    ext = fixed_extraction(item, {"amount_text": "$999M", "quotes": {"amount_text": "Aster builds AI software"},
                                  "ai": {"verdict": "是", "quote": ""}})
    assert ext.amount_text is None and ext.ai.verdict == "是" and ext.ai.quote == ""


@pytest.mark.parametrize("argv,message", [
    (["--deep-budget-day", "1"], "该参数已取消"),
    (["--deep-budget-month=2"], "该参数已取消"),
    (["--deep-budget-new", "3"], "该参数已取消"),
    (["--first-week-as-new"], "该参数已取消"),
    (["--now", "24:00", "--dry-run"], "HH:MM"),
    (["--assume-review-site-passed"], "只能"),
    (["--apply"], "需要"),
    (["--deep-only", "--resend"], "不能"),
    (["--deep-final", "09:30"], "必须晚于"),
    (["--deep-final", "09:29"], "必须晚于"),
    (["--total-timeout", "nan"], "大于 0"),
    (["--total-timeout", "0"], "大于 0"),
    (["--date", "2026-02-30"], "YYYY-MM-DD"),
    (["--now", "8:00", "--dry-run"], "HH:MM"),
    (["--live-model", "--replay", "unused", "--fixture", "unused"], "不能"),
])
def test_invalid_arguments_fail_before_any_write(tmp_path, capsys, argv, message):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + ["--config", str(tmp_path / "missing.toml")])
    assert exc.value.code == 2 and message in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_deep_only_and_corrected_now_boundary_do_not_open_files(tmp_path, capsys):
    state = tmp_path / "state"
    config = tmp_path / "c.toml"
    config.write_text(f'state_dir = "{state}"\n')
    assert cli.main(["--deep-only", "--now", "10:29", "--dry-run", "--config", str(config)]) == 0
    assert "总计 0 家" in capsys.readouterr().out
    assert not (state / "test" / "vc-radar-test.db").exists()
    assert cli.parse_args(["--now", "08:00", "--dry-run"]).now == daytime(8)

