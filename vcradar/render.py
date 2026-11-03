"""推送与页面卡片文字（任务 C），遵守 PRD V16e 第 9、9.1 节。"""
from __future__ import annotations

from datetime import date, datetime
import re
import json
from pathlib import Path
from dataclasses import replace

from .config import Config, validate_link_path_template
from . import wording
from .fx import format_amount_text
from .models import Card, DigestData, ErrorType


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
    """生成周报；发布验收开关关闭时省略站内链接和网页提示。

    d.cards 提供全部已纳入公司，amount_usd 按显示金额；仅金额待核取第一来源的主金额。
    early_push 由日历侧判定周五放假后的提前推送；failed_days 由调用方统计窗口内
    非推送工作日整天失败、缺记录/文件或漏抓的去重天数，单源失败后恢复不计。
    incomplete_until 是首期未补全的最后一个北京日期；仅 baseline_count > 0 时写基线提示。
    since_last_push 由调用方按本次推送日与上个 push=true 主文件的北京日期差 > 7 天判定；
    只有显式传 True 才写「上次推送后」，不根据窗口时长、运行状态或旧证据提示推断。
    filtered_count 可传去重后的已过滤总数，省略时取 d.filtered 的正计数之和。
    first_seen 按 company_id 提供带时区的首次入库时间；缺失者在同金额、同标记时排后，
    再按公司名和 id 稳定排序。所有参数均不改变 d；旧 weekly 开关不改变 V15e 周报结构。
    没有已纳入公司时写无新增文案，再加来源状态及可用的上次有新公司记录。
    """
    if not d.review_site_passed:
        d = replace(d, base_url="")
    hints = first_lines(d, early_push=early_push, failed_days=failed_days,
                        since_last_push=since_last_push, incomplete_until=incomplete_until)
    if d.cards:
        period = "上次推送后" if since_last_push else "本周"
        ai = sum("AI相关" in card.tag_label for card in d.cards)
        social = sum("社交/社群相关" in card.tag_label for card in d.cards)
        new_round = sum("新一轮" in card.marks for card in d.cards)
        large = sum("大额" in card.marks for card in d.cards)
        counts = wording.SITE_TEXT["counts"].format(period=period, count=len(d.cards), ai=ai, social=social,
                                                  new_round=new_round, large=large)
    else:
        counts = wording.NO_NEW
    counts += _pending_counts(d, filtered_count)

    title = wording.PUSH_TITLE.format(day=f"{d.push_date:%m-%d}", weekday=_WEEKDAYS[d.push_date.weekday()])
    lines = [title, counts]
    if hints:
        lines.append(" · ".join(hints))
    official = official_links(links_file)
    for number, card in enumerate(sort_cards(d.cards, first_seen=first_seen)[:10], start=1):
        lines.append(f"{number}. {_highlight(card, official)}")
    if d.yc_new_batch is not None:
        _, count = d.yc_new_batch
        yc = wording.YC_HINT.format(count=count)
        lines.append(yc + (f"，[{wording.SITE_TEXT['see_yc']}]({_url(d.base_url, 'yc')})" if d.base_url else ""))
    if not d.cards:
        lines.append(_source_line(d))
        if d.last_push_summary is not None:
            last_date, count = d.last_push_summary
            lines.append(wording.LAST_NEW.format(day=f"{last_date:%m-%d}", count=count))
    for section in d.extra_sections:
        if not section.cards:
            continue
        lines.append(section.title)
        cards = section.cards if section.preserve_order else sort_cards(section.cards, first_seen=first_seen)
        lines.extend(f"{i}. {_highlight(card, official)}" for i, card in enumerate(cards[:10], 1))
        overflow = section.overflow_count or max(0, len(section.cards) - 10)
        if overflow > 0:
            overflow_text = section.overflow_text or (wording.OVERFLOW_LINK.format(
                count=overflow, url=_url(d.base_url, 'backfill')) if d.base_url else
                wording.OVERFLOW.format(count=overflow))
            lines.append(overflow_text if d.base_url else remove_page_links(overflow_text))
    if d.base_url:
        lines.append(wording.PAGE_LINKS.format(count=len(d.cards), week=_url(d.base_url, 'week'),
                                              sources=_url(d.base_url, 'sources')))
    return "\n".join(lines)


def first_lines(
    d: DigestData, *, early_push: bool = False, failed_days: int = 0,
    since_last_push: bool = False, incomplete_until: date | None = None,
) -> list[str]:
    """返回提示项：基线、首期补全、提前推送、重发、报警类、新证据。

    参数含义同 render_push；提示以「 · 」连接为单独一行，不添加句末句号，9.1 长文案保留原句号。
    当天整体失败时用总提示替代逐信源报错，仍保留其他提示。
    """
    lines = []
    if d.baseline_count > 0:
        lines.append(wording.BASELINE_HINT.format(count=d.baseline_count))
    lines.extend(section.hint for section in d.extra_sections if section.cards and section.hint)
    if incomplete_until is not None:
        lines.append(wording.INCOMPLETE_HINT.format(day=f"{incomplete_until:%m-%d}"))
    if early_push:
        lines.append("提前推送")
    if d.resend:
        lines.append("重发")
    if d.status in _RUN_ERROR_TEXT:
        lines.append(_RUN_ERROR_TEXT[d.status])
    if failed_days > 0:
        lines.append(wording.FAILED_DAYS.format(count=failed_days))
    if d.status not in _RUN_ERROR_TEXT:
        for source in d.sources:
            if source.stopped or source.ok:
                continue
            error = source.error_type or ErrorType.UNREACHABLE
            if error == ErrorType.TS_STOPPED:
                continue
            template = ERROR_TEXT.get(error, ERROR_TEXT[ErrorType.UNREACHABLE])
            latest = source.latest_beijing_date.isoformat() if source.latest_beijing_date else "日期不明"
            lines.append(template.format(X=source.name, date=latest))
    for source in d.sources:
        if source.missed_range:
            lines.append(wording.MISSED_HINT.format(name=source.name, span=source.missed_range))
    if d.ts_stopped_notice:
        lines.append(ERROR_TEXT[ErrorType.TS_STOPPED].format(reason=wording.STOP_REASONS.get(d.ts_stopped_notice, wording.display_text(d.ts_stopped_notice))).removesuffix("。"))
    if d.new_evidence_count > 0:
        since = "上次推送后" if since_last_push else "本周"
        evidence = wording.EVIDENCE_HINT.format(period=since, count=d.new_evidence_count)
        lines.append(f"[{evidence}]({_url(d.base_url, 'sources')})" if d.review_site_passed and d.base_url else evidence)
    return list(dict.fromkeys(lines))


def remove_page_links(text: str) -> str:
    """去掉新旧推送中的站内链接和网页提示，保留官网及来源链接。"""
    lines = []
    for line in text.splitlines():
        if re.match(r"\[看本周全部 \d+ 家\]", line):
            continue
        line = re.sub(r"\[((?:本周|上次推送后) \d+ 家(?:有新证据|资料有出入))\]\([^\n]*?\)", r"\1", line)
        line = re.sub(r"(YC 新(?:批次|一批) \d+ 家)[，,].*", r"\1", line)
        line = re.sub(r"另有 (\d+) 家(?:没有列出)?，\[在往期页看\]\([^\n]*\)", r"另有 \1 家没有列出", line)
        lines.append(line)
    return "\n".join(lines)


def push_first_lines(text: str) -> tuple[str, str]:
    """读取新旧计数及可选提示行，不把名单、段名和来源状态当提示。"""
    lines = text.removeprefix("重发\n").splitlines()
    counts = lines[1] if len(lines) > 1 else ""
    hints = lines[2] if len(lines) > 2 else ""
    prefixes = ("已收录 ", "首次运行收录 ", "另补充此前漏掉的 ", "本周一至 ", "本周一到 ",
                "提前推送", "重发", "今天", "本周有 ", "可能有遗漏：", "有漏抓：", "Term Sheet 已", "[本周 ",
                "[上次推送后 ", "本周 ", "上次推送后 ")
    if hints and not hints.startswith(prefixes) and not re.search(r"(?:没抓到|可能没更新|可能出错)：", hints):
        hints = ""
    return counts, hints


def add_resend_notice(text: str) -> str:
    """把重发放在提前推送之后、报警之前，兼容历史推送。"""
    text = text.removeprefix("重发\n")
    _, hints = push_first_lines(text)
    parts = [part for part in hints.split(" · ") if part and part != "重发"]
    position = 0
    while position < len(parts) and (parts[position].startswith(("已收录 ", "首次运行收录 ",
            "另补充此前漏掉的 ", "本周一至 ", "本周一到 ")) or parts[position] == "提前推送"):
        position += 1
    parts.insert(position, "重发")
    lines = text.splitlines()
    if hints:
        lines[2] = " · ".join(parts)
    else:
        lines.insert(2, " · ".join(parts))
    return "\n".join(lines)


def is_alarm(d: DigestData, *, failed_days: int = 0, push: bool = True) -> bool:
    """判断报警；failed_days 同 render_push，push=False 时只看当天的前四种错误。

    当天信源出错、没跑完、全部失败、漏抓始终报警；本期失败天数和停抓首次提示只在推送日报警。
    """
    today_failed = (d.status in _RUN_ERROR_TEXT
                    or any(not source.ok and not source.stopped for source in d.sources)
                    or any(bool(source.missed_range) for source in d.sources))
    return today_failed or (push and (failed_days > 0 or bool(d.ts_stopped_notice)))


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


def _pending_counts(d: DigestData, filtered_count: int | None) -> str:
    pending = []
    if d.round_pending:
        pending.append(wording.SITE_TEXT["pending_count"].format(count=len(d.round_pending)))
    text = " · ".join(pending)
    filtered = (sum(count for reason, count in d.filtered.items() if count > 0 and reason != "总部非美国")
                if filtered_count is None else filtered_count)
    if filtered > 0:
        text += (" · " if text else "") + wording.SITE_TEXT["filtered"].format(count=filtered)
    return " · " + text if text else ""


def _source_line(d: DigestData) -> str:
    # 一个信源可能有多个抓取地址，接入数按信源名数。
    names = dict.fromkeys(source.name for source in d.sources)
    stopped = {name for name in names if all(source.stopped for source in d.sources if source.name == name)}
    failed = {source.name for source in d.sources if not source.ok and not source.stopped}
    line = wording.SITE_TEXT["connected"].format(count=len(names)) + "，"
    states = []
    if stopped:
        states.append(wording.SITE_TEXT["stopped_count"].format(count=len(stopped)))
    if failed:
        states.append(wording.SITE_TEXT["error_count"].format(count=len(failed)))
    line += wording.SITE_TEXT["some"] + " · ".join(states) if states else wording.SITE_TEXT["all_normal"]
    counts = []
    for source in d.sources:
        if source.stopped:
            continue
        label = source.count_label if source.ok else wording.SITE_TEXT["not_caught"]
        counts.append(f"{source.name} {label}".rstrip())
    return line + ("：" + " · ".join(counts) if counts else "")


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
