"""RSS 公共解析，正文长度在去标签后截断。"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import ErrorType, HttpResponse, RawItem
from .common import Parsed, newest, parse_time, strip_html


def parse(response: HttpResponse, source_id: str) -> Parsed:
    """读取 RSS item；格式或内容类型不符按拦截处理。"""
    try:
        mime = response.content_type.partition(";")[0].strip().lower()
        if mime not in {"application/xml", "text/xml"} and not mime.endswith("+xml"):
            raise ValueError
        root = ET.fromstring(response.body)
        if root.tag.rsplit("}", 1)[-1].lower() not in {"rss", "rdf"}:
            raise ValueError
        items = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] != "item":
                continue
            values = {child.tag.rsplit("}", 1)[-1]: "".join(child.itertext()).strip() for child in node}
            link = values.get("link", "")
            external_id = values.get("guid") or link
            if not link or not external_id:
                raise ValueError
            description = strip_html(values.get("description", ""))
            body = strip_html(values.get("encoded") or values.get("description", ""))
            items.append(RawItem(source_id, external_id, link, strip_html(values.get("title", "")),
                                 parse_time(values.get("pubDate")), description[:1000], body[:8000]))
        return Parsed(items, newest(items))
    except (ET.ParseError, ValueError, TypeError):
        return Parsed(error_type=ErrorType.INTERCEPTED, error_summary="RSS 内容类型不符或无法解析")
