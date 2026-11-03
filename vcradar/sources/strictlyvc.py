"""StrictlyVC sitemap 与正文 HTML 的纯解析。"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from ..models import ErrorType
from .common import Issue, Parsed, html_lines, parse_time
from .newsletter import displayed_date, unique_deals

log = logging.getLogger(__name__)
_KNOWN = {"Massive Fundings", "Big-But-Not-Crazy-Big Fundings", "Smaller Fundings", "Prefer-Not-to-Say Fundings"}


def issues(response) -> list[Issue]:
    """按 lastmod 倒序读取本站 /p/ 期网址。"""
    mime = response.content_type.partition(";")[0].strip().lower()
    if mime not in {"application/xml", "text/xml"} and not mime.endswith("+xml"):
        raise ValueError("sitemap 内容类型不符")
    try:
        root = ET.fromstring(response.body)
    except ET.ParseError as exc:
        raise ValueError("sitemap 无法解析") from exc
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        raise ValueError("sitemap 格式不符")
    values: dict[str, Issue] = {}
    for node in root:
        fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in node}
        url = fields.get("loc", "")
        parsed = urlsplit(url)
        if parsed.scheme in {"https", "http"} and parsed.hostname == urlsplit(response.url).hostname and parsed.path.startswith("/p/"):
            values[url] = Issue(url, parse_time(fields.get("lastmod")))
    return sorted(values.values(), key=lambda issue: issue.at.timestamp() if issue.at else float("-inf"), reverse=True)


def parse(response, source_id="strictlyvc", issue_url=None) -> Parsed:
    """提取 New Funds 之前的融资段，广告块跳至下一个段名。"""
    issue_url = issue_url or response.url
    lines = html_lines(response.body)
    date_match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', response.body)
    published = parse_time(date_match[1]) if date_match else None
    selected = []
    in_fundings = False
    for line in lines:
        text = line.text.strip().rstrip(":")
        if re.fullmatch(r"New Funds", text, re.I):
            break
        if re.match(r"^Sponsored By\b", text, re.I):
            in_fundings = False
            continue
        if len(text) < 100 and text.lower().endswith("fundings"):
            if text not in _KNOWN:
                log.info("StrictlyVC 未知段名：%s", text)
            in_fundings = True
            continue
        if line.heading and len(text) < 100:
            if in_fundings:
                log.info("StrictlyVC 未知段名：%s", text)
            in_fundings = False
            continue
        if in_fundings:
            selected.append(line)
    items = unique_deals(selected, source_id, issue_url, published, displayed_date(lines))
    if not items:
        return Parsed(latest_at=published, error_type=ErrorType.NEWSLETTER_NO_DEALS, error_summary="读不到融资清单")
    if published is None:
        return Parsed(items, error_type=ErrorType.INTERCEPTED, error_summary="通讯缺少机器发布时间")
    return Parsed(items, published)
