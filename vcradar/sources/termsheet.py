"""Term Sheet WP 期列表与 VENTURE 正文解析。"""
from __future__ import annotations

import re

from ..models import ErrorType
from .common import Issue, Parsed, html_lines, json_body, parse_time
from .newsletter import displayed_date, unique_deals

ISSUE_PREFIX = "https://content.fortune.com/newsletter/termsheet/?post_id="


def issues(response) -> list[Issue]:
    """使用 WP id 生成期网址，date_gmt 按 UTC 解析。"""
    payload = json_body(response)
    if not isinstance(payload, list):
        raise ValueError("WP 期列表格式不符")
    values = {}
    try:
        for post in payload:
            ident = str(post["id"])
            published = parse_time(post["date_gmt"])
            if not ident.isdigit() or published is None:
                raise ValueError
            values[ident] = Issue(ISSUE_PREFIX + ident, published, ident)
    except (KeyError, TypeError) as exc:
        raise ValueError("WP 期列表无法解析") from exc
    return sorted(values.values(), key=lambda issue: issue.at, reverse=True)


def parse(response, source_id="ts_web", issue_url=None, published_at=None) -> Parsed:
    """提取 VENTURE 段；没有段名时使用城市和 raised $ 句式。"""
    lines = html_lines(response.body)
    selected = []
    active = False
    found = False
    for line in lines:
        text = line.text.strip().rstrip(":")
        if re.match(r"^VENTURE\b", text) and len(text) < 100:
            active = found = True
            continue
        if active and text == text.upper() and re.fullmatch(r"[A-Z][A-Z &/\-]{2,80}", text):
            active = False
        if active:
            selected.append(line)
    items = unique_deals(selected if found else lines, source_id, issue_url or response.url,
                         published_at, displayed_date(lines), require_based=not found)
    if not items:
        return Parsed(latest_at=published_at, error_type=ErrorType.NEWSLETTER_NO_DEALS, error_summary="读不到融资清单")
    return Parsed(items, published_at)
