"""解析器共用的结果、时间和 HTML 清洗；不发请求。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

from ..config import UTC
from ..models import ErrorType, HttpResponse, RawItem


@dataclass
class Parsed:
    """纯解析结果；skip_reason 非空的条目只参与计数和追页。"""
    items: list[RawItem] = field(default_factory=list)
    latest_at: datetime | None = None
    error_type: ErrorType | None = None
    error_summary: str = ""


@dataclass(frozen=True)
class Issue:
    """通讯索引中的一期；StrictlyVC 的时间只是 lastmod。"""
    url: str
    at: datetime | None
    external_id: str = ""


@dataclass
class HTMLLine:
    text: str
    links: list[tuple[str, str]] = field(default_factory=list)
    heading: bool = False


class _Lines(HTMLParser):
    _blocks = {"p", "div", "li", "br", "article", "section", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    _ignored = {"script", "style", "noscript", "template", "svg", "nav", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lines: list[HTMLLine] = []
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.ignored: list[str] = []
        self.heading = False
        self.anchor: tuple[str, list[str]] | None = None

    def flush(self):
        value = re.sub(r"\s+", " ", "".join(self.parts)).strip()
        if value:
            self.lines.append(HTMLLine(value, self.links[:], self.heading))
        self.parts.clear()
        self.links.clear()

    def handle_starttag(self, tag, attrs):
        if self.ignored:
            if tag in self._ignored:
                self.ignored.append(tag)
            return
        if tag in self._ignored:
            self.flush()
            self.ignored.append(tag)
            return
        if tag in self._blocks:
            self.flush()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = True
        if tag == "a":
            self.anchor = (dict(attrs).get("href", ""), [])

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == "a" and self.anchor:
            href, words = self.anchor
            self.links.append((href, "".join(words).strip()))
            self.anchor = None
        if tag in self._blocks:
            self.flush()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = False

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)
            if self.anchor:
                self.anchor[1].append(data)


def html_lines(body: str) -> list[HTMLLine]:
    """仅提取可见 HTML 行，丢弃脚本内重复的 JSON 正文。"""
    parser = _Lines()
    parser.feed(body)
    parser.close()
    parser.flush()
    return parser.lines


def strip_html(body: str) -> str:
    """去标签并统一空白。"""
    return " ".join(line.text for line in html_lines(body))


def parse_time(value) -> datetime | None:
    """解析 ISO、RSS 或 Unix 时间；无时区的接口时间按 UTC。"""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, UTC)
        value = str(value).strip()
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            result = parsedate_to_datetime(value)
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def json_body(response: HttpResponse):
    """按 JSON 内容类型读取；类型错误或格式错误抛 ValueError。"""
    mime = response.content_type.partition(";")[0].strip().lower()
    if mime not in {"application/json", "text/json"} and not mime.endswith("+json"):
        raise ValueError("JSON 内容类型不符")
    try:
        return json.loads(response.body)
    except (ValueError, TypeError) as exc:
        raise ValueError("JSON 无法解析") from exc


def newest(items: list[RawItem]) -> datetime | None:
    """取可用发布时间的最大值。"""
    return max((item.published_at for item in items if item.published_at), default=None)
