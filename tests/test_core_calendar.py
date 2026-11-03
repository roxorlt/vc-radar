from datetime import date, datetime, timezone

import pytest

from vcradar import cal


@pytest.mark.parametrize("value,expected", [
    ("2026-10-30", True), ("2026-10-29", False), ("2026-10-02", False),
    ("2026-09-30", True), ("2026-09-29", False), ("2026-10-10", False),
    ("2026-09-20", False), ("2026-09-24", True), ("2026-09-25", False),
    ("2026-02-20", False), ("2026-02-19", False),
])
def test_weekly_push(value, expected):
    assert cal.is_push_day(date.fromisoformat(value)) is expected


@pytest.mark.parametrize("value", [
    "2025-01-26", "2025-02-08", "2025-04-27", "2025-09-28", "2025-10-11",
    "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09", "2026-09-20", "2026-10-10",
])
def test_cn_adjusted_weekends(value):
    day = date.fromisoformat(value)
    assert day.weekday() >= 5
    assert cal.is_cn_workday(day)
    assert not cal.is_push_day(day)


@pytest.mark.parametrize("year,ranges", [
    (2025, [(1, 1, 1, 1), (1, 28, 2, 4), (4, 4, 4, 6), (5, 1, 5, 5), (5, 31, 6, 2), (10, 1, 10, 8)]),
    (2026, [(1, 1, 1, 3), (2, 15, 2, 23), (4, 4, 4, 6), (5, 1, 5, 5), (6, 19, 6, 21), (9, 25, 9, 27), (10, 1, 10, 7)]),
])
def test_full_cn_holiday_ranges(year, ranges):
    expected = set()
    from datetime import timedelta
    for sm, sd, em, ed in ranges:
        first, last = date(year, sm, sd), date(year, em, ed)
        expected.update(first + timedelta(days=n) for n in range((last - first).days + 1))
    assert cal.CN_HOLIDAYS[year] == expected
    assert all(not cal.is_cn_workday(day) for day in expected)


@pytest.mark.parametrize("year,dates", [
    (2025, "01-01 01-20 02-17 05-26 06-19 07-04 09-01 10-13 11-11 11-27 12-25"),
    (2026, "01-01 01-19 02-16 05-25 06-19 07-03 09-07 10-12 11-11 11-26 12-25"),
    (2027, "01-01 01-18 02-15 05-31 06-18 07-05 09-06 10-11 11-11 11-25 12-24 12-31"),
])
def test_all_us_holidays_and_observed_rules(year, dates):
    expected = {date.fromisoformat(f"{year}-{value}") for value in dates.split()}
    assert cal.US_FEDERAL_HOLIDAYS[year] == expected
    assert all(not cal.is_us_workday(day) for day in expected)


def test_workday_ranges_and_timezone_dates():
    assert cal.us_workdays_between(date(2026, 11, 25), date(2026, 11, 29)) == 1
    assert cal.us_workdays_between(date(2026, 11, 26), date(2026, 11, 30)) == 2
    assert cal.us_workdays_between(date(2026, 11, 29), date(2026, 11, 26)) == 0
    assert cal.us_workdays_between(date(2026, 11, 26), date(2026, 11, 26)) == 0
    instant = datetime(2026, 10, 24, 5, 45, tzinfo=timezone.utc)
    assert cal.bj_date(instant) == date(2026, 10, 24)
    assert cal.et_date(instant) == date(2026, 10, 24)
    assert cal.et_date(datetime(2026, 11, 2, 1, tzinfo=timezone.utc)) == date(2026, 11, 1)
    with pytest.raises(ValueError):
        cal.bj_date(datetime(2026, 10, 30))


def test_previous_push_day_respects_early_week():
    assert not cal.is_push_day(date(2026, 10, 8))
    assert cal.is_push_day(date(2026, 10, 9))
    assert not cal.is_push_day(date(2026, 10, 10))
    assert cal.is_push_day(date(2026, 9, 30))
    assert cal.previous_push_day(date(2026, 10, 9)) == date(2026, 9, 30)
    assert cal.previous_push_day(date(2026, 10, 30)) == date(2026, 10, 23)
    assert not cal.is_push_day(date(2026, 10, 28))
    assert not cal.is_push_day(date(2026, 10, 4))


@pytest.mark.parametrize("value,expected,early", [
    ("2026-10-30", "2026-10-30", False), ("2026-10-29", "2026-10-30", False),
    ("2026-10-02", "2026-09-30", False), ("2026-09-28", "2026-09-30", False),
    ("2026-09-30", "2026-09-30", True), ("2026-10-08", "2026-10-09", False),
    ("2026-10-09", "2026-10-09", False), ("2026-10-10", "2026-10-09", False),
    ("2026-02-20", None, False), ("2026-02-22", None, False),
])
def test_push_day_of_week_and_early_push(value, expected, early):
    day = date.fromisoformat(value)
    assert cal.push_day_of_week(day) == (date.fromisoformat(expected) if expected else None)
    assert cal.is_early_push(day) is early


@pytest.mark.parametrize("options", [{"mode": "daily"}, {"weekday": 2}, {"holiday_shift": "next_workday"}, {"holiday_shift": "skip"}])
def test_old_schedule_overrides_are_rejected(options):
    with pytest.raises(ValueError, match="V15c"):
        cal.is_push_day(date(2026, 10, 30), **options)


@pytest.mark.parametrize("function,day", [
    (cal.is_cn_workday, date(2027, 1, 1)), (cal.is_cn_workday, date(2024, 1, 6)),
    (cal.is_us_workday, date(2028, 1, 1)), (cal.is_push_day, date(2027, 1, 1)),
])
def test_missing_calendar_never_silently_falls_back(function, day):
    with pytest.raises(cal.CalendarMissing):
        function(day)


@pytest.mark.parametrize("source,start,end", cal.KNOWN_HIATUS)
def test_hiatus_and_calendar_rows(source, start, end):
    from datetime import timedelta
    assert cal.in_hiatus(source, start)
    assert cal.in_hiatus(source, end)
    assert not cal.in_hiatus(source, start - timedelta(days=1))
    assert not cal.in_hiatus(source, end + timedelta(days=1))
    rows = cal.calendar_rows()
    assert (start.isoformat(), "美国", "已知停刊", source) in rows
    assert ("2026-10-10", "中国", "调休上班", None) in rows
    assert ("2026-11-26", "美国", "联邦假日", None) in rows
    assert len(rows) == len(set(rows))
