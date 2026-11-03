"""逐地址抓取 + 出错判定（任务 A）。PRD 第 3 节、P0-2/P0-3/P0-4、9.1、4.1 过期规则。

纯函数式：输入 SourceDef + SourceState + Transport + 时钟，输出 FetchOutcome；不碰数据库。
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from threading import Event
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .config import BJ, ET
from .models import ErrorType, FetchOutcome, FetchResult, FetchRunRecord, HttpResponse, RawItem, SourceDef, SourceState
from .http import Transport, save_raw_response
from .robots import RobotsChecker
from .sources import alleywatch, crunchbase, launch_hn, strictlyvc, techcrunch, termsheet, yc_directory, yc_launches
from .sources.common import Parsed, parse_time

Sleep = Callable[[float], None]


class _Failure(Exception):
    def __init__(self, error, summary, status=None, blocked=False, note=""):
        self.error, self.summary, self.status = error, summary, status
        self.blocked, self.note = blocked, note


class _Cancelled(Exception):
    pass


@dataclass
class _Stats:
    items: list[RawItem] = field(default_factory=list)
    count: int = 0
    latest: datetime | None = None
    label: str = ""
    pages: int = 1
    backfill: bool = False
    missed: str = ""
    missed_start: datetime | None = None
    missed_end: datetime | None = None
    batch: str | None = None
    batch_total: int | None = None


def _intercepted_html(response):
    title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", response.body, re.I | re.S)
    return bool(re.search(r"cf-chl|Just a moment", response.body, re.I) or
                (title and re.search(r"access denied|forbidden|captcha|attention required|verify.*human|security check|challenge", title[1], re.I)))


def _validate(response, kind):
    status = response.status
    if status is None:
        error = ErrorType.TIMEOUT if response.error == "timeout" else ErrorType.UNREACHABLE
        raise _Failure(error, "网站没响应" if error == ErrorType.TIMEOUT else "页面打不开")
    if not 200 <= status < 300:
        blocked = status in {401, 403, 429, 451}
        raise _Failure(ErrorType.RATE_LIMITED if blocked else ErrorType.UNREACHABLE,
                       "对方限制了访问" if blocked else "页面打不开", status, blocked, str(status))
    content_type = response.content_type.lower()
    mime = content_type.partition(";")[0].strip()
    if (kind == "html" or "html" in content_type or re.match(r"\s*(?:<!doctype\s+html|<html\b)", response.body, re.I)) and _intercepted_html(response):
        raise _Failure(ErrorType.INTERCEPTED, "被网站拦下来了", status, True, "拦截页")
    if kind == "json" and mime not in {"application/json", "text/json"} and not mime.endswith("+json"):
        raise _Failure(ErrorType.INTERCEPTED, "JSON 内容类型不符", status)
    if kind in {"rss", "xml"} and mime not in {"application/xml", "text/xml"} and not mime.endswith("+xml"):
        raise _Failure(ErrorType.INTERCEPTED, "RSS/XML 内容类型不符", status)


def _term_exception(src, url):
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    return (src.id == "ts_web" and src.robots_exception and parsed.scheme == "https" and
            parsed.netloc == "content.fortune.com" and
            ((parsed.path == "/wp-json/wp/v2/posts" and params == {"newsletter": ["7249411"]}) or
             (parsed.path == "/newsletter/termsheet/" and set(params) == {"post_id"} and
              len(params["post_id"]) == 1 and params["post_id"][0].isdigit())))


class _Session:
    def __init__(self, src, state, transport, robots, now, sleep, raw_dir):
        self.src, self.state, self.transport, self.robots = src, state, transport, robots
        self.now, self.sleep, self.raw_dir = now, sleep, raw_dir
        self.stats = _Stats(backfill=not state.last_ok)
        self.runs: list[FetchRunRecord] = []
        self.monotonic_start = time.monotonic()
        self.elapsed = 0.0
        self.started = now
        self.last_status = None
        self.requests = 0
        self.retried = False

    def at(self):
        return self.now + timedelta(seconds=max(self.elapsed, time.monotonic() - self.monotonic_start))

    def active(self):
        deadline = getattr(self.transport, "deadline_monotonic", None)
        cancelled = getattr(self.transport, "cancelled", None)
        if (deadline is not None and time.monotonic() >= deadline) or (cancelled is not None and cancelled.is_set()):
            raise _Cancelled

    def pause(self, seconds):
        self.active()
        deadline = getattr(self.transport, "deadline_monotonic", None)
        allowed = seconds if deadline is None else min(seconds, max(0.0, deadline - time.monotonic()))
        started = time.monotonic()
        self.sleep(allowed)
        self.elapsed += max(allowed, time.monotonic() - started)
        if allowed < seconds:
            raise _Cancelled
        self.active()

    def record(self, result, error=None, summary="", note="", status=None):
        stats = self.stats
        return FetchRunRecord(self.src.id, self.started, self.at(), self.last_status if status is None else status,
                              stats.count, result, error, summary, note, stats.latest,
                              stats.backfill, stats.missed, stats.pages, stats.missed_start, stats.missed_end)

    def mark_missed(self, boundary, known=None):
        dates = [stamp for stamp in (boundary, known) if stamp is not None]
        # RSS 的 known 是上次已知起点，boundary 是本次翻到的终点。
        start = min(dates) if len(dates) == 2 else known
        end = max(dates) if len(dates) == 2 else boundary
        baseline = self.state.baseline_at
        if baseline and ((end and end < baseline) or (end is None and start and start < baseline)):
            return
        if baseline and start and start < baseline:
            start = baseline
        self.stats.missed_start, self.stats.missed_end = start, end
        self.stats.missed = _range(start, end)

    def _once(self, url, kind, headers, data):
        self.active()
        if kind == "json":
            headers = {key: value for key, value in (headers or {}).items() if key.lower() != "accept"}
            headers["Accept"] = "application/json"
        def send():
            self.active()
            started = time.monotonic()
            deadline = getattr(self.transport, "deadline_monotonic", None)
            timeout = 30.0 if deadline is None else min(30.0, max(0.001, deadline - started))
            response = self.transport.get(url, headers=headers, timeout=timeout, data=data)
            self.elapsed += max(response.elapsed_s, time.monotonic() - started)
            self.requests += 1
            self.last_status = response.status
            save_raw_response(response, self.raw_dir, self.src.id, self.requests, self.at())
            return response
        response = self.robots.request(url, send, self.pause, exception=_term_exception(self.src, url))
        if response is None:
            self.last_status = None
            raise _Failure(ErrorType.UNREACHABLE, "robots 禁止", None)
        self.active()
        _validate(response, kind)
        return response

    def get(self, url, kind, headers=None, data=None):
        try:
            return self._once(url, kind, headers, data)
        except _Failure as failure:
            if self.src.id != "ts_web" or not failure.blocked or self.retried:
                raise
            self.runs.append(self.record(FetchResult.FAIL, failure.error, failure.summary,
                                         failure.note, failure.status))
            self.retried = True
            self.pause(120)
            self.started = self.at()
            return self._once(url, kind, headers, data)


def _check(parsed: Parsed, session):
    if parsed.error_type:
        raise _Failure(parsed.error_type, parsed.error_summary, session.last_status)


def _page_url(src, page):
    if page == 1:
        return src.url
    parsed = urlsplit(src.url)
    separator = "&" if parsed.query else ""
    key = "paged" if src.kind == "rss" else "page"
    query = parsed.query + separator + urlencode({key: page})
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _range(oldest, known_latest=None):
    def label(value):
        return value.astimezone(BJ).strftime("%Y-%m-%d %H:%M") if value else "时间未知"
    # SourceState 没有上次最新时间，不能根据外部 id 编造这个上界。
    upper = label(known_latest) if known_latest else "上次已知最新时间（状态未提供）" if oldest else "时间未知"
    return f"{label(oldest)} ~ {upper}（北京时间）"


def missed_interval(text: str, start=None, end=None, *, reference=None) -> tuple[datetime | None, datetime | None]:
    """优先读取结构化起止时间；历史文本只在读取时解析，不改旧记录。"""
    def moment(value):
        if value is None:
            return None
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        return stamp.replace(tzinfo=BJ) if stamp.tzinfo is None else stamp.astimezone(BJ)
    if start is not None or end is not None:
        return moment(start), moment(end)
    matches = list(re.finditer(r"(?<!\d)(?:(\d{4})-)?(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?(?!\d)", text))
    values = []
    reference = moment(reference) if reference else None
    for match in matches:
        year = int(match[1]) if match[1] else reference.year if reference else None
        if year is None:
            continue
        try:
            stamp = datetime(year, int(match[2]), int(match[3]), int(match[4] or 0), int(match[5] or 0), int(match[6] or 0), tzinfo=BJ)
            if not match[1] and reference and stamp > reference + timedelta(days=1):
                stamp = stamp.replace(year=year - 1)
            values.append(stamp)
        except ValueError:
            continue
    if len(values) >= 2:
        return min(values), max(values)
    if values:
        return (None, values[0]) if "之前" in text or "时间未知" in text or "起点未知" in text else (values[0], None)
    return None, None


def has_missed(text: str, baseline_at=None, start=None, end=None, *, reference=None) -> bool:
    """按终点、无终点时按起点过滤基线前漏抓；两端未知或无基线时照算。"""
    if not (text and text.strip()) and start is None and end is None:
        return False
    since, until = missed_interval(text, start, end, reference=reference or baseline_at)
    boundary = until if until is not None else since
    return baseline_at is None or boundary is None or boundary >= baseline_at


def merge_missed_ranges(segments, baseline_at=None, *, reference=None):
    """合并仍有效的漏抓段；任一段两端未知时保留未知，跨基线截断。"""
    valid = []
    for text, start, end in segments:
        if not has_missed(text, baseline_at, start, end, reference=reference):
            continue
        start, end = missed_interval(text, start, end, reference=reference)
        clipped = bool(start and baseline_at and start < baseline_at)
        valid.append((text, baseline_at if clipped else start, end, clipped))
    if not valid:
        return "", None, None
    unknown = next((span for span in valid if span[1] is None and span[2] is None), None)
    if unknown is not None:
        return unknown[0], None, None
    start = min((span[1] for span in valid if span[1] is not None), default=None)
    end = max((span[2] for span in valid if span[2] is not None), default=None)
    # 单段的历史原文仍供内部状态查看；合并、截断才重写内存中的显示文本。
    text = valid[0][0] if len(valid) == 1 and valid[0][0] and not valid[0][3] else _range(start, end)
    return text, start, end


def _feeds(session):
    src, state, stats = session.src, session.state, session.stats
    if src.kind == "rss":
        parser = alleywatch.parse if src.id == "alleywatch" else crunchbase.parse if src.name == "Crunchbase News" else techcrunch.parse
        kind = "rss"
    else:
        parser = launch_hn.parse if src.id == "launch_hn" else yc_launches.parse
        kind = "json"
    seen = set()
    oldest = None
    for page in range(1, 4 if not state.last_ok else 2):
        stats.pages = page
        try:
            parsed = parser(session.get(_page_url(src, page), kind), src.id)
            _check(parsed, session)
        except _Failure:
            if page > 1:
                session.mark_missed(oldest, state.last_latest_at)
            raise
        if parsed.latest_at and (stats.latest is None or parsed.latest_at > stats.latest):
            stats.latest = parsed.latest_at
        stats.count += len(parsed.items)
        stats.label = f"{stats.count} 条"
        caught = False
        for item in parsed.items:
            if item.published_at is not None:
                oldest = min(oldest, item.published_at) if oldest else item.published_at
            if not state.last_ok and item.external_id in state.known_external_ids:
                caught = True
                break
            if item.external_id not in seen and not item.structured.get("skip_reason"):
                seen.add(item.external_id)
                if not state.last_ok:
                    item.structured["backfill"] = True
                stats.items.append(item)
        if state.last_ok or caught:
            break
        if page == 3:
            session.mark_missed(oldest, state.last_latest_at)
    if stats.count == 0:
        raise _Failure(ErrorType.EMPTY, "页面能打开，但一条都读不出来", session.last_status)


def _directory(session):
    stats = session.stats
    try:
        bootstrap = session.get(session.src.url, "html")
        if bootstrap.status != 200:
            raise ValueError
        options = yc_directory.options(bootstrap)
        if options is None:
            raise ValueError
        endpoint = f'https://{options["app"].lower()}-dsn.algolia.net/1/indexes/*/queries'
        headers = {"X-Algolia-Application-Id": options["app"], "X-Algolia-API-Key": options["key"],
                   "Content-Type": "application/json", "Referer": session.src.url}
        batches_response = session.get(endpoint, "json", headers, yc_directory.query_body())
        if batches_response.status != 200:
            raise ValueError
        batches = yc_directory.latest_batches(batches_response)
        if not batches:
            raise ValueError
        for batch in batches:
            response = session.get(endpoint, "json", headers, yc_directory.query_body(batch))
            if response.status != 200:
                raise ValueError
            parsed = yc_directory.parse(response, session.src.id)
            _check(parsed, session)
            if stats.batch is None:
                stats.batch, stats.batch_total = batch, yc_directory.total(response)
                stats.label = f"当期 {stats.batch_total} 家"
                stats.latest = max((at for item in parsed.items
                                    if (at := parse_time(item.structured.get("launched_at"))) is not None), default=None)
            for item in parsed.items:
                if not session.state.last_ok and item.external_id not in session.state.known_external_ids:
                    item.structured["backfill"] = True
            stats.items.extend(parsed.items)
            stats.count += len(parsed.items)
    except _Failure as failure:
        if failure.summary == "robots 禁止" or failure.error == ErrorType.RATE_LIMITED:
            raise
        raise _Failure(ErrorType.YC_UNREADABLE, "读不到公司目录", session.last_status) from None
    except (ValueError, TypeError, KeyError, IndexError):
        raise _Failure(ErrorType.YC_UNREADABLE, "读不到公司目录", session.last_status) from None
    previous = session.state
    if (stats.batch == previous.yc_last_batch and previous.yc_last_batch_total is not None and
            stats.batch_total * 2 < previous.yc_last_batch_total):
        raise _Failure(ErrorType.YC_DROP, "公司目录数量比上次少一半以上", session.last_status)


def _newsletters(session):
    src, state, stats = session.src, session.state, session.stats
    strict = src.id == "strictlyvc"
    response = session.get(src.url, "xml" if strict else "json")
    try:
        issues = strictlyvc.issues(response) if strict else termsheet.issues(response)
    except ValueError:
        raise _Failure(ErrorType.INTERCEPTED, "通讯期列表无法解析", session.last_status) from None
    if not issues:
        raise _Failure(ErrorType.EMPTY, "通讯期列表为空", session.last_status)
    def known(issue):
        return issue.url in state.known_issue_urls or (issue.external_id and issue.external_id in state.known_issue_urls)
    pending = [issue for issue in issues if not known(issue) and
               (state.baseline_at is None or issue.at is None or issue.at >= state.baseline_at)]
    # 最新已知期也读一遍，使用真实发布时间检查过期，不能用 lastmod 代替。
    selected = pending[:3]
    if not selected or selected[0].url != issues[0].url:
        selected = [issues[0]] + selected[:2]
    missing = [issue for issue in pending if issue not in selected]

    def mark_missing():
        # 只取本次没抓成的缺期，不把已知期或已成功补回的期夹进漏抓段。
        dates = [issue.at for issue in missing if issue.at is not None]
        if missing:
            session.mark_missed(min(dates) if dates else None, max(dates) if dates else None)

    if state.known_issue_urls or not state.last_ok:
        mark_missing()
    for index, issue in enumerate(selected):
        stats.pages = index + 1
        try:
            page = session.get(issue.url, "html")
            parsed = (strictlyvc.parse(page, src.id, issue.url) if strict else
                      termsheet.parse(page, src.id, issue.url, issue.at))
            if index == 0:
                stats.latest = parsed.latest_at
                stats.count = len(parsed.items)
                if stats.latest:
                    stats.label = f"最新一期 {stats.latest.astimezone(BJ):%m-%d}"
            _check(parsed, session)
        except _Failure:
            if index > 0 or state.known_issue_urls or not state.last_ok:
                missing.extend(ref for ref in selected[index:] if not known(ref))
                mark_missing()
            raise
        if not known(issue):
            for item in parsed.items:
                if not state.last_ok:
                    item.structured["backfill"] = True
                stats.items.append(item)


def _not_run(src, now):
    return FetchOutcome(src, [FetchRunRecord(src.id, now, now, None, 0, FetchResult.NOT_RUN,
                                          error_summary="这次没轮到")], [], False)


def fetch_source(src: SourceDef, state: SourceState, transport: Transport, robots: RobotsChecker,
                 now: datetime, us_workdays_between: Callable, sleep: Sleep, raw_dir=None) -> FetchOutcome:
    """抓一个地址（含翻页补抓 ≤3 页、过期判定、拦截判定、Term Sheet 封禁重试和停抓）。
    us_workdays_between(et_date_a, et_date_b) -> int 由 cal 模块提供（注入，便于测试）。
    sleep 用于 Term Sheet 封禁后等 120 秒（测试注入假 sleep）。"""
    if src.id == "ts_web" and state.crawl_status == "已停抓":
        return FetchOutcome(src, [], [], True, ErrorType.TS_STOPPED)
    session = _Session(src, state, transport, robots, now, sleep, raw_dir)
    error = None
    stopped = False
    summary = ""
    note = ""
    try:
        if src.id == "yc_directory":
            _directory(session)
        elif src.id in {"strictlyvc", "ts_web"}:
            _newsletters(session)
        else:
            _feeds(session)
        stats = session.stats
        if stats.latest and src.stale_days is not None and us_workdays_between(
                stats.latest.astimezone(ET).date(), now.astimezone(ET).date()) > src.stale_days:
            error = ErrorType.NEWSLETTER_STALE if src.id in {"strictlyvc", "ts_web"} else ErrorType.STALE
            summary = "最新一期太久没更新" if error == ErrorType.NEWSLETTER_STALE else "最新一条太久没更新"
    except _Cancelled:
        return _not_run(src, now)
    except _Failure as failure:
        error, summary = failure.error, failure.summary
        note = failure.note if session.retried else ""
        stopped = src.id == "ts_web" and session.retried and failure.blocked
        if stopped:
            error = ErrorType.TS_STOPPED
    except Exception as exc:
        error, summary = ErrorType.UNREACHABLE, f"抓取失败（{type(exc).__name__}）"
    stats = session.stats
    if session.retried and not note:
        note = str(session.last_status) if session.last_status is not None else ("超时" if error == ErrorType.TIMEOUT else "连接失败")
    result = (FetchResult.STOPPED if stopped else FetchResult.STALE if error in {ErrorType.STALE, ErrorType.NEWSLETTER_STALE}
              else FetchResult.FAIL if error else FetchResult.SUCCESS)
    session.runs.append(session.record(result, error, summary, note))
    return FetchOutcome(src, session.runs, stats.items, error is None or stopped, error, stopped,
                        stats.label, stats.latest.astimezone(BJ).date() if stats.latest else None,
                        stats.missed, stats.batch, stats.batch_total, stats.missed_start, stats.missed_end)


def fetch_all(sources: list[SourceDef], states: dict[str, SourceState], transport: Transport,
              robots: RobotsChecker, now: datetime, us_workdays_between: Callable, sleep: Sleep,
              deadline_monotonic: float | None = None, raw_dir=None, max_workers: int = 8) -> list[FetchOutcome]:
    """Term Sheet 网页版最先发起；其余地址随后并行（TS 封禁后等 120 秒重试不阻塞其他地址）。到 deadline 还没轮到/没完成的
    地址返回 FetchResult.NOT_RUN。"""
    ordered = sorted(sources, key=lambda src: src.order)
    if not ordered:
        return []
    cancelled = Event()
    first_request_done = Event()

    class BudgetTransport:
        def __init__(self, signal_first=False):
            self.deadline_monotonic = deadline_monotonic
            self.cancelled = cancelled
            self.signal_first = signal_first

        def get(self, *args, **kwargs):
            try:
                return transport.get(*args, **kwargs)
            finally:
                if self.signal_first:
                    first_request_done.set()

    def remaining():
        return None if deadline_monotonic is None else max(0.0, deadline_monotonic - time.monotonic())

    pool = ThreadPoolExecutor(max_workers=max(1, max_workers))
    futures = {}
    completed = {}
    try:
        for src in ordered:
            if src.id == "ts_web" and states.get(src.id, SourceState(src.id)).crawl_status == "已停抓":
                completed[src.id] = FetchOutcome(src, [], [], True, ErrorType.TS_STOPPED)
        ts = next((src for src in ordered if src.id == "ts_web" and src.id not in completed), None)
        if ts and (remaining() is None or remaining() > 0):
            future = pool.submit(fetch_source, ts, states.get(ts.id, SourceState(ts.id)), BudgetTransport(True),
                                 robots, now, us_workdays_between, sleep, raw_dir)
            futures[ts.id] = future
            future.add_done_callback(lambda _: first_request_done.set())
            # 首个 TS 响应之后即放行；两分钟重试等待在自己的线程里。
            first_request_done.wait(remaining())
        for src in ordered:
            if src.id in futures or src.id in completed or (remaining() is not None and remaining() <= 0):
                continue
            futures[src.id] = pool.submit(fetch_source, src, states.get(src.id, SourceState(src.id)),
                                         BudgetTransport(), robots, now, us_workdays_between, sleep, raw_dir)
        if futures:
            done, _ = wait(futures.values(), timeout=remaining())
            for source_id, future in futures.items():
                if future in done:
                    completed[source_id] = future.result()
    finally:
        cancelled.set()
        for future in futures.values():
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return [completed.get(src.id, _not_run(src, now)) for src in ordered]
