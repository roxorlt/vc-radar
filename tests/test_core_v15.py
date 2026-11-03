from datetime import date, datetime, timedelta, timezone

import pytest

from vcradar import db, scan
from vcradar.config import BJ
from vcradar.models import RawItem, SourceDef


PUSH_DATE = date(2026, 10, 30)
PREVIOUS = datetime(2026, 10, 23, 8, 55, tzinfo=BJ)
SOURCE = SourceDef("synthetic", "合成信源", "news", "https://synthetic.example/feed", "rss", 2, 1)


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "v15.db")
    db.sync_sources(connection, [SOURCE])
    put_fetch(connection, datetime(2026, 1, 1, 8, 40, tzinfo=BJ))
    yield connection
    connection.close()


def put_digest(conn, value, status="没有新增"):
    day = date.fromisoformat(value)
    scan.upsert_digest(conn, {"date": day, "status": status,
                              "generated_at": datetime.combine(day, datetime.min.time(), tzinfo=BJ) + timedelta(hours=8, minutes=55)})


def fill_week(conn):
    for value in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"):
        put_digest(conn, value)


def put_fetch(conn, stamp, missed_range="", result="成功"):
    serial = conn.execute("SELECT COUNT(*) FROM fetch_run").fetchone()[0]
    conn.execute("INSERT INTO fetch_run(run_id, source_id, started_at, ended_at, result, missed_range) "
                 "VALUES (?, ?, ?, ?, ?, ?)",
                 (f"synthetic-{serial}", SOURCE.id, stamp.isoformat(), stamp.isoformat(), result, missed_range))


@pytest.mark.parametrize("value,expected", [
    ("2026-10-26", "2026-10-26"), ("2026-10-30", "2026-10-26"), ("2026-11-01", "2026-10-26"),
    ("2026-01-01", "2025-12-29"), ("2026-01-04", "2025-12-29"), ("2027-01-01", "2026-12-28"),
])
def test_baseline_cutoff_is_monday_independent_of_holiday_calendar(value, expected):
    assert scan.baseline_cutoff(date.fromisoformat(value)) == date.fromisoformat(expected)


@pytest.mark.parametrize("published,expected", [
    ("2026-10-25T15:59:59+00:00", True), ("2026-10-25T16:00:00+00:00", False),
    ("2026-10-26T00:00:00+08:00", False), ("2026-10-29T12:00:00-04:00", False),
    ("2026-10-20T12:00:00+08:00", True),
])
def test_baseline_media_beijing_monday_boundary(published, expected):
    item = RawItem("synthetic", "media", "https://synthetic.example/one", "手写首期样例", datetime.fromisoformat(published),
                   structured={"launched_at": 1})
    assert scan.is_baseline_item(item, PUSH_DATE, True) is expected
    assert scan.is_baseline_item(item, PUSH_DATE, False) is False


@pytest.mark.parametrize("published", [
    "2026-10-25T15:59:59+00:00", "2026-10-25T16:00:00+00:00", "2026-10-27T18:00:00+08:00",
])
def test_baseline_yc_directory_without_publication_ignores_launched_at(published):
    stamp = datetime.fromisoformat(published).timestamp()
    item = RawItem("yc_directory", "yc", "https://www.ycombinator.com/companies/synthetic", "手写 YC 样例", None,
                   structured={"launched_at": stamp})
    assert scan.is_baseline_item(item, PUSH_DATE, True)
    assert not scan.is_baseline_item(item, PUSH_DATE, False)


@pytest.mark.parametrize("value", [None, "invalid", "nan", "inf", 1e30])
def test_missing_or_invalid_yc_launch_time_is_conservatively_baseline(value):
    item = RawItem("yc_directory", "yc", "https://www.ycombinator.com/companies/synthetic", "未知上线日期", None,
                   structured={"launched_at": value})
    assert scan.is_baseline_item(item, PUSH_DATE, True)
    assert not scan.is_baseline_item(item, PUSH_DATE, False)


def test_missing_media_publication_is_conservatively_baseline():
    item = RawItem("synthetic", "undated", "https://synthetic.example/undated", "未知发布日期", None)
    assert scan.is_baseline_item(item, PUSH_DATE, True)
    assert not scan.is_baseline_item(item, PUSH_DATE, False)


@pytest.mark.parametrize("status,expected", [
    ("没跑完", 1), ("全部失败", 1), ("部分失败", 0), ("有新增", 0), ("没有新增", 0), ("基线", 0),
])
def test_failed_days_statuses_on_nonpush_workdays(conn, status, expected):
    fill_week(conn)
    put_digest(conn, "2026-10-27", status)
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == expected


def test_failed_days_count_missing_digest_even_when_fetch_succeeded(conn):
    fill_week(conn)
    conn.execute("DELETE FROM digest WHERE date='2026-10-27'")
    put_fetch(conn, datetime(2026, 10, 27, 8, 40, tzinfo=BJ))
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == 1


def test_failed_days_no_previous_window_starts_at_this_monday(conn):
    assert scan.failed_days_in_window(conn, None, PUSH_DATE) == 4
    fill_week(conn)
    assert scan.failed_days_in_window(conn, None, PUSH_DATE) == 0


@pytest.mark.parametrize("start,expected", [
    ("2026-10-26T08:55:00+08:00", 3), ("2026-10-25T20:55:00+00:00", 3),
    ("2026-10-29T08:55:00+08:00", 0), ("2026-10-30T08:55:00+08:00", 0),
    ("2026-10-31T08:55:00+08:00", 0),
])
def test_failed_days_excludes_start_day_and_push_day(conn, start, expected):
    assert scan.failed_days_in_window(conn, datetime.fromisoformat(start), PUSH_DATE) == expected


def test_failed_days_excludes_previous_and_current_push_day_errors(conn):
    fill_week(conn)
    put_digest(conn, "2026-10-23", "全部失败")
    put_digest(conn, "2026-10-30", "没跑完")
    put_fetch(conn, PREVIOUS, "之前的漏抓")
    put_fetch(conn, datetime(2026, 10, 30, 8, 40, tzinfo=BJ), "当天漏抓")
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == 0


def test_failed_days_missed_range_uses_beijing_date_and_deduplicates_day(conn):
    fill_week(conn)
    put_fetch(conn, datetime(2026, 10, 25, 23, 40, tzinfo=timezone.utc), "手写漏抓区间")
    put_fetch(conn, datetime(2026, 10, 26, 8, 45, tzinfo=BJ), "另一个漏抓区间")
    put_digest(conn, "2026-10-26", "全部失败")
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == 1


def test_failed_days_single_source_failure_then_recovery_is_not_a_failed_day(conn):
    fill_week(conn)
    put_digest(conn, "2026-10-27", "部分失败")
    put_fetch(conn, datetime(2026, 10, 27, 8, 40, tzinfo=BJ), result="失败")
    put_fetch(conn, datetime(2026, 10, 27, 9, 0, tzinfo=BJ), result="成功")
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == 0


def test_failed_days_ignores_empty_missed_range(conn):
    fill_week(conn)
    put_fetch(conn, datetime(2026, 10, 27, 8, 40, tzinfo=BJ), "  ")
    assert scan.failed_days_in_window(conn, PREVIOUS, PUSH_DATE) == 0


def test_failed_days_holiday_week_counts_only_workdays_before_early_push(conn):
    put_digest(conn, "2026-09-28")
    put_digest(conn, "2026-09-29")
    assert scan.failed_days_in_window(conn, None, date(2026, 9, 30)) == 0
    put_fetch(conn, datetime(2026, 9, 29, 8, 40, tzinfo=BJ), "手写漏抓区间")
    assert scan.failed_days_in_window(conn, None, date(2026, 9, 30)) == 1
    start = datetime(2026, 9, 30, 8, 55, tzinfo=BJ)
    assert scan.failed_days_in_window(conn, start, date(2026, 10, 9)) == 1
    put_digest(conn, "2026-10-08")
    assert scan.failed_days_in_window(conn, start, date(2026, 10, 9)) == 0


def test_failed_days_adjusted_saturday_is_a_nonpush_workday(conn):
    for day in ("2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"):
        put_digest(conn, day)
    start = datetime(2026, 10, 9, 8, 55, tzinfo=BJ)
    assert scan.failed_days_in_window(conn, start, date(2026, 10, 16)) == 1
    put_digest(conn, "2026-10-10")
    assert scan.failed_days_in_window(conn, start, date(2026, 10, 16)) == 0


def test_failed_days_skips_full_holiday_week_without_a_previous_window(conn):
    assert scan.failed_days_in_window(conn, None, date(2026, 2, 20)) == 0


def test_digest_push_is_correct_on_every_workday_and_adjusted_saturday(conn):
    expected = {"2026-10-26": 0, "2026-10-27": 0, "2026-10-28": 0, "2026-10-29": 0,
                "2026-10-30": 1, "2026-09-30": 1, "2026-10-10": 0}
    for day in expected:
        put_digest(conn, day, "部分失败")
    assert {row["date"]: row["push"] for row in conn.execute("SELECT date, push FROM digest")} == expected
    assert all(row[0] == "部分失败" for row in conn.execute("SELECT status FROM digest"))


def test_digest_send_timestamps_are_naive_beijing_and_partial_update_preserves_manual_push(conn):
    stamp = datetime(2026, 10, 29, 1, 0, 1, tzinfo=timezone.utc)
    scan.upsert_digest(conn, {"date": "2026-10-29", "push": True, "generated_at": stamp, "sent_at": stamp,
                              "resend_sent_at": "2026-10-29T02:00:02+00:00"})
    scan.upsert_digest(conn, {"date": "2026-10-29", "sent_result": "ok"})
    row = conn.execute("SELECT * FROM digest").fetchone()
    assert row["push"] == 1
    assert row["generated_at"] == row["sent_at"] == "2026-10-29 09:00:01"
    assert row["resend_sent_at"] == "2026-10-29 10:00:02"
