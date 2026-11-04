"""推送与页面卡片文字（任务 C），遵守 PRD V16e 第 9、9.1 节。"""
from __future__ import annotations

from datetime import date, datetime
import re
import json
from pathlib import Path
from dataclasses import replace

from .config import BJ, Config, validate_link_path_template
from .fetcher import has_missed, missed_interval
from . import wording
from .fx import format_amount_text
from .models import Card, DigestData, ErrorType
from .sources.registry import DISPLAY_ORDER, display_rank


ERROR_TEXT = wording.ERROR_TEXT
_RUN_ERROR_TEXT = wording.RUN_ERRORS
_MARKS = wording.MARK_ORDER
_WEEKDAYS = "一二三四五六日"


def link_base_url(base_url: str, link_path_template: str, push_date: date) -> str:
    """按推送日期拼接回看基址；空基址或空模板沿用原行为。"""
    validate_link_path_template(link_path_template)
    if not base_url or not link_path_template:
        return base_url
    path = link_path_template.format(date=push_date.isoformat()).strip("/")
    return f"{base_url.rstrip('/')}/{path}"


def render_push(
    d: DigestData, *, early_push: bool = False, failed_days: int = 0,
    incomplete_until: date | None = None, since_last_push: bool = False,
    filtered_count: int | None = None, first_seen: dict[str, datetime] | None = None,
    links_file: Path | None = None,
) -> str:
    """生成 V17 推送，只展示公司内容和一行抓取报警；统计输入保持原样。"""
    if not d.review_site_passed:
        d = replace(d, base_url="")
    title = wording.PUSH_TITLE.format(day=f"{d.push_date:%m-%d}", weekday=_WEEKDAYS[d.push_date.weekday()])
    lines = [title]
    warning = fetch_warning(d)
    if warning:
        lines.append(warning)
    if not d.cards:
        lines.append(wording.NO_NEW)
    official = official_links(links_file)
    for number, card in enumerate(sort_cards(d.cards, first_seen=first_seen)[:10], start=1):
        lines.append(f"{number}. {_highlight(card, official)}")
    if d.yc_new_batch is not None:
        yc = wording.YC_HINT.format(count=d.yc_new_batch[1])
        lines.append(yc + (f"，[名单见网页]({_url(d.base_url, 'yc')})" if d.base_url else ""))
    for section in d.extra_sections:
        if not section.cards:
            continue
        lines.append(section.title)
        cards = section.cards if section.preserve_order else sort_cards(section.cards, first_seen=first_seen)
        lines.extend(f"{i}. {_highlight(card, official)}" for i, card in enumerate(cards[:10], 1))
        overflow = section.overflow_count or max(0, len(section.cards) - 10)
        if overflow > 0:
            lines.append(wording.OVERFLOW_LINK.format(url=_url(d.base_url, 'backfill'))
                         if d.base_url else wording.OVERFLOW)
    if d.base_url:
        lines.append(wording.PAGE_LINKS.format(week=_url(d.base_url, 'week')))
    return "\n".join(lines)


def _missed_reason(source, reference, baseline_at=None):
    start, end = missed_interval(source.missed_range, source.missed_start, source.missed_end, reference=reference)
    label = lambda value: value.astimezone(BJ).strftime("%m-%d %H:%M")
    if start and baseline_at and start < baseline_at:
        start = baseline_at
    if start and end:
        span = label(end) if label(start) == label(end) else f"{label(start)} 到 {label(end)}"
        reason = span + " 的内容没补上"
    elif end:
        span = f"{label(end)} 之前"
        reason = span + "的内容没补上"
    elif start:
        span = f"{label(start)} 之后"
        reason = span + "的内容没补上"
    else:
        return "有一段时间的内容没补上", ("missed", None, None)
    return reason, ("missed", span)


def fetch_warning(d: DigestData) -> str:
    """按类型及全部参数合并报警；停抓句始终单独接在同一行。"""
    messages = []
    if d.status in _RUN_ERROR_TEXT:
        messages.append(_RUN_ERROR_TEXT[d.status])
    else:
        failures = []
        for source in d.sources:
            if source.stopped:
                continue
            if not source.ok:
                error = source.error_type or ErrorType.UNREACHABLE
                if error == ErrorType.TS_STOPPED:
                    continue
                day = source.latest_beijing_date.strftime("%m-%d") if source.latest_beijing_date else "日期不明"
                reason = wording.PUSH_FAILURE_REASONS.get(error, "页面打不开").format(date=day)
                signature = (error, day if error in {ErrorType.STALE, ErrorType.NEWSLETTER_STALE} else None)
            elif has_missed(source.missed_range, d.baseline_at, source.missed_start, source.missed_end, reference=d.window_end):
                reason, signature = _missed_reason(source, d.window_end, d.baseline_at)
            else:
                continue
            rank = (display_rank(source.source_id) if source.source_id is not None else
                    DISPLAY_ORDER.index(source.name) if source.name in DISPLAY_ORDER else len(DISPLAY_ORDER))
            failures.append((source.name, reason, signature, rank))
        if failures:
            failures.sort(key=lambda item: (item[3], item[0]))
            names = "、".join(dict.fromkeys(item[0] for item in failures))
            reason = failures[0][1] if all(item[2] == failures[0][2] for item in failures) else "网站出了问题"
            messages.append(f"{wording.warning_subject(names)}没抓到：{reason}" + wording.FETCH_WARNING_END)
    if d.ts_stopped_notice:
        reason = "对方要求停止抓取" if d.ts_stopped_notice == "异议" else "对方不再允许抓取"
        messages.append("Term Sheet 没抓到：" + reason + wording.FETCH_WARNING_END)
    return "".join(messages)


def first_lines(
    d: DigestData, *, early_push: bool = False, failed_days: int = 0,
    since_last_push: bool = False, incomplete_until: date | None = None,
) -> list[str]:
    """返回唯一报警行；旧计数和提示参数只保留接口兼容。"""
    warning = fetch_warning(d)
    return [warning] if warning else []


def remove_page_links(text: str) -> str:
    """去掉新旧推送中的站内链接和网页提示，保留官网及来源链接。"""
    lines = []
    for line in text.splitlines():
        if re.match(r"\[看本周全部(?: \d+ 家)?\]", line):
            continue
        line = re.sub(r"\[((?:本周|上次推送后) \d+ 家(?:有新证据|资料有出入))\]\([^\n]*?\)", r"\1", line)
        line = re.sub(r"(YC 新(?:批次|一批) \d+ 家)[，,].*", r"\1", line)
        line = re.sub(r"另有 (\d+) 家(?:没有列出)?，\[在往期页看\]\([^\n]*\)", r"另有 \1 家没有列出", line)
        line = re.sub(r"还有一些没有列出，\[在往期页看\]\([^\n]*\)", wording.OVERFLOW, line)
        lines.append(line)
    return "\n".join(lines)


def push_first_lines(text: str) -> tuple[str, str]:
    """读取新旧计数及可选提示行，不把名单、段名和来源状态当提示。"""
    lines = text.removeprefix("重发\n").splitlines()
    counts = lines[1] if len(lines) > 1 else ""
    hints = lines[2] if len(lines) > 2 else ""
    if re.search(r"(?:没抓到|可能没更新|可能出错)：", counts):
        return "", counts
    if not counts.startswith(("本周共", "上次推送后共", "本周没有", "没有新增")):
        counts = ""
    prefixes = ("已收录 ", "首次运行收录 ", "另补充此前漏掉的 ", "本周一至 ", "本周一到 ",
                "提前推送", "重发", "今天", "本周有 ", "可能有遗漏：", "有漏抓：", "Term Sheet 已", "[本周 ",
                "[上次推送后 ", "本周 ", "上次推送后 ")
    if hints and not hints.startswith(prefixes) and not re.search(r"(?:没抓到|可能没更新|可能出错)：", hints):
        hints = ""
    return counts, hints


def add_resend_notice(text: str) -> str:
    """重发沿用公司内容，显示时去掉新旧统计提示，不回写原主文件。"""
    lines = text.removeprefix("重发\n").splitlines()
    if not lines:
        return ""
    warning = warning_from_text(text)
    content, footer = [], ""
    for line in lines[1:]:
        if match := re.search(r"\[看本周全部(?: \d+ 家)?\]\(([^)]+)\)", line):
            footer = wording.PAGE_LINKS.format(week=match[1])
        elif line.startswith("另有 ") and "没有列出" in line:
            link = re.search(r"\[在往期页看\]\(([^)]+)\)", line)
            content.append(wording.OVERFLOW_LINK.format(url=link[1]) if link else wording.OVERFLOW)
        elif line.startswith(("还有一些没有列出", "YC 新一批")) or re.match(r"\d+\. ", line) or line == wording.NO_NEW:
            content.append(line)
        elif line and not re.search(
            r"本周共|上次推送后|已过滤|不算新增|没抓全|来源网站|已接入|上一次有新公司|"
            r"首次运行|已收录|另补充|另有 \d+ 家轮次|提前推送|重发|本周有 \d+ 天|"
            r"YC 新(?:批次|一批)|资料有出入|有新证据|没抓到：|可能没更新：|可能出错：|"
            r"今天没抓完|今天所有来源|Term Sheet 已|可能有遗漏", line
        ):
            content.append(line)
    return "\n".join([lines[0], *([warning] if warning else []), *content,
                       *([footer] if footer else [])])


def warning_from_text(text: str) -> str:
    """从新旧推送中只取抓取报警，供本周页与历史重发共用。"""
    failures = []
    for line in text.splitlines():
        if re.match(r"\d+\. ", line):
            continue
        if wording.FETCH_WARNING_END in line and " · " not in line and "；" not in line and not any(
            old in line for old in ("提前推送", "今天没抓完", "今天所有来源都没抓到", "可能没更新", "可能出错")):
            return wording.normalize_warning(line.strip())
        for part in re.split(r" · |；", line):
            if "今天所有来源都没抓到" in part:
                failures.append(wording.RUN_ERRORS["全部失败"].removesuffix(wording.FETCH_WARNING_END))
            elif "今天没抓完" in part:
                failures.append(wording.RUN_ERRORS["没跑完"].removesuffix(wording.FETCH_WARNING_END))
            else:
                match = re.match(r"(.+?)(?:没抓到|可能没更新|可能出错)：([^。]+)", part)
                if match:
                    failures.append(f"{wording.warning_subject(match[1].strip())}没抓到：{match[2]}")
        for match in re.finditer(r"可能有遗漏：([^，]+)，[^·\n]+", line):
            failures.append(f"{wording.warning_subject(match[1].strip())}没抓到：此前遗漏的内容还没有补齐")
    return wording.normalize_warning("；".join(dict.fromkeys(failures)) + wording.FETCH_WARNING_END) if failures else ""


def is_alarm(d: DigestData, *, failed_days: int = 0, push: bool = True) -> bool:
    """当天抓取错误与基线后漏抓报警；停抓提示只在实际推送日报警。"""
    today_failed = (d.status in _RUN_ERROR_TEXT
                    or any(not source.ok and not source.stopped and source.error_type != ErrorType.TS_STOPPED for source in d.sources)
                    or any(not source.stopped and has_missed(source.missed_range, d.baseline_at, source.missed_start, source.missed_end, reference=d.window_end) for source in d.sources))
    return today_failed or (push and bool(d.ts_stopped_notice))


def sort_cards(cards: list[Card], *, first_seen: dict[str, datetime] | None = None) -> list[Card]:
    """返回完整名单排序副本：披露、金额待核、币种待确认、未披露；同额按标记、入库时间、名字。

    first_seen 的键为 company_id，值须带时区；未提供的时间排在已提供的之后。
    """
    first_seen = first_seen or {}

    def key(card: Card) -> tuple:
        bucket = (4 if card.amount_status == "金额待确认" or card.amount_label == "金额待确认" else
                  1 if {"金额待核", "金额说法不一"}.intersection(card.marks) else
                  2 if card.amount_status in {"币种待确认", "暂未换算"}
                  or "币种待确认" in card.amount_label or "暂未换算成美元" in card.amount_label else
                  3 if card.amount_usd is None else 0)
        first = first_seen.get(card.company_id)
        if first is not None and first.utcoffset() is None:
            raise ValueError("first_seen 必须使用带时区的 datetime")
        return (bucket, -card.amount_usd if bucket < 2 and card.amount_usd is not None else 0,
                "新一轮" not in card.marks, "大额" not in card.marks,
                first.timestamp() if first is not None else float("inf"),
                card.name.casefold(), card.company_id)

    return sorted(cards, key=key)


def render_card(c: Card, base_url: str) -> str:
    """渲染三行卡片，保留全部来源；base_url 为空时省略详情链接。"""
    funding = " · ".join((c.round_label or "轮次未披露", format_amount_text(c.amount_label),
                          c.lead_label or "领投未披露"))
    header = f"{c.name}{_marks(c)}｜{c.country}｜{wording.display_text(funding)}｜{wording.display_text(c.tag_label)}"
    summary = _one_line(format_amount_text(c.summary)) if c.summary else ""
    links = [f"[{source.name} · {source.beijing_date:%m-%d}]({source.url})" for source in c.sources]
    if base_url:
        links.append(f"[详情]({_url(base_url, 'c/' + c.company_id)})")
    return f"{header}\n{summary}\n来源：" + "　".join(links)


def _highlight(card: Card, official: dict[str, str]) -> str:
    summary = _one_line(format_amount_text(card.summary)) if card.summary else ""
    first, stop, _ = summary.partition("。")
    # 没有简介（例：首期按旧规则过滤、从没写过简介的补充公司）时省掉这一段，不留空的「｜｜」。
    middle = f"{first}{stop}｜" if first else ""
    line = (f"{card.name}{_marks(card)}｜{middle}"
            f"{card.round_label or '轮次未披露'} · {format_amount_text(card.amount_label)}")
    line = re.sub(r"，按 \d{2}-\d{2} 汇率", "", line)
    links = []
    url = official.get(card.name.strip().casefold())
    if url:
        links.append(f"[官网]({url})")
    if card.sources:
        links.append(f"[来源]({card.sources[0].url})")
    return line + ("｜" + " ".join(links) if links else "")


def _marks(card: Card) -> str:
    return "".join(f"【{wording.MARK_LABELS.get(mark, mark)}】" for mark in _MARKS
                   if mark in card.marks or wording.MARK_LABELS.get(mark, mark) in card.marks)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _url(base_url: str, route: str) -> str:
    return f"{base_url.rstrip('/')}/#{route}"


def read_link_records(path: Path | None = None) -> list[dict]:
    """只读链接库，缺失、损坏或不支持的结构均按空处理。"""
    try:
        value = json.loads(Path(path if path is not None else Config().links_file).read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value = value.get("links", [])
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def official_links(path: Path | None = None) -> dict[str, str]:
    """按 V16e 完整公司名取第一条已核实官网，不推断别名或域名。"""
    from urllib.parse import urlsplit
    result = {}
    for record in read_link_records(path):
        if record.get("type") != "公司" or record.get("verified") is not True:
            continue
        if record.get("checker") == "脚本":
            if record.get("verified_by") not in {"来源页", "官网核对", "来源页（弱证据）", "官网核对（弱证据）"}:
                continue
            try:
                datetime.strptime(record.get("verified_at", ""), "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                continue
        name, url = record.get("company"), record.get("url")
        if not isinstance(name, str) or not name.strip() or not isinstance(url, str) or not url.strip():
            continue
        url = url.strip()
        try:
            parts = urlsplit(url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                continue
            if re.search(r"[\s\x00-\x1f]", url):
                continue
        except ValueError:
            continue
        result.setdefault(name.strip().casefold(), url)
    return result
