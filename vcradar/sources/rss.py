"""RSS 2.0 / RDF / Atom 1.0 公共解析，正文长度在去标签后截断。"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from ..models import ErrorType, HttpResponse, RawItem
from .common import Parsed, newest, parse_time, strip_html


def _atom_link(node: ET.Element) -> str:
    fallback = ""
    for child in node:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        href = (child.attrib.get("href") or "".join(child.itertext())).strip()
        if not href:
            continue
        rel = (child.attrib.get("rel") or "alternate").strip().lower()
        if rel == "alternate":
            return href
        if not fallback:
            fallback = href
    return fallback


def parse(response: HttpResponse, source_id: str) -> Parsed:
    """读取 RSS item 或 Atom entry；格式或内容类型不符按拦截处理。"""
    try:
        mime = response.content_type.partition(";")[0].strip().lower()
        if mime not in {"application/xml", "text/xml"} and not mime.endswith("+xml"):
            raise ValueError
        root = ET.fromstring(response.body)
        root_tag = root.tag.rsplit("}", 1)[-1].lower()
        if root_tag not in {"rss", "rdf", "feed"}:
            raise ValueError
        items = []
        if root_tag == "feed":
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] != "entry":
                    continue
                values = {child.tag.rsplit("}", 1)[-1]: "".join(child.itertext()).strip() for child in node}
                link = _atom_link(node)
                external_id = values.get("id") or link
                if not link or not external_id:
                    raise ValueError
                description = strip_html(values.get("summary") or values.get("content", ""))
                body = strip_html(values.get("content") or values.get("summary", ""))
                items.append(RawItem(source_id, external_id, link, strip_html(values.get("title", "")),
                                     parse_time(values.get("published") or values.get("updated")),
                                     description[:1000], body[:8000]))
            return Parsed(items, newest(items))
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
