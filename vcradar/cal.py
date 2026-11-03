"""两套日历（任务 B）。PRD 4.1、10.1 a/m；calendar 表的数据源也是这里的常量。

中国：国务院办公厅放假通知（2025、2026 手抄）；美国：OPM 联邦假日（2025、2026、2027）。
已知停刊：Term Sheet 2025-12-24～2026-01-05；StrictlyVC 2025-12-20～2026-01-06、2026-08-01～2026-08-18。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .config import BJ, ET


def _days(start: date, end: date) -> set[date]:
    return {start + timedelta(days=n) for n in range((end - start).days + 1)}


# 按国务院办公厅 2025、2026 年放假通知手抄；放假区间包含周末。
CN_HOLIDAYS: dict[int, set[date]] = {
    2025: {date(2025, 1, 1)}
    | _days(date(2025, 1, 28), date(2025, 2, 4))
    | _days(date(2025, 4, 4), date(2025, 4, 6))
    | _days(date(2025, 5, 1), date(2025, 5, 5))
    | _days(date(2025, 5, 31), date(2025, 6, 2))
    | _days(date(2025, 10, 1), date(2025, 10, 8)),
    2026: _days(date(2026, 1, 1), date(2026, 1, 3))
    | _days(date(2026, 2, 15), date(2026, 2, 23))
    | _days(date(2026, 4, 4), date(2026, 4, 6))
    | _days(date(2026, 5, 1), date(2026, 5, 5))
    | _days(date(2026, 6, 19), date(2026, 6, 21))
    | _days(date(2026, 9, 25), date(2026, 9, 27))
    | _days(date(2026, 10, 1), date(2026, 10, 7)),
}
CN_ADJUSTED_WORKDAYS: dict[int, set[date]] = {
    2025: {date(2025, 1, 26), date(2025, 2, 8), date(2025, 4, 27),
           date(2025, 9, 28), date(2025, 10, 11)},
    2026: {date(2026, 1, 4), date(2026, 2, 14), date(2026, 2, 28),
           date(2026, 5, 9), date(2026, 9, 20), date(2026, 10, 10)},
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _observed(d: date) -> date:
    # OPM：周六的固定假日提前到周五，周日的顺延到周一。
    return d + timedelta(days={5: -1, 6: 1}.get(d.weekday(), 0))


def _us_holidays(year: int) -> set[date]:
    last_may = date(year, 5, 31)
    result = {
        _nth_weekday(year, 1, 0, 3), _nth_weekday(year, 2, 0, 3),
        last_may - timedelta(days=last_may.weekday()),
        _nth_weekday(year, 9, 0, 1), _nth_weekday(year, 10, 0, 2),
        _nth_weekday(year, 11, 3, 4),
    }
    # 包括次年元旦的提前补休（2028-01-01 周六 → 2027-12-31）。
    for actual_year in (year - 1, year, year + 1):
        for month, day in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25)):
            observed = _observed(date(actual_year, month, day))
            if observed.year == year:
                result.add(observed)
    return result


US_FEDERAL_HOLIDAYS: dict[int, set[date]] = {
    year: _us_holidays(year) for year in (2025, 2026, 2027)
}
KNOWN_HIATUS: list[tuple[str, date, date]] = [
    ("Term Sheet", date(2025, 12, 24), date(2026, 1, 5)),
    ("StrictlyVC", date(2025, 12, 20), date(2026, 1, 6)),
    ("StrictlyVC", date(2026, 8, 1), date(2026, 8, 18)),
]


class CalendarMissing(Exception):
    """某年日历缺失（跨年时报警给维护者x）。"""


def is_cn_workday(d: date) -> bool:
    """中国工作日，优先使用调休和法定假日表；缺年抛 CalendarMissing。"""
    if d.year not in CN_HOLIDAYS or d.year not in CN_ADJUSTED_WORKDAYS:
        raise CalendarMissing(f"缺少 {d.year} 年中国日历")
    if d in CN_ADJUSTED_WORKDAYS[d.year]:
        return True
    return d.weekday() < 5 and d not in CN_HOLIDAYS[d.year]


def is_us_workday(d: date) -> bool:
    """美国周一至周五且不在 OPM 联邦补休日表中的日期。"""
    if d.year not in US_FEDERAL_HOLIDAYS:
        raise CalendarMissing(f"缺少 {d.year} 年美国日历")
    return d.weekday() < 5 and d not in US_FEDERAL_HOLIDAYS[d.year]


def is_push_day(d: date, mode: str = "weekly", weekday: int = 4, holiday_shift: str = "prev_workday") -> bool:
    """V15c：只在当周周一～周五中的最后一个中国工作日推送，周末调休不推。"""
    if (mode, weekday, holiday_shift) != ("weekly", 4, "prev_workday"):
        raise ValueError("V15c 仅支持 weekly、weekday=4、holiday_shift=prev_workday")
    is_cn_workday(d)
    return d == push_day_of_week(d)


def push_day_of_week(d: date) -> date | None:
    """返回 d 所在周周一～周五里最后一个中国工作日；整周放假返回 None。"""
    monday = d - timedelta(days=d.weekday())
    for offset in range(4, -1, -1):
        candidate = monday + timedelta(days=offset)
        if is_cn_workday(candidate):
            return candidate
    return None


def is_early_push(d: date) -> bool:
    """当天是否因周五放假而提前推送；同周的其他日期返回 False。"""
    return is_push_day(d) and d.weekday() < 4


def since_last_push(prev_push_date: date | None, today: date) -> bool:
    """两次推送的北京日期相差超过 7 个自然日才用「上次推送后」；首期返回 False。"""
    return prev_push_date is not None and (today - prev_push_date).days > 7


def previous_push_day(d: date, **kw) -> date | None:
    """严格早于 d 的最近一个推送日（最多往前找 60 天）。"""
    for n in range(1, 61):
        candidate = d - timedelta(days=n)
        if is_push_day(candidate, **kw):
            return candidate
    return None


def us_workdays_between(et_a: date, et_b: date) -> int:
    """(et_a, et_b] 区间里美国工作日数（周末、联邦假日不计）。et_b <= et_a 返回 0。"""
    return sum(is_us_workday(et_a + timedelta(days=n))
               for n in range(1, max(0, (et_b - et_a).days) + 1))


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return dt


def et_date(dt: datetime) -> date:
    """带时区的时刻对应的美东日期（自动处理夏令时）。"""
    return _aware(dt).astimezone(ET).date()


def bj_date(dt: datetime) -> date:
    """带时区的时刻对应的北京日期。"""
    return _aware(dt).astimezone(BJ).date()


def in_hiatus(source_name: str, d: date) -> bool:
    """信源是否处于已知停刊区间，含起止日。"""
    return any(name.casefold() == source_name.casefold() and start <= d <= end
               for name, start, end in KNOWN_HIATUS)


def calendar_rows() -> list[tuple[str, str, str, str | None]]:
    """calendar 表行：(日期 iso, 国家 中国/美国, 类型 法定假日/调休上班/联邦假日/已知停刊, 信源名)。"""
    result = []
    for holidays, country, kind in (
        (CN_HOLIDAYS, "中国", "法定假日"),
        (CN_ADJUSTED_WORKDAYS, "中国", "调休上班"),
        (US_FEDERAL_HOLIDAYS, "美国", "联邦假日"),
    ):
        result.extend((d.isoformat(), country, kind, None)
                      for dates in holidays.values() for d in dates)
    for source, start, end in KNOWN_HIATUS:
        result.extend((d.isoformat(), "美国", "已知停刊", source) for d in _days(start, end))
    return sorted(result, key=lambda row: (row[0], row[1], row[2], row[3] or ""))
