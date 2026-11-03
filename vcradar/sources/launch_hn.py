"""Launch HN 的 Algolia story 解析。"""
import re

from ..models import ErrorType, RawItem
from .common import Parsed, json_body, newest, parse_time, strip_html


def parse(response, source_id="launch_hn"):
    """读取 story hits，并在标题格式匹配时提取公司、批次和一句话。"""
    try:
        hits = json_body(response)["hits"]
        if not isinstance(hits, list):
            raise ValueError
        items = []
        for hit in hits:
            title = hit.get("title") or ""
            structured = {}
            match = re.match(r"Launch HN:\s*(.*?)\s*\(YC\s+([^)]*)\)\s*[-–—:]\s*(.*)", title, re.I)
            if match:
                structured = {"company_name": match[1].strip(), "yc_batch": match[2].strip(), "one_liner": match[3].strip()}
            body = strip_html(hit.get("story_text") or "")
            external_id = str(hit["objectID"])
            items.append(RawItem(source_id, external_id, "https://news.ycombinator.com/item?id=" + external_id,
                                 title, parse_time(hit.get("created_at") or hit.get("created_at_i")),
                                 body[:1000], body[:8000], structured=structured))
        return Parsed(items, newest(items))
    except (KeyError, TypeError, ValueError, AttributeError):
        return Parsed(error_type=ErrorType.INTERCEPTED, error_summary="Launch HN JSON 无法解析")
