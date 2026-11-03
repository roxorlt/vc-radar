"""R14h / R14i：真实运行只对推送日当天漏抓报警，状态与计数保留本期口径。"""
from dataclasses import replace
from datetime import date, datetime, time as day_time, timedelta
import json
import sqlite3

import pytest

from vcradar import db, fetcher, outbox, pipeline, scan, site, wording
from vcradar.config import BJ, Config
from vcradar.models import ErrorType, FetchResult
from test_pipe_pipeline import arguments, offline, run_at, write_fixture  # noqa: F401


DAY = "2026-11-06"
UNKNOWN = "StrictlyVC 没抓到：有一段时间的内容没补上。本周汇总可能不全，不代表没有新公司。"
AFTER = "StrictlyVC 没抓到：11-04 19:56 之后的内容没补上。本周汇总可能不全，不代表没有新公司。"
STOP_NOTICE = "Term Sheet 没抓到：对方不再允许抓取。本周汇总可能不全，不代表没有新公司。"
OLD_TEXT = "2026-10-23 17:28 ~ 2026-10-30 19:56（北京时间）"


def moment(value):
    stamp = datetime.fromisoformat(value)
    return stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)


def healthy_days(cfg, days, window):
    with db.connect(cfg.db_path) as conn:
        for day in days:
            scan.upsert_digest(conn, {"date": day, "push": False, "status": "没有新增",
                                     "window_start": window, "generated_at": moment(day + " 08:40")})
            outbox.write_atomic(cfg.outbox_dir / f"{day}-084000.json", {"date": day, "push": False, "text": ""})


@pytest.fixture
def env(tmp_path, monkeypatch):
    class FileClock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime(2026, 11, 3, 12, tzinfo=BJ)
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    monkeypatch.setattr(pipeline, "datetime", FileClock)
    links = tmp_path / "research-links.json"
    links.write_text("[]")
    monkeypatch.setattr(pipeline, "Config", lambda: replace(Config(), links_file=links))
    cfg = Config(state_dir=tmp_path / "state", test_mode=True, links_file=links)
    assert pipeline.run(cfg, arguments(reset_test_db=True), pipeline.Clock(date(2026, 10, 31), day_time(0, 36))) == 0
    fixture = write_fixture(tmp_path / "fixture", "2026-10-31")
    assert pipeline.run(cfg, arguments(fixture, first_issue=True, date=date(2026, 10, 31), no_deep=True),
                        pipeline.Clock(date(2026, 10, 31), day_time(0, 36))) == 0
    with db.connect(cfg.db_path) as conn:
        baseline = db.first_issue_generated_at(conn)
    healthy_days(cfg, ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"], baseline)
    write_fixture(fixture, DAY)
    return cfg, fixture, baseline


def insert_history(cfg, *, source="strictlyvc", text="不详", start=None, end=None,
                   started="2026-11-06T08:30:00+08:00", run_id="r14h-history"):
    # 用原生连接造迁移前数据，读取断言也不触发 db.connect 的迁移。
    with sqlite3.connect(cfg.db_path) as conn:
        cursor = conn.execute("INSERT INTO fetch_run(run_id,source_id,started_at,ended_at,result,"
                              "missed_range,missed_start,missed_end) VALUES (?,?,?,?,'成功',?,?,?)",
                              (run_id, source, started, started, text, start, end))
        return cursor.lastrowid


def history_row(cfg, row_id):
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM fetch_run WHERE id=?", (row_id,)).fetchone())


def digest_row(cfg, day=DAY):
    with sqlite3.connect(cfg.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM digest WHERE date=?", (day,)).fetchone())


def status(cfg):
    return json.loads((cfg.state_dir / "test/status.json").read_text())


def stop_termsheet(cfg):
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE source SET enabled=0,crawl_status='已停抓',stop_reason='封禁',"
                     "stopped_at='2026-11-03 08:43',stop_notice_date=NULL WHERE id='ts_web'")


def push(env, day=DAY):
    cfg, fixture, _ = env
    write_fixture(fixture, day)
    return run_at(cfg, fixture, day, date=date.fromisoformat(day), no_deep=True)[0]


def assert_no_missed(cfg, payload, day=DAY):
    assert payload["push"] is True and payload["alarm"] is False
    assert "没抓到" not in payload["text"]
    assert status(cfg)["missed"] == []
    assert json.loads(digest_row(cfg, day)["counts_json"])["failed_days"] == 0


def test_48_3a_normal_run_keeps_only_post_baseline_open_start(env):
    cfg, _, _ = env
    old_term = insert_history(cfg, source="ts_web", text=OLD_TEXT, start="2026-10-23 17:28", end="2026-10-30 19:56",
                              started="2026-11-06T08:10:00+08:00")
    old_strict = insert_history(cfg, text="手写起点", start="2026-10-28 09:00", started="2026-11-06T08:20:00+08:00")
    new_strict = insert_history(cfg, text="手写起点", start="2026-11-04 19:56")
    payload = push(env)
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == AFTER
    assert [item["name"] for item in status(cfg)["missed"]] == ["StrictlyVC"]
    assert json.loads(digest_row(cfg)["counts_json"])["failed_days"] == 0
    assert history_row(cfg, old_term)["missed_range"] == OLD_TEXT
    assert history_row(cfg, old_strict)["missed_start"] == "2026-10-28 09:00"
    assert history_row(cfg, new_strict)["missed_end"] is None


def test_48_3b_normal_run_migrates_pre_baseline_text_without_alarm_or_failed_day(env):
    cfg, _, _ = env
    row_id = insert_history(cfg, source="ts_web", text=OLD_TEXT)
    assert history_row(cfg, row_id)["missed_start"] is None
    assert_no_missed(cfg, push(env))
    migrated = history_row(cfg, row_id)
    assert moment(migrated["missed_start"]) == moment("2026-10-23 17:28")
    assert moment(migrated["missed_end"]) == moment("2026-10-30 19:56")
    assert migrated["missed_range"] == OLD_TEXT
    assert_no_missed(cfg, push(env))
    assert history_row(cfg, row_id) == migrated


def test_48_3c_normal_run_unknown_history_survives_later_success(env):
    cfg, _, _ = env
    row_id = insert_history(cfg)
    payload = push(env)
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == UNKNOWN
    assert status(cfg)["missed"] == [{"name": "StrictlyVC", "range": "不详"}]
    assert json.loads(digest_row(cfg)["counts_json"])["failed_days"] == 0
    row = history_row(cfg, row_id)
    assert row["missed_range"] == "不详" and row["missed_start"] is None and row["missed_end"] is None
    with sqlite3.connect(cfg.db_path) as conn:
        assert conn.execute("SELECT result,missed_range FROM fetch_run WHERE source_id='strictlyvc' "
                            "ORDER BY id DESC LIMIT 1").fetchone() == ("成功", "")


@pytest.mark.parametrize("text,start,end,expected", [
    ("手写起点", "2026-11-04 19:56", None, 1),
    ("不详", None, None, 1),
    (OLD_TEXT, "2026-10-23 17:28", "2026-10-30 19:56", 0),
])
def test_pre_push_day_missed_only_affects_failed_days_and_status(env, text, start, end, expected):
    cfg, _, _ = env
    row_id = insert_history(cfg, text=text, start=start, end=end, started="2026-11-04T20:00:00+08:00")
    payload = push(env)
    assert payload["push"] is True and payload["alarm"] is False
    assert "没抓到" not in payload["text"]
    assert json.loads(digest_row(cfg)["counts_json"])["failed_days"] == expected
    assert status(cfg)["missed"] == ([{"name": "StrictlyVC", "range": text}] if expected else [])
    assert history_row(cfg, row_id)["missed_range"] == text


@pytest.mark.parametrize("position", ["before", "at", "future"])
def test_history_outside_previous_push_window_does_not_alarm(env, position):
    cfg, _, _ = env
    assert_no_missed(cfg, push(env))
    previous = moment(digest_row(cfg)["generated_at"])
    healthy_days(cfg, ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"], previous)
    started = {"before": previous - timedelta(seconds=1), "at": previous,
               "future": moment("2026-11-13 12:00")}[position]
    row_id = insert_history(cfg, started=started.isoformat())
    assert_no_missed(cfg, push(env, "2026-11-13"), "2026-11-13")
    assert history_row(cfg, row_id)["missed_range"] == "不详"


def test_empty_issue_window_falls_back_to_first_generated_at(env):
    cfg, _, baseline = env
    # 窗口起点为空时仍保留首期 meta/digest，直接核对共享读入口的基线兜底。
    old = insert_history(cfg, started=(baseline - timedelta(seconds=1)).isoformat())
    boundary = insert_history(cfg, started=baseline.isoformat())
    current = insert_history(cfg, started=(baseline + timedelta(seconds=1)).isoformat())
    with db.connect(cfg.db_path) as conn:
        rows = scan.missed_runs_in_window(conn, None, moment(DAY + " 08:40"))
    assert [row["id"] for row in rows] == [current]
    assert history_row(cfg, old) and history_row(cfg, boundary)


def test_window_comparison_is_exclusive_inclusive_with_microseconds_and_timezones(env):
    cfg, _, _ = env
    since, until = moment("2026-11-05 08:40:00.500000"), moment("2026-11-06 08:40:00.500000")
    ids = [insert_history(cfg, started=stamp) for stamp in (
        (since - timedelta(microseconds=1)).isoformat(), since.isoformat(),
        "2026-11-05T00:40:00.500001+00:00", until.isoformat(),
        (until + timedelta(microseconds=1)).isoformat())]
    with db.connect(cfg.db_path) as conn:
        rows = scan.missed_runs_in_window(conn, since, until)
    assert [row["id"] for row in rows] == ids[2:4]


def test_normal_run_merges_earliest_start_latest_end_and_current_backfill(env, monkeypatch):
    cfg, _, _ = env
    insert_history(cfg, text="手写缺期", start="2026-11-03 08:00", end="2026-11-04 19:56")
    insert_history(cfg, text="手写缺期", start="2026-11-02 09:00", end="2026-11-03 12:00", started="2026-11-06T08:20:00+08:00")
    original = fetcher.fetch_all

    def current_backfill(*args, **kwargs):
        outcomes = original(*args, **kwargs)
        for outcome in outcomes:
            if outcome.source.id == "strictlyvc":
                outcome.missed_range = "本次手写缺期"
                outcome.missed_start = moment("2026-11-04 08:00")
                outcome.missed_end = moment("2026-11-05 10:00")
                outcome.runs[-1] = replace(outcome.runs[-1], missed_range=outcome.missed_range,
                                           missed_start=outcome.missed_start, missed_end=outcome.missed_end)
        return outcomes

    monkeypatch.setattr(fetcher, "fetch_all", current_backfill)
    payload = push(env)
    expected = "StrictlyVC 没抓到：11-02 09:00 到 11-05 10:00 的内容没补上" + wording.FETCH_WARNING_END
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == expected
    assert len(status(cfg)["missed"]) == 1
    assert "2026-11-02 09:00 ~ 2026-11-05 10:00" in status(cfg)["missed"][0]["range"]
    assert json.loads(digest_row(cfg)["counts_json"])["failed_days"] == 0


def test_current_address_merge_filters_old_segment_before_taking_minimum(env, monkeypatch):
    cfg, _, _ = env
    original = fetcher.fetch_all

    def mixed_current(*args, **kwargs):
        outcomes = original(*args, **kwargs)
        for outcome in outcomes:
            if outcome.source.id not in {"tc_venture", "tc_startups"}:
                continue
            old = outcome.source.id == "tc_venture"
            outcome.missed_range = "手写补抓段"
            outcome.missed_start = moment("2026-10-28 09:00" if old else "2026-11-04 19:56")
            outcome.missed_end = moment("2026-10-30 19:56") if old else None
            outcome.runs[-1] = replace(outcome.runs[-1], missed_range=outcome.missed_range,
                                       missed_start=outcome.missed_start, missed_end=outcome.missed_end)
        return outcomes

    monkeypatch.setattr(fetcher, "fetch_all", mixed_current)
    payload = push(env)
    assert payload["alarm"] is True
    assert payload["text"].splitlines()[1] == AFTER.replace("StrictlyVC", "TechCrunch")
    assert [item["name"] for item in status(cfg)["missed"]] == ["TechCrunch"]


def test_unknown_history_dominates_known_bounds_for_the_same_source(env):
    cfg, _, _ = env
    insert_history(cfg, text="手写缺期", start="2026-11-03 08:00", end="2026-11-04 19:56")
    insert_history(cfg, text="不详", started="2026-11-06T08:20:00+08:00")
    payload = push(env)
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == UNKNOWN
    assert status(cfg)["missed"] == [{"name": "StrictlyVC", "range": "不详"}]


def test_history_from_multiple_addresses_groups_and_orders_source_names(env):
    cfg, _, _ = env
    for source in ("ts_web", "strictlyvc", "tc_startups", "tc_venture", "cbn_seed"):
        insert_history(cfg, source=source, text="不详")
    payload = push(env)
    expected = "TechCrunch、Crunchbase News、StrictlyVC、Term Sheet 没抓到：有一段时间的内容没补上" + wording.FETCH_WARNING_END
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == expected
    assert [item["name"] for item in status(cfg)["missed"]] == ["TechCrunch", "Crunchbase News", "StrictlyVC", "Term Sheet 网页版"]


def test_history_crossing_baseline_is_clipped_without_rewriting_old_row(env):
    cfg, _, _ = env
    row_id = insert_history(cfg, source="tc_venture", text="手写跨基线", start="2026-10-29 10:00", end="2026-11-01 09:00")
    payload = push(env)
    expected = "TechCrunch 没抓到：10-31 00:36 到 11-01 09:00 的内容没补上" + wording.FETCH_WARNING_END
    assert payload["alarm"] is True and payload["text"].splitlines()[1] == expected
    assert "2026-10-31 00:36" in status(cfg)["missed"][0]["range"]
    assert history_row(cfg, row_id)["missed_start"] == "2026-10-29 10:00"


def test_stopped_source_history_does_not_report_missed(env):
    cfg, _, _ = env
    row_id = insert_history(cfg, source="ts_web")
    with db.connect(cfg.db_path) as conn:
        conn.execute("UPDATE source SET enabled=0,crawl_status='已停抓',stop_reason='封禁',"
                     "stopped_at='2026-11-02 08:43',stop_notice_date='2026-10-31' WHERE id='ts_web'")
    payload = push(env)
    assert payload["alarm"] is False and "没抓到" not in payload["text"]
    assert status(cfg)["missed"] == [] and history_row(cfg, row_id)["missed_range"] == "不详"


def test_non_push_run_keeps_empty_text_with_historical_missed(env):
    cfg, fixture, _ = env
    insert_history(cfg, started="2026-11-02 09:00")
    write_fixture(fixture, "2026-11-03")
    payload, _ = run_at(cfg, fixture, "2026-11-03", date=date(2026, 11, 3), no_deep=True)
    assert payload["push"] is False and payload["text"] == "" and payload["alarm"] is False
    assert status(cfg)["missed"] == [{"name": "StrictlyVC", "range": "不详"}]
    assert json.loads(digest_row(cfg, "2026-11-03")["counts_json"]) == {}


@pytest.mark.parametrize("stopped", [False, True], ids=["run-only", "with-stop-notice"])
def test_fetch_all_exception_failure_file_suppresses_historical_missed(env, monkeypatch, stopped):
    cfg, fixture, _ = env
    insert_history(cfg)
    if stopped:
        stop_termsheet(cfg)

    def broken_fetch(*args, **kwargs):
        raise RuntimeError("手写抓取故障")

    monkeypatch.setattr(fetcher, "fetch_all", broken_fetch)
    assert pipeline.run(cfg, arguments(fixture, date=date(2026, 11, 6), no_deep=True),
                        pipeline.Clock(date(2026, 11, 6), day_time(8, 40))) == 1
    payload = json.loads(pipeline._main_file(cfg, date(2026, 11, 6)).read_text())
    expected = wording.RUN_ERRORS["全部失败"] + (STOP_NOTICE if stopped else "")
    assert payload["push"] is True and payload["alarm"] is True
    assert payload["text"].splitlines()[1] == expected
    assert "StrictlyVC" not in payload["text"]
    assert status(cfg)["missed"] == [{"name": "StrictlyVC", "range": "不详"}]
    assert json.loads(digest_row(cfg)["counts_json"])["failed_days"] == 0


@pytest.mark.parametrize("run_status", ["全部失败", "没跑完"])
@pytest.mark.parametrize("stopped", [False, True], ids=["run-only", "with-stop-notice"])
def test_run_wide_failure_suppresses_historical_missed(env, monkeypatch, run_status, stopped):
    cfg, _, _ = env
    insert_history(cfg)
    if stopped:
        stop_termsheet(cfg)
    original = fetcher.fetch_all

    def failed_outcomes(*args, **kwargs):
        outcomes = original(*args, **kwargs)
        if run_status == "全部失败":
            for outcome in outcomes:
                outcome.ok, outcome.error_type = False, ErrorType.UNREACHABLE
                outcome.runs[-1] = replace(outcome.runs[-1], result=FetchResult.FAIL, error_type=ErrorType.UNREACHABLE)
        else:
            outcomes[0].runs[-1] = replace(outcomes[0].runs[-1], result=FetchResult.NOT_RUN)
        return outcomes

    monkeypatch.setattr(fetcher, "fetch_all", failed_outcomes)
    payload = push(env)
    assert payload["alarm"] is True
    assert payload["text"].splitlines()[1] == wording.RUN_ERRORS[run_status] + (STOP_NOTICE if stopped else "")
    assert "StrictlyVC" not in payload["text"]
    assert status(cfg)["missed"] == [{"name": "StrictlyVC", "range": "不详"}]
    assert digest_row(cfg)["status"] == run_status


def test_status_rebuild_uses_current_window_and_generation_time_with_18_keys(env):
    cfg, _, _ = env
    insert_history(cfg)
    payload = push(env)
    old = status(cfg)
    insert_history(cfg, source="alleywatch", started=DAY + " 12:00")
    with db.connect(cfg.db_path) as conn:
        rebuilt = site.write_status(conn, cfg, moment(DAY + " 13:00"))
    assert payload["alarm"] is True and old["missed"] == [{"name": "StrictlyVC", "range": "不详"}]
    assert rebuilt["missed"] == old["missed"] + [{"name": "AlleyWatch", "range": "不详"}]
    assert set(rebuilt) == set(site.RUNTIME_STATUS_KEYS) | set(site.V16_STATUS_KEYS)
    assert len(rebuilt) == 18
